// End-to-end test of the live pager feed without a browser: run the audio worker's Receiver (NFM +
// pager tap) over a ci8 recording and stream the 22050 Hz audio into multimon-ng WASM.
//   node tests/web/test_pager_stream.js testdata/p2000_169.sigmf-data 169.95 2 169.65
// Expected on the 2026-10-19 reference capture: the same 4 FLEX pages native rfscan decodes.
"use strict";
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..', '..');
const { Receiver, PagerDecoder } = require(path.join(root, 'web', 'audio-worker.js'));
const [file, centreMHz, rateMsps, freqMHz, demods = 'FLEX'] = process.argv.slice(2);
if (!file) { console.error('usage: test_pager_stream.js <ci8 file> <centre MHz> <rate Msps> <freq MHz> [FLEX,POCSAG1200]'); process.exit(2); }
const rate = +rateMsps * 1e6, center = +centreMHz * 1e6, freq = +freqMHz * 1e6;

const factory = require(path.join(root, 'web', 'tools', 'multimon-ng.js'));
const wasm = fs.readFileSync(path.join(root, 'web', 'tools', 'multimon-ng.wasm'));
const rx = new Receiver({ rate, center, freq, mode: 'nfm', nfmBw: 16e3, pager: demods });
const msgs = [];
rx.pagerDec = new PagerDecoder(factory, wasm, demods, m => { msgs.push(m); console.log(`${m.proto} ${m.capcodes.join(' ')} ${m.type} ${m.text}`); });
console.log(`demods opened: ${rx.pagerDec.n}, channel rate ${rx.fe.chanRate} Hz`);

const fd = fs.openSync(file, 'r'), buf = Buffer.alloc(262144);
let n, total = 0; const t0 = Date.now();
while ((n = fs.readSync(fd, buf, 0, buf.length, null)) > 0) {
  rx.process(new Int8Array(buf.buffer.slice(0, n)));
  total += n / 2;
}
rx.pagerDec.close();
const secs = total / rate, dt = (Date.now() - t0) / 1000;
console.log(`${msgs.length} messages from ${secs.toFixed(1)} s of IQ in ${dt.toFixed(1)} s (${(secs / dt).toFixed(1)}x real time), level ${rx.level.toFixed(0)} dB`);
process.exit(msgs.length ? 0 : 1);
