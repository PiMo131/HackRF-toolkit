// Tests the audio worker's data modes and SSB on the synthetic recording from make_aprs_synth.py:
//   node tests/web/test_audio_modes.js /path/to/aprs_synth.ci8
// Checks: APRS packet decoded (AFSK1200 via multimon-ng WASM) with the right position, CTCSS 100.0 Hz
// detected, DTMF digits decoded, and USB demod of a +1 kHz tone gives a 1 kHz audio tone.
"use strict";
const fs = require('fs'), path = require('path');
const root = path.resolve(__dirname, '..', '..');
const { Receiver, PagerDecoder } = require(path.join(root, 'web', 'audio-worker.js'));
const factory = require(path.join(root, 'web', 'tools', 'multimon-ng.js'));
const wasm = fs.readFileSync(path.join(root, 'web', 'tools', 'multimon-ng.wasm'));
const file = process.argv[2]; if (!file) { console.error('usage: test_audio_modes.js <ci8>'); process.exit(2); }
const RATE = 2e6, CENTER = 144.5e6, iq = new Int8Array(fs.readFileSync(file).buffer.slice(0));
let fails = 0;
function run(cfg, demods) {
  const rx = new Receiver({ rate: RATE, center: CENTER, ...cfg });
  const msgs = [];
  if (demods) rx.pagerDec = new PagerDecoder(factory, wasm, demods, m => msgs.push(m));
  const audio = [];
  for (let i = 0; i < iq.length; i += 262144) { const r = rx.process(iq.subarray(i, Math.min(i + 262144, iq.length))); if (r) audio.push(r.L); }
  if (rx.pagerDec) rx.pagerDec.close();
  return { rx, msgs, audio: Float32Array.from(audio.flatMap(a => Array.from(a))) };
}
function check(name, ok, detail) { console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ': ' + detail : ''}`); if (!ok) fails++; }

// 1. APRS + CTCSS on the NFM channel at +300 kHz
let r = run({ freq: CENTER + 300e3, mode: 'nfm', pager: 'AFSK1200', ctcss: true }, 'AFSK1200');
const aprs = r.msgs.filter(m => m.proto === 'APRS');
check('APRS decoded', aprs.length >= 1, `${aprs.length} packets, first: ${aprs[0] ? aprs[0].src + '>' + aprs[0].dest + ',' + aprs[0].path + ':' + aprs[0].text : '-'}`);
check('APRS position', aprs[0] && Math.abs(aprs[0].lat - 51.2057) < 1e-3 && Math.abs(aprs[0].lon - 4.4093) < 1e-3, aprs[0] ? `${aprs[0].lat},${aprs[0].lon} type ${aprs[0].type}` : '');
check('CTCSS 100.0', r.rx.demod.ctcss.tone === 100.0, `tone ${r.rx.demod.ctcss.tone}`);

// 2. DTMF on the NFM channel at +900 kHz
r = run({ freq: CENTER + 900e3, mode: 'nfm', pager: 'DTMF' }, 'DTMF');
const keys = r.msgs.filter(m => m.proto === 'DTMF').map(m => m.text).join('');
check('DTMF digits', keys === '1234#', `got "${keys}"`);

// 3. USB at +600 kHz: expect a 1 kHz tone in the audio (zero-crossing rate over the last second)
r = run({ freq: CENTER + 600e3, mode: 'usb' });
const a = r.audio.subarray(r.audio.length - 48000); let zc = 0;
for (let k = 1; k < a.length; k++) if ((a[k - 1] < 0) !== (a[k] < 0)) zc++;
const hz = zc / 2;
let rms = 0; for (const v of a) rms += v * v; rms = Math.sqrt(rms / a.length);
check('USB 1 kHz tone', Math.abs(hz - 1000) < 30 && rms > 0.05, `${hz.toFixed(0)} Hz, rms ${rms.toFixed(3)}`);
r = run({ freq: CENTER + 600e3 + 2000, mode: 'lsb' });    // tune 2 kHz above: the tone is now 1 kHz below
const b = r.audio.subarray(r.audio.length - 48000); zc = 0;
for (let k = 1; k < b.length; k++) if ((b[k - 1] < 0) !== (b[k] < 0)) zc++;
check('LSB 1 kHz tone', Math.abs(zc / 2 - 1000) < 30, `${(zc / 2).toFixed(0)} Hz`);
console.log(fails ? `${fails} FAILED` : 'all passed');
process.exit(fails ? 1 : 0);
