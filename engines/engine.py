#!/usr/bin/env python3
"""Maps routing/transit engine supervisor (runs in the pinned Valhalla image, which ships python3).

  python3 engine.py valhalla   one combined Valhalla graph for every region; rebuilt into a new generation while the
                               previous one keeps serving, then swapped.
  python3 engine.py motis      one MOTIS (transit) server per region that has GTFS feeds.

The Maps app writes /data/engines/<engine>/want.json; this loop builds and writes status.json. No network listener
other than the engine itself; every path stays under /data.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ENGINE = sys.argv[1] if len(sys.argv) > 1 else 'valhalla'
D = Path('/data/engines') / ENGINE
D.mkdir(parents=True, exist_ok=True)
THREADS = int(os.environ.get('ENGINE_THREADS', '4'))
procs = {}


def read(name, default=None):
    try:
        return json.loads((D / name).read_text())
    except (OSError, ValueError):
        return default


def write(name, doc):
    tmp = D / (name + '.tmp')
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, D / name)


state = read('state.json', {}) or {}


def status(**kw):
    doc = read('status.json', {}) or {}
    doc.update(kw, updated=int(time.time()))
    write('status.json', doc)


def tail(path, n=400):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode('utf-8', 'replace').replace('\n', ' ').strip()
    except OSError:
        return ''


def run(cmd, log, step, pct_from, pct_to, cwd=None, stdout=None):
    """Run one build step with low priority, reporting progress from the log's last line."""
    status(state='building', step=step, pct=pct_from)
    with open(log, 'ab') as lf:
        p = subprocess.Popen(['nice', '-n', '10'] + cmd, cwd=cwd, stdout=stdout or lf, stderr=lf)
        while p.poll() is None:
            status(state='building', step=step, pct=pct_from, message=tail(log, 200)[-160:])
            time.sleep(5)
    if p.returncode:
        raise RuntimeError(f'{step} failed ({p.returncode}): {tail(log)}')
    status(state='building', step=step, pct=pct_to)


def stop(key):
    p = procs.pop(key, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()


# ---- Valhalla ----------------------------------------------------------------------------------------------------
def valhalla_serve():
    gen = state.get('serving')
    conf = D / f'gen-{gen}' / 'valhalla.json'
    if gen and conf.is_file() and ('v' not in procs or procs['v'].poll() is not None):
        log = open(D / 'serve.log', 'ab')
        procs['v'] = subprocess.Popen(['valhalla_service', str(conf), str(max(1, THREADS // 2))], stdout=log, stderr=log)


def valhalla_build(want):
    gen = want['gen']
    out = D / f'gen-{gen}'
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    log = out / 'build.log'
    pbfs = [p for p in want.get('pbfs', []) if Path(p).is_file()]
    if not pbfs:
        raise RuntimeError('no OSM extracts to build from')
    conf = out / 'valhalla.json'
    with open(conf, 'wb') as f:
        run(['valhalla_build_config', '--mjolnir-tile-dir', str(out / 'tiles'), '--mjolnir-tile-extract', str(out / 'tiles.tar'),
             '--mjolnir-timezone', str(out / 'tz.sqlite'), '--mjolnir-admin', str(out / 'admins.sqlite'),
             '--mjolnir-concurrency', str(THREADS), '--httpd-service-listen', 'tcp://*:8002',
             '--service-limits-pedestrian-max-distance', '100000', '--service-limits-bicycle-max-distance', '300000'],
            log, 'config', 0, 2, stdout=f)
    run(['valhalla_build_admins', '-c', str(conf)] + pbfs, log, 'admins', 2, 10)
    tz = D / 'tz.sqlite'                               # world time zones: built once, shared by every generation
    if not tz.is_file() or tz.stat().st_size < 1_000_000:
        tmp = D / 'tz-work'
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            with open(tmp / 'tz.sqlite', 'wb') as f:
                run(['valhalla_build_timezones'], log, 'timezones', 10, 14, cwd=tmp, stdout=f)
            os.replace(tmp / 'tz.sqlite', tz)
        except RuntimeError:
            pass                                       # optional: times fall back to UTC offsets
        shutil.rmtree(tmp, ignore_errors=True)
    if tz.is_file():
        shutil.copyfile(tz, out / 'tz.sqlite')
    run(['valhalla_build_tiles', '-c', str(conf)] + pbfs, log, 'graph', 14, 90)
    run(['valhalla_build_extract', '-c', str(conf), '-v', '--overwrite'], log, 'pack', 90, 99)
    shutil.rmtree(out / 'tiles', ignore_errors=True)
    if not (out / 'tiles.tar').is_file():
        raise RuntimeError('no routing graph produced')
    old = state.get('serving')
    state['serving'] = gen
    write('state.json', state)
    stop('v')
    valhalla_serve()
    if old and old != gen:
        shutil.rmtree(D / f'gen-{old}', ignore_errors=True)


# ---- MOTIS -------------------------------------------------------------------------------------------------------
MOTIS_CONFIG = """server:
  host: 0.0.0.0
  port: {port}
  n_threads: {threads}
osm: {pbf}
timetable:
  first_day: TODAY
  num_days: 30
  railviz: false
  with_shapes: true
  update_interval: 60
  link_stop_distance: 100
  max_footpath_length: 15
  default_transfer_time: 2
  datasets:
{datasets}street_routing: true
osr_footpath: false
geocoding: false
reverse_geocoding: false
"""


def motis_port(rid):
    ports = state.setdefault('ports', {})
    if rid not in ports:
        used = set(ports.values())
        ports[rid] = next(p for p in range(8100, 8200) if p not in used)
    return ports[rid]


def motis_serve(rid):
    built = state.get('built', {}).get(rid)
    if not built:
        return
    work = D / rid / f"gen-{built['version']}"
    if (work / 'data').is_dir() and (rid not in procs or procs[rid].poll() is not None):
        log = open(work / 'serve.log', 'ab')
        procs[rid] = subprocess.Popen(['/data/bin/motis', 'server', '-d', 'data'], cwd=work,
                                      stdout=log, stderr=log)


def motis_build(region):
    rid, version = region['id'], region['version']
    work = D / rid / f'gen-{version}'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    datasets = ''.join(f"    {f['id']}:\n      path: {f['path']}\n" for f in region['feeds'] if Path(f['path']).is_file())
    if not datasets:
        raise RuntimeError('no transit feeds downloaded')
    (work / 'config.yml').write_text(MOTIS_CONFIG.format(port=motis_port(rid), threads=max(1, THREADS // 2),
                                                         pbf=region['pbf'], datasets=datasets))
    run(['/data/bin/motis', 'import', '-c', 'config.yml', '-d', 'data'], work / 'import.log', f'transit {rid}', 0, 100, cwd=work)
    old = state.setdefault('built', {}).get(rid)
    state['built'][rid] = {'version': version, 'port': motis_port(rid), 'at': int(time.time())}
    write('state.json', state)
    stop(rid)
    motis_serve(rid)
    if old and old['version'] != version:
        shutil.rmtree(D / rid / f"gen-{old['version']}", ignore_errors=True)


def motis_tick(want):
    regions = {r['id']: r for r in want.get('regions', [])}
    for rid in list(state.get('built', {})):          # removed regions: stop and delete
        if rid not in regions:
            stop(rid)
            state['built'].pop(rid)
            state.get('ports', {}).pop(rid, None)
            shutil.rmtree(D / rid, ignore_errors=True)
            write('state.json', state)
    failed = state.setdefault('failed', {})
    for rid, r in regions.items():
        if state.get('built', {}).get(rid, {}).get('version') == r['version'] or failed.get(rid) == r['version']:
            continue
        try:
            motis_build(r)
            failed.pop(rid, None)
        except Exception as e:  # noqa: BLE001 - report every failure to the app
            failed[rid] = r['version']
            write('state.json', state)
            status(state='failed', step=f'transit {rid}', message=str(e)[-300:])
            continue
    for rid in state.get('built', {}):
        motis_serve(rid)
    status(state='serving' if state.get('built') else 'idle', step='', pct=100,
           regions={k: v for k, v in state.get('built', {}).items()}, failed=failed)


def main():
    signal.signal(signal.SIGTERM, lambda *a: [stop(k) for k in list(procs)] and sys.exit(0) or sys.exit(0))
    if ENGINE == 'valhalla':
        valhalla_serve()
        status(state='serving' if state.get('serving') else 'idle', serving=state.get('serving'), step='', pct=100)
    while True:
        want = read('want.json')
        try:
            if ENGINE == 'valhalla':
                if want and want.get('gen', 0) > state.get('done', 0):
                    try:
                        valhalla_build(want)
                        status(state='serving', serving=state.get('serving'), step='', pct=100, message='Routing ready')
                    except Exception as e:  # noqa: BLE001
                        status(state='failed', serving=state.get('serving'), message=str(e)[-300:])
                    state['done'] = want['gen']
                    write('state.json', state)
                valhalla_serve()
            elif want:
                motis_tick(want)
        except Exception as e:  # noqa: BLE001
            status(state='failed', message=str(e)[-300:])
        time.sleep(5)


if __name__ == '__main__':
    main()
