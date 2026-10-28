// node tests/web/test_audio_worker.js <sigmf base> <center MHz> <station MHz> [mode]
// Feeds a ci8 SigMF file through web/audio-worker.js's Receiver and reports stereo / RDS / tone content.
const fs = require('fs'), path = require('path');
global.performance = global.performance || require('perf_hooks').performance;
const { Receiver } = require(path.resolve('web/audio-worker.js'));
const [base, centerMHz, stationMHz, mode = 'wfm'] = process.argv.slice(2);
const meta = JSON.parse(fs.readFileSync(base + '.sigmf-meta')), rate = meta.global['core:sample_rate'];
const data = new Int8Array(fs.readFileSync(base + '.sigmf-data').buffer);
const rx = new Receiver({ rate, center: +centerMHz * 1e6, freq: +stationMHz * 1e6, mode });
const L = [], R = []; let stereoFrames = 0, frames = 0;
const t0 = performance.now();
for (let o = 0; o < data.length; o += 262144) {
  const r = rx.process(data.subarray(o, Math.min(data.length, o + 262144)));
  if (!r) continue; frames++; if (r.stereo) stereoFrames++; L.push(r.L); R.push(r.R);
}
const ms = performance.now() - t0, sec = data.length / 2 / rate;
const cat = a => { const n = a.reduce((s, x) => s + x.length, 0), o = new Float32Array(n); let p = 0; for (const x of a) { o.set(x, p); p += x.length; } return o; };
const goertzel = (x, f, fs) => { const w = 2 * Math.PI * f / fs, c = 2 * Math.cos(w); let s0 = 0, s1 = 0, s2 = 0; for (let i = x.length >> 1; i < x.length; i++) { s0 = x[i] + c * s1 - s2; s2 = s1; s1 = s0; } return (s1 * s1 + s2 * s2 - c * s1 * s2) / (x.length / 2); };
const l = cat(L), r = cat(R), db = v => (10 * Math.log10(v + 1e-20)).toFixed(1);
const st = rx.status();
console.log(JSON.stringify({
  file: path.basename(base), seconds: sec, chanRate: st.chanRate, cpu_fraction: +(ms / 1000 / sec).toFixed(3),
  level_db: +st.level.toFixed(1), stereo_frames: `${stereoFrames}/${frames}`, pilot: +(st.pilot || 0).toFixed(4),
  L_1k_db: db(goertzel(l, 1000, 48000)), L_2k5_db: db(goertzel(l, 2500, 48000)), R_1k_db: db(goertzel(r, 1000, 48000)), R_2k5_db: db(goertzel(r, 2500, 48000)),
  audio_rms: +Math.sqrt(l.reduce((s, x) => s + x * x, 0) / l.length).toFixed(4), rds: st.rds
}, null, 1));
