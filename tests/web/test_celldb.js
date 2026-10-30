// node tests/web/test_celldb.js  -- the browser "cell DB (offline)" lookup from web/index.html against web/cells/*.csv.gz
// Extracts the code between "// ---- offline cell DB" and "// ---- end offline cell DB" (plus haversine), serves
// cells/<mcc>.csv.gz from disk through a fake fetch, and compares with the Python result (rfscan/cellpos.py).
const fs = require('fs'), path = require('path'), zlib = require('zlib'), assert = require('assert');
const html = fs.readFileSync('web/index.html', 'utf8');
const hav = html.match(/function haversine\(a, b\) \{.*\}\n/)[0];
const block = html.slice(html.indexOf('// ---- offline cell DB'), html.indexOf('// ---- end offline cell DB'));
let fetched = [];
const fakeFetch = async url => {                       // serve from a dir; synthetic file for MCC 999; 404 otherwise
  fetched.push(url);
  if (url === 'cells/999.csv.gz') return new Response(zlib.gzipSync('radio,net,area,cell,lon,lat,range,samples\nGSM,1,10,5,4.4,51.7,0,1\nGSM,1,10,6,4.5,51.7,0,1\n'));
  if (url === 'cells/998.csv.gz') return new Response('radio,net,area,cell,lon,lat,range,samples\nGSM,1,10,5,4.4,51.7,0,1\n');   // server undid gzip
  const p = path.join('web', url); return fs.existsSync(p) ? new Response(fs.readFileSync(p)) : new Response('', { status: 404 });
};
const api = new Function('fetch', hav + block + '; return { cellDbLookup, cellDbEstimate, CELLDB, haversine };')(fakeFetch);

(async () => {
  // synthetic: exact cell, LAC fallback, unknown LAC, missing file, plain-text file, cache
  let { towers, missing } = await api.cellDbLookup([{ mcc: 999, mnc: 1, lac: 10, ci: 5, power_db: -30 }, { mcc: 999, mnc: 1, lac: 10, ci: 77, power_db: -20 },
    { mcc: 999, mnc: 2, lac: 1, ci: 1 }, { mcc: 997, mnc: 1, lac: 1, ci: 1 }, { mcc: 998, mnc: 1, lac: 10, ci: 5 }]);
  assert.strictEqual(towers.length, 3); assert.strictEqual(missing.length, 2);
  assert.deepStrictEqual(towers.map(t => t.how), ['cell', 'lac', 'cell']);
  assert(Math.abs(towers[1].lon - 4.45) < 1e-9 && Math.abs(towers[1].range - 3460) < 20, towers[1].range);   // half of the 6.9 km tower spacing
  let e = api.cellDbEstimate(towers.slice(0, 2));
  assert(e.lat === 51.7 && e.lon === 4.4 && e.n === 1 && /not used/.test(e.note) && e.acc === 3000, JSON.stringify(e));
  await api.cellDbLookup([{ mcc: 999, mnc: 1, lac: 10, ci: 5 }]);
  assert.strictEqual(fetched.filter(u => u === 'cells/999.csv.gz').length, 1, 'cached');
  assert.strictEqual(api.CELLDB.get(997), null);

  // real data: cells of the 2026-10-30 gsm_937 capture (same as `python -m rfscan.cellpos` on its rfscan.sqlite)
  if (!fs.existsSync('web/cells/204.csv.gz')) { console.log('synthetic ok; web/cells/204.csv.gz absent, real-data check skipped'); return; }
  ({ towers, missing } = await api.cellDbLookup([
    { mcc: 204, mnc: 4, lac: 203, ci: 21752, power_db: -44.7 }, { mcc: 204, mnc: 8, lac: 3350, ci: 48694, power_db: -37.3 },
    { mcc: 204, mnc: 8, lac: 3350, ci: 56061, power_db: -25.5 }, { mcc: 204, mnc: 8, lac: 3350, ci: 56062, power_db: -28.9 }]));
  e = api.cellDbEstimate(towers);
  const err = api.haversine(e, { lat: 51.69, lon: 4.44 });
  console.log(JSON.stringify({ estimate: e, towers: towers.map(t => `${t.ci}:${t.how}`), errVsWillemstad_m: Math.round(err) }));
  assert(Math.abs(e.lat - 51.68521) < 1e-4 && Math.abs(e.lon - 4.43775) < 1e-4, 'matches the Python result');
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
