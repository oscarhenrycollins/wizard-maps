"""Maps service layer: saved places, regions and their background builds, search/route proxies, offline packs.

Stdlib only. Shared by the HTTP API, the MCP tools and the background worker. Heavy work happens in sidecar engines
(Photon, Valhalla, MOTIS) that read job files under DATA/engines; this module downloads inputs and orchestrates.
"""
import csv
import gzip
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from compression import zstd  # Python 3.14+
except ImportError:  # pragma: no cover - the pinned image is 3.14
    zstd = None

DATA = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent.parent / '.data'))
PHOTON_URL = os.environ.get('PHOTON_URL', 'http://maps-search:2322')
VALHALLA_URL = os.environ.get('VALHALLA_URL', 'http://maps-routing:8002')
MOTIS_HOST = os.environ.get('MOTIS_HOST', 'maps-transit')
UA = 'WizardOS-Maps/0.1 (+https://github.com/humanitylabs-org/wizard-maps)'
WORLD_MAXZOOM = 6
REGION_MAXZOOM = 15
REFRESH_DAYS = int(os.environ.get('MAPS_REFRESH_DAYS', '30'))
TRANSIT_REFRESH_DAYS = int(os.environ.get('MAPS_TRANSIT_REFRESH_DAYS', '7'))
PACK_MAX_BYTES = 1_500_000_000
MAX_FEEDS = 6
MIN_FREE_BYTES = int(os.environ.get('MAPS_MIN_FREE_GB', '8')) << 30

ARCH = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), 'amd64')
TOOLS = {  # pinned upstream releases, verified by SHA-256 before use
    'pmtiles': {'amd64': ('https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz',
                          '3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f'),
                'arm64': ('https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_arm64.tar.gz',
                          'f8bd47e7ea866863489cad588fbaf2f31f42e5821f7a03f009b3769f05801cb1')},
    'motis': {'amd64': ('https://github.com/motis-project/motis/releases/download/v2.11.4/motis-linux-amd64.tar.bz2',
                        'b521bb29c961a3b994ff4dac663778ba64fee7b4cfaf3f2b20ff1d5e1d15e0dc'),
              'arm64': ('https://github.com/motis-project/motis/releases/download/v2.11.4/motis-linux-arm64.tar.bz2',
                        '76f41df5bb4db14ccd163dad0a23dd85bebbb9f21a9736834e9b3d4201080f3b')},
    'photon.jar': {'any': ('https://github.com/komoot/photon/releases/download/1.3.0/photon-1.3.0.jar',
                           'a89707c0045e4807b2a1180e132e68e108d998709f48b6c94b98a6e281f571a5')},
}
ASSETS_TARBALL = 'https://codeload.github.com/protomaps/basemaps-assets/tar.gz/028c18f713baecad011301ff7a69acc39bcc2ae7'
GEOFABRIK_INDEX = 'https://download.geofabrik.de/index-v1.json'
GRAPHHOPPER = 'https://download1.graphhopper.com/public/'
GH_CONTINENTS = ['africa', 'asia', 'australia-oceania', 'europe', 'north-america', 'south-america']
GH_ALIASES = {'luxembourg': 'luxemburg', 'south-korea': 'korea', 'israel-and-palestine': 'isreal', 'east-timor': 'east-timor'}
MDB_CSV = 'https://share.mobilitydata.org/catalogs-csv'
PROTOMAPS_BUILDS = 'https://build-metadata.protomaps.dev/builds.json'
LANGS = {'th': 'th', 'lu': 'lb,fr,de', 'de': 'de', 'fr': 'fr', 'es': 'es', 'it': 'it', 'nl': 'nl', 'pt': 'pt', 'jp': 'ja',
         'cn': 'zh', 'kr': 'ko', 'vn': 'vi', 'id': 'id', 'my': 'ms', 'ru': 'ru', 'br': 'pt', 'mx': 'es', 'at': 'de',
         'ch': 'de,fr,it', 'be': 'nl,fr,de', 'pl': 'pl', 'cz': 'cs', 'gr': 'el', 'tr': 'tr', 'il': 'he', 'ar': 'es',
         'la': 'lo', 'kh': 'km', 'mm': 'my', 'sg': 'zh,ms', 'tw': 'zh', 'hk': 'zh', 'ua': 'uk', 'se': 'sv', 'no': 'no',
         'dk': 'da', 'fi': 'fi'}

for sub in ('cache/osm', 'cache/photon', 'cache/gtfs', 'tiles', 'packs', 'assets', 'bin', 'engines/photon',
            'engines/valhalla', 'engines/motis'):
    (DATA / sub).mkdir(parents=True, exist_ok=True)


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---- storage -----------------------------------------------------------------------------------------------------
_db_lock = threading.Lock()


def connect():
    c = sqlite3.connect(DATA / 'maps.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, created REAL);
      CREATE TABLE IF NOT EXISTS places (id TEXT PRIMARY KEY, list_id TEXT NOT NULL, name TEXT NOT NULL, lat REAL NOT NULL,
        lon REAL NOT NULL, address TEXT DEFAULT '', note TEXT DEFAULT '', ref TEXT DEFAULT '', created REAL, updated REAL);
      CREATE TABLE IF NOT EXISTS regions (id TEXT PRIMARY KEY, name TEXT, parent TEXT, bbox TEXT, pbf_url TEXT, iso TEXT,
        state TEXT, step TEXT DEFAULT '', pct REAL DEFAULT 0, message TEXT DEFAULT '', added REAL, built_at REAL,
        transit_at REAL, info TEXT DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS packs (id TEXT PRIMARY KEY, name TEXT, bbox TEXT, maxzoom INTEGER, state TEXT,
        size INTEGER DEFAULT 0, message TEXT DEFAULT '', created REAL);
      CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
    ''')
    if not c.execute('SELECT 1 FROM lists').fetchone():
        c.execute("INSERT OR IGNORE INTO lists VALUES ('saved','Saved',?)", (time.time(),))
        c.execute("INSERT OR IGNORE INTO lists VALUES ('want-to-go','Want to go',?)", (time.time(),))
        c.commit()
    return c


def db(fn):
    def wrapped(*a, **kw):
        c = connect()
        try:
            with c:
                return fn(c, *a, **kw)
        finally:
            c.close()
    wrapped.__name__ = fn.__name__
    return wrapped


@db
def kv_get(c, key, default=None):
    r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(r['value']) if r else default


@db
def kv_set(c, key, value):
    c.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))


# ---- saved places ------------------------------------------------------------------------------------------------
def _text(v, field, limit, required=False):
    if v is None:
        v = ''
    if not isinstance(v, str) or len(v.strip()) > limit or (required and not v.strip()):
        raise Problem(400, f'{field} must be text up to {limit} characters' + (' and not empty.' if required else '.'))
    return v.strip()


def _coord(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise Problem(400, 'lat and lon must be numbers.') from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or math.isnan(lat) or math.isnan(lon):
        raise Problem(400, 'lat must be -90..90 and lon -180..180.')
    return round(lat, 7), round(lon, 7)


def _list_id(c, name_or_id):
    if not name_or_id:
        return 'saved'
    r = c.execute('SELECT id FROM lists WHERE id=? OR name=?', (name_or_id, name_or_id)).fetchone()
    if r:
        return r['id']
    name = _text(name_or_id, 'list', 40, True)
    lid = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or uuid.uuid4().hex[:8]
    if c.execute('SELECT 1 FROM lists WHERE id=?', (lid,)).fetchone():
        lid = f'{lid}-{uuid.uuid4().hex[:4]}'
    c.execute('INSERT INTO lists VALUES (?,?,?)', (lid, name, time.time()))
    return lid


@db
def lists(c):
    rows = c.execute('SELECT l.id, l.name, (SELECT COUNT(*) FROM places p WHERE p.list_id=l.id) AS count FROM lists l '
                     'ORDER BY l.created').fetchall()
    return {'lists': [dict(r) for r in rows]}


@db
def add_list(c, args):
    lid = _list_id(c, _text(args.get('name'), 'name', 40, True))
    return {'list': dict(c.execute('SELECT id,name FROM lists WHERE id=?', (lid,)).fetchone())}


@db
def list_places(c, list_id=None, query=None):
    q, a = 'SELECT p.*, l.name AS list FROM places p JOIN lists l ON l.id=p.list_id', []
    w = []
    if list_id:
        w.append('(l.id=? OR l.name=?)'); a += [list_id, list_id]
    if query:
        w.append('(p.name LIKE ? OR p.address LIKE ? OR p.note LIKE ?)'); a += [f'%{query}%'] * 3
    rows = c.execute(q + (' WHERE ' + ' AND '.join(w) if w else '') + ' ORDER BY p.created', a).fetchall()
    return {'places': [_place(r) for r in rows]}


def _place(r):
    d = dict(r)
    d.pop('list_id', None)
    d['list_id'] = r['list_id']
    return d


@db
def save_place(c, args):
    """Create or replace by id (idempotent: the same id and body twice is one place)."""
    pid = args.get('id') or str(uuid.uuid4())
    if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9-]{6,64}', pid):
        raise Problem(400, 'id must be 6-64 letters, digits or dashes.')
    lat, lon = _coord(args.get('lat'), args.get('lon'))
    name = _text(args.get('name'), 'name', 120, True)
    address, note = _text(args.get('address'), 'address', 300), _text(args.get('note'), 'note', 1000)
    ref = _text(args.get('ref'), 'ref', 80)
    lid = _list_id(c, args.get('list'))
    now = time.time()
    c.execute('INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET list_id=excluded.list_id, '
              'name=excluded.name, lat=excluded.lat, lon=excluded.lon, address=excluded.address, note=excluded.note, '
              'ref=excluded.ref, updated=excluded.updated', (pid, lid, name, lat, lon, address, note, ref, now, now))
    r = c.execute('SELECT p.*, l.name AS list FROM places p JOIN lists l ON l.id=p.list_id WHERE p.id=?', (pid,)).fetchone()
    return {'place': _place(r)}


@db
def update_place(c, pid, args):
    r = c.execute('SELECT * FROM places WHERE id=?', (pid,)).fetchone()
    if not r:
        raise Problem(404, 'Place not found.')
    name = _text(args['name'], 'name', 120, True) if 'name' in args else r['name']
    note = _text(args['note'], 'note', 1000) if 'note' in args else r['note']
    lid = _list_id(c, args['list']) if args.get('list') else r['list_id']
    c.execute('UPDATE places SET name=?, note=?, list_id=?, updated=? WHERE id=?', (name, note, lid, time.time(), pid))
    r = c.execute('SELECT p.*, l.name AS list FROM places p JOIN lists l ON l.id=p.list_id WHERE p.id=?', (pid,)).fetchone()
    return {'place': _place(r)}


@db
def delete_place(c, pid):
    c.execute('DELETE FROM places WHERE id=?', (pid,))
    return {'deleted': pid}


# ---- HTTP helpers ------------------------------------------------------------------------------------------------
def http_json(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'User-Agent': UA, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def download(url, dest, progress=None, expect_sha=None, max_age=None):
    """Resumable download to dest (skips a fresh complete copy). progress(done, total) is called as bytes arrive."""
    dest = Path(dest)
    if dest.is_file() and max_age is not None and time.time() - dest.stat().st_mtime < max_age:
        return dest
    part = dest.with_name(dest.name + '.part')
    have = part.stat().st_size if part.exists() else 0
    headers = {'User-Agent': UA}
    if have:
        headers['Range'] = f'bytes={have}-'
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            have, r = 0, urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=60)
        else:
            raise
    with r:
        if have and r.status != 206:
            have = 0
        total = int(r.headers.get('Content-Length') or 0) + have
        done = have
        with open(part, 'ab' if have else 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if expect_sha:
        h = hashlib.sha256()
        with open(part, 'rb') as f:
            for b in iter(lambda: f.read(1 << 20), b''):
                h.update(b)
        if h.hexdigest() != expect_sha:
            part.unlink()
            raise RuntimeError(f'checksum mismatch for {url}')
    os.replace(part, dest)
    return dest


def tool(name):
    """Download a pinned engine binary once (SHA-256 checked)."""
    if name == 'photon.jar':
        dest = DATA / 'bin/photon.jar'
        if not dest.is_file():
            url, sha = TOOLS[name]['any']
            download(url, dest, expect_sha=sha)
        return dest
    dest = DATA / 'bin' / name
    if dest.is_file():
        return dest
    url, sha = TOOLS[name][ARCH]
    arc = download(url, DATA / 'bin' / Path(url).name, expect_sha=sha)
    with tarfile.open(arc) as t:
        member = next(m for m in t.getmembers() if Path(m.name).name == name and m.isfile())
        with t.extractfile(member) as src, open(dest.with_suffix('.tmp'), 'wb') as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest.with_suffix('.tmp'), 0o755)
    os.replace(dest.with_suffix('.tmp'), dest)
    arc.unlink()
    return dest


def planet_url():
    """Newest Protomaps daily planet build (cached a day)."""
    cached = kv_get('planet')
    if cached and time.time() - cached['at'] < 86400:
        return cached['url']
    builds = http_json(PROTOMAPS_BUILDS)
    key = sorted(b['key'] for b in builds if str(b.get('key', '')).endswith('.pmtiles'))[-1]
    url = 'https://build.protomaps.com/' + key
    kv_set('planet', {'url': url, 'at': time.time()})
    return url


def pmtiles_extract(src, dest, *, bbox=None, region_file=None, maxzoom=None, dry_run=False, on_line=None):
    cmd = [str(tool('pmtiles')), 'extract', str(src), str(dest), '--download-threads=4']
    if bbox:
        cmd.append('--bbox=' + ','.join(f'{v:.6f}' for v in bbox))
    if region_file:
        cmd.append(f'--region={region_file}')
    if maxzoom is not None:
        cmd.append(f'--maxzoom={maxzoom}')
    if dry_run:
        cmd.append('--dry-run')
    p = subprocess.Popen(['nice', '-n', '10'] + cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors='replace')
    out = []
    buf = ''
    while True:
        ch = p.stdout.read(1)
        if not ch:
            break
        if ch in '\r\n':
            if buf.strip():
                out.append(buf.strip())
                if on_line:
                    on_line(buf.strip())
            buf = ''
        else:
            buf += ch
    if p.wait():
        raise RuntimeError('pmtiles extract failed: ' + ' '.join(out[-3:]))
    return out


# ---- region catalogue (Geofabrik) ---------------------------------------------------------------------------------
_catalog_lock = threading.Lock()


def _bbox_of(geom):
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(geom['coordinates'])
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def catalog():
    path = DATA / 'cache/regions.json'
    with _catalog_lock:
        if path.is_file() and time.time() - path.stat().st_mtime < 30 * 86400:
            return json.loads(path.read_text())
        req = urllib.request.Request(GEOFABRIK_INDEX, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            idx = json.load(r)
        regions, geo = [], {}
        for f in idx['features']:
            p = f['properties']
            if not f.get('geometry') or 'pbf' not in p.get('urls', {}):
                continue
            iso = (p.get('iso3166-1:alpha2') or [None])[0] or (p.get('iso3166-2') or [''])[0][:2] or ''
            regions.append({'id': p['id'], 'name': p['name'], 'parent': p.get('parent'), 'pbf': p['urls']['pbf'],
                            'bbox': _bbox_of(f['geometry']), 'iso': iso.lower()})
            geo[p['id']] = f['geometry']
        by = {r['id']: r for r in regions}
        for r in regions:                     # countries inherit iso downwards
            p = r
            while not r['iso'] and p.get('parent') in by:
                p = by[p['parent']]
                r['iso'] = p['iso']
        path.write_text(json.dumps(regions))
        (DATA / 'cache/region-shapes.json').write_text(json.dumps(geo))
        return regions


def region_shape(rid):
    """Write the region polygon as GeoJSON for `pmtiles extract --region`."""
    geo = json.loads((DATA / 'cache/region-shapes.json').read_text()).get(rid)
    if not geo:
        return None
    path = DATA / f'cache/{rid}.geojson'
    path.write_text(json.dumps({'type': 'Feature', 'properties': {}, 'geometry': geo}))
    return path


def find_regions(query=None, lat=None, lon=None, limit=20):
    regions = catalog()
    by = {r['id']: r for r in regions}

    def path(r):
        names, p = [], r
        while p.get('parent') in by:
            p = by[p['parent']]
            names.append(p['name'])
        return ', '.join(names)
    out = []
    if lat is not None and lon is not None:
        lat, lon = _coord(lat, lon)
        hits = [r for r in regions if r['bbox'][0] <= lon <= r['bbox'][2] and r['bbox'][1] <= lat <= r['bbox'][3]]
        hits.sort(key=lambda r: (r['bbox'][2] - r['bbox'][0]) * (r['bbox'][3] - r['bbox'][1]))
        out = hits
    else:
        q = (query or '').strip().lower()
        out = [r for r in regions if not q or q in r['name'].lower() or q in r['id']]
        out.sort(key=lambda r: (not r['name'].lower().startswith(q), r['parent'] is not None and r['parent'] not in
                                ('europe', 'asia', 'africa', 'north-america', 'south-america', 'australia-oceania',
                                 'central-america', 'russia'), r['name']))
    return {'regions': [{'id': r['id'], 'name': r['name'], 'in': path(r), 'bbox': r['bbox']} for r in out[:limit]]}


def _ancestors(rid, by):
    out, p = [], by.get(rid)
    while p and p.get('parent') in by:
        out.append(p['parent'])
        p = by[p['parent']]
    return out


# ---- GraphHopper Photon dumps -------------------------------------------------------------------------------------
def photon_dumps():
    path = DATA / 'cache/photon-dumps.json'
    if path.is_file() and time.time() - path.stat().st_mtime < 7 * 86400:
        return json.loads(path.read_text())
    found = {}
    for cont in GH_CONTINENTS:
        try:
            req = urllib.request.Request(f'{GRAPHHOPPER}{cont}/index.html', headers={'User-Agent': UA})
            html = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')
        except OSError:
            continue
        for name in set(re.findall(rf'public/{cont}/([a-z0-9-]+)/index\.html', html)):
            found[name] = f'{GRAPHHOPPER}{cont}/{name}/photon-dump-{name}-1.0-latest.jsonl.zst'
    if found:
        path.write_text(json.dumps(found))
    return found


def dump_for(rid, by):
    dumps = photon_dumps()
    for cand in [rid] + _ancestors(rid, by):
        name = GH_ALIASES.get(cand, cand)
        if name in dumps:
            return name, dumps[name]
    return None, None


# ---- transit feeds (Mobility Database) ----------------------------------------------------------------------------
def mdb_rows():
    path = DATA / 'cache/mdb.csv'
    download(MDB_CSV, path, max_age=TRANSIT_REFRESH_DAYS * 86400)
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def _overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def feed_candidates(region):
    """Active, open, static GTFS feeds for the region's country whose bounding box overlaps the region."""
    iso = (region.get('iso') or '').upper()
    out = []
    for r in mdb_rows():
        if r['data_type'] != 'gtfs' or r['status'] in ('deprecated', 'inactive', 'development') or r['redirect.id']:
            continue
        if r['urls.authentication_type'] not in ('', '0') or r['location.country_code'] != iso:
            continue
        try:
            fb = [float(r['location.bounding_box.minimum_longitude']), float(r['location.bounding_box.minimum_latitude']),
                  float(r['location.bounding_box.maximum_longitude']), float(r['location.bounding_box.maximum_latitude'])]
            if not _overlap(fb, region['bbox']):
                continue
        except ValueError:
            pass                                   # no bbox in the catalogue: the country match is enough
        out.append({'id': 'mdb-' + r['mdb_source_id'], 'provider': r['provider'][:120], 'official': r['is_official'] == 'True',
                    'url': r['urls.direct_download'], 'mirror': r['urls.latest'], 'license': r['urls.license']})
    out.sort(key=lambda f: (not f['official'], f['id']))
    return out


def _gtfs_summary(path):
    """(agencies, last service day) of a GTFS zip, or None if it isn't one."""
    import zipfile
    try:
        z = zipfile.ZipFile(path)
        names = {Path(n).name: n for n in z.namelist()}
        if 'stops.txt' not in names or 'trips.txt' not in names:
            return None
        agencies = set()
        if 'agency.txt' in names:
            for row in csv.DictReader(io.TextIOWrapper(z.open(names['agency.txt']), 'utf-8-sig', errors='replace')):
                agencies.add((row.get('agency_name') or '').strip().lower())
        end = ''
        for fname, col in (('calendar.txt', 'end_date'), ('calendar_dates.txt', 'date')):
            if fname in names:
                for row in csv.DictReader(io.TextIOWrapper(z.open(names[fname]), 'utf-8-sig', errors='replace')):
                    end = max(end, (row.get(col) or '').strip())
        return agencies, end
    except Exception:  # noqa: BLE001 - a broken feed is simply skipped
        return None


def fetch_feeds(region, progress):
    today = time.strftime('%Y%m%d')
    kept, seen_agencies = [], []
    cands = feed_candidates(region)[:MAX_FEEDS * 2]
    for i, f in enumerate(cands):
        if len(kept) >= MAX_FEEDS:
            break
        progress(f"Transit feed {i + 1}/{len(cands)}: {f['provider'][:40]}")
        dest = DATA / 'cache/gtfs' / f"{f['id']}.zip"
        ok = False
        for url in (f['url'], f['mirror']):
            if not url:
                continue
            try:
                download(url, dest, max_age=TRANSIT_REFRESH_DAYS * 86400)
                ok = True
                break
            except Exception:  # noqa: BLE001
                continue
        info = _gtfs_summary(dest) if ok else None
        if not info or (info[1] and info[1] < today):
            dest.unlink(missing_ok=True)
            continue
        agencies, end = info
        if any(agencies and len(agencies & a) >= max(1, len(agencies) // 2) for a in seen_agencies):
            continue                                # same operators as a feed already kept
        seen_agencies.append(agencies)
        kept.append(dict(f, path=str(dest), until=end, agencies=len(agencies)))
    return kept


# ---- regions -----------------------------------------------------------------------------------------------------
def _region_row(r):
    d = dict(r)
    d['bbox'] = json.loads(d['bbox'] or '[]')
    d['info'] = json.loads(d['info'] or '{}')
    return d


@db
def _regions(c):
    return [_region_row(r) for r in c.execute('SELECT * FROM regions ORDER BY added').fetchall()]


@db
def _set_region(c, rid, **kw):
    if 'info' in kw:
        kw['info'] = json.dumps(kw['info'])
    if 'bbox' in kw:
        kw['bbox'] = json.dumps(kw['bbox'])
    c.execute('UPDATE regions SET ' + ', '.join(f'{k}=?' for k in kw) + ' WHERE id=?', (*kw.values(), rid))


def engine_status(name):
    try:
        doc = json.loads((DATA / 'engines' / name / 'status.json').read_text())
    except (OSError, ValueError):
        return {'state': 'offline'}
    if time.time() - doc.get('updated', 0) > 120 and doc.get('state') == 'building':
        doc['state'] = 'stalled'
    return doc


def regions_doc():
    rs = _regions()
    world = kv_get('world', {}) or {}
    wfile = DATA / 'tiles/world.pmtiles'
    out = []
    for r in rs:
        tiles = DATA / f"tiles/{r['id']}.pmtiles"
        info = r['info']
        out.append({'id': r['id'], 'name': r['name'], 'state': r['state'], 'step': r['step'], 'pct': round(r['pct'] or 0),
                    'message': r['message'], 'bbox': r['bbox'], 'built_at': r['built_at'],
                    'tiles': f"api/tiles/{r['id']}.pmtiles" if tiles.is_file() else None,
                    'tiles_bytes': tiles.stat().st_size if tiles.is_file() else 0,
                    'search': bool(info.get('search')), 'routing': bool(info.get('routing')),
                    'transit': [{'id': f['id'], 'provider': f['provider'], 'license': f.get('license', ''),
                                 'until': f.get('until', '')} for f in info.get('feeds', [])] if info.get('transit') else [],
                    'transit_feeds_found': len(info.get('feeds', []))})
    return {'world': {'state': world.get('state', 'queued'), 'message': world.get('message', ''),
                      'tiles': 'api/tiles/world.pmtiles' if wfile.is_file() else None,
                      'tiles_bytes': wfile.stat().st_size if wfile.is_file() else 0, 'maxzoom': WORLD_MAXZOOM},
            'regions': out,
            'engines': {k: {x: v for x, v in engine_status(k).items() if x in ('state', 'step', 'message', 'pct')}
                        for k in ('photon', 'valhalla', 'motis')},
            'busy': any(r['state'] in ('queued', 'building') for r in rs)}


@db
def _insert_region(c, r, state='queued'):
    c.execute('INSERT INTO regions (id,name,parent,bbox,pbf_url,iso,state,added,info) VALUES (?,?,?,?,?,?,?,?,?) '
              "ON CONFLICT(id) DO UPDATE SET state=excluded.state, step='', pct=0, message=''",
              (r['id'], r['name'], r['parent'], json.dumps(r['bbox']), r['pbf'], r['iso'], state, time.time(), '{}'))


def add_region(rid):
    if not isinstance(rid, str) or not re.fullmatch(r'[a-z0-9-]{2,80}', rid):
        raise Problem(400, 'region must be a region id such as "thailand" or "luxembourg" (see find_region).')
    by = {r['id']: r for r in catalog()}
    if rid not in by:
        raise Problem(404, f'No region "{rid}". Use find_region to look one up.')
    have = {r['id']: r for r in _regions()}
    if rid in have and have[rid]['state'] != 'failed':
        return {'region': rid, 'state': have[rid]['state'], 'already': True}
    covering = [a for a in _ancestors(rid, by) if a in have]
    if covering:
        raise Problem(409, f'{by[covering[0]]["name"]} is already on your server and includes {by[rid]["name"]}.')
    for child in [h for h in have if rid in _ancestors(h, by)]:
        remove_region(child, rebuild=False)        # the bigger region replaces the smaller one
    _insert_region(by[rid])
    WORKER.kick()
    return {'region': rid, 'state': 'queued', 'already': False}


@db
def _delete_region_row(c, rid):
    c.execute('DELETE FROM regions WHERE id=?', (rid,))


def remove_region(rid, rebuild=True):
    if rid not in {r['id'] for r in _regions()}:
        raise Problem(404, 'That region is not on your server.')
    _delete_region_row(rid)
    for p in (DATA / f'tiles/{rid}.pmtiles', DATA / f'cache/osm/{rid}.osm.pbf', DATA / f'cache/{rid}.geojson'):
        p.unlink(missing_ok=True)
    if rebuild:
        kv_set('engines_dirty', True)
        WORKER.kick()
    return {'removed': rid}


def refresh_region(rid, what='all'):
    have = {r['id']: r for r in _regions()}
    if rid not in have:
        raise Problem(404, 'That region is not on your server.')
    _set_region(rid, state='queued', step='', pct=0, message='', built_at=None if what == 'all' else have[rid]['built_at'])
    if what == 'all':
        for p in (DATA / f'cache/osm/{rid}.osm.pbf', DATA / f'tiles/{rid}.pmtiles'):
            p.unlink(missing_ok=True)
    WORKER.kick()
    return {'region': rid, 'state': 'queued'}


def region_for(lat, lon, need=None):
    for r in _regions():
        b = r['bbox']
        if r['state'] == 'ready' and b and b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            if need is None or r['info'].get(need):
                return r
    return None


# ---- background worker -------------------------------------------------------------------------------------------
class Worker:
    """One build at a time: world basemap and assets first, then region inputs, then the shared engines."""

    def __init__(self):
        self.event = threading.Event()
        self.thread = None

    def start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self.loop, daemon=True, name='maps-worker')
            self.thread.start()

    def kick(self):
        self.event.set()

    def loop(self):
        time.sleep(2)
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything; errors land on the job
                kv_set('worker_error', str(e)[-400:])
            self.event.wait(3600)
            self.event.clear()

    def tick(self):
        self.setup()
        cleanup_packs()
        now = time.time()
        for r in _regions():                       # scheduled refresh
            if r['state'] == 'ready' and r['built_at'] and now - r['built_at'] > REFRESH_DAYS * 86400:
                _set_region(r['id'], state='queued', step='', message='Monthly refresh')
            elif r['state'] == 'ready' and r['info'].get('feeds') and now - (r['transit_at'] or 0) > TRANSIT_REFRESH_DAYS * 86400:
                kv_set('transit_dirty', True)
        queued = [r for r in _regions() if r['state'] in ('queued', 'building')]
        for r in queued:
            try:
                self.inputs(r)
            except Exception as e:  # noqa: BLE001
                _set_region(r['id'], state='failed', message=str(e)[-300:])
        if queued or kv_get('engines_dirty') or kv_get('transit_dirty'):
            self.engines([r['id'] for r in queued], transit_only=not queued and not kv_get('engines_dirty'))

    # -- world + assets
    def setup(self):
        world = kv_get('world', {}) or {}
        wfile = DATA / 'tiles/world.pmtiles'
        if not (DATA / 'assets/fonts').is_dir():
            kv_set('world', dict(world, state='building', message='Fonts and map symbols'))
            self.assets()
        if not wfile.is_file() or time.time() - wfile.stat().st_mtime > REFRESH_DAYS * 86400:
            kv_set('world', {'state': 'building', 'message': 'World overview map'})
            tmp = DATA / 'tiles/world.pmtiles.tmp'
            try:
                pmtiles_extract(planet_url(), tmp, maxzoom=WORLD_MAXZOOM,
                                on_line=lambda line: kv_set('world', {'state': 'building',
                                                                      'message': 'World overview map: ' + line[-60:]}))
                os.replace(tmp, wfile)
            except Exception as e:  # noqa: BLE001
                kv_set('world', {'state': 'failed' if not wfile.is_file() else 'ready', 'message': str(e)[-200:]})
                return
        kv_set('world', {'state': 'ready', 'message': '', 'at': wfile.stat().st_mtime})

    def assets(self):
        req = urllib.request.Request(ASSETS_TARBALL, headers={'User-Agent': UA})
        data = urllib.request.urlopen(req, timeout=120).read()
        tmp = DATA / 'assets.tmp'
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
            keep = [m for m in t.getmembers() if re.search(r'/(fonts|sprites/v4)/', m.name)]
            t.extractall(tmp, members=keep, filter='data')
        root = next(tmp.iterdir())
        for sub in ('fonts', 'sprites'):
            dst = DATA / 'assets' / sub
            shutil.rmtree(dst, ignore_errors=True)
            shutil.move(str(root / sub), dst)
        shutil.rmtree(tmp, ignore_errors=True)

    # -- per-region inputs: tiles, OSM extract, search dump, transit feeds
    def inputs(self, r):
        rid = r['id']
        by = {x['id']: x for x in catalog()}
        info = r['info']

        def step(name, pct, message=''):
            _set_region(rid, state='building', step=name, pct=pct, message=message)

        free = shutil.disk_usage(DATA).free
        if free < MIN_FREE_BYTES:
            raise RuntimeError(f'Not enough free disk on the server ({free >> 30} GB free, needs {MIN_FREE_BYTES >> 30} GB '
                               'to build a region). Free some space, then refresh this region.')
        step('map', 2, 'Cutting the detailed map')
        tiles = DATA / f'tiles/{rid}.pmtiles'
        seed = DATA / f'seed/{rid}.pmtiles'
        if seed.is_file():
            os.replace(seed, tiles)
        if not tiles.is_file() or time.time() - tiles.stat().st_mtime > REFRESH_DAYS * 86400:
            shape = region_shape(rid)
            tmp = DATA / f'tiles/{rid}.pmtiles.tmp'

            def on_line(line, _n=[0]):
                m = re.search(r'(\d+)%', line)
                if m:
                    step('map', 2 + int(m.group(1)) * 0.13, 'Cutting the detailed map')
            pmtiles_extract(planet_url(), tmp, region_file=shape, bbox=None if shape else r['bbox'],
                            maxzoom=REGION_MAXZOOM, on_line=on_line)
            os.replace(tmp, tiles)
        step('osm', 15, 'Downloading OpenStreetMap data')
        pbf = DATA / f'cache/osm/{rid}.osm.pbf'
        seed = DATA / f'seed/{rid}.osm.pbf'
        if seed.is_file():
            os.replace(seed, pbf)
        download(r['pbf_url'], pbf, max_age=REFRESH_DAYS * 86400,
                 progress=lambda d, t: step('osm', 15 + (15 * d / t if t else 0), f'OpenStreetMap data {d >> 20} MB'))
        step('search data', 30, 'Downloading search data')
        name, url = dump_for(rid, by)
        info['dump'] = name
        if url:
            dest = DATA / f'cache/photon/{name}.jsonl.zst'
            seed = DATA / f'seed/{name}.jsonl.zst'
            if seed.is_file():
                os.replace(seed, dest)
            download(url, dest, max_age=7 * 86400,
                     progress=lambda d, t: step('search data', 30 + (8 * d / t if t else 0), f'Search data {d >> 20} MB'))
        step('transit data', 38, 'Looking for transit timetables')
        seed_dir = DATA / 'seed/gtfs'
        if seed_dir.is_dir():
            for f in seed_dir.iterdir():
                os.replace(f, DATA / 'cache/gtfs' / f.name)
            shutil.rmtree(seed_dir, ignore_errors=True)
        try:
            info['feeds'] = fetch_feeds(dict(r, iso=r['iso'] or by.get(rid, {}).get('iso', '')),
                                        lambda m: step('transit data', 40, m))
        except Exception as e:  # noqa: BLE001 - transit is optional
            info['feeds'] = []
            info['transit_error'] = str(e)[-200:]
        _set_region(rid, info=info, step='waiting', pct=42, message='Waiting for the server to index it')

    # -- shared engines
    def engines(self, building, transit_only=False):
        rs = [r for r in _regions() if r['state'] in ('ready', 'building')]
        ids = [r['id'] for r in rs]

        def each(step, pct, message):
            for rid in building:
                _set_region(rid, state='building', step=step, pct=pct, message=message)
        if not transit_only:
            kv_set('engines_dirty', True)
            each('search index', 45, 'Building the search index')
            search_ok = self.build_search(rs, lambda pct, m: each('search index', 45 + pct * 0.2, m))
            each('routing', 65, 'Building the road network for directions')
            routing_ok = self.build_routing(rs, lambda pct, m: each('routing', 65 + pct * 0.25, m))
            kv_set('engines_dirty', False)
        else:
            search_ok = routing_ok = None
        each('transit', 90, 'Building transit timetables')
        transit = self.build_transit(rs, lambda m: each('transit', 92, m))
        kv_set('transit_dirty', False)
        for r in _regions():
            if r['id'] not in ids:
                continue
            info = r['info']
            if search_ok is not None:
                info['search'] = bool(search_ok and info.get('dump'))
                info['routing'] = bool(routing_ok)
            info['transit'] = r['id'] in transit
            msg = []
            if not info.get('dump'):
                msg.append('No search data published for this region')
            elif not info['search']:
                msg.append('Search index failed: ' + engine_status('photon').get('message', '')[-120:])
            if not info['routing']:
                msg.append('Directions failed: ' + engine_status('valhalla').get('message', '')[-120:])
            if info.get('feeds') and not info['transit']:
                msg.append('Transit failed: ' + str(engine_status('motis').get('failed', {}))[:80])
            done = {'state': 'ready', 'step': '', 'pct': 100, 'message': '; '.join(msg), 'info': info}
            if r['id'] in building:
                done['built_at'] = time.time()
            if info['transit']:
                done['transit_at'] = time.time()
            _set_region(r['id'], **done)

    @staticmethod
    def wait(engine, done, progress, timeout=6 * 3600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = engine_status(engine)
            if done(st):
                return st
            if st['state'] == 'offline' and time.time() - t0 > 180:
                raise RuntimeError(f'the {engine} engine is not running')
            progress(st)
            time.sleep(5)
        raise RuntimeError(f'{engine} build timed out')

    def build_search(self, rs, progress):
        if zstd is None:
            return False
        try:
            tool('photon.jar')
        except Exception as e:  # noqa: BLE001
            progress(0, f'Could not get the search engine: {e}')
            return False
        by = {x['id']: x for x in catalog()}
        groups = {}
        for r in rs:
            name = r['info'].get('dump') or dump_for(r['id'], by)[0]
            if name and (DATA / f'cache/photon/{name}.jsonl.zst').is_file():
                groups.setdefault(name, []).append(r)
        if not groups:
            return False
        gen = (kv_get('photon_gen', 0) or 0) + 1
        out = DATA / f'engines/photon/input-{gen}.jsonl'
        langs, lines, header = {'en'}, 0, False
        with open(out, 'w', encoding='utf-8') as w:
            for name, regs in groups.items():
                whole = any(GH_ALIASES.get(r['id'], r['id']) == name for r in regs)
                boxes = [r['bbox'] for r in regs]
                for r in regs:
                    langs.update((LANGS.get(r['iso'] or '', '') or '').split(','))
                with zstd.open(DATA / f'cache/photon/{name}.jsonl.zst', 'rt', encoding='utf-8') as f:
                    for i, line in enumerate(f):
                        if line.startswith('{"type":"NominatimDumpFile"') or line.startswith('{"type":"CountryInfo"'):
                            if not header:
                                w.write(line)
                            continue
                        if not whole:
                            m = re.search(r'"centroid":\[([-0-9.eE]+),([-0-9.eE]+)\]', line)
                            if not m or not any(b[0] <= float(m.group(1)) <= b[2] and b[1] <= float(m.group(2)) <= b[3]
                                                for b in boxes):
                                continue
                        w.write(line)
                        lines += 1
                        if lines % 50000 == 0:
                            progress(0, f'Preparing search data ({lines:,} places)')
                header = True
        langs.discard('')
        env = f"GEN={gen}\nINPUT={out}\nLANGS={','.join(sorted(langs))}\nEXTRA=opening_hours,website,phone,cuisine,brand\n"
        tmp = DATA / 'engines/photon/want.env.tmp'
        tmp.write_text(env)
        os.replace(tmp, DATA / 'engines/photon/want.env')
        kv_set('photon_gen', gen)
        kv_set('photon_langs_next', sorted(langs))

        def prog(st):
            m = re.search(r'Imported (\d+)', st.get('message') or '')
            n = int(m.group(1)) if m else 0
            progress(min(100, 100 * n / max(1, lines)), f'Indexing places for search ({n:,} of {lines:,})')
        try:
            st = self.wait('photon', lambda s: int(s.get('done') or 0) >= gen and s.get('state') != 'building', prog)
        except RuntimeError as e:
            progress(100, str(e))
            out.unlink(missing_ok=True)
            return False
        ok = int(st.get('serving') or 0) == gen
        if ok:
            kv_set('photon_langs', kv_get('photon_langs_next', []))
        return ok

    def build_routing(self, rs, progress):
        pbfs = [str(DATA / f"cache/osm/{r['id']}.osm.pbf") for r in rs if (DATA / f"cache/osm/{r['id']}.osm.pbf").is_file()]
        if not pbfs:
            return False
        gen = (kv_get('valhalla_gen', 0) or 0) + 1
        tmp = DATA / 'engines/valhalla/want.json.tmp'
        tmp.write_text(json.dumps({'gen': gen, 'pbfs': pbfs}))
        os.replace(tmp, DATA / 'engines/valhalla/want.json')
        kv_set('valhalla_gen', gen)
        steps = {'config': 'Preparing', 'admins': 'Borders', 'timezones': 'Time zones', 'graph': 'Road network',
                 'pack': 'Packing'}
        try:
            st = self.wait('valhalla', lambda s: s.get('state') in ('serving', 'failed') and
                           (s.get('serving') == gen or s.get('state') == 'failed') and s.get('updated', 0) > 0,
                           lambda s: progress(s.get('pct') or 0, 'Directions: ' + steps.get(s.get('step'), 'building')))
        except RuntimeError as e:
            progress(100, str(e))
            return False
        return st.get('serving') == gen

    def build_transit(self, rs, progress):
        regions = []
        for r in rs:
            feeds = [f for f in r['info'].get('feeds', []) if Path(f['path']).is_file()]
            pbf = DATA / f"cache/osm/{r['id']}.osm.pbf"
            if not feeds or not pbf.is_file():
                continue
            h = hashlib.sha256()
            for f in feeds:
                st = Path(f['path']).stat()
                h.update(f"{f['id']}:{st.st_size}:{int(st.st_mtime)}".encode())
            h.update(str(int(pbf.stat().st_mtime)).encode())
            regions.append({'id': r['id'], 'pbf': str(pbf), 'version': h.hexdigest()[:12],
                            'feeds': [{'id': re.sub(r'[^a-z0-9]', '', f['id']), 'path': f['path']} for f in feeds]})
        if not regions:
            want = {'regions': []}
        else:
            try:
                tool('motis')
            except Exception as e:  # noqa: BLE001
                progress(f'Could not get the transit engine: {e}')
                return set()
            want = {'regions': regions}
        tmp = DATA / 'engines/motis/want.json.tmp'
        tmp.write_text(json.dumps(want))
        os.replace(tmp, DATA / 'engines/motis/want.json')
        if not regions:
            return set()
        wanted = {r['id']: r['version'] for r in regions}

        def done(s):
            built, failed = s.get('regions') or {}, s.get('failed') or {}
            return s.get('state') != 'building' and all(
                (built.get(k) or {}).get('version') == v or failed.get(k) == v for k, v in wanted.items())
        try:
            st = self.wait('motis', done, lambda s: progress('Transit: ' + (s.get('step') or 'building')))
        except RuntimeError as e:
            progress(str(e))
            return set()
        built = st.get('regions') or {}
        return {k for k, v in wanted.items() if (built.get(k) or {}).get('version') == v}


WORKER = Worker()


# ---- search, reverse, route --------------------------------------------------------------------------------------
def _norm_feature(f):
    p, (lon, lat) = f.get('properties', {}), f['geometry']['coordinates']
    street = ' '.join(x for x in (p.get('housenumber'), p.get('street')) if x)
    parts = [street, p.get('district') or p.get('locality'), p.get('city'), p.get('state'), p.get('country')]
    seen, label = set(), []
    for x in parts:
        if x and x not in seen and x != p.get('name'):
            seen.add(x); label.append(x)
    kind = (p.get('osm_value') or '').replace('_', ' ')
    return {'name': p.get('name') or street or p.get('city') or 'Dropped pin', 'address': ', '.join(label),
            'lat': round(lat, 7), 'lon': round(lon, 7), 'kind': kind, 'category': p.get('osm_key', ''),
            'ref': f"{p.get('osm_type', '')}{p.get('osm_id', '')}", 'extent': p.get('extent'),
            'country': p.get('countrycode', ''), 'extra': {k: v for k, v in (p.get('extra') or {}).items()
                                                           if k in ('opening_hours', 'website', 'phone', 'cuisine', 'brand')}}


def _photon(path, params):
    try:
        return http_json(f'{PHOTON_URL}{path}?' + urllib.parse.urlencode(params, doseq=True), timeout=10)
    except (urllib.error.URLError, OSError, ValueError):
        raise Problem(503, 'Search is not ready yet. Add a region in Maps (Regions) and wait for it to finish.') from None


def search(q, lat=None, lon=None, limit=8, lang=None):
    q = _text(q, 'q', 200, True)
    try:
        limit = max(1, min(20, int(limit or 8)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    params = {'q': q, 'limit': limit}
    if lat not in (None, '') and lon not in (None, ''):
        params['lat'], params['lon'] = _coord(lat, lon)
        params['location_bias_scale'] = 0.3
    if lang and str(lang) in (kv_get('photon_langs') or []):
        params['lang'] = lang
    d = _photon('/api', params)
    saved = list_places(query=q)['places'][:3]
    return {'query': q, 'results': [_norm_feature(f) for f in d.get('features', [])],
            'saved': [{'name': p['name'], 'address': p['address'], 'lat': p['lat'], 'lon': p['lon'], 'id': p['id'],
                       'list': p['list']} for p in saved]}


def reverse(lat, lon, lang=None):
    lat, lon = _coord(lat, lon)
    params = {'lat': lat, 'lon': lon, 'limit': 1}
    if lang and str(lang) in (kv_get('photon_langs') or []):
        params['lang'] = lang
    try:
        d = _photon('/reverse', params)
        feats = d.get('features') or []
    except Problem:
        feats = []
    if feats:
        return {'place': _norm_feature(feats[0])}
    return {'place': {'name': 'Dropped pin', 'address': f'{lat:.5f}, {lon:.5f}', 'lat': lat, 'lon': lon, 'kind': '',
                      'ref': '', 'extra': {}}}


def decode_polyline(s, precision=6):
    coords, idx, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while idx < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[idx]) - 63
                idx += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        coords.append([round(lon / f, 6), round(lat / f, 6)])
    return coords


def parse_point(v, field):
    """'lat,lon' or a place name (first search result)."""
    if isinstance(v, dict):
        return _coord(v.get('lat'), v.get('lon')), v.get('name') or ''
    if not isinstance(v, str) or not v.strip():
        raise Problem(400, f'{field} must be "lat,lon" or a place name.')
    m = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*', v)
    if m:
        return _coord(m.group(1), m.group(2)), ''
    res = search(v, limit=1)['results']
    if not res:
        raise Problem(404, f'Could not find "{v}".')
    return (res[0]['lat'], res[0]['lon']), res[0]['name']


COSTING = {'car': 'auto', 'motorbike': 'motorcycle', 'bike': 'bicycle', 'walk': 'pedestrian'}


def route(frm, to, mode='car', depart_at=None, lang='en'):
    mode = mode or 'car'
    if mode not in (*COSTING, 'transit'):
        raise Problem(400, 'mode must be car, motorbike, bike, walk or transit.')
    (a, a_name), (b, b_name) = parse_point(frm, 'from'), parse_point(to, 'to')
    if mode == 'transit':
        return transit_route(a, b, depart_at, a_name, b_name)
    body = {'locations': [{'lat': a[0], 'lon': a[1]}, {'lat': b[0], 'lon': b[1]}], 'costing': COSTING[mode],
            'directions_options': {'units': 'kilometers', 'language': lang if lang in ('en', 'de', 'fr', 'es', 'it', 'nl', 'pt', 'ru', 'ja', 'zh', 'cs', 'pl') else 'en'},
            'alternates': 2}
    try:
        d = http_json(f'{VALHALLA_URL}/route', body, timeout=30)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get('error', '')
        except ValueError:
            msg = ''
        raise Problem(404 if e.code == 400 else 502, f'No route found: {msg or e.code}.') from None
    except (urllib.error.URLError, OSError):
        raise Problem(503, 'Directions are not ready yet. Add the region in Maps (Regions) and wait for it to finish.') from None
    trips = [d['trip']] + [x['trip'] for x in d.get('alternates', [])]
    routes = []
    for t in trips:
        leg = t['legs'][0]
        routes.append({'distance_km': round(t['summary']['length'], 2), 'duration_min': round(t['summary']['time'] / 60, 1),
                       'geometry': {'type': 'LineString', 'coordinates': decode_polyline(leg['shape'])},
                       'steps': [{'instruction': m.get('instruction', ''), 'distance_km': round(m.get('length', 0), 2),
                                  'type': m.get('type'), 'at': m.get('begin_shape_index', 0)} for m in leg.get('maneuvers', [])]})
    return {'mode': mode, 'from': {'lat': a[0], 'lon': a[1], 'name': a_name}, 'to': {'lat': b[0], 'lon': b[1], 'name': b_name},
            'routes': routes, 'note': 'ETAs do not include live traffic.' if mode in ('car', 'motorbike') else ''}


def _iso(t):
    return t if isinstance(t, str) else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t / 1000 if t > 1e11 else t))


def transit_route(a, b, depart_at, a_name='', b_name=''):
    r = region_for(a[0], a[1], 'transit') or region_for(b[0], b[1], 'transit')
    st = engine_status('motis').get('regions') or {}
    if not r or r['id'] not in st:
        raise Problem(503, 'No transit timetables on your server for this area yet.')
    when = depart_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if not isinstance(when, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d{1,6})?)?(Z|[+-]\d\d:?\d\d)?', when):
        raise Problem(400, 'depart_at must be an ISO time like 2026-11-19T09:00:00+07:00.')
    when = re.sub(r'(:\d\d)\.\d+', r'\1', when)
    q = urllib.parse.urlencode({'fromPlace': f'{a[0]},{a[1]}', 'toPlace': f'{b[0]},{b[1]}', 'time': when,
                                'numItineraries': 3})
    try:
        d = http_json(f"http://{MOTIS_HOST}:{st[r['id']]['port']}/api/v5/plan?{q}", timeout=30)
    except (urllib.error.URLError, OSError, ValueError):
        raise Problem(503, 'Transit is not answering right now. Try again in a minute.') from None
    routes = []
    for it in d.get('itineraries', [])[:3]:
        legs, line = [], []
        for leg in it.get('legs', []):
            g = leg.get('legGeometry') or {}
            coords = decode_polyline(g['points'], g.get('precision', 7)) if g.get('points') else []
            line += coords
            legs.append({'mode': leg.get('mode'), 'line': leg.get('routeShortName') or leg.get('routeLongName') or '',
                         'color': ('#' + leg['routeColor']) if re.fullmatch(r'[0-9A-Fa-f]{6}', leg.get('routeColor') or '') else '',
                         'from': (leg.get('from') or {}).get('name', ''), 'to': (leg.get('to') or {}).get('name', ''),
                         'start': leg.get('startTime'), 'end': leg.get('endTime'),
                         'duration_min': round(leg.get('duration', 0) / 60, 1), 'headsign': leg.get('headsign', ''),
                         'agency': leg.get('agencyName', ''),
                         'geometry': {'type': 'LineString', 'coordinates': coords}})
        routes.append({'duration_min': round(it.get('duration', 0) / 60, 1), 'transfers': it.get('transfers', 0),
                       'start': it.get('startTime'), 'end': it.get('endTime'), 'legs': legs,
                       'geometry': {'type': 'LineString', 'coordinates': line}})
    feeds = r['info'].get('feeds', [])
    return {'mode': 'transit', 'from': {'lat': a[0], 'lon': a[1], 'name': a_name}, 'to': {'lat': b[0], 'lon': b[1], 'name': b_name},
            'routes': routes, 'attribution': [f"{f['provider']} ({f.get('license') or 'see feed'})" for f in feeds],
            'note': 'Scheduled times; no live delays.' if routes else 'No transit connection found at that time.'}


# ---- offline packs -----------------------------------------------------------------------------------------------
def _bbox(v):
    if isinstance(v, str):
        v = v.split(',')
    try:
        w, s, e, n = (float(x) for x in v)
    except (TypeError, ValueError):
        raise Problem(400, 'bbox must be west,south,east,north.') from None
    if not (-180 <= w < e <= 180 and -85 <= s < n <= 85):
        raise Problem(400, 'bbox must be west,south,east,north within the world.')
    return [w, s, e, n]


def _pack_source(bbox):
    for r in _regions():
        b = r['bbox']
        tiles = DATA / f"tiles/{r['id']}.pmtiles"
        if b and tiles.is_file() and b[0] <= bbox[0] and b[1] <= bbox[1] and bbox[2] <= b[2] and bbox[3] <= b[3]:
            return str(tiles)
    return planet_url()


def estimate_pack(bbox, maxzoom=15):
    bbox, maxzoom = _bbox(bbox), max(8, min(15, int(maxzoom or 15)))
    out = pmtiles_extract(_pack_source(bbox), DATA / 'packs/estimate.pmtiles', bbox=bbox, maxzoom=maxzoom, dry_run=True)
    size = 0
    for line in out:
        m = re.search(r'archive size of ([\d.]+) ?([KMG]?B)', line)
        if m:
            size = int(float(m.group(1)) * {'B': 1, 'KB': 1e3, 'MB': 1e6, 'GB': 1e9}[m.group(2)])
    return {'bbox': bbox, 'maxzoom': maxzoom, 'bytes': size}


@db
def _pack_rows(c):
    return [dict(r, bbox=json.loads(r['bbox'])) for r in c.execute('SELECT * FROM packs ORDER BY created').fetchall()]


@db
def _pack_put(c, pid, **kw):
    if c.execute('SELECT 1 FROM packs WHERE id=?', (pid,)).fetchone():
        c.execute('UPDATE packs SET ' + ', '.join(f'{k}=?' for k in kw) + ' WHERE id=?', (*kw.values(), pid))
    else:
        kw.setdefault('created', time.time())
        c.execute(f"INSERT INTO packs (id,{','.join(kw)}) VALUES (?,{','.join('?' * len(kw))})", (pid, *kw.values()))


def packs():
    out = []
    for p in _pack_rows():
        f = DATA / f"packs/{p['id']}.pmtiles"
        out.append(dict(p, url=f"api/packs/{p['id']}.pmtiles" if p['state'] == 'ready' and f.is_file() else None))
    return {'packs': out}


def make_pack(args):
    bbox = _bbox(args.get('bbox'))
    maxzoom = max(8, min(15, int(args.get('maxzoom') or 15)))
    name = _text(args.get('name') or 'Offline area', 'name', 80, True)
    pid = args.get('id') or uuid.uuid4().hex[:12]
    if not re.fullmatch(r'[A-Za-z0-9-]{6,64}', str(pid)):
        raise Problem(400, 'id must be 6-64 letters, digits or dashes.')
    existing = {p['id']: p for p in _pack_rows()}
    if pid in existing and existing[pid]['state'] in ('building', 'ready'):
        return {'pack': dict(existing[pid], url=f'api/packs/{pid}.pmtiles' if existing[pid]['state'] == 'ready' else None)}
    _pack_put(pid, name=name, bbox=json.dumps(bbox), maxzoom=maxzoom, state='building', message='')

    def run():
        tmp, dest = DATA / f'packs/{pid}.pmtiles.tmp', DATA / f'packs/{pid}.pmtiles'
        try:
            pmtiles_extract(_pack_source(bbox), tmp, bbox=bbox, maxzoom=maxzoom)
            if tmp.stat().st_size > PACK_MAX_BYTES:
                raise RuntimeError('This area is too big for one download. Zoom in and try again.')
            os.replace(tmp, dest)
            _pack_put(pid, state='ready', size=dest.stat().st_size)
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            _pack_put(pid, state='failed', message=str(e)[-200:])
    threading.Thread(target=run, daemon=True).start()
    return {'pack': {'id': pid, 'name': name, 'bbox': bbox, 'maxzoom': maxzoom, 'state': 'building', 'url': None}}


@db
def _pack_delete(c, pid):
    c.execute('DELETE FROM packs WHERE id=?', (pid,))


def cleanup_packs(max_age=7 * 86400):
    for p in _pack_rows():
        if time.time() - (p['created'] or 0) > max_age:
            (DATA / f"packs/{p['id']}.pmtiles").unlink(missing_ok=True)
            _pack_delete(p['id'])


def disk_usage():
    total = 0
    for p in DATA.rglob('*'):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total
