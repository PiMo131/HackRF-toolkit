// Decodes an RS41 IQ recording (8- or 16-bit stereo WAV, I/Q channels) through web/rs41-live.js in
// 4 s windows, as the browser does, and checks that frames with a position come out.
//   node tests/web/test_rs41.js /path/to/RS41.wav
// Reference sample: https://sigidwiki.com/images/4/43/RS41.zip (RS41.wav, 75 kHz 8-bit stereo IQ, 32 s,
// sonde U4345245 near Bratislava, 2024-03-01): 48 frames, lat 48.14 lon 17.12 alt ~197 m.
"use strict";
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..', '..');
const { Rs41Live } = require(path.join(root, 'web', 'rs41-live.js'));
const factory = require(path.join(root, 'web', 'tools', 'rs41.js'));
const wasm = fs.readFileSync(path.join(root, 'web', 'tools', 'rs41.wasm'));
const file = process.argv[2]; if (!file) { console.error('usage: test_rs41.js <iq wav>'); process.exit(2); }
const b = fs.readFileSync(file), v = new DataView(b.buffer, b.byteOffset, b.byteLength);
const ch = v.getUint16(22, true), rate = v.getUint32(24, true), bits = v.getUint16(34, true);
if (ch !== 2) { console.error('need a stereo (I/Q) wav'); process.exit(2); }
let off = 12; while (off < b.length - 8 && b.toString('ascii', off, off + 4) !== 'data') off += 8 + v.getUint32(off + 4, true);
const data = b.subarray(off + 8), n = Math.floor(data.length / (ch * bits / 8)), I = new Float32Array(n), Q = new Float32Array(n);
for (let k = 0; k < n; k++) {
  if (bits === 8) { I[k] = (data[2 * k] - 128) / 128; Q[k] = (data[2 * k + 1] - 128) / 128; }
  else { I[k] = v.getInt16(off + 8 + 4 * k, true) / 32768; Q[k] = v.getInt16(off + 8 + 4 * k + 2, true) / 32768; }
}
const frames = [];
const dec = new Rs41Live(factory, wasm, { rate, onFrame: f => frames.push(f) });
const t0 = Date.now(), step = Math.round(rate * 0.5);
for (let i = 0; i < n; i += step) dec.feed(I.subarray(i, Math.min(i + step, n)), Q.subarray(i, Math.min(i + step, n)));
dec.flush();
const pos = frames.filter(f => f.lat != null);
console.log(`${frames.length} frames (${pos.length} with position, ${dec.stats.dupes} dupes dropped) from ${(n / rate).toFixed(1)} s at ${rate} Hz in ${Date.now() - t0} ms, ${dec.stats.windows} windows, ${dec.stats.msPerWindow.toFixed(0)} ms per window`);
for (const f of pos.slice(0, 3)) console.log(`  #${f.frame} ${f.id} ${f.datetime} lat ${f.lat} lon ${f.lon} alt ${f.alt} m vH ${f.vel_h} m/s temp ${f.temp ?? '-'}`);
process.exit(pos.length >= 10 ? 0 : 1);
