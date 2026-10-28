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
  constructor(inRate, outRate) { this.step = inRate / outRate; this.pos = 0; this.last = 0; }
  process(x) {
    const out = []; let pos = this.pos;
    while (pos < x.length) {
      const i = Math.floor(pos), f = pos - i;
      const a = i < 0 ? this.last : x[i], b = x[Math.min(i + 1, x.length - 1)];
      out.push(a + (b - a) * f); pos += this.step;
    }
    this.pos = pos - x.length; this.last = x[x.length - 1];
    return Float32Array.from(out);
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
  }
  process(I, Q) {
    const m = this.disc.process(I, Q), n = m.length, s = this.devScale;
    const lr = new Float32Array(n), rdsI = new Float32Array(n), rdsQ = new Float32Array(n);
    const p = this.pll; let ph = p.phase, fr = p.freq, lock = p.lock;
    for (let k = 0; k < n; k++) {
      const x = m[k] * s; m[k] = x;
      const pil = this.pilotBpf.run(x);
      const e = pil * -Math.sin(ph);
      lock += (pil * Math.cos(ph) - lock) * 0.0005;
      fr += p.beta * e; ph += fr + p.alpha * e;
      if (ph > Math.PI) ph -= 2 * Math.PI;
      lr[k] = x * 2 * Math.cos(2 * ph);
      rdsI[k] = x * Math.cos(3 * ph); rdsQ[k] = x * Math.sin(3 * ph);
    }
    p.phase = ph; p.freq = fr; p.lock = lock;
    // pilot is ~9% of deviation -> lock ~0.045 when locked; the band-pass halves it roughly
    const stereo = this.stereoOn && lock > 0.012;
    const mono = this.lpfM.process(m), diff = this.lpfS.process(lr);
    const L = new Float32Array(mono.length), R = new Float32Array(mono.length);
    const dl = this.deemL, dr = this.deemR;
    for (let k = 0; k < mono.length; k++) {
      const a = stereo ? (mono[k] + diff[k]) : mono[k], b = stereo ? (mono[k] - diff[k]) : mono[k];
      dl.y += (a - dl.y) * (1 - dl.a); dr.y += (b - dr.y) * (1 - dr.a);
      L[k] = dl.y; R[k] = dr.y;
    }
    this.rds.process(rdsI, rdsQ);
    return { L: this.rsL.process(L), R: this.rsR.process(R), stereo, pilot: lock };
  }
  status() { return { stereo: this.pll.lock > 0.012, pilot: this.pll.lock, rds: this.rds.status() }; }
}

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
    for (let k = 0; k < d.length; k++) { d[k] *= this.devScale; const y = this.deem.y + (d[k] - this.deem.y) * (1 - this.deem.a); this.deem.y = y; d[k] = y; }
    const a = this.rs.process(this.lpf.process(d));
    return { L: a, R: a, stereo: false };
  }
  status() { return {}; }
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
    const bw = this.mode === 'wfm' ? 220e3 : this.mode === 'nfm' ? 40e3 : 30e3;
    this.fe = new FrontEnd(this.rate, this.freq - this.center, bw);
    const fs = this.fe.chanRate;
    this.demod = this.mode === 'wfm' ? new WfmDemod(fs) : this.mode === 'nfm' ? new NfmDemod(fs, cfg.nfmBw || 12.5e3) : new AmDemod(fs);
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
    this.busyMs += performance.now() - t;
    return r;
  }
  status() {
    const inS = this.inSamples / this.rate, cpu = inS ? this.busyMs / 1000 / inS : 0;
    this.busyMs = 0; this.inSamples = 0;
    return { level: this.level, open: this.open, cpu, chanRate: this.fe.chanRate, freq: this.freq, bank: this.bank ? this.bank.status() : null, ...this.demod.status() };
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
  onmessage = e => {
    const m = e.data;
    if (m.cmd === 'start') { rx = new Receiver(m); specAcc = null; specCount = 0; postMessage({ started: true, chanRate: rx.fe.chanRate }); return; }
    if (m.cmd === 'stop') { rx = null; return; }
    if (!rx) return;
    if (m.cmd === 'tune') { rx.tune(m.freq); return; }
    if (m.cmd === 'squelch') { rx.squelchDb = m.db; return; }
    if (m.cmd === 'bank' && rx.bank) { if (m.thresholdDb !== undefined) rx.bank.thresholdDb = m.thresholdDb; if (m.holdMs !== undefined) rx.bank.holdMs = m.holdMs; if (m.priority !== undefined) rx.bank.priority = m.priority; if (m.lockouts) rx.bank.lockouts = new Set(m.lockouts); return; }
    if (m.cmd === 'stereo') { if (rx.demod.stereoOn !== undefined) rx.demod.stereoOn = m.on; return; }
    if (m.cmd === 'iq') {
      const iq8 = new Int8Array(m.buf);
      if (specCount < 8) spectrum(iq8);
      const r = rx.process(iq8);
      if (r && r.L.length) {
        const n = r.L.length, out = new Float32Array(2 * n);
        for (let k = 0; k < n; k++) { out[2 * k] = r.L[k]; out[2 * k + 1] = r.R[k]; }
        postMessage({ audio: out.buffer }, [out.buffer]);
      }
      const now = performance.now();
      if (now - lastStatus > 250) {
        lastStatus = now;
        const msg = { status: rx.status() };
        if (specAcc && specCount >= 8) { msg.spectrum = specAcc; specAcc = null; specCount = 0; }
        postMessage(msg);
      }
    }
  };
} else if (typeof module !== 'undefined') {
  module.exports = { Receiver, FrontEnd, WfmDemod, RdsDecoder, ChannelBank, Spectrum, rdsSyndrome, lowpassTaps, decimPlan };
}
