// Audio receiver DSP for the browser version of rfscan. Runs in a Web Worker.
//
// Input: interleaved 8-bit IQ at the capture rate (HackRF stream or a file). Chain:
//   NCO mix to the tuned channel -> polyphase FIR decimation to ~250 kHz ("channel rate")
//   -> demodulator (WFM stereo + RDS, NFM, AM) -> 48 kHz stereo float audio -> main thread.
// Everything is plain Float32 JavaScript; the first decimation stage is the only heavy part.
"use strict";

const AUDIO_RATE = 48000;
const CHANNEL_RATE_TARGET = 250e3;

// ---------------------------------------------------------------- filters
function lowpassTaps(n, cutoff, fs) {
  // windowed-sinc (Blackman) lowpass, unity DC gain
  const h = new Float32Array(n), m = (n - 1) / 2, fc = cutoff / fs;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const x = i - m;
    const sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
    const w = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (n - 1));
    h[i] = sinc * w; sum += h[i];
  }
  for (let i = 0; i < n; i++) h[i] /= sum;
  return h;
}

// FIR + decimate on a real signal, with history across calls
class FirDecim {
  constructor(taps, D) {
    this.h = taps; this.D = D; this.n = taps.length;
    this.hist = new Float32Array(this.n - 1 + 65536);
    this.have = this.n - 1;          // samples currently in hist (leading history)
  }
  process(x) {
    const { h, D, n } = this;
    if (this.hist.length < this.have + x.length) { const b = new Float32Array(this.have + x.length + 65536); b.set(this.hist.subarray(0, this.have)); this.hist = b; }
    this.hist.set(x, this.have); const total = this.have + x.length;
    const nOut = Math.floor((total - (n - 1)) / D), out = new Float32Array(nOut), hb = this.hist;
    const n4 = n - (n & 3);
    for (let o = 0, p = n - 1; o < nOut; o++, p += D) {
      let a0 = 0, a1 = 0, a2 = 0, a3 = 0, k = 0;
      for (; k < n4; k += 4) { a0 += h[k] * hb[p - k]; a1 += h[k + 1] * hb[p - k - 1]; a2 += h[k + 2] * hb[p - k - 2]; a3 += h[k + 3] * hb[p - k - 3]; }
      for (; k < n; k++) a0 += h[k] * hb[p - k];
      out[o] = a0 + a1 + a2 + a3;
    }
    const used = nOut * D;
    this.hist.copyWithin(0, used, total); this.have = total - used;
    return out;
  }
}
// same filter on a complex (I,Q) pair
class FirDecimC {
  constructor(taps, D) { this.i = new FirDecim(taps, D); this.q = new FirDecim(taps, D); }
  process(I, Q) { return [this.i.process(I), this.q.process(Q)]; }
}
// FIR without decimation (lowpass at the channel rate)
class Fir {
  constructor(taps) { this.f = new FirDecim(taps, 1); }
  process(x) { return this.f.process(x); }
}
class Biquad {        // RBJ band-pass (constant skirt gain)
  constructor(f0, Q, fs) {
    const w = 2 * Math.PI * f0 / fs, a = Math.sin(w) / (2 * Q), c = Math.cos(w), a0 = 1 + a;
    this.b0 = a / a0; this.b2 = -a / a0; this.a1 = -2 * c / a0; this.a2 = (1 - a) / a0;
    this.x1 = this.x2 = this.y1 = this.y2 = 0;
  }
  run(x) {
    const y = this.b0 * x + this.b2 * this.x2 - this.a1 * this.y1 - this.a2 * this.y2;
    this.x2 = this.x1; this.x1 = x; this.y2 = this.y1; this.y1 = y;
    return y;
  }
}
// linear-interpolation resampler (input is already lowpassed well below its Nyquist)
class Resampler {
  constructor(inRate, outRate) { this.base = inRate / outRate; this.step = this.base; this.pos = 0; this.last = 0; this.buf = new Float32Array(8192); }
  trim(ppm) { this.step = this.base * (1 + ppm * 1e-6); }     // +ppm = consume input faster = fewer output samples
  process(x) {
    const need = Math.ceil(x.length / this.step) + 2;
    if (this.buf.length < need) this.buf = new Float32Array(need * 2);
    const out = this.buf; let pos = this.pos, n = 0;
    while (pos < x.length) {
      const i = Math.floor(pos), f = pos - i;
      const a = x[i], b = x[Math.min(i + 1, x.length - 1)];
      out[n++] = a + (b - a) * f; pos += this.step;
    }
    this.pos = pos - x.length; this.last = x[x.length - 1];
    return out.slice(0, n);
  }
}

// ---------------------------------------------------------------- front end
// choose D1*D2 so the channel rate lands near 250 kHz for any capture rate
function decimPlan(rate) {
  const F = Math.max(1, Math.round(rate / CHANNEL_RATE_TARGET));
  let D1 = F;
  for (const d of [8, 6, 5, 4, 3, 2]) if (F % d === 0 && F / d >= 1) { D1 = d; break; }
  if (F <= 8) D1 = F;
  return { D1, D2: F / D1, chanRate: rate / F };
}
class FrontEnd {
  constructor(rate, offsetHz, chanBw) {
    this.rate = rate; this.phase = 0; this.setOffset(offsetHz);
    const { D1, D2, chanRate } = decimPlan(rate);
    this.chanRate = chanRate;
    const r1 = rate / D1;
    this.s1 = D1 > 1 ? new FirDecimC(lowpassTaps(6 * D1, Math.min(0.35 * r1, 0.5 * r1 - 60e3), rate), D1) : null;
    this.s2 = D2 > 1 ? new FirDecimC(lowpassTaps(Math.max(16, 12 * D2), chanBw / 2, r1), D2) : null;
    this.scale = 1 / 128;
  }
  setOffset(hz) { this.dphi = -2 * Math.PI * hz / this.rate; }
  process(iq8) {          // Int8Array interleaved -> [I, Q] Float32 at chanRate
    const n = iq8.length >> 1, I = new Float32Array(n), Q = new Float32Array(n), s = this.scale;
    // recursive oscillator: one complex multiply per sample, renormalised every 1024 samples
    let c = Math.cos(this.phase), sn = Math.sin(this.phase);
    const dc = Math.cos(this.dphi), ds = Math.sin(this.dphi);
    for (let i = 0, j = 0; i < n; i++, j += 2) {
      const a = iq8[j] * s, b = iq8[j + 1] * s;
      I[i] = a * c - b * sn; Q[i] = a * sn + b * c;
      const nc = c * dc - sn * ds; sn = c * ds + sn * dc; c = nc;
      if ((i & 1023) === 1023) { const g = 1 / Math.sqrt(c * c + sn * sn); c *= g; sn *= g; }
    }
    this.phase = Math.atan2(sn, c);
    let r = [I, Q];
    if (this.s1) r = this.s1.process(r[0], r[1]);
    if (this.s2) r = this.s2.process(r[0], r[1]);
    return r;
  }
}

// ---------------------------------------------------------------- demodulators
class Discriminator {
  constructor() { this.pi = 1; this.pq = 0; }
  process(I, Q) {
    const out = new Float32Array(I.length); let pi = this.pi, pq = this.pq;
    for (let k = 0; k < I.length; k++) {
      const i = I[k], q = Q[k];
      out[k] = Math.atan2(q * pi - i * pq, i * pi + q * pq);
      pi = i; pq = q;
    }
    this.pi = pi; this.pq = pq; return out;
  }
}
function power(I, Q) { let p = 0; for (let k = 0; k < I.length; k++) p += I[k] * I[k] + Q[k] * Q[k]; return p / Math.max(1, I.length); }

// WFM: mono/stereo MPX decode + RDS
class WfmDemod {
  constructor(fs) {
    this.fs = fs; this.disc = new Discriminator();
    this.pilotBpf = new Biquad(19000, 40, fs);
    this.pll = { phase: 0, freq: 2 * Math.PI * 19000 / fs, lock: 0, alpha: 0.03, beta: 0.0004 };
    this.lpfM = new Fir(lowpassTaps(48, 15000, fs)); this.lpfS = new Fir(lowpassTaps(48, 15000, fs));
    this.deemL = { a: Math.exp(-1 / (50e-6 * fs)), y: 0 }; this.deemR = { a: Math.exp(-1 / (50e-6 * fs)), y: 0 };
    this.rsL = new Resampler(fs, AUDIO_RATE); this.rsR = new Resampler(fs, AUDIO_RATE);
    this.devScale = fs / (2 * Math.PI * 75000);      // rad/sample -> fraction of 75 kHz deviation
    this.rds = new RdsDecoder(fs);
    this.stereoOn = true;
    // noise gauge: MPX power around 80 kHz holds no programme, only demodulated noise
    this.noiseBpf = new Biquad(80000, 4, fs); this.noise = 1e-4; this.blend = 0;
  }
  process(I, Q) {
    const m = this.disc.process(I, Q), n = m.length, s = this.devScale;
    const lr = new Float32Array(n), rdsI = new Float32Array(n), rdsQ = new Float32Array(n);
    const p = this.pll; let ph = p.phase, fr = p.freq, lock = p.lock;
    for (let k = 0; k < n; k++) {
      const x = m[k] * s; m[k] = x;
      const pil = this.pilotBpf.run(x);
      const nz = this.noiseBpf.run(x); this.noise += (nz * nz - this.noise) * 0.0005;
      const e = pil * -Math.sin(ph);
      lock += (pil * Math.cos(ph) - lock) * 0.0005;
      fr += p.beta * e; ph += fr + p.alpha * e;
      if (ph > Math.PI) ph -= 2 * Math.PI;
      lr[k] = x * 2 * Math.cos(2 * ph);
      rdsI[k] = x * Math.cos(3 * ph); rdsQ[k] = x * Math.sin(3 * ph);
    }
    p.phase = ph; p.freq = fr; p.lock = lock;
    // pilot is ~9% of deviation -> lock ~0.045 when locked; the band-pass halves it roughly
    const locked = this.stereoOn && lock > 0.012;
    // stereo blend from pilot-to-noise ratio: full stereo only on strong signals, mono when weak
    this.pnr = 10 * Math.log10((lock * lock) / (this.noise + 1e-12) + 1e-12);
    // gauge calibration: a station at FM threshold (~11 dB CNR) reads about -5 dB, a clean one 10-12 dB
    const target = locked ? Math.max(0, Math.min(1, (this.pnr - 2) / 8)) : 0;
    this.blend += (target - this.blend) * 0.2;
    const stereo = this.blend > 0.05, g = this.blend;
    const mono = this.lpfM.process(m), diff = this.lpfS.process(lr);
    const L = new Float32Array(mono.length), R = new Float32Array(mono.length);
    const dl = this.deemL, dr = this.deemR;
    for (let k = 0; k < mono.length; k++) {
      const a = mono[k] + g * diff[k], b = mono[k] - g * diff[k];
      dl.y += (a - dl.y) * (1 - dl.a); dr.y += (b - dr.y) * (1 - dr.a);
      L[k] = softLimit(dl.y * 0.8); R[k] = softLimit(dr.y * 0.8);
    }
    this.rds.process(rdsI, rdsQ);
    return { L: this.rsL.process(L), R: this.rsR.process(R), stereo, pilot: lock };
  }
  status() { return { stereo: this.blend > 0.05, blend: this.blend, pnr: this.pnr, pilot: this.pll.lock, rds: this.rds.status() }; }
}
// gentle knee above 0.7, never exceeds 1.0
function softLimit(v) { const a = Math.abs(v); if (a <= 0.7) return v; const y = 0.7 + 0.3 * Math.tanh((a - 0.7) / 0.3); return v < 0 ? -y : y; }

// NFM voice: 12.5/25 kHz channel, +-2.5/5 kHz deviation
class NfmDemod {
  constructor(fs, bw = 12.5e3) {
    this.fs = fs; this.disc = new Discriminator();
    this.chan = new FirDecimC(lowpassTaps(64, bw / 2, fs), 1);
    this.lpf = new Fir(lowpassTaps(48, 3400, fs));
    this.deem = { a: Math.exp(-1 / (300e-6 * fs)), y: 0 };
    this.rs = new Resampler(fs, AUDIO_RATE);
    this.devScale = fs / (2 * Math.PI * (bw > 20e3 ? 5000 : 2500));
    this.agc = 1;
  }
  process(I, Q) {
    [I, Q] = this.chan.process(I, Q);
    const d = this.disc.process(I, Q);
    if (this.pager) this.pagerOut = this.pager.process(d);     // raw discriminator, before de-emphasis
    if (this.ctcss) this.ctcss.process(d);
    for (let k = 0; k < d.length; k++) { d[k] *= this.devScale; const y = this.deem.y + (d[k] - this.deem.y) * (1 - this.deem.a); this.deem.y = y; d[k] = y; }
    const a = this.rs.process(this.lpf.process(d));
    for (let k = 0; k < a.length; k++) a[k] = softLimit(a[k] * 0.7);
    return { L: a, R: a, stereo: false };
  }
  status() { return {}; }
}
// Pager tap: discriminator output -> 6 kHz lowpass -> 22050 Hz s16 (±4.8 kHz deviation = ±12000),
// the same audio native rfscan pipes into multimon-ng. Decoded by PagerDecoder (multimon-ng WASM).
const PAGER_RATE = 22050;
class PagerTap {
  // fs: input rate. audioScale: when the input is demodulated audio (±1) instead of a discriminator (rad/sample)
  constructor(fs, audioScale) {
    this.lpf = new Fir(lowpassTaps(64, 6000, fs)); this.rs = new Resampler(fs, PAGER_RATE);
    this.scale = audioScale ? audioScale * 32767 : 12000 * fs / (2 * Math.PI * 4800); this.dc = 0;
  }
  process(d) {
    const y = this.rs.process(this.lpf.process(d)), out = new Int16Array(y.length);
    let dc = this.dc;
    for (let k = 0; k < y.length; k++) {
      dc += (y[k] - dc) * 5e-5;                                  // residual carrier offset (~1 s)
      const v = (y[k] - dc) * this.scale;
      out[k] = v > 32767 ? 32767 : v < -32767 ? -32767 : v;
    }
    this.dc = dc; return out;
  }
}
// APRS payload helpers: data type from the first character, position from the uncompressed or compressed formats
function aprsType(p) {
  const c = p[0];
  if (c === '!' || c === '=' || c === '/' || c === '@') return 'POS';
  if (c === ':') return 'MSG'; if (c === '>') return 'STATUS'; if (c === ';') return 'OBJECT'; if (c === 'T') return 'TELEM';
  if (c === '`' || c === "'" || c === '\x1c' || c === '\x1d') return 'MIC-E'; if (c === '}') return '3RD';
  return 'OTHER';
}
function aprsPosition(p) {
  let s = p.slice(1);
  if ((p[0] === '/' || p[0] === '@') && /^\d{6}[zh\/]/.test(s)) s = s.slice(7);        // timestamped
  let m = /^(\d{2})(\d{2}\.\d+)([NS])(.)(\d{3})(\d{2}\.\d+)([EW])(.)(.*)$/.exec(s);
  if (m) {
    const lat = (+m[1] + +m[2] / 60) * (m[3] === 'S' ? -1 : 1), lon = (+m[5] + +m[6] / 60) * (m[7] === 'W' ? -1 : 1);
    return { lat: +lat.toFixed(5), lon: +lon.toFixed(5), symbol: m[4] + m[8], comment: m[9].trim() };
  }
  m = /^([\/\\A-Za-j])([!-{]{4})([!-{]{4})(.)(..)(.)(.*)$/.exec(s);              // compressed (base-91)
  if (m) {
    const b91 = t => [...t].reduce((a, ch) => a * 91 + (ch.charCodeAt(0) - 33), 0);
    const lat = 90 - b91(m[2]) / 380926, lon = -180 + b91(m[3]) / 190463;
    return { lat: +lat.toFixed(5), lon: +lon.toFixed(5), symbol: m[1] + m[4], comment: m[7].trim() };
  }
  return {};
}
// Streams 22050 Hz s16 audio into multimon-ng's FLEX/POCSAG demodulators (web/tools/multimon-ng.wasm,
// entry points in web/tools/src/mm_stream.c). Output lines arrive through Module.print.
//   FLEX|2026-10-29 09:04:42|1600/2/K/A|07.029|002029582 001420999|ALN|B2 AMBU 18190 ...
//   POCSAG1200: Address: 1234567  Function: 0  Alpha:   text
class PagerDecoder {
  constructor(factory, wasmBinary, demods, onMessage) {
    this.onMessage = onMessage; this.count = 0; this.lastT = 0;
    this.M = { wasmBinary, noInitialRun: true, print: l => this.line(l), printErr: () => {} };
    factory(this.M);
    this.n = this.M._mm_stream_open(this.M.stringToNewUTF8(demods));
    this.cap = 8192; this.ptr = this.M._malloc(this.cap * 2);
  }
  feed(s16) {
    for (let i = 0; i < s16.length; i += this.cap) {
      const c = s16.subarray(i, Math.min(i + this.cap, s16.length));
      this.M.HEAP16.set(c, this.ptr >> 1);                       // re-read HEAP16: memory may have grown
      this.M._mm_stream_feed(this.ptr, c.length);
    }
  }
  close() { try { this.M._mm_stream_close(); } catch (e) { /* module state is discarded anyway */ } }
  line(l) {
    let msg = null, m;
    if (l.startsWith('APRS: ')) {                                // APRS: SRC>DEST,PATH:payload (AX.25 UI via AFSK1200)
      m = /^APRS: ([^>]+)>([^,:]+)((?:,[^,:]+)*):(.*)$/.exec(l);
      if (m) msg = { proto: 'APRS', mode: '', frame: '', capcodes: [m[1]], type: aprsType(m[4]), text: m[4], src: m[1], dest: m[2], path: m[3].replace(/^,/, ''), ...aprsPosition(m[4]) };
    } else if ((m = /^DTMF: (.)$/.exec(l))) {
      msg = { proto: 'DTMF', mode: '', frame: '', capcodes: [], type: 'KEY', text: m[1] };
    } else if ((m = /^(ZVEI1|ZVEI2|ZVEI3|CCIR|EEA|EIA): ([0-9A-F]+)$/.exec(l))) {
      msg = { proto: m[1], mode: '', frame: '', capcodes: [m[2]], type: 'SEL', text: m[2] };
    } else if ((m = /^Morse: (.*)$/.exec(l))) {
      msg = { proto: 'CW', mode: '', frame: '', capcodes: [], type: 'CW', text: m[1].trim() };
    } else if (l.startsWith('FLEX')) {
      const p = l.split('|');
      if (p.length >= 7) msg = { proto: 'FLEX', mode: p[2], frame: p[3], capcodes: p[4].trim().split(/\s+/), type: p[5], text: p.slice(6).join('|').trim() };
      else { const m = /^FLEX: \S+ \S+ (\S+) (\S+) \[(\d+)\] (\w+)\s?(.*)$/.exec(l); if (m) msg = { proto: 'FLEX', mode: m[1], frame: m[2], capcodes: [m[3]], type: m[4], text: m[5].trim() }; }
    } else {
      const m = /^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d)\s*(?:(Alpha|Numeric|Skyper):\s*(.*))?$/.exec(l);
      if (m) msg = { proto: m[1], mode: '', frame: '', capcodes: [m[2]], type: m[4] ? m[4].slice(0, 3).toUpperCase() : 'TON', text: (m[5] || '').trim(), func: +m[3] };
    }
    if (!msg) return;
    msg.t = Date.now(); msg.raw = l; this.count++; this.lastT = msg.t;
    this.onMessage(msg);
  }
}
// SSB / CW: shift the wanted sideband to baseband, lowpass, take the real part, audio AGC.
// usb: 300-3000 Hz above the carrier; lsb: below; cw: 500 Hz wide around the carrier, 700 Hz pitch.
class SsbDemod {
  constructor(fs, mode = 'usb') {
    this.fs = fs; this.mode = mode;
    const bw = mode === 'cw' ? 500 : 2700, mid = mode === 'cw' ? 0 : 1650;
    this.sign = mode === 'lsb' ? -1 : 1;
    this.shift = mode === 'cw' ? 700 : mid;                     // audio position of the band centre
    this.mid = mid;
    this.chan = new FirDecimC(lowpassTaps(96, bw / 2, fs), 1);
    this.rs = new Resampler(fs, AUDIO_RATE); this.rsQ = new Resampler(fs, AUDIO_RATE);
    this.ph1 = 0; this.ph2 = 0; this.w1 = -2 * Math.PI * this.sign * mid / fs; this.w2 = 2 * Math.PI * this.shift / AUDIO_RATE;
    this.gain = 1; this.peak = 0;
  }
  process(I, Q) {
    // 1) rotate so the band centre sits at DC, 2) complex lowpass, 3) rotate back to the audio pitch and take Re
    const n = I.length, i2 = new Float32Array(n), q2 = new Float32Array(n);
    for (let k = 0; k < n; k++) {
      const c = Math.cos(this.ph1), s = Math.sin(this.ph1);
      i2[k] = I[k] * c - Q[k] * s; q2[k] = I[k] * s + Q[k] * c;
      this.ph1 += this.w1; if (this.ph1 > Math.PI) this.ph1 -= 2 * Math.PI; else if (this.ph1 < -Math.PI) this.ph1 += 2 * Math.PI;
    }
    const [fi, fq] = this.chan.process(i2, q2);
    const ai = this.rs.process(fi), aq = this.rsQ.process(fq), a = new Float32Array(Math.min(ai.length, aq.length));
    for (let k = 0; k < ai.length; k++) {
      const c = Math.cos(this.ph2), s = Math.sin(this.ph2);
      let v = (ai[k] * c - aq[k] * s * this.sign) * this.gain * 4;
      this.ph2 += this.w2; if (this.ph2 > Math.PI) this.ph2 -= 2 * Math.PI;
      const pk = Math.abs(v); if (pk > 0.8) this.gain *= 0.95; else if (this.gain < 300) this.gain *= 1.0003;   // fast attack, slow decay
      a[k] = softLimit(v);
    }
    return { L: a, R: a, stereo: false };
  }
  status() { return { ssbGain: 20 * Math.log10(this.gain) }; }
}
// CTCSS sub-audible tone detector: Goertzel bank on the raw NFM discriminator, 0.4 s blocks at 4 kHz
const CTCSS_TONES = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
  131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1];
class Ctcss {
  constructor(fs) {
    this.lpf = new Fir(lowpassTaps(64, 300, fs)); this.rs = new Resampler(fs, 4000);
    this.block = new Float32Array(1600); this.n = 0; this.tone = null; this.dc = 0;
    this.coef = CTCSS_TONES.map(f => 2 * Math.cos(2 * Math.PI * f / 4000));
  }
  process(d) {
    const y = this.rs.process(this.lpf.process(d));
    for (let k = 0; k < y.length; k++) {
      this.dc += (y[k] - this.dc) * 0.002;
      this.block[this.n++] = y[k] - this.dc;
      if (this.n === this.block.length) { this.analyse(); this.n = 0; }
    }
  }
  analyse() {
    const b = this.block; let tot = 0; for (let k = 0; k < b.length; k++) tot += b[k] * b[k];
    let best = -1, bestP = 0, sum = 0;
    for (let t = 0; t < this.coef.length; t++) {
      const c = this.coef[t]; let s0 = 0, s1 = 0, s2 = 0;
      for (let k = 0; k < b.length; k++) { s0 = b[k] + c * s1 - s2; s2 = s1; s1 = s0; }
      const p = s1 * s1 + s2 * s2 - c * s1 * s2; sum += p;
      if (p > bestP) { bestP = p; best = t; }
    }
    const mean = (sum - bestP) / (this.coef.length - 1);
    this.tone = (bestP > 8 * mean && bestP > 1e-7 * b.length * b.length) ? CTCSS_TONES[best] : null;
  }
}
// AM (airband): envelope, with slow AGC
class AmDemod {
  constructor(fs, bw = 8e3) {
    this.fs = fs; this.chan = new FirDecimC(lowpassTaps(64, bw / 2, fs), 1);
    this.lpf = new Fir(lowpassTaps(48, 3400, fs)); this.rs = new Resampler(fs, AUDIO_RATE);
    this.dc = 0; this.gain = 1;
  }
  process(I, Q) {
    [I, Q] = this.chan.process(I, Q);
    const e = new Float32Array(I.length);
    for (let k = 0; k < I.length; k++) {
      const v = Math.sqrt(I[k] * I[k] + Q[k] * Q[k]);
      this.dc += (v - this.dc) * 0.002;
      e[k] = (v - this.dc) * this.gain;
      const pk = Math.abs(e[k]); if (pk > 0.9) this.gain *= 0.99; else if (this.gain < 50) this.gain *= 1.00002;
    }
    const a = this.rs.process(this.lpf.process(e));
    for (let k = 0; k < a.length; k++) a[k] = softLimit(a[k]);
    return { L: a, R: a, stereo: false };
  }
  status() { return {}; }
}

// ---------------------------------------------------------------- RDS
// 57 kHz BPSK, 1187.5 bit/s, differential + biphase coded; 26-bit blocks with 10-bit check words.
const RDS_SYNDROME = { A: 383, B: 14, C: 303, D: 663, Cp: 748 };   // offsets A, B, C, D, C'
const RDS_NEXT = { A: 'B', B: 'C', C: 'D', Cp: 'D', D: 'A' };
const PTY_EU = ['None', 'News', 'Current affairs', 'Information', 'Sport', 'Education', 'Drama', 'Culture', 'Science', 'Varied',
  'Pop music', 'Rock music', 'Easy listening', 'Light classical', 'Serious classical', 'Other music', 'Weather', 'Finance',
  "Children's", 'Social affairs', 'Religion', 'Phone-in', 'Travel', 'Leisure', 'Jazz', 'Country', 'National music', 'Oldies',
  'Folk', 'Documentary', 'Alarm test', 'Alarm'];
function rdsSyndrome(block26) {
  let reg = 0;
  for (let i = 26; i > 0; i--) { reg = (reg << 1) | ((block26 / Math.pow(2, i - 1)) & 1); if (reg & 1024) reg ^= 0x5B9; }
  for (let i = 10; i > 0; i--) { reg <<= 1; if (reg & 1024) reg ^= 0x5B9; }
  return reg & 1023;
}
class RdsDecoder {
  constructor(fs) {
    this.fs = fs;
    this.D = Math.max(1, Math.round(fs / 12000)); this.r = fs / this.D;       // ~12 kHz
    this.lpf = new FirDecimC(lowpassTaps(64, 2400, fs), this.D);
    this.sps = this.r / 2375;                 // samples per half-bit (biphase symbol)
    this.rot = 0; this.rotAcc = { re: 0, im: 0 };
    this.mu = 0; this.prevHalf = 0; this.lastHalf = 0; this.halfN = 0; this.scoreOdd = 0; this.scoreEven = 0; this.lastSample = 0; this.prevE = 0;
    this.bits = 0; this.nbits = 0; this.expect = null; this.skip = 0; this.errors = 0;
    this.blocks = {}; this.groups = 0; this.badBlocks = 0; this.goodRun = 0;
    this.pi = null; this.ps = Array(8).fill(' '); this.psOk = 0; this.rt = Array(64).fill(' '); this.rtAB = null; this.pty = null; this.tp = null;
    this.rtDone = ''; this.psDone = '';
  }
  process(rI, rQ) {
    const [I, Q] = this.lpf.process(rI, rQ);
    // carrier phase: BPSK squared -> angle/2, slow average
    const acc = this.rotAcc;
    for (let k = 0; k < I.length; k++) { const re = I[k] * I[k] - Q[k] * Q[k], im = 2 * I[k] * Q[k]; acc.re += (re - acc.re) * 0.002; acc.im += (im - acc.im) * 0.002; }
    const th = -0.5 * Math.atan2(acc.im, acc.re), c = Math.cos(th), s = Math.sin(th);
    const x = new Float32Array(I.length);
    for (let k = 0; k < I.length; k++) x[k] = I[k] * c - Q[k] * s;
    // symbol timing at 2375 baud: sample at mu, Gardner error from the midpoint sample
    const sps = this.sps; let mu = this.mu;
    let prev = this.lastSample;
    const samp = (p) => { const i = Math.floor(p), f = p - i; const a = i < 0 ? prev : x[i], b = i + 1 < 0 ? prev : x[Math.min(i + 1, x.length - 1)]; return a + (b - a) * f; };
    while (mu < x.length) {
      const cur = samp(mu), mid = samp(mu - sps / 2);
      const err = (cur - this.prevHalf) * mid;
      mu += sps - Math.max(-0.3, Math.min(0.3, 0.05 * err));
      this.symbol(cur);
      this.prevHalf = cur;
    }
    this.mu = mu - x.length; this.lastSample = x[x.length - 1];
  }
  symbol(v) {
    // biphase: each bit is a (+,-) or (-,+) pair of half-symbols. Two pairings are possible;
    // keep a running score of |first - second| for both and use whichever is consistently larger.
    const p = this.lastHalf; this.lastHalf = v; this.halfN = (this.halfN || 0) + 1;
    const d = Math.abs(p - v);
    if (this.halfN & 1) this.scoreOdd = (this.scoreOdd || 0) * 0.99 + d * 0.01; else this.scoreEven = (this.scoreEven || 0) * 0.99 + d * 0.01;
    const useOdd = (this.scoreOdd || 0) >= (this.scoreEven || 0);
    if ((this.halfN & 1) === (useOdd ? 1 : 0)) {
      const e = p - v > 0 ? 1 : 0;
      const bit = e ^ this.prevE; this.prevE = e;
      this.pushBit(bit);
    }
  }
  pushBit(bit) {
    this.bits = ((this.bits * 2) + bit) % 67108864; this.nbits++;     // keep 26 bits
    if (this.skip > 0) { this.skip--; return; }
    if (this.nbits < 26) return;
    const syn = rdsSyndrome(this.bits);
    let off = null;
    for (const k in RDS_SYNDROME) if (RDS_SYNDROME[k] === syn) off = k;
    if (this.expect) {
      if (off === this.expect || (this.expect === 'C' && off === 'Cp')) { this.accept(off); this.errors = 0; this.goodRun++; }
      else { this.badBlocks++; this.goodRun = 0; if (++this.errors > 8) { this.expect = null; this.blocks = {}; } else { this.blocks = {}; this.expect = RDS_NEXT[this.expect]; } }
      this.skip = 25;
    } else if (off) {
      this.accept(off); this.skip = 25; this.goodRun = 1;
    }
  }
  accept(off) {
    const data = Math.floor(this.bits / 1024) & 0xffff;
    this.blocks[off === 'Cp' ? 'C' : off] = data; if (off === 'Cp') this.blocks.cp = true;
    this.expect = RDS_NEXT[off];
    if (off === 'D') { this.group(this.blocks); this.blocks = {}; }
    else if (off === 'A') this.blocks = { A: data };
  }
  group(b) {
    if (b.A === undefined || b.B === undefined || b.D === undefined) return;
    this.groups++;
    this.pi = b.A;
    const gt = b.B >> 12, ver = (b.B >> 11) & 1;
    this.tp = (b.B >> 10) & 1; this.pty = (b.B >> 5) & 31;
    if (gt === 0) {
      const a = b.B & 3;
      this.ps[2 * a] = chr(b.D >> 8); this.ps[2 * a + 1] = chr(b.D & 255);
      this.psOk |= 1 << a; if (this.psOk === 15) this.psDone = this.ps.join('');
    } else if (gt === 2) {
      const ab = (b.B >> 4) & 1, a = b.B & 15;
      if (this.rtAB !== null && ab !== this.rtAB) { this.rt.fill(' '); }
      this.rtAB = ab;
      if (ver === 0 && b.C !== undefined && !b.cp) { this.rt[4 * a] = chr(b.C >> 8); this.rt[4 * a + 1] = chr(b.C & 255); this.rt[4 * a + 2] = chr(b.D >> 8); this.rt[4 * a + 3] = chr(b.D & 255); }
      else { this.rt[2 * a] = chr(b.D >> 8); this.rt[2 * a + 1] = chr(b.D & 255); }
      this.rtDone = this.rt.join('').replace(/\r.*$/, '').trimEnd();
    }
  }
  status() {
    return { pi: this.pi === null ? null : this.pi.toString(16).toUpperCase().padStart(4, '0'), ps: this.ps.join(''), rt: this.rtDone,
      pty: this.pty === null ? null : PTY_EU[this.pty], tp: this.tp, groups: this.groups, bad: this.badBlocks, synced: this.goodRun >= 2 };
  }
}
function chr(c) { return c >= 32 && c < 127 ? String.fromCharCode(c) : (c === 13 ? '\r' : ' '); }

// ---------------------------------------------------------------- spectrum
class Spectrum {
  constructor(n) {
    this.n = n; this.re = new Float32Array(n); this.im = new Float32Array(n); this.win = new Float32Array(n); this.rev = new Uint32Array(n);
    const bits = Math.log2(n);
    for (let i = 0; i < n; i++) { let r = 0; for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b); this.rev[i] = r; this.win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)); }
    this.cos = new Float32Array(n / 2); this.sin = new Float32Array(n / 2);
    for (let i = 0; i < n / 2; i++) { this.cos[i] = Math.cos(2 * Math.PI * i / n); this.sin[i] = Math.sin(2 * Math.PI * i / n); }
    this.norm = 1 / (n * n / 4);
  }
  // fftshifted power in dB of the first n samples of an Int8 IQ buffer
  db(iq8) {
    const { n, re, im, win, rev, cos, sin } = this;
    for (let i = 0; i < n; i++) { const j = rev[i]; re[i] = iq8[2 * j] * win[j] / 128; im[i] = iq8[2 * j + 1] * win[j] / 128; }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1, step = n / size;
      for (let i = 0; i < n; i += size) for (let j = i, k = 0; j < i + half; j++, k += step) {
        const l = j + half, c = cos[k], sn = sin[k];
        const tr = re[l] * c + im[l] * sn, ti = im[l] * c - re[l] * sn;
        re[l] = re[j] - tr; im[l] = im[j] - ti; re[j] += tr; im[j] += ti;
      }
    }
    const out = new Float32Array(n), h = n >> 1, norm = this.norm;
    for (let i = 0; i < n; i++) out[(i + h) & (n - 1)] = 10 * Math.log10((re[i] * re[i] + im[i] * im[i]) * norm + 1e-20);
    return out;
  }
}

// ---------------------------------------------------------------- channel bank (scanner)
// Power of every channel in a grid from one FFT per block; per-channel noise floor; squelch;
// lock on the strongest open channel (priority channel first), hold after it closes.
class ChannelBank {
  constructor(cfg, rate, center) {
    this.rate = rate; this.center = center; this.step = cfg.step; this.bw = cfg.bw;
    this.thresholdDb = cfg.thresholdDb ?? 10; this.holdMs = cfg.holdMs ?? 2000; this.priority = cfg.priority || null;
    this.lockouts = new Set(cfg.lockouts || []);
    this.N = 4096; this.spec = new Spectrum(this.N); this.binHz = rate / this.N;
    const lo = Math.max(cfg.lo, center - rate * 0.42), hi = Math.min(cfg.hi, center + rate * 0.42);
    this.freqs = [];
    // keep channels off the DC spike: a few kHz for narrow channels, 150 kHz for broadcast FM
    const dcGuard = cfg.bw >= 100e3 ? 150e3 : 20e3;
    for (let f = Math.ceil((lo - cfg.lo) / cfg.step) * cfg.step + cfg.lo; f <= hi + 1; f += cfg.step) if (Math.abs(f - center) > dcGuard) this.freqs.push(Math.round(f));
    const n = this.freqs.length;
    this.power = new Float32Array(n).fill(-120); this.floor = new Float32Array(n).fill(NaN); this.open = new Uint8Array(n); this.above = new Uint8Array(n); this.peak = new Float32Array(n).fill(-200);
    this.locked = -1; this.lockedSince = 0; this.closedAt = 0; this.events = []; this.t = 0; this.n = n; this.blocks = 0;
    // measure the middle of the channel only, so a neighbour's splatter does not open it
    this.halfBins = Math.max(1, Math.round(Math.min(this.bw / 2, this.step * 0.4) / this.binHz));
  }
  // returns the channel index to demodulate (or -1); `now` in ms
  update(iq8, now) {
    if (iq8.length < 2 * this.N) return this.locked;
    const db = this.spec.db(iq8), N = this.N, hb = this.halfBins, lo = this.center - this.rate / 2;
    this.blocks++;
    for (let i = 0; i < this.n; i++) {
      const k = Math.round((this.freqs[i] - lo) / this.binHz);
      let acc = 0, c = 0;
      for (let j = Math.max(0, k - hb); j <= Math.min(N - 1, k + hb); j++) { acc += Math.pow(10, db[j] / 10); c++; }
      const p = 10 * Math.log10(acc / Math.max(1, c) + 1e-20);
      this.power[i] += (p - this.power[i]) * 0.35;
    }
    // noise floor per channel = 30th percentile of the smoothed power over the neighbouring channels
    // (so a channel that is always busy, like a broadcast station, still stands out), smoothed in time
    const half = Math.max(8, Math.round(this.n / 8)), tmp = [];
    for (let i = 0; i < this.n; i++) {
      tmp.length = 0;
      for (let j = Math.max(0, i - half); j <= Math.min(this.n - 1, i + half); j++) tmp.push(this.power[j]);
      tmp.sort((a, b) => a - b);
      const fl = tmp[Math.floor(tmp.length * 0.3)];
      this.floor[i] = Number.isNaN(this.floor[i]) || this.blocks < 6 ? fl : this.floor[i] + (fl - this.floor[i]) * 0.05;
    }
    if (this.blocks < 12) return -1;      // let the estimates settle (~150 ms)
    let best = -1, bestDb = -Infinity, prio = -1;
    for (let i = 0; i < this.n; i++) {
      const over = this.power[i] - this.floor[i];
      const wasOpen = this.open[i];
      // a channel whose direct neighbour is much stronger is seeing that neighbour's skirts, not its own signal
      const nb = Math.max(i > 0 ? this.power[i - 1] : -200, i + 1 < this.n ? this.power[i + 1] : -200);
      const splatter = nb - this.power[i] > 8 && Math.abs(this.freqs[(i > 0 && this.power[i - 1] >= nb) ? i - 1 : i + 1] - this.freqs[i]) <= this.step * 1.01;
      // debounce: two consecutive blocks above threshold before a channel counts as open
      const above = !splatter && (over > this.thresholdDb || (wasOpen && over > this.thresholdDb - 3));
      this.above[i] = above ? Math.min(3, this.above[i] + 1) : 0;
      this.open[i] = this.above[i] >= 2 || (wasOpen && above) ? 1 : 0;
      if (this.open[i] && over > this.peak[i]) this.peak[i] = over;
      if (!this.open[i] || this.lockouts.has(this.freqs[i])) continue;
      if (this.priority && Math.abs(this.freqs[i] - this.priority) < this.step / 2) prio = i;
      if (over > bestDb) { bestDb = over; best = i; }
    }
    if (prio >= 0) best = prio;
    const L = this.locked;
    if (L >= 0) {
      if (this.open[L] && !this.lockouts.has(this.freqs[L])) { this.closedAt = 0; if (prio >= 0 && prio !== L) this.switchTo(prio, now); }
      else {
        if (!this.closedAt) this.closedAt = now;
        if (now - this.closedAt > this.holdMs || (best >= 0 && best !== L && now - this.closedAt > 300)) { this.release(now); if (best >= 0) this.switchTo(best, now); }
      }
    } else if (best >= 0) this.switchTo(best, now);
    return this.locked;
  }
  switchTo(i, now) { if (this.locked >= 0) this.release(now); this.locked = i; this.lockedSince = now; this.closedAt = 0; this.peak[i] = this.power[i] - this.floor[i]; }
  release(now) {
    const i = this.locked; if (i < 0) return;
    this.events.push({ freq: this.freqs[i], t0: this.lockedSince, dur: (now - this.lockedSince) / 1000, peak: this.peak[i] });
    this.locked = -1; this.peak[i] = -200;
  }
  status() {
    const ev = this.events; this.events = [];
    return { freqs: this.freqs, over: Array.from(this.power, (p, i) => +(p - this.floor[i]).toFixed(1)), open: Array.from(this.open),
      locked: this.locked >= 0 ? this.freqs[this.locked] : null, events: ev, lockouts: [...this.lockouts] };
  }
}

// ---------------------------------------------------------------- receiver
class Receiver {
  constructor(cfg) {
    this.rate = cfg.rate; this.center = cfg.center; this.mode = cfg.mode || 'wfm'; this.freq = cfg.freq;
    const bw = this.mode === 'wfm' ? 180e3 : this.mode === 'nfm' ? 40e3 : 30e3;
    this.fe = new FrontEnd(this.rate, this.freq - this.center, bw);
    const fs = this.fe.chanRate;
    this.demod = this.mode === 'wfm' ? new WfmDemod(fs) : this.mode === 'nfm' ? new NfmDemod(fs, cfg.nfmBw || 12.5e3)
      : this.mode === 'am' ? new AmDemod(fs) : new SsbDemod(fs, this.mode);
    // data tap for multimon-ng: NFM feeds the raw discriminator (FSK data), AM/SSB/CW feed the demodulated audio (tones)
    if (cfg.pager && this.mode === 'nfm') this.demod.pager = new PagerTap(fs);
    else if (cfg.pager && this.mode !== 'wfm') this.audioTap = new PagerTap(AUDIO_RATE, 0.5);
    if (cfg.ctcss && this.mode === 'nfm') this.demod.ctcss = new Ctcss(fs);
    this.id = cfg.id || 0; this.dataOnly = !!cfg.dataOnly;
    this.pagerDec = null;                                        // PagerDecoder, attached by the worker glue once the WASM is loaded
    this.squelchDb = cfg.squelch ?? -200; this.level = -100; this.open = true;
    this.busyMs = 0; this.inSamples = 0;
    this.bank = cfg.scan ? new ChannelBank({ ...cfg.scan, bw: bw / 2 }, this.rate, this.center) : null;
    this.bankIdx = -1;
  }
  tune(freq) { this.freq = freq; this.fe.setOffset(freq - this.center); }
  process(iq8) {
    const t = performance.now();
    if (this.bank) {
      const i = this.bank.update(iq8, t);
      if (i !== this.bankIdx) { this.bankIdx = i; if (i >= 0) this.tune(this.bank.freqs[i]); }
      if (i < 0) { this.busyMs += performance.now() - t; this.inSamples += iq8.length / 2; return { L: new Float32Array(0), R: new Float32Array(0), stereo: false }; }
    }
    const [I, Q] = this.fe.process(iq8);
    this.inSamples += iq8.length / 2;
    if (!I.length) return null;
    const pw = power(I, Q);
    const db = 10 * Math.log10(pw + 1e-12);
    this.level += (db - this.level) * 0.3;
    this.open = this.level > this.squelchDb;
    const r = this.demod.process(I, Q);
    if (!this.open) { r.L.fill(0); r.R.fill(0); }
    if (this.pagerDec && this.demod.pagerOut) this.pagerDec.feed(this.demod.pagerOut);   // never squelched: the decoder has its own sync
    else if (this.pagerDec && this.audioTap) this.pagerDec.feed(this.audioTap.process(r.L));
    this.busyMs += performance.now() - t;
    return r;
  }
  status() {
    const inS = this.inSamples / this.rate, cpu = inS ? this.busyMs / 1000 / inS : 0;
    this.busyMs = 0; this.inSamples = 0;
    const s = { level: this.level, open: this.open, cpu, chanRate: this.fe.chanRate, freq: this.freq, bank: this.bank ? this.bank.status() : null, ...this.demod.status() };
    if (this.pagerDec) s.pager = { count: this.pagerDec.count, lastT: this.pagerDec.lastT };
    if (this.demod.ctcss) s.ctcss = this.demod.ctcss.tone;
    return s;
  }
}

// ---------------------------------------------------------------- worker glue
if (typeof importScripts === 'function') {
  let rx = null, disp = null, specAcc = null, specCount = 0, lastStatus = 0;
  function spectrum(iq8) {
    if (iq8.length < 4096) return;
    disp = disp || new Spectrum(2048);
    const db = disp.db(iq8);
    if (!specAcc) specAcc = new Float32Array(2048).fill(-200);
    for (let i = 0; i < 2048; i++) if (db[i] > specAcc[i]) specAcc[i] = db[i];
    specCount++;
  }
  // rtl433-live.js (IqChannelizer, Rtl433Live) is plain JS: load it now; the rtl_433 WASM itself is fetched on first use
  try { importScripts('rtl433-live.js'); } catch (e) { /* ISM decoder stays unavailable */ }
  // multimon-ng WASM for the pager feed, loaded on first use and kept; one decoder instance per Receiver
  let mmWasm = null, mmLoading = null;
  async function pagerAttach(r, demods) {
    try {
      if (!mmWasm) {
        mmLoading = mmLoading || (async () => { importScripts('tools/multimon-ng.js'); return (await fetch('tools/multimon-ng.wasm')).arrayBuffer(); })();
        mmWasm = await mmLoading;
      }
      if ((rx !== r && !extra.has(r.id)) || !(r.demod.pager || r.audioTap)) return;   // stopped or retuned while loading
      r.pagerDec = new PagerDecoder(self.create_multimon, mmWasm, demods, msg => postMessage({ pager: { ...msg, vfo: r.id, freq: r.freq } }));
      postMessage({ pagerReady: demods, demods: r.pagerDec.n, vfo: r.id });
    } catch (err) { postMessage({ pagerError: String(err), vfo: r.id }); }
  }
  // extra data-only VFOs (pager/APRS/... decoders on other channels of the same capture)
  const extra = new Map();
  function closeRx(r) { if (r && r.pagerDec) { r.pagerDec.close(); r.pagerDec = null; } if (r && r.ism) r.ism = null; }
  // ISM sensors via rtl_433 (web/rtl433-live.js): channelize the raw capture to 250 k (433 MHz) or 1 M (868 MHz wM-Bus)
  let rtlWasm = null, rtlLoading = null;
  class IsmVfo {
    constructor(id, freq, rate, center) {
      this.id = id; this.freq = freq; this.ismRate = freq > 600e6 ? 1e6 : 250e3; this.ism = null; this.count = 0; this.level = -100;
      this.ch = new IqChannelizer(rate, freq - center, this.ismRate);
      this.attach();
    }
    async attach() {
      await null;                                                // let the caller register this VFO first
      try {
        if (!rtlWasm) {
          rtlLoading = rtlLoading || (async () => { importScripts('tools/rtl_433.js'); return (await fetch('tools/rtl_433.wasm')).arrayBuffer(); })();
          rtlWasm = await rtlLoading;
        }
        if (extra.get(this.id) !== this) return;
        this.ism = new Rtl433Live(self.create_rtl433, rtlWasm, { rate: this.ismRate, centerHz: this.freq, onMessage: m => { this.count++; postMessage({ ism: { ...m, vfo: this.id } }); } });
        postMessage({ pagerReady: `rtl_433 @ ${this.ismRate / 1e3} k`, vfo: this.id });
      } catch (err) { postMessage({ pagerError: String(err), vfo: this.id }); }
    }
    process(iq8) {
      const [I, Q] = this.ch.process(iq8);
      if (I.length) { const p = power(I, Q); this.level += (10 * Math.log10(p + 1e-12) - this.level) * 0.3; }
      if (this.ism) this.ism.feed(I, Q);
    }
    status() { return { level: this.level, pager: { count: this.count, lastT: 0 }, cpu: this.ism ? this.ism.stats.msPerChunk : 0 }; }
  }
  onmessage = e => {
    const m = e.data;
    if (m.cmd === 'start') {
      closeRx(rx); for (const r of extra.values()) closeRx(r); extra.clear();
      rx = new Receiver(m); specAcc = null; specCount = 0; postMessage({ started: true, chanRate: rx.fe.chanRate });
      if (m.pager === 'RTL433') extra.set(0, new IsmVfo(0, m.freq, rx.rate, rx.center));
      else if (m.pager && (rx.demod.pager || rx.audioTap)) pagerAttach(rx, m.pager);
      return;
    }
    if (m.cmd === 'stop') { closeRx(rx); rx = null; for (const r of extra.values()) closeRx(r); extra.clear(); return; }
    if (!rx) return;
    if (m.cmd === 'vfo') {                                       // {id, freq, mode, pager, nfmBw}
      closeRx(extra.get(m.id));
      if (m.pager === 'RTL433') { extra.set(m.id, new IsmVfo(m.id, m.freq, rx.rate, rx.center)); return; }
      const r = new Receiver({ ...m, rate: rx.rate, center: rx.center, dataOnly: true, squelch: -200 });
      extra.set(m.id, r); if (m.pager) pagerAttach(r, m.pager); return;
    }
    if (m.cmd === 'vfo-del') { closeRx(extra.get(m.id)); extra.delete(m.id); return; }
    if (m.cmd === 'tune') { rx.tune(m.freq); return; }
    if (m.cmd === 'squelch') { rx.squelchDb = m.db; return; }
    if (m.cmd === 'fill') {        // buffer above target -> make fewer samples (positive trim), below -> more
      const ppm = Math.max(-3000, Math.min(3000, (m.fill - 0.3) * 4000));
      for (const r of [rx.demod.rsL, rx.demod.rsR, rx.demod.rs, rx.demod.rsQ]) if (r) r.trim(ppm);
      return;
    }
    if (m.cmd === 'bank' && rx.bank) { if (m.thresholdDb !== undefined) rx.bank.thresholdDb = m.thresholdDb; if (m.holdMs !== undefined) rx.bank.holdMs = m.holdMs; if (m.priority !== undefined) rx.bank.priority = m.priority; if (m.lockouts) rx.bank.lockouts = new Set(m.lockouts); return; }
    if (m.cmd === 'stereo') { if (rx.demod.stereoOn !== undefined) rx.demod.stereoOn = m.on; return; }
    if (m.cmd === 'iq') {
      const iq8 = new Int8Array(m.buf);
      if (specCount < 8) spectrum(iq8);
      const r = rx.process(iq8);
      for (const x of extra.values()) x.process(iq8);
      if (r && r.L.length) {
        const n = r.L.length, out = new Float32Array(2 * n);
        for (let k = 0; k < n; k++) { out[2 * k] = r.L[k]; out[2 * k + 1] = r.R[k]; }
        postMessage({ audio: out.buffer }, [out.buffer]);
      }
      const now = performance.now();
      if (now - lastStatus > 250) {
        lastStatus = now;
        const msg = { status: rx.status() };
        if (extra.size) { msg.status.vfos = {}; for (const [id, x] of extra) { const s = x.status(); msg.status.vfos[id] = { freq: x.freq, level: s.level, ctcss: s.ctcss, pager: s.pager, cpu: s.cpu }; } }
        if (specAcc && specCount >= 8) { msg.spectrum = specAcc; specAcc = null; specCount = 0; }
        postMessage(msg);
      }
    }
  };
} else if (typeof module !== 'undefined') {
  module.exports = { Receiver, FrontEnd, WfmDemod, RdsDecoder, ChannelBank, Spectrum, PagerTap, PagerDecoder, rdsSyndrome, lowpassTaps, decimPlan };
}
