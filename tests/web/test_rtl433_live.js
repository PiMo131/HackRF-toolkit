// Live rtl_433 in Node, without a browser: channelize a HackRF ci8 recording to 250 kHz / 1 MHz and
// stream it through web/rtl433-live.js (rtl_433 25.02 WASM, one fresh module per 2 s chunk).
//   node tests/web/test_rtl433_live.js <ci8 file> <centreMHz> <rateMsps> [--tune MHz[:kHz]]... [--format cs16|cu8|cf32]
//                                      [--seconds N] [--expect model:id]... [--no-merge] [--chunk S] [--log]
// Default tunes: centre near 433 MHz -> 433.92 MHz at 250 k; near 868 MHz -> 868.95 MHz at 1000 k (wM-Bus C/T)
// and 868.3 MHz at 1000 k (wM-Bus S/T). Acceptance (2026-10-19 reference captures):
//   node tests/web/test_rtl433_live.js testdata/ism868.sigmf-data 868 4 --expect ELS:36325352
//   node tests/web/test_rtl433_live.js testdata/ism433.sigmf-data 434.4 4
// Sample rate per band: 250 k is enough for 433 MHz OOK/FSK sensors and remotes; wM-Bus (868.95 / 868.3 MHz,
// 100 kchip/s C/T and 32.768 kbps S/T) needs 1000 k (rtl_433 recommends 1200 k for C/T).
"use strict";
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..', '..');
const { Rtl433Live, IqChannelizer } = require(path.join(root, 'web', 'rtl433-live.js'));

const argv = process.argv.slice(2), pos = [], opt = { tune: [], expect: [], format: 'cs16', seconds: Infinity, chunk: 2, merge: 0.25, log: false };
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--tune') opt.tune.push(argv[++i]);
  else if (a === '--expect') opt.expect.push(argv[++i]);
  else if (a === '--format') opt.format = argv[++i];
  else if (a === '--seconds') opt.seconds = +argv[++i];
  else if (a === '--chunk') opt.chunk = +argv[++i];
  else if (a === '--no-merge') opt.merge = 0;
  else if (a === '--log') opt.log = true;
  else pos.push(a);
}
const [file, centreMHz, rateMsps] = pos;
if (!file || !centreMHz || !rateMsps) { console.error('usage: test_rtl433_live.js <ci8 file> <centre MHz> <rate Msps> [--tune MHz[:kHz]] [--format cs16|cu8|cf32] [--seconds N] [--expect model:id]'); process.exit(2); }
const rate = +rateMsps * 1e6, center = +centreMHz * 1e6;
if (!opt.tune.length) opt.tune = center < 600e6 ? ['433.92:250'] : ['868.95:1000', '868.3:1000'];

const factory = require(path.join(root, 'web', 'tools', 'rtl_433.js'));
const wasm = fs.readFileSync(path.join(root, 'web', 'tools', 'rtl_433.wasm'));

const chans = opt.tune.map(spec => {
  const [mhz, khz] = spec.split(':');
  const fHz = Math.round(+mhz * 1e6), outRate = (khz ? +khz : (fHz > 600e6 ? 1000 : 250)) * 1e3;
  if (Math.abs(fHz - center) > rate / 2 - outRate / 2) console.warn(`warning: ${mhz} MHz is at the edge of / outside the ${centreMHz} MHz +-${rate / 2e6} MHz capture`);
  const ch = new IqChannelizer(rate, fHz - center, outRate);
  const dec = new Rtl433Live(factory, wasm, { rate: outRate, centerHz: fHz, format: opt.format, chunkS: opt.chunk, mergeWindow: opt.merge,
    onMessage: m => onMsg(spec, m), onLog: opt.log ? l => console.error(`  [${spec}] ${l}`) : null });
  return { spec, fHz, outRate, ch, dec };
});
console.log(`capture ${file}: ${centreMHz} MHz at ${rateMsps} Msps; tunes: ${chans.map(c => `${c.spec} (${c.outRate / 1e3} k)`).join(', ')}; format ${opt.format}, chunk ${opt.chunk} s, merge ${opt.merge} s`);
const devices = new Map(), msgs = [];
function onMsg(spec, m) {
  msgs.push(m);
  const id = m.id !== undefined ? m.id : (m.channel !== undefined ? m.channel : (m.code !== undefined ? m.code : ''));
  const key = `${m.model}:${id}`;
  const d = devices.get(key) || { n: 0, first: m }; d.n++; devices.set(key, d);
  const fields = Object.entries(m).filter(([k]) => !['time', 'model', 'id', 'protocol', 'mod', 'freq', 'freq1', 'freq2', 'rssi', 'snr', 'noise', 't', 'ts', 'chunk', 'freqHz', 'gainDb'].includes(k)).map(([k, v]) => `${k}=${v}`).join(' ');
  console.log(`@${m.ts.toFixed(2)}s ${(m.freqHz / 1e6).toFixed(3)} MHz ${m.mod || ''} snr ${m.snr} dB  ${m.model} id=${id} [${m.protocol}] ${fields}`);
}

const fd = fs.openSync(file, 'r'), buf = Buffer.alloc(1 << 20);
let n, total = 0; const maxSamples = opt.seconds * rate; const t0 = Date.now(); let dspMs = 0;
while ((n = fs.readSync(fd, buf, 0, buf.length, null)) > 0 && total < maxSamples) {
  const iq8 = new Int8Array(buf.buffer.slice(0, n));
  for (const c of chans) { const a = Date.now(); const [I, Q] = c.ch.process(iq8); dspMs += Date.now() - a; c.dec.feed(I, Q); }
  total += n / 2;
}
for (const c of chans) c.dec.flush();
const secs = total / rate, dt = (Date.now() - t0) / 1000;
console.log(`\n${msgs.length} decodes, ${devices.size} devices from ${secs.toFixed(1)} s of IQ in ${dt.toFixed(1)} s (${(secs / dt).toFixed(1)}x real time; channelizer ${(dspMs / 1000).toFixed(1)} s)`);
for (const [k, d] of [...devices].sort((a, b) => b[1].n - a[1].n)) console.log(`  ${k}  x${d.n}  ${(d.first.freqHz / 1e6).toFixed(3)} MHz ${d.first.mod || ''} snr ${d.first.snr} dB`);
for (const c of chans) {
  const s = c.dec.stats;
  console.log(`  ${c.spec}: ${s.chunks} chunks, ${s.decodes} decodes, ${s.dupes} dupes dropped, ${s.msPerChunk.toFixed(0)} ms per ${opt.chunk} s chunk (${(100 * s.msPerChunk / 1000 / opt.chunk).toFixed(0)} % of real time)`);
}
let ok = msgs.length > 0;
for (const e of opt.expect) {
  const [model, id] = e.split(':');
  const hit = [...devices.keys()].some(k => k.startsWith(model) && (id === undefined || k.endsWith(':' + id)));
  console.log(`  expect ${e}: ${hit ? 'found' : 'MISSING'}`);
  ok = ok && hit;
}
process.exit(ok ? 0 : 1);
