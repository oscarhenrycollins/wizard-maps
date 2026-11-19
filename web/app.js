// Maps view: MapLibre over the owner's own server, with phone GPS and downloaded areas kept on the phone (OPFS).
import * as maplibregl from './dist/maplibre-gl.mjs';

const el = id => document.getElementById(id);
const pm = window.pmtiles, bm = window.basemaps;
const LANG = (navigator.language || 'en').slice(0, 2).toLowerCase();
const hosted = (() => { try { return window.parent !== window; } catch (e) { return true; } })();
const store = {
  get(k, d) { try { const v = localStorage.getItem('maps.' + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('maps.' + k, JSON.stringify(v)); } catch (e) {} },
};

// ---- state ------------------------------------------------------------------------------------------------------
const ui = {
  server: null,            // last /api/state
  places: store.get('places', []),
  lists: store.get('lists', [{ id: 'saved', name: 'Saved' }]),
  offline: store.get('offline', []),  // [{id,name,bbox,maxzoom,size,at,file}]
  selected: null,          // place shown in the panel
  dir: { from: null, to: null, mode: 'car', routes: [], pick: 0, editing: null },
  panel: null,             // 'place' | 'dir' | null
  me: null, watch: null, follow: false,
};

function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
  if (kind) { clearTimeout(say.t); say.t = setTimeout(() => { s.textContent = ''; s.classList.add('wz-sr'); }, 6000); }
}
async function api(path, data, opts = {}) {
  const init = data === undefined ? { signal: opts.signal } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data), signal: opts.signal };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || 'Your server could not do that. Try again.');
  return body;
}
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const km = d => d < 1 ? Math.round(d * 1000) + ' m' : (d < 10 ? d.toFixed(1) : Math.round(d)) + ' km';
const mins = m => m < 60 ? Math.round(m) + ' min' : Math.floor(m / 60) + ' h ' + Math.round(m % 60) + ' min';
const mb = b => b > 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b / 1e6)) + ' MB';
function dist(a, b) { const R = 6371, r = Math.PI / 180, dl = (b.lat - a.lat) * r, dn = (b.lon - a.lon) * r;
  const h = Math.sin(dl / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dn / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(h)); }
function row(main, sub, extra) {
  const b = document.createElement('button'); b.type = 'button'; b.className = 'wz-row';
  const m = document.createElement('span'); m.className = 'wz-main';
  const t = document.createElement('b'); t.textContent = main; m.append(t);
  if (sub) { const s = document.createElement('small'); s.textContent = sub; m.append(s); }
  b.append(m);
  if (extra) { const x = document.createElement('span'); x.className = 'tag'; x.textContent = extra; b.append(x); }
  const li = document.createElement('li'); li.append(b); return { li, b, m };
}

// ---- tiles: downloaded areas on the phone first, then the owner's server -------------------------------------------
const archives = { world: [], detail: [] };
const tileBox = (z, x, y) => { const n = 2 ** z, lon = v => v / n * 360 - 180,
  lat = v => Math.atan(Math.sinh(Math.PI * (1 - 2 * v / n))) * 180 / Math.PI; return [lon(x), lat(y + 1), lon(x + 1), lat(y)]; };
const inside = (t, b) => t[0] < b[2] && t[2] > b[0] && t[1] < b[3] && t[3] > b[1];
class BlobSource { constructor(key, blob) { this.key = key; this.blob = blob; } getKey() { return this.key; }
  async getBytes(o, l) { return { data: await this.blob.slice(o, o + l).arrayBuffer() }; } }
function setArchives(kind, list) { archives[kind] = list; }
maplibregl.addProtocol('wzt', async params => {
  const m = /^wzt:\/\/(\w+)\/(\d+)\/(\d+)\/(\d+)/.exec(params.url);
  if (!m) return { data: new Uint8Array() };
  const [kind, z, x, y] = [m[1], +m[2], +m[3], +m[4]], tb = tileBox(z, x, y);
  for (const a of archives[kind] || []) {
    if (z < a.minzoom || z > a.maxzoom || !inside(tb, a.bbox)) continue;
    try { const t = await a.pm.getZxy(z, x, y); if (t && t.data) return { data: t.data }; }
    catch (e) { /* server unreachable or archive gone: try the next one */ }
  }
  return { data: new Uint8Array() };
});
// Fonts and map symbols are kept in Cache Storage as they are used, so downloaded areas render offline too.
async function cachedFetch(url) {
  let cache = null;
  try { cache = await caches.open('maps-assets-v1'); const hit = await cache.match(url); if (hit) return hit.arrayBuffer(); } catch (e) {}
  const r = await fetch(url);
  if (!r.ok) throw new Error('asset ' + r.status);
  if (cache) cache.put(url, r.clone()).catch(() => {});
  return r.arrayBuffer();
}
maplibregl.addProtocol('wzg', async params => {
  const p = decodeURIComponent(params.url.slice(6)).split('/');
  const stack = p[0].split(',')[0], range = p[1];
  try { return { data: await cachedFetch(`api/assets/fonts/${encodeURIComponent(stack)}/${range}.pbf`) }; }
  catch (e) { return { data: new Uint8Array() }; }
});
maplibregl.addProtocol('wzs', async params => {
  const name = params.url.slice(6).replace(/\//g, '');  // 'wzs://light/@2x.json' -> 'light@2x.json'
  const data = await cachedFetch('api/assets/sprites/v4/' + name);
  if (name.endsWith('.json')) return { data: JSON.parse(new TextDecoder().decode(data)) };
  return { data };
});

// ---- map -----------------------------------------------------------------------------------------------------------
const flavorName = () => (document.documentElement.getAttribute('data-color-scheme') ||
  (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')) === 'light' ? 'light' : 'dark';
function buildStyle() {
  const flavor = flavorName();
  const f = bm.namedFlavor(flavor);
  const world = bm.layers('world', f, { lang: LANG }).map(l => {
    if (l.type === 'symbol') l.maxzoom = 8;  // the overview's labels never hide the detailed ones
    return l;
  });
  const detail = bm.layers('detail', f, { lang: LANG }).filter(l => l.type !== 'background').map(l => ({ ...l, id: 'd-' + l.id }));
  return {
    version: 8, glyphs: 'wzg://{fontstack}/{range}', sprite: new URL('wzs://' + flavor, location.href).href.replace(/\/$/, ''),
    sources: {
      world: { type: 'vector', tiles: ['wzt://world/{z}/{x}/{y}'], maxzoom: 6, attribution: '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>' },
      detail: { type: 'vector', tiles: ['wzt://detail/{z}/{x}/{y}'], minzoom: 5, maxzoom: 15 },
    },
    layers: [...world, ...detail],
  };
}
const view = store.get('view', { center: [100.5, 13.75], zoom: 2 });
const map = window.__map = new maplibregl.Map({
  container: 'map', style: buildStyle(), center: view.center, zoom: view.zoom, attributionControl: { compact: true },
  maxPitch: 60, dragRotate: true, cooperativeGestures: false,
});
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
map.on('moveend', () => { const c = map.getCenter(); store.set('view', { center: [c.lng, c.lat], zoom: map.getZoom() }); });
map.on('dragstart', () => setFollow(false));

function overlays() {
  const accent = css('--wz-accent') || 'royalblue', muted = css('--wz-text-muted') || 'gray', danger = css('--wz-danger') || 'crimson';
  if (!map.getSource('route')) map.addSource('route', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  if (!map.getSource('saved')) map.addSource('saved', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  if (!map.getSource('acc')) map.addSource('acc', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  const add = l => { if (!map.getLayer(l.id)) map.addLayer(l); };
  add({ id: 'acc', type: 'fill', source: 'acc', paint: { 'fill-color': accent, 'fill-opacity': 0.12 } });
  add({ id: 'route-alt', type: 'line', source: 'route', filter: ['==', ['get', 'alt'], true], layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': muted, 'line-width': 6, 'line-opacity': 0.8 } });
  add({ id: 'route-walk', type: 'line', source: 'route', filter: ['all', ['!=', ['get', 'alt'], true], ['==', ['get', 'walk'], true]],
    layout: { 'line-cap': 'round' }, paint: { 'line-color': accent, 'line-width': 4, 'line-dasharray': [0.1, 2] } });
  add({ id: 'route', type: 'line', source: 'route', filter: ['all', ['!=', ['get', 'alt'], true], ['!=', ['get', 'walk'], true]],
    layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['coalesce', ['get', 'color'], accent], 'line-width': 7 } });
  add({ id: 'saved', type: 'circle', source: 'saved', paint: { 'circle-radius': 6, 'circle-color': accent, 'circle-stroke-width': 2, 'circle-stroke-color': css('--wz-text') || 'white' } });
  add({ id: 'saved-label', type: 'symbol', source: 'saved', minzoom: 12, layout: { 'text-field': ['get', 'name'], 'text-font': ['Noto Sans Medium'], 'text-size': 12, 'text-offset': [0, 1.2], 'text-anchor': 'top' },
    paint: { 'text-color': accent, 'text-halo-color': css('--wz-base') || 'black', 'text-halo-width': 1.5 } });
  void danger;
  drawSaved(); drawRoute();
}
map.on('load', overlays);
map.on('style.load', overlays);
if (window.WizardApp && WizardApp.version === 1 && WizardApp.theme && WizardApp.theme.onChange) {
  WizardApp.theme.onChange(() => setTimeout(() => { if (map.__flavor !== flavorName()) { map.__flavor = flavorName(); map.setStyle(buildStyle()); } }, 0));
}
map.__flavor = flavorName();

// ---- archives: server regions + world, downloaded areas ---------------------------------------------------------------
const serverArchives = {};
function serverArchive(url, bbox, minzoom, maxzoom) {
  if (!serverArchives[url]) serverArchives[url] = { pm: new pm.PMTiles(new pm.FetchSource(new URL(url, location.href).href)), bbox, minzoom, maxzoom, key: url };
  return serverArchives[url];
}
let opfs = null;
async function opfsDir() { if (opfs) return opfs; try { opfs = await navigator.storage.getDirectory(); } catch (e) { opfs = false; } return opfs; }
async function readLocal(file) {
  const d = await opfsDir();
  if (d) { try { return await (await d.getFileHandle(file)).getFile(); } catch (e) {} }
  try { const c = await caches.open('maps-offline-v1'); const r = await c.match('offline/' + file); if (r) return await r.blob(); } catch (e) {}
  return null;
}
const localArchives = {};
async function refreshArchives() {
  const local = [];
  for (const o of ui.offline) {
    if (!localArchives[o.id]) { const blob = await readLocal(o.file); if (blob) localArchives[o.id] = new pm.PMTiles(new BlobSource(o.id, blob)); }
    if (localArchives[o.id]) local.push({ pm: localArchives[o.id], bbox: o.bbox, minzoom: 0, maxzoom: o.maxzoom, key: o.id });
  }
  const s = ui.server;
  const detail = [...local];
  for (const r of (s && s.regions) || []) if (r.tiles) detail.push(serverArchive(r.tiles, r.bbox, 0, 15));
  const world = [];
  if (!localArchives.world && store.get('worldOffline', false)) { const blob = await readLocal('world.pmtiles'); if (blob) localArchives.world = new pm.PMTiles(new BlobSource('world', blob)); }
  if (localArchives.world) world.push({ pm: localArchives.world, bbox: [-180, -85.1, 180, 85.1], minzoom: 0, maxzoom: 6 });
  if (s && s.world && s.world.tiles) world.push(serverArchive(s.world.tiles, [-180, -85.1, 180, 85.1], 0, 6));
  const before = JSON.stringify([archives.world.map(a => a.key || 'w'), archives.detail.map(a => a.key)]);
  setArchives('world', world); setArchives('detail', detail);
  if (before !== JSON.stringify([world.map(a => a.key || 'w'), detail.map(a => a.key)])) reloadTiles();
}
function reloadTiles() {
  for (const id of ['world', 'detail']) { const src = map.getSource(id); if (src && src.setTiles) src.setTiles([`wzt://${id}/{z}/{x}/{y}?v=${Date.now()}`]); }
}

// ---- server state -------------------------------------------------------------------------------------------------
let firstState = true;
async function loadState() {
  try {
    const s = await api('api/state');
    ui.server = s; ui.places = s.places; ui.lists = s.lists;
    store.set('places', s.places); store.set('lists', s.lists); store.set('regions', s.regions.map(r => ({ id: r.id, name: r.name, bbox: r.bbox })));
    drawSaved(); renderBanner();
    if (firstState && !store.get('view-set', false) && s.regions.length) {
      const b = s.regions[0].bbox; map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 40, duration: 0 }); store.set('view-set', true);
    }
    firstState = false;
    if (el('regions-sheet').open) renderRegions();
    clearTimeout(loadState.t);
    loadState.t = setTimeout(loadState, s.busy || s.world.state !== 'ready' ? 4000 : 60000);
  } catch (e) {
    ui.server = ui.server || null;
    renderBanner(true);
    clearTimeout(loadState.t); loadState.t = setTimeout(loadState, 15000);
  }
  refreshArchives();
}
function renderBanner(offline) {
  const b = el('banner'), s = ui.server;
  b.replaceChildren();
  let text = '';
  if (offline) text = ui.offline.length ? 'No connection to your server. Downloaded areas and saved places still work.' : 'No connection to your server.';
  else if (s && s.world.state !== 'ready') text = 'Your server is getting the world map ready…';
  else if (s && !s.regions.length) text = 'Add your country or region for detailed maps, search and directions. ';
  else if (s && s.busy) { const r = s.regions.find(x => x.state === 'building' || x.state === 'queued'); text = `Building ${r.name}: ${r.step || 'queued'} ${r.pct ? r.pct + '%' : ''}`; }
  if (!text) { b.hidden = true; return; }
  b.append(text);
  if (s && !offline && (!s.regions.length || s.busy)) {
    const a = document.createElement('button'); a.type = 'button'; a.textContent = s.regions.length ? 'Details' : 'Add a region'; a.onclick = openRegions; b.append(' ', a);
  }
  b.hidden = el('results').hidden === false;
}

// ---- saved places -------------------------------------------------------------------------------------------------
function drawSaved() {
  const src = map.getSource && map.getSource('saved');
  if (!src) return;
  src.setData({ type: 'FeatureCollection', features: ui.places.map(p => ({ type: 'Feature', properties: { id: p.id, name: p.name },
    geometry: { type: 'Point', coordinates: [p.lon, p.lat] } })) });
}
const savedOf = p => p && ui.places.find(x => (p.id && x.id === p.id) || (Math.abs(x.lat - p.lat) < 1e-5 && Math.abs(x.lon - p.lon) < 1e-5));
map.on('click', 'saved', e => { const f = e.features[0]; const p = ui.places.find(x => x.id === f.properties.id); if (p) showPlace(p); });

function openSaved() {
  const body = el('saved-body'); body.replaceChildren();
  if (!ui.places.length) { const p = document.createElement('p'); p.className = 'sub'; p.textContent = 'Nothing saved yet. Open a place and tap Save.'; body.append(p); }
  for (const l of ui.lists) {
    const items = ui.places.filter(p => p.list_id === l.id);
    if (!items.length) continue;
    const g = document.createElement('section'); g.className = 'list-group';
    const h = document.createElement('h3'); h.className = 'wz-sec-h'; h.textContent = `${l.name} · ${items.length}`;
    const ul = document.createElement('ul'); ul.className = 'wz-list';
    for (const p of items) {
      const { li, b } = row(p.name, p.address || p.note || `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`, ui.me ? km(dist(ui.me, p)) : '');
      b.onclick = () => { WzSheet.close(el('saved-sheet')); showPlace(p); };
      ul.append(li);
    }
    g.append(h, ul); body.append(g);
  }
  WzSheet.open(el('saved-sheet'));
}

function openListChooser(place) {
  const ul = el('list-choices'); ul.replaceChildren();
  for (const l of ui.lists) {
    const { li, b } = row(l.name, `${ui.places.filter(p => p.list_id === l.id).length} places`);
    b.onclick = () => { WzSheet.close(el('list-sheet')); savePlace(place, l.name); };
    ul.append(li);
  }
  el('new-list').onsubmit = ev => { ev.preventDefault(); const n = el('new-list-name').value.trim(); if (!n) return;
    el('new-list-name').value = ''; WzSheet.close(el('list-sheet')); savePlace(place, n); };
  WzSheet.open(el('list-sheet'));
}
async function savePlace(place, list) {
  // State first: the place shows as saved before the server answers.
  const id = (savedOf(place) || {}).id || (crypto.randomUUID ? crypto.randomUUID() : 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  const local = { id, name: place.name, lat: place.lat, lon: place.lon, address: place.address || '', note: '', list, list_id: (ui.lists.find(l => l.name === list) || {}).id || 'saved' };
  const before = ui.places;
  ui.places = [...ui.places.filter(p => p.id !== id), local];
  ui.selected = { ...place, id };
  renderPlace(); drawSaved(); say(`Saved to ${list}.`, 'note');
  const hold = window.WizardApp && WizardApp.update ? WizardApp.update.hold('Saving a place') : null;
  try {
    const r = await api('api/places', { id, name: place.name, lat: place.lat, lon: place.lon, address: place.address || '', list });
    ui.places = [...ui.places.filter(p => p.id !== id), r.place];
    store.set('places', ui.places);
    loadState();
  } catch (e) { ui.places = before; renderPlace(); drawSaved(); say(e.message, 'error'); }
  finally { if (hold) hold.release(); }
}
async function unsavePlace(p) {
  const before = ui.places;
  ui.places = ui.places.filter(x => x.id !== p.id); renderPlace(); drawSaved(); say('Removed from saved.', 'note');
  try { await api(`api/places/${p.id}/delete`, {}); store.set('places', ui.places); }
  catch (e) { ui.places = before; renderPlace(); drawSaved(); say(e.message, 'error'); }
}

// ---- search as you type ----------------------------------------------------------------------------------------------
let searchCtl = null;
function localMatches(q) {
  const s = q.toLowerCase();
  return ui.places.filter(p => (p.name + ' ' + (p.address || '') + ' ' + (p.note || '')).toLowerCase().includes(s)).slice(0, 4)
    .map(p => ({ ...p, saved: true }));
}
async function searchPlaces(q, list, onPick) {
  if (searchCtl) searchCtl.abort();
  list.replaceChildren();
  if (!q.trim()) { list.hidden = true; renderBanner(); return; }
  const show = (items, note) => {
    list.replaceChildren();
    for (const p of items) {
      const { li, b } = row(p.name, [p.kind, p.address].filter(Boolean).join(' · '), p.saved ? 'Saved' : (ui.me ? km(dist(ui.me, p)) : ''));
      b.onclick = () => onPick(p);
      list.append(li);
    }
    if (note) { const li = document.createElement('li'); li.className = 'sub'; li.style.padding = '8px'; li.textContent = note; list.append(li); }
    list.hidden = !list.children.length; el('banner').hidden = true;
  };
  const local = localMatches(q);
  show(local);
  searchCtl = new AbortController();
  const c = map.getCenter();
  const near = ui.me && ui.follow ? ui.me : { lat: c.lat, lon: c.lng };
  try {
    const r = await api(`api/search?q=${encodeURIComponent(q)}&lat=${near.lat.toFixed(5)}&lon=${near.lon.toFixed(5)}&lang=${LANG}&limit=8`, undefined, { signal: searchCtl.signal });
    const seen = new Set(local.map(p => p.id));
    show([...local, ...r.results.filter(x => !seen.has(x.id))], r.results.length || local.length ? '' : 'No places found.');
  } catch (e) {
    if (e.name === 'AbortError') return;
    show(local, local.length ? '' : e.message.includes('not ready') ? e.message : 'Search needs your server. Only saved places can be searched offline.');
  }
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const runSearch = debounce(() => searchPlaces(el('q').value, el('results'), p => { el('results').hidden = true; el('q').blur(); showPlace(p, true); }), 180);
el('q').addEventListener('input', () => { el('q-clear').hidden = !el('q').value; runSearch(); });
el('q').addEventListener('focus', () => { if (el('q').value) runSearch(); });
el('search').onsubmit = ev => { ev.preventDefault(); const first = el('results').querySelector('button'); if (first) first.click(); };
el('q-clear').onclick = () => { el('q').value = ''; el('q-clear').hidden = true; el('results').hidden = true; renderBanner(); el('q').focus(); };

// ---- place panel ------------------------------------------------------------------------------------------------------
let pin = null;
function setPin(p) {
  if (pin) { pin.remove(); pin = null; }
  if (!p || savedOf(p)) return;
  const d = document.createElement('div'); d.className = 'pin';
  pin = new maplibregl.Marker({ element: d, anchor: 'bottom', offset: [0, -4] }).setLngLat([p.lon, p.lat]).addTo(map);
}
function openPanel(which) {
  ui.panel = which;
  el('panel').hidden = !which;
  el('place').hidden = which !== 'place';
  el('dir').hidden = which !== 'dir';
}
function showPlace(p, fly = true) {
  ui.selected = { ...p };
  openPanel('place'); renderPlace(); setPin(p);
  if (fly) map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), p.kind === 'city' || p.kind === 'country' ? 11 : 16), speed: 1.6 });
}
function renderPlace() {
  const p = ui.selected; if (!p) return;
  const saved = savedOf(p);
  el('place-name').textContent = p.name;
  el('place-sub').textContent = [p.kind, p.address].filter(Boolean).join(' · ') || `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`;
  const ex = p.extra || {};
  const lines = [];
  if (ui.me) lines.push(km(dist(ui.me, p)) + ' away');
  if (ex.opening_hours) lines.push('Hours: ' + ex.opening_hours);
  if (ex.phone) lines.push('Phone: ' + ex.phone);
  if (ex.website) lines.push(ex.website);
  if (ex.cuisine) lines.push('Cuisine: ' + ex.cuisine.replace(/;/g, ', '));
  if (saved) lines.push('Saved in ' + (saved.list || (ui.lists.find(l => l.id === saved.list_id) || {}).name || 'Saved') + (saved.note ? '\n' + saved.note : ''));
  el('place-extra').textContent = lines.join('\n');
  el('place-save').textContent = saved ? 'Saved' : 'Save';
  el('place-save').setAttribute('aria-pressed', String(!!saved));
}
el('place-save').onclick = () => { const s = savedOf(ui.selected); if (s) unsavePlace(s); else openListChooser(ui.selected); };
el('place-share').onclick = async () => {
  const p = ui.selected; if (!p) return;
  const url = `https://www.openstreetmap.org/?mlat=${p.lat.toFixed(6)}&mlon=${p.lon.toFixed(6)}#map=17/${p.lat.toFixed(6)}/${p.lon.toFixed(6)}`;
  const text = `${p.name}${p.address ? ', ' + p.address : ''}\n${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}\ngeo:${p.lat.toFixed(6)},${p.lon.toFixed(6)}`;
  if (!window.WizardApp || !WizardApp.share) { say('Sharing needs Wizard.', 'error'); return; }
  const out = await WizardApp.share.text({ title: p.name, text, url });
  if (out === 'copied') say('Location copied.', 'note');
  else if (out === 'unsupported') say('Sharing is not available here.', 'error');
};
el('place-go').onclick = () => startDirections(null, ui.selected);
for (const x of document.querySelectorAll('[data-close]')) x.onclick = () => closePanel();
function closePanel() {
  if (ui.panel === 'dir') { ui.dir.routes = []; drawRoute(); }
  openPanel(null); setPin(null);
}

// Tap a map label for its details; long-press (or right-click) anywhere for what's there.
map.on('click', e => {
  if (map.queryRenderedFeatures(e.point, { layers: ['saved'] }).length) return;
  const f = map.queryRenderedFeatures(e.point).find(x => /pois$|places$/.test(x.layer.id) && x.properties && x.properties.name);
  if (!f) return;
  const [lon, lat] = f.geometry.type === 'Point' ? f.geometry.coordinates : [e.lngLat.lng, e.lngLat.lat];
  const p = { name: f.properties['name:' + LANG] || f.properties.name, lat, lon, kind: (f.properties.kind || '').replace(/_/g, ' '), address: '' };
  showPlace(p, false);
  api(`api/reverse?lat=${lat}&lon=${lon}&lang=${LANG}`).then(r => {
    if (ui.selected && ui.selected.lat === lat) { ui.selected.address = r.place.address; ui.selected.extra = r.place.name === p.name ? r.place.extra : {}; renderPlace(); }
  }).catch(() => {});
});
async function dropPin(lngLat) {
  const p = { name: 'Dropped pin', lat: +lngLat.lat.toFixed(6), lon: +lngLat.lng.toFixed(6), kind: '', address: '' };
  showPlace(p, false);
  try { const r = await api(`api/reverse?lat=${p.lat}&lon=${p.lon}&lang=${LANG}`);
    if (ui.selected && ui.selected.lat === p.lat) { ui.selected.address = [r.place.name !== 'Dropped pin' ? r.place.name : '', r.place.address].filter(Boolean).join(', '); renderPlace(); } }
  catch (e) {}
}
map.on('contextmenu', e => dropPin(e.lngLat));
let press = null;
map.on('touchstart', e => { if (e.originalEvent.touches.length !== 1) return; const at = e.lngLat; press = setTimeout(() => dropPin(at), 600); });
for (const ev of ['touchend', 'touchmove', 'touchcancel', 'movestart']) map.on(ev, () => clearTimeout(press));

// ---- directions ---------------------------------------------------------------------------------------------------------
const label = p => p ? (p.mine ? 'Your location' : p.name) : '';
function startDirections(from, to) {
  ui.dir.from = from || (ui.me ? { ...ui.me, name: 'Your location', mine: true } : null);
  ui.dir.to = to || null;
  el('from').value = label(ui.dir.from); el('to').value = label(ui.dir.to);
  openPanel('dir'); setPin(null);
  renderModes();
  if (ui.dir.from && ui.dir.to) getRoutes(); else { el('routes').replaceChildren(); (ui.dir.from ? el('to') : el('from')).focus(); }
  if (!ui.me && !ui.watch) startGps(false);
}
function renderModes() { for (const b of el('modes').querySelectorAll('[data-mode]')) b.setAttribute('aria-pressed', String(b.dataset.mode === ui.dir.mode)); }
el('modes').onclick = e => {
  const b = e.target.closest('[data-mode]'); if (!b) return;
  ui.dir.mode = b.dataset.mode; renderModes();   // state first, then the request
  getRoutes();
};
el('swap').onclick = () => { [ui.dir.from, ui.dir.to] = [ui.dir.to, ui.dir.from]; el('from').value = label(ui.dir.from); el('to').value = label(ui.dir.to); getRoutes(); };
for (const which of ['from', 'to']) {
  const input = el(which);
  const run = debounce(() => {
    ui.dir.editing = which;
    const q = input.value;
    searchPlaces(q, el('end-results'), p => {
      ui.dir[which] = p; input.value = label(p); el('end-results').hidden = true; getRoutes();
    }).then(() => {
      if (ui.me && 'your location'.startsWith(q.trim().toLowerCase())) {
        const { li, b } = row('Your location', 'Phone GPS'); b.onclick = () => { ui.dir[which] = { ...ui.me, name: 'Your location', mine: true }; input.value = 'Your location'; el('end-results').hidden = true; getRoutes(); };
        el('end-results').prepend(li); el('end-results').hidden = false;
      }
    });
  }, 180);
  input.addEventListener('input', run);
  input.addEventListener('focus', () => input.select());
}
let routeSeq = 0;
async function getRoutes() {
  const { from, to, mode } = ui.dir;
  el('end-results').hidden = true;
  if (!from || !to) return;
  const seq = ++routeSeq;
  el('routes').replaceChildren(); el('route-note').textContent = 'Finding routes…'; el('steps-box').hidden = true;
  ui.dir.routes = []; drawRoute();
  try {
    const body = { from: { lat: from.lat, lon: from.lon, name: from.name }, to: { lat: to.lat, lon: to.lon, name: to.name }, mode, lang: LANG };
    if (mode === 'transit') body.depart_at = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const r = await api('api/route', body);
    if (seq !== routeSeq) return;
    ui.dir.routes = r.routes; ui.dir.pick = 0; ui.dir.result = r;
    el('route-note').textContent = r.note || '';
    renderRoutes(); drawRoute(true);
  } catch (e) {
    if (seq !== routeSeq) return;
    el('route-note').textContent = e.message;
  }
}
function renderRoutes() {
  const ul = el('routes'); ul.replaceChildren();
  ui.dir.routes.forEach((r, i) => {
    const main = mins(r.duration_min);
    let sub = r.distance_km != null ? km(r.distance_km) : '';
    const { li, b, m } = row(main, sub, i === 0 ? 'Best' : '');
    if (r.legs) {
      const t = d => d ? new Date(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      m.querySelector('small') ? m.querySelector('small').remove() : null;
      const s = document.createElement('small'); s.textContent = `${t(r.start)} – ${t(r.end)} · ${r.transfers} transfer${r.transfers === 1 ? '' : 's'}`; m.append(s);
      const legs = document.createElement('span'); legs.className = 'legs';
      for (const l of r.legs) {
        const g = document.createElement('span'); g.className = 'leg';
        if (l.color) g.style.setProperty('--leg', l.color);
        g.textContent = l.mode === 'WALK' ? `Walk ${Math.round(l.duration_min)}′` : (l.line || l.mode.toLowerCase());
        legs.append(g);
      }
      m.append(legs);
    }
    b.setAttribute('aria-pressed', String(i === ui.dir.pick));
    b.onclick = () => { ui.dir.pick = i; renderRoutes(); drawRoute(); };
    ul.append(li);
  });
  const r = ui.dir.routes[ui.dir.pick];
  const steps = el('steps'); steps.replaceChildren();
  if (r) {
    for (const s of r.steps || []) { const li = document.createElement('li'); li.textContent = s.instruction; if (s.distance_km) { const k = document.createElement('small'); k.textContent = ' · ' + km(s.distance_km); li.append(k); } steps.append(li); }
    for (const l of r.legs || []) { const li = document.createElement('li');
      li.textContent = l.mode === 'WALK' ? `Walk to ${l.to} (${Math.round(l.duration_min)} min)` : `${l.line || l.mode} towards ${l.headsign || l.to}: ${l.from} → ${l.to} (${Math.round(l.duration_min)} min)`; steps.append(li); }
  }
  el('steps-box').hidden = !steps.children.length;
}
function drawRoute(fit) {
  const src = map.getSource && map.getSource('route');
  if (!src) return;
  const feats = [];
  ui.dir.routes.forEach((r, i) => {
    if (i === ui.dir.pick) return;
    feats.push({ type: 'Feature', properties: { alt: true }, geometry: r.geometry });
  });
  const r = ui.dir.routes[ui.dir.pick];
  if (r) {
    if (r.legs) for (const l of r.legs) feats.push({ type: 'Feature', properties: { walk: l.mode === 'WALK', color: l.color || null }, geometry: l.geometry });
    else feats.push({ type: 'Feature', properties: {}, geometry: r.geometry });
  }
  src.setData({ type: 'FeatureCollection', features: feats.filter(f => f.geometry && f.geometry.coordinates.length > 1) });
  if (fit && r && r.geometry.coordinates.length) {
    const b = new maplibregl.LngLatBounds();
    for (const c of r.geometry.coordinates) b.extend(c);
    const wide = map.getContainer().clientWidth >= 760;
    map.fitBounds(b, { padding: wide ? { top: 80, bottom: 90, left: 430, right: 40 } : { top: 80, bottom: Math.min(360, map.getContainer().clientHeight * 0.55), left: 30, right: 30 }, maxZoom: 16 });
  }
}
el('open-dir').onclick = () => startDirections(null, ui.panel === 'place' ? ui.selected : null);

// ---- GPS: the phone's own location, follow-me ---------------------------------------------------------------------------
let meMarker = null;
function setFollow(on) { ui.follow = on; el('locate').setAttribute('aria-pressed', String(!!ui.watch && on)); }
function circle(lat, lon, m) {
  const pts = [], R = 6378137;
  for (let i = 0; i <= 48; i++) { const a = i / 48 * 2 * Math.PI; pts.push([lon + (m * Math.cos(a) / (R * Math.cos(lat * Math.PI / 180))) * 180 / Math.PI, lat + (m * Math.sin(a) / R) * 180 / Math.PI]); }
  return { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [pts] } };
}
function onPos(pos) {
  const { latitude: lat, longitude: lon, accuracy, heading, speed } = pos.coords;
  const first = !ui.me;
  ui.me = { lat, lon, accuracy, heading };
  if (!meMarker) {
    const w = document.createElement('div'); w.className = 'me-wrap'; w.innerHTML = '<span class="cone" hidden></span><span class="me"></span>';
    meMarker = new maplibregl.Marker({ element: w, rotationAlignment: 'map' }).setLngLat([lon, lat]).addTo(map);
  }
  meMarker.setLngLat([lon, lat]);
  const cone = meMarker.getElement().querySelector('.cone');
  cone.hidden = heading == null || isNaN(heading);
  if (!cone.hidden) meMarker.setRotation(heading);
  const acc = map.getSource('acc'); if (acc) acc.setData({ type: 'FeatureCollection', features: accuracy < 2000 ? [circle(lat, lon, accuracy)] : [] });
  if (ui.follow) {
    const opts = { center: [lon, lat], duration: first ? 0 : 600 };
    if (first) opts.zoom = Math.max(map.getZoom(), 15);
    if (speed > 2 && heading != null && !isNaN(heading)) opts.bearing = heading;
    map.easeTo(opts);
  }
  if (ui.dir.from && ui.dir.from.mine) ui.dir.from = { ...ui.dir.from, lat, lon };
  if (first && ui.panel === 'dir' && !ui.dir.from) { ui.dir.from = { ...ui.me, name: 'Your location', mine: true }; el('from').value = 'Your location'; getRoutes(); }
}
function onGpsError(err) {
  if (err.code === 1) {
    stopGps();
    const b = el('banner'); b.replaceChildren();
    if (hosted) {
      b.append('Wizard is not sharing your location with apps yet. ');
      const a = document.createElement('a'); a.href = location.href; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Open Maps on its own';
      b.append(a, ' to use GPS.');
    } else b.append('Location is blocked. Allow it for this site in your browser settings.');
    b.hidden = false;
  } else say(err.code === 3 ? 'Still looking for GPS…' : 'Location is unavailable right now.', 'note');
}
function startGps(follow = true) {
  if (!('geolocation' in navigator)) { say('This device has no location.', 'error'); return; }
  setFollow(follow);
  if (!ui.watch) ui.watch = navigator.geolocation.watchPosition(onPos, onGpsError, { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
  setFollow(follow);
  if (follow && ui.me) map.easeTo({ center: [ui.me.lon, ui.me.lat], zoom: Math.max(map.getZoom(), 15) });
}
function stopGps() { if (ui.watch != null) navigator.geolocation.clearWatch(ui.watch); ui.watch = null; setFollow(false); }
el('locate').onclick = () => {
  if (ui.watch && ui.follow) { setFollow(false); map.easeTo({ bearing: 0, pitch: 0 }); return; }
  startGps(true);
};

// ---- regions on the server ------------------------------------------------------------------------------------------------
function openRegions() { renderRegions(); WzSheet.open(el('regions-sheet')); loadState(); }
el('open-regions').onclick = openRegions;
function pill(state) {
  const p = document.createElement('span'); p.className = 'wz-pill ' + ({ ready: 'ok', failed: 'error', building: 'warn', queued: 'warn' }[state] || '');
  p.textContent = { ready: 'Ready', failed: 'Failed', building: 'Building', queued: 'Queued', removing: 'Removing' }[state] || state; return p;
}
function renderRegions() {
  const ul = el('region-list'); ul.replaceChildren();
  const s = ui.server;
  if (!s) { const li = document.createElement('li'); li.className = 'sub'; li.textContent = 'Your server is not reachable.'; ul.append(li); return; }
  const w = document.createElement('li'); w.className = 'wz-row region';
  const wm = document.createElement('span'); wm.className = 'wz-main';
  const wb = document.createElement('b'); wb.textContent = 'World overview';
  const ws = document.createElement('small'); ws.textContent = s.world.state === 'ready' ? `Whole world, less detail · ${mb(s.world.tiles_bytes)}` : (s.world.message || 'Preparing…');
  wm.append(wb, ws); w.append(wm, pill(s.world.state)); ul.append(w);
  for (const r of s.regions) {
    const li = document.createElement('li'); li.className = 'wz-row region';
    const m = document.createElement('span'); m.className = 'wz-main';
    const b = document.createElement('b'); b.textContent = r.name;
    const sm = document.createElement('small');
    if (r.state === 'ready') {
      const parts = [r.tiles ? 'Map' : '', r.search ? 'Search' : '', r.routing ? 'Directions' : '', r.transit.length ? `Transit (${r.transit.length} feed${r.transit.length > 1 ? 's' : ''})` : ''].filter(Boolean);
      sm.textContent = parts.join(' · ') + (r.message ? ' — ' + r.message : '');
    } else sm.textContent = r.state === 'failed' ? r.message : `${r.message || r.step || 'Waiting'}`;
    m.append(b, sm);
    if (r.state === 'building' || r.state === 'queued') {
      const meter = document.createElement('span'); meter.className = 'wz-meter';
      const i = document.createElement('i'); const bar = document.createElement('b'); bar.style.setProperty('--v', (r.pct || 0) + '%'); i.append(bar); meter.append(i); m.append(meter);
    }
    const acts = document.createElement('span'); acts.className = 'row-actions';
    if (r.state === 'ready' || r.state === 'failed') {
      const re = document.createElement('button'); re.type = 'button'; re.className = 'wz-button secondary'; re.textContent = r.state === 'failed' ? 'Retry' : 'Update';
      re.onclick = () => regionAction(r, 'refresh');
      acts.append(re);
    }
    const rm = document.createElement('button'); rm.type = 'button'; rm.className = 'wz-button secondary'; rm.textContent = 'Remove';
    rm.onclick = () => { if (rm.dataset.armed) regionAction(r, 'remove'); else { rm.dataset.armed = '1'; rm.textContent = 'Tap to confirm'; rm.classList.add('destructive'); } };
    acts.append(rm);
    li.append(m, pill(r.state), acts);
    ul.append(li);
  }
}
async function regionAction(r, what) {
  const before = ui.server.regions;
  if (what === 'remove') ui.server.regions = before.filter(x => x.id !== r.id);
  else ui.server.regions = before.map(x => x.id === r.id ? { ...x, state: 'queued', step: '', pct: 0, message: 'Queued' } : x);
  ui.server.busy = true; renderRegions();
  try { await api(`api/regions/${r.id}/${what}`, {}); say(what === 'remove' ? `${r.name} removed.` : `${r.name} will be rebuilt.`, 'note'); loadState(); }
  catch (e) { ui.server.regions = before; renderRegions(); say(e.message, 'error'); }
}
async function addRegion(c) {
  if (!ui.server) return;
  if (ui.server.regions.some(r => r.id === c.id)) { say(`${c.name} is already on your server.`, 'note'); return; }
  ui.server.regions = [...ui.server.regions, { id: c.id, name: c.name, state: 'queued', step: '', pct: 0, message: 'Queued', bbox: c.bbox, transit: [] }];
  ui.server.busy = true; renderRegions(); el('region-results').replaceChildren(); el('region-q').value = '';
  try { await api('api/regions', { id: c.id }); say(`${c.name} is being added. This can take a while; you can close Maps.`, 'note'); loadState(); }
  catch (e) { ui.server.regions = ui.server.regions.filter(r => r.id !== c.id); renderRegions(); say(e.message, 'error'); }
}
async function regionChoices(query) {
  const ul = el('region-results'); ul.replaceChildren();
  try {
    const r = await api('api/regions/catalog?' + query);
    for (const c of r.regions.slice(0, 12)) {
      const { li, b } = row(c.name, c.in, 'Add'); b.onclick = () => addRegion(c); ul.append(li);
    }
    if (!r.regions.length) { const li = document.createElement('li'); li.className = 'sub'; li.textContent = 'No region found.'; ul.append(li); }
  } catch (e) { say(e.message, 'error'); }
}
el('region-q').addEventListener('input', debounce(() => { const q = el('region-q').value.trim(); if (q.length >= 2) regionChoices('q=' + encodeURIComponent(q)); else el('region-results').replaceChildren(); }, 250));
el('region-form').onsubmit = e => e.preventDefault();
el('region-here').onclick = () => { const c = ui.me || { lat: map.getCenter().lat, lon: map.getCenter().lng }; regionChoices(`lat=${c.lat}&lon=${c.lon}`); };

// ---- offline areas on the phone -------------------------------------------------------------------------------------------
el('open-offline').onclick = openOffline;
let areaReq = 0;
async function openOffline() {
  renderOffline();
  WzSheet.open(el('offline-sheet'));
  const b = map.getBounds(), bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => +v.toFixed(4));
  bbox[0] = Math.max(-180, bbox[0]); bbox[2] = Math.min(180, bbox[2]); bbox[1] = Math.max(-85, bbox[1]); bbox[3] = Math.min(85, bbox[3]);
  el('area-get').disabled = true; el('area-size').textContent = 'Checking size…';
  const c = map.getCenter();
  el('area-name').textContent = 'This area';
  api(`api/reverse?lat=${c.lat}&lon=${c.lng}`).then(r => { const a = r.place.address.split(', '); el('area-name').textContent = (a.length > 2 ? a[a.length - 3] : r.place.name) || 'This area'; }).catch(() => {});
  const seq = ++areaReq;
  if (map.getZoom() < 8) { el('area-size').textContent = 'Zoom in to a city or province first.'; return; }
  try {
    const e = await api('api/packs/estimate?bbox=' + bbox.join(',') + '&maxzoom=15');
    if (seq !== areaReq) return;
    el('area-size').textContent = `About ${mb(e.bytes)} · streets and places, zoom up to 15`;
    el('area-get').disabled = false;
    el('area-get').onclick = () => downloadArea(el('area-name').textContent, bbox);
  } catch (err) { el('area-size').textContent = err.message; }
}
function renderOffline() {
  const ul = el('offline-list'); ul.replaceChildren();
  for (const o of ui.offline) {
    const li = document.createElement('li'); li.className = 'wz-row region';
    const m = document.createElement('span'); m.className = 'wz-main';
    const b = document.createElement('b'); b.textContent = o.name;
    const s = document.createElement('small'); s.textContent = o.state === 'ready' ? `${mb(o.size)} · saved ${new Date(o.at).toLocaleDateString()}` : (o.message || 'Downloading…');
    m.append(b, s);
    if (o.state !== 'ready') { const meter = document.createElement('span'); meter.className = 'wz-meter'; const i = document.createElement('i'); const bar = document.createElement('b'); bar.style.setProperty('--v', (o.pct || 0) + '%'); i.append(bar); meter.append(i); m.append(meter); }
    const go = document.createElement('button'); go.type = 'button'; go.className = 'wz-button secondary'; go.textContent = 'Show';
    go.onclick = () => { WzSheet.close(el('offline-sheet')); map.fitBounds([[o.bbox[0], o.bbox[1]], [o.bbox[2], o.bbox[3]]], { padding: 20 }); };
    const rm = document.createElement('button'); rm.type = 'button'; rm.className = 'wz-button secondary'; rm.textContent = 'Delete';
    rm.onclick = () => deleteArea(o);
    const acts = document.createElement('span'); acts.className = 'row-actions'; acts.append(go, rm);
    li.append(m, acts); ul.append(li);
  }
  if (navigator.storage && navigator.storage.estimate) navigator.storage.estimate().then(e => {
    el('storage-note').textContent = `This phone: ${mb(e.usage || 0)} used for Maps and Wizard of ${mb(e.quota || 0)} available.` +
      (ui.offline.length ? '' : ' Search and directions still need your server for now.');
  });
}
const saveOffline = () => store.set('offline', ui.offline.filter(o => o.state === 'ready'));
async function writeLocal(file, response, onBytes) {
  const d = await opfsDir();
  if (d && 'createWritable' in FileSystemFileHandle.prototype) {
    const h = await d.getFileHandle(file, { create: true });
    const w = await h.createWritable();
    const reader = response.body.getReader(); let done = 0;
    try { for (;;) { const { value, done: end } = await reader.read(); if (end) break; await w.write(value); done += value.length; onBytes(done); } await w.close(); }
    catch (e) { await w.abort().catch(() => {}); throw e; }
    return;
  }
  const c = await caches.open('maps-offline-v1');   // browsers without OPFS writes: Cache Storage
  await c.put('offline/' + file, response);
}
async function downloadArea(name, bbox) {
  const id = 'a' + Date.now().toString(36);
  const item = { id, name, bbox, maxzoom: 15, state: 'building', pct: 0, message: 'Your server is cutting the map…', file: `area-${id}.pmtiles`, at: Date.now() };
  ui.offline = [item, ...ui.offline]; renderOffline();    // state first
  el('area-get').disabled = true;
  const hold = window.WizardApp && WizardApp.update ? WizardApp.update.hold('Downloading an offline map') : null;
  try {
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
    let pack = (await api('api/packs', { id: 'pack-' + id, bbox, maxzoom: 15, name })).pack;
    while (pack.state === 'building') { await new Promise(r => setTimeout(r, 1500)); pack = (await api('api/packs')).packs.find(p => p.id === pack.id) || { state: 'failed', message: 'The server lost the download.' }; }
    if (pack.state !== 'ready') throw new Error(pack.message || 'The server could not cut this area.');
    item.message = 'Downloading to this phone…'; renderOffline();
    const resp = await fetch(pack.url);
    if (!resp.ok) throw new Error('Download failed.');
    await writeLocal(item.file, resp, n => { item.pct = Math.round(100 * n / (pack.size || 1)); item.message = `Downloading ${mb(n)} of ${mb(pack.size)}`; if (n % (4 << 20) < 70000) renderOffline(); });
    item.state = 'ready'; item.size = pack.size; item.pct = 100;
    if (!store.get('worldOffline', false) && ui.server && ui.server.world.tiles) {   // the world overview too, once
      item.message = 'Adding the world overview…'; item.state = 'building'; renderOffline();
      const w = await fetch(ui.server.world.tiles);
      if (w.ok) { await writeLocal('world.pmtiles', w, () => {}); store.set('worldOffline', true); }
      item.state = 'ready';
    }
    await prefetchAssets();
    saveOffline(); delete localArchives[id]; await refreshArchives(); renderOffline();
    say(`${name} is saved on this phone.`, 'note');
  } catch (e) {
    ui.offline = ui.offline.filter(o => o.id !== id); renderOffline(); say(e.message, 'error');
  } finally { if (hold) hold.release(); el('area-get').disabled = false; }
}
async function prefetchAssets() {
  const flavor = flavorName(), urls = [];
  for (const f of ['Noto Sans Regular', 'Noto Sans Medium', 'Noto Sans Italic'])
    for (const r of ['0-255', '256-511', '512-767', '768-1023', '3584-3839', '8192-8447', '8448-8703'])
      urls.push(`api/assets/fonts/${encodeURIComponent(f)}/${r}.pbf`);
  for (const s of [flavor, flavor + '@2x']) urls.push(`api/assets/sprites/v4/${s}.json`, `api/assets/sprites/v4/${s}.png`);
  await Promise.all(urls.map(u => cachedFetch(u).catch(() => {})));
}
async function deleteArea(o) {
  ui.offline = ui.offline.filter(x => x.id !== o.id); saveOffline(); renderOffline();
  delete localArchives[o.id];
  const d = await opfsDir(); if (d) d.removeEntry(o.file).catch(() => {});
  caches.open('maps-offline-v1').then(c => c.delete('offline/' + o.file)).catch(() => {});
  if (!ui.offline.length && store.get('worldOffline', false)) { store.set('worldOffline', false); delete localArchives.world; if (d) d.removeEntry('world.pmtiles').catch(() => {}); }
  refreshArchives();
}

// ---- credits -----------------------------------------------------------------------------------------------------------
map.on('load', () => {
  const ctl = map.getContainer().querySelector('.maplibregl-ctrl-attrib-inner');
  if (ctl) { const a = document.createElement('a'); a.href = '#'; a.textContent = ' · Data & licences'; a.onclick = e => { e.preventDefault(); openCredits(); }; ctl.append(a); }
});
function openCredits() {
  const ul = el('credits'); ul.replaceChildren();
  const add = (text, href) => { const li = document.createElement('li'); if (href) { const a = document.createElement('a'); a.href = href; a.target = '_blank'; a.rel = 'noopener'; a.textContent = text; li.append(a); } else li.textContent = text; ul.append(li); };
  add('Map data © OpenStreetMap contributors, Open Database License (ODbL)', 'https://www.openstreetmap.org/copyright');
  add('Basemap tiles and style: Protomaps (BSD-3; data ODbL)', 'https://protomaps.com');
  add('Search: Photon with GraphHopper data dumps (Apache-2.0; data ODbL)', 'https://github.com/komoot/photon');
  add('Directions: Valhalla (MIT)', 'https://github.com/valhalla/valhalla');
  add('Transit: MOTIS (MIT); feeds found through the Mobility Database', 'https://mobilitydatabase.org');
  add('Map renderer: MapLibre GL JS (BSD-3)', 'https://maplibre.org');
  for (const r of (ui.server && ui.server.regions) || []) for (const f of r.transit || []) add(`Transit timetables (${r.name}): ${f.provider}${f.license ? ' — ' + f.license : ''}`);
  add('Fonts: Noto Sans (SIL Open Font License)');
  WzSheet.open(el('about-sheet'));
}

// ---- Back: close search results, then the panel. Sheets register themselves. ------------------------------------------------
if (!window.WizardApp || WizardApp.version !== 1) {
  say('Wizard system functions are unavailable. Open Maps through Wizard.', 'error');
} else {
  WizardApp.ready({ name: 'maps', version: '0.1.0' });
  WizardApp.back.register({
    canGoBack: () => !el('results').hidden || !el('end-results').hidden || !!ui.panel,
    back: () => {
      if (!el('results').hidden) { el('results').hidden = true; el('q').blur(); renderBanner(); return true; }
      if (!el('end-results').hidden) { el('end-results').hidden = true; return true; }
      if (ui.panel) { closePanel(); return true; }
      return false;
    },
  });
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadState(); });
window.addEventListener('online', loadState);
refreshArchives().then(loadState);
