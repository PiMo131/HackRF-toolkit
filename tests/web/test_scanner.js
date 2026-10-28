// node tests/web/test_scanner.js  -- channel-bank scanner on the synthetic PMR446 file
const fs = require('fs'), path = require('path');
global.performance = require('perf_hooks').performance;
const { Receiver } = require(path.resolve('web/audio-worker.js'));
const base = 'tests/web/pmr_synth', meta = JSON.parse(fs.readFileSync(base + '.sigmf-meta')), rate = meta.global['core:sample_rate'];
const data = new Int8Array(fs.readFileSync(base + '.sigmf-data').buffer);
const rx = new Receiver({ rate, center: 445.9e6, freq: 445.9e6, mode: 'nfm',
  scan: { lo: 446.00625e6, hi: 446.19375e6, step: 12.5e3, thresholdDb: 10, holdMs: 1000 } });
// the bank uses performance.now() for hold timing; replace with file time
let fileMs = 0; const realNow = performance.now; performance.now = () => fileMs;
const locks = [], events = []; let last = null, audioSamples = 0, audioEnergyWhileLocked = 0;
for (let o = 0; o < data.length; o += 262144) {
  const chunk = data.subarray(o, Math.min(data.length, o + 262144));
  fileMs = o / 2 / rate * 1000;
  const r = rx.process(chunk);
  const st = rx.bank.status();
  if (st.locked !== last) { locks.push([+(fileMs / 1000).toFixed(2), st.locked && (st.locked / 1e6).toFixed(5)]); last = st.locked; }
  events.push(...st.events);
  if (r && r.L.length) { audioSamples += r.L.length; for (const v of r.L) audioEnergyWhileLocked += v * v; }
}
performance.now = realNow;
console.log(JSON.stringify({ channels: rx.bank.n, locks, events: events.map(e => ({ f: (e.freq / 1e6).toFixed(5), t0: +(e.t0 / 1000).toFixed(2), dur: +e.dur.toFixed(2), peak: +e.peak.toFixed(0) })),
  audioSeconds: +(audioSamples / 48000).toFixed(2), audioRms: +Math.sqrt(audioEnergyWhileLocked / Math.max(1, audioSamples)).toFixed(3) }, null, 1));
