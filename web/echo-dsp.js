// Echo tab: shared signal processing (no DOM, no WebUSB). Runs in the page, in echo-worker.js and in node tests.
//
//  * NarrowChannelizer: 8-bit IQ at the hardware rate (2..20 Msps) -> NCO -> two FIR decimation stages -> 50 ksps complex
//  * classifyDmrLike: activity / unknown digital / possible DMR / confirmed DMR (physical-layer sync only, no decoding)
//  * toCi8 / fromCi8: compact storage with clip statistics
//  * TxInterpolator: 50 ksps complex -> TX hardware rate, anti-imaging FIR, LO-offset NCO, limiter, int8
//  * SweepParser: the firmware sweep stream (16 KiB blocks, 7F 7F + u64 frequency) -> power bins, as the Scan tab does
//  * synth: deterministic DMR-like 4FSK, analogue FM and noise generators for tests and demos
//
// "DMR-like" synthetic signals use the public ETSI TS 102 361-1 sync words with random payload: they are NOT valid DMR
// frames and carry no voice or data.
"use strict";
(function (root, factory) {
  const m = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = m; else root.EchoDsp = m;
})(typeof self !== 'undefined' ? self : this, function () {

  const TWO_PI = 2 * Math.PI;
  const ECHO_RATE = 50e3;               // compact IQ rate (hypothesis, see docs: 100/50/25 k are test variants)
  const SYMBOL_RATE = 4800;             // DMR: 4FSK, 4800 symbols/s, 2 bits/symbol
  const DEV_UNIT = 648;                 // Hz per symbol unit: +-1 -> +-648 Hz, +-3 -> +-1944 Hz

  // ------------------------------------------------------------ deterministic random
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  function gauss(rnd) { let u = 0; while (u === 0) u = rnd(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(TWO_PI * rnd()); }

  // ------------------------------------------------------------ filters
  function lowpassTaps(n, cutoff, fs) {          // Blackman windowed sinc, unity DC gain
    const h = new Float32Array(n), m = (n - 1) / 2, fc = cutoff / fs;
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const x = i - m, sinc = x === 0 ? 2 * fc : Math.sin(TWO_PI * fc * x) / (Math.PI * x);
      const w = 0.42 - 0.5 * Math.cos(TWO_PI * i / (n - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (n - 1));
      h[i] = sinc * w; sum += h[i];
    }
    for (let i = 0; i < n; i++) h[i] /= sum;
    return h;
  }
  function rrcTaps(sps, span, alpha) {             // root raised cosine, unit energy
    const n = sps * span + 1, h = new Float32Array(n), m = (n - 1) / 2; let e = 0;
    for (let i = 0; i < n; i++) {
      const t = (i - m) / sps; let v;
      if (Math.abs(t) < 1e-9) v = 1 - alpha + 4 * alpha / Math.PI;
      else if (Math.abs(Math.abs(t) - 1 / (4 * alpha)) < 1e-9) v = alpha / Math.SQRT2 * ((1 + 2 / Math.PI) * Math.sin(Math.PI / (4 * alpha)) + (1 - 2 / Math.PI) * Math.cos(Math.PI / (4 * alpha)));
      else v = (Math.sin(Math.PI * t * (1 - alpha)) + 4 * alpha * t * Math.cos(Math.PI * t * (1 + alpha))) / (Math.PI * t * (1 - (4 * alpha * t) ** 2));
      h[i] = v; e += v * v;
    }
    const g = 1 / Math.sqrt(e); for (let i = 0; i < n; i++) h[i] *= g;
    return h;
  }
  // FIR + decimate on one real stream, history kept across calls
  class FirDecim {
    constructor(taps, D) { this.h = taps; this.D = D; this.n = taps.length; this.hist = new Float32Array(this.n - 1 + 65536); this.have = this.n - 1; }
    process(x) {
      const { h, D, n } = this;
      if (this.hist.length < this.have + x.length) { const b = new Float32Array(this.have + x.length + 65536); b.set(this.hist.subarray(0, this.have)); this.hist = b; }
      this.hist.set(x, this.have); const total = this.have + x.length;
      const nOut = Math.max(0, Math.floor((total - (n - 1)) / D)), out = new Float32Array(nOut), hb = this.hist;
      for (let o = 0, p = n - 1; o < nOut; o++, p += D) {
        let a = 0;
        for (let k = 0; k < n; k++) a += h[k] * hb[p - k];
        out[o] = a;
      }
      const used = nOut * D;
      this.hist.copyWithin(0, used, total); this.have = total - used;
      return out;
    }
  }
  class FirDecimC { constructor(t, D) { this.i = new FirDecim(t, D); this.q = new FirDecim(t, D); } process(I, Q) { return [this.i.process(I), this.q.process(Q)]; } }
  function firFilterC(I, Q, taps) { const f = new FirDecimC(taps, 1), d = (taps.length - 1) >> 1; const [a, b] = f.process(concatPad(I, d), concatPad(Q, d)); return [a.subarray(d), b.subarray(d)]; }
  function concatPad(x, d) { const o = new Float32Array(x.length + d); o.set(x); return o; }

  // ------------------------------------------------------------ channelizer
  // stage plan for inRate -> outRate (integer ratio): first stage to ~1 MHz with a short filter (only the
  // regions that alias onto +-outRate/2 must be rejected), second stage to outRate with the channel filter
  function channelizerPlan(inRate, outRate = ECHO_RATE) {
    const D = Math.round(inRate / outRate);
    if (Math.abs(D * outRate - inRate) > 1e-6 * inRate) throw new Error(`rate ${inRate} is not an integer multiple of ${outRate}`);
    let D1 = 1;
    for (let d = 1; d <= D; d++) if (D % d === 0 && inRate / d >= 400e3 && D / d >= 5) D1 = d;
    return { D, D1, D2: D / D1, mid: inRate / D1 };
  }
  class NarrowChannelizer {
    constructor(inRate, offsetHz, { outRate = ECHO_RATE, cutoff = 20e3 } = {}) {
      this.inRate = inRate; this.outRate = outRate; this.phase = 0; this.setOffset(offsetHz);
      const p = channelizerPlan(inRate, outRate); this.plan = p;
      this.s1 = p.D1 > 1 ? new FirDecimC(lowpassTaps(4 * p.D1, Math.min(0.15 * p.mid, 250e3), inRate), p.D1) : null;
      // channel stage: flat to +-cutoff, transition to (outRate - cutoff); length from the Blackman transition width
      const trans = Math.max(5e3, outRate - 2 * cutoff + 10e3), n2 = Math.min(511, Math.max(31, Math.ceil(5.5 * p.mid / trans) | 1));
      this.s2 = new FirDecimC(lowpassTaps(n2, cutoff + trans / 4, p.mid), p.D2);
    }
    setOffset(hz) { this.offset = hz; this.dphi = -TWO_PI * hz / this.inRate; }
    // iq: Int8Array (HackRF, full scale 128) or Float32Array (already scaled) interleaved
    process(iq) {
      const n = iq.length >> 1, I = new Float32Array(n), Q = new Float32Array(n), s = iq instanceof Int8Array ? 1 / 128 : 1;
      let c = Math.cos(this.phase), sn = Math.sin(this.phase);
      const dc = Math.cos(this.dphi), ds = Math.sin(this.dphi);
      for (let i = 0, j = 0; i < n; i++, j += 2) {
        const a = iq[j] * s, b = iq[j + 1] * s;
        I[i] = a * c - b * sn; Q[i] = a * sn + b * c;
        const nc = c * dc - sn * ds; sn = c * ds + sn * dc; c = nc;
        if ((i & 1023) === 1023) { const g = 1 / Math.sqrt(c * c + sn * sn); c *= g; sn *= g; }
      }
      this.phase = Math.atan2(sn, c);
      let r = [I, Q];
      if (this.s1) r = this.s1.process(r[0], r[1]);
      return this.s2.process(r[0], r[1]);
    }
  }

  // complex ring buffer (pre-roll)
  class ComplexRing {
    constructor(cap) { this.cap = Math.max(1, cap | 0); this.I = new Float32Array(this.cap); this.Q = new Float32Array(this.cap); this.w = 0; this.n = 0; }
    push(I, Q) {
      for (let k = 0; k < I.length; k++) { this.I[this.w] = I[k]; this.Q[this.w] = Q[k]; this.w = (this.w + 1) % this.cap; }
      this.n = Math.min(this.cap, this.n + I.length);
    }
    read(count = this.n) {
      count = Math.min(count, this.n);
      const I = new Float32Array(count), Q = new Float32Array(count);
      let r = (this.w - count + this.cap) % this.cap;
      for (let k = 0; k < count; k++) { I[k] = this.I[r]; Q[k] = this.Q[r]; r = (r + 1) % this.cap; }
      return [I, Q];
    }
    clear() { this.n = 0; this.w = 0; }
  }
  // growable complex float buffer
  class ComplexBuf {
    constructor(cap = 65536) { this.I = new Float32Array(cap); this.Q = new Float32Array(cap); this.n = 0; }
    push(I, Q) {
      if (this.n + I.length > this.I.length) { const c = Math.max(this.n + I.length, this.I.length * 2); const a = new Float32Array(c), b = new Float32Array(c); a.set(this.I.subarray(0, this.n)); b.set(this.Q.subarray(0, this.n)); this.I = a; this.Q = b; }
      this.I.set(I, this.n); this.Q.set(Q, this.n); this.n += I.length;
    }
    take(n = this.n) { return [this.I.slice(0, n), this.Q.slice(0, n)]; }
  }

  const power = (I, Q, a = 0, b = I.length) => { let p = 0; for (let k = a; k < b; k++) p += I[k] * I[k] + Q[k] * Q[k]; return b > a ? p / (b - a) : 0; };
  const db = p => 10 * Math.log10(p + 1e-20);

  // ------------------------------------------------------------ compact storage: ci8
  // Floats are in HackRF full-scale units (int8 / 128). The stored int8 is round(x * gain * 127) with gain chosen so
  // the peak lands at `target` of full scale; `gain` goes into the metadata so absolute level stays recoverable.
  function toCi8(I, Q, { target = 0.8, maxGain = 64, gain } = {}) {
    let peak = 0;
    for (let k = 0; k < I.length; k++) { const a = Math.abs(I[k]), b = Math.abs(Q[k]); if (a > peak) peak = a; if (b > peak) peak = b; }
    const g = gain !== undefined ? gain : (peak > 0 ? Math.min(maxGain, target / peak) : 1);
    const out = new Int8Array(I.length * 2); let clipped = 0;
    for (let k = 0, j = 0; k < I.length; k++, j += 2) {
      let a = Math.round(I[k] * g * 127), b = Math.round(Q[k] * g * 127);
      if (a > 127 || a < -127) { clipped++; a = a > 0 ? 127 : -127; }
      if (b > 127 || b < -127) { clipped++; b = b > 0 ? 127 : -127; }
      out[j] = a; out[j + 1] = b;
    }
    return { data: out, gain: g, peak, clipped, clipFraction: I.length ? clipped / (2 * I.length) : 0 };
  }
  function fromCi8(data, gain = 1) {
    const n = data.length >> 1, I = new Float32Array(n), Q = new Float32Array(n), s = 1 / (127 * gain);
    for (let k = 0; k < n; k++) { I[k] = data[2 * k] * s; Q[k] = data[2 * k + 1] * s; }
    return [I, Q];
  }

  // ------------------------------------------------------------ FFT (radix 2, in place)
  class FFT {
    constructor(n) {
      this.n = n; this.re = new Float32Array(n); this.im = new Float32Array(n);
      this.cos = new Float32Array(n / 2); this.sin = new Float32Array(n / 2);
      for (let i = 0; i < n / 2; i++) { this.cos[i] = Math.cos(TWO_PI * i / n); this.sin[i] = Math.sin(TWO_PI * i / n); }
      this.rev = new Uint32Array(n); const bits = Math.log2(n);
      for (let i = 0; i < n; i++) { let r = 0; for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b); this.rev[i] = r; }
      this.win = new Float32Array(n); let s = 0;
      for (let i = 0; i < n; i++) { this.win[i] = 0.5 * (1 - Math.cos(TWO_PI * i / (n - 1))); s += this.win[i]; }
      this.norm = 1 / (s * s);
    }
    run() {
      const { n, re, im, cos, sin, rev } = this;
      for (let i = 0; i < n; i++) { const j = rev[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
      for (let size = 2; size <= n; size <<= 1) {
        const half = size >> 1, step = n / size;
        for (let i = 0; i < n; i += size) for (let j = i, k = 0; j < i + half; j++, k += step) {
          const l = j + half, c = cos[k], s = sin[k], tr = re[l] * c + im[l] * s, ti = im[l] * c - re[l] * s;
          re[l] = re[j] - tr; im[l] = im[j] - ti; re[j] += tr; im[j] += ti;
        }
      }
    }
    // windowed power spectrum, fftshifted (index n/2 = DC), added into acc
    powerInto(acc, getI, getQ, at) {
      const { n, re, im, win, norm } = this;
      for (let i = 0; i < n; i++) { re[i] = getI(at + i) * win[i]; im[i] = getQ(at + i) * win[i]; }
      this.run();
      const h = n >> 1;
      for (let i = 0; i < n; i++) acc[(i + h) & (n - 1)] += (re[i] * re[i] + im[i] * im[i]) * norm;
    }
  }
  const fftCache = {};
  const getFFT = n => fftCache[n] || (fftCache[n] = new FFT(n));
  // averaged power spectrum of complex float arrays (fftshifted, linear)
  function psd(I, Q, n = 1024, maxFrames = 64) {
    const f = getFFT(n), acc = new Float64Array(n), frames = Math.max(1, Math.min(maxFrames, Math.floor(I.length / n)));
    const stride = frames > 1 ? Math.floor((I.length - n) / (frames - 1)) : 0;
    if (I.length < n) return acc;
    for (let k = 0; k < frames; k++) f.powerInto(acc, i => I[i], i => Q[i], k * stride);
    for (let i = 0; i < n; i++) acc[i] /= frames;
    return acc;
  }

  // ------------------------------------------------------------ DMR-like physical layer
  // ETSI TS 102 361-1 sync words (48 bits = 24 dibits, all outer symbols +-3). Dibit -> symbol: 01 +3, 00 +1, 10 -1, 11 -3.
  const DMR_SYNC = {
    bs_voice: '755FD7DF75F7', bs_data: 'DFF57D75DF5D', ms_voice: '7F7D5DD57DFD', ms_data: 'D5D7F77FD757',
  };
  const DIBIT_SYM = [1, 3, -1, -3];      // index = dibit value (00, 01, 10, 11)
  function hexToSymbols(hex) {
    const out = [];
    for (const ch of hex) { const v = parseInt(ch, 16); out.push(DIBIT_SYM[(v >> 2) & 3], DIBIT_SYM[v & 3]); }
    return out;
  }
  const SYNC_SYMS = Object.fromEntries(Object.entries(DMR_SYNC).map(([k, v]) => [k, hexToSymbols(v)]));

  function discriminator(I, Q, rate) {          // instantaneous frequency in Hz
    const f = new Float32Array(I.length), k = rate / TWO_PI; let pi = I[0] || 1, pq = Q[0] || 0;
    for (let n = 0; n < I.length; n++) { const i = I[n], q = Q[n]; f[n] = Math.atan2(q * pi - i * pq, i * pi + q * pq) * k; pi = i; pq = q; }
    return f;
  }
  function resampleLinear(x, inRate, outRate) {
    const step = inRate / outRate, n = Math.floor((x.length - 1) / step), out = new Float32Array(Math.max(0, n));
    for (let k = 0; k < n; k++) { const p = k * step, i = Math.floor(p), u = p - i; out[k] = x[i] + (x[i + 1] - x[i]) * u; }
    return out;
  }
  function goertzelPower(x, f, fs) {
    const w = TWO_PI * f / fs, c = 2 * Math.cos(w); let s1 = 0, s2 = 0;
    for (let n = 0; n < x.length; n++) { const s = x[n] + c * s1 - s2; s2 = s1; s1 = s; }
    return (s1 * s1 + s2 * s2 - c * s1 * s2) / Math.max(1, x.length);
  }

  // Classify a narrowband complex capture (rate ~50 ksps, channel at 0 Hz).
  //   floorDb: noise power per sample in the channel filter (dB, same units as `power`) if the caller knows it
  // Returns label: 'none' | 'activity' | 'unknown_digital' | 'possible_dmr' | 'confirmed_dmr' plus the features.
  const CLASS_DEFAULTS = { thresholdDb: 10, chanCutoff: 7e3, minSyncHits: 2, maxSyncErrors: 2 };
  function classifyDmrLike(I, Q, rate, opts = {}) {
    const o = { ...CLASS_DEFAULTS, ...opts };
    const res = { label: 'none', snrDb: -Infinity, duty: 0, devHz: 0, fourLevel: 0, symLine: 0, syncHits: 0, syncTypes: {}, inverted: false, freqOffsetHz: 0, activeS: 0 };
    if (I.length < rate * 0.02) return res;
    const taps = lowpassTaps(Math.min(255, Math.ceil(5.5 * rate / 6e3) | 1), o.chanCutoff, rate);
    const [ci, cq] = firFilterC(I, Q, taps);
    // block powers (5 ms) -> floor -> active mask
    const B = Math.max(8, Math.round(rate * 0.005)), nb = Math.floor(ci.length / B), bp = new Float32Array(nb);
    for (let b = 0; b < nb; b++) bp[b] = db(power(ci, cq, b * B, (b + 1) * B));
    const sorted = Array.from(bp).filter(v => v > -150).sort((a, b) => a - b);      // digital silence (padding) is not a noise floor
    if (!sorted.length) return res;
    const floor = Number.isFinite(o.floorDb) ? o.floorDb : sorted[Math.floor(sorted.length * 0.1)];
    const act = bp.map(v => v > floor + o.thresholdDb ? 1 : 0);
    let nAct = 0, sig = 0; for (let b = 0; b < nb; b++) if (act[b]) { nAct++; sig += 10 ** (bp[b] / 10); }
    res.duty = nb ? nAct / nb : 0; res.activeS = nAct * B / rate;
    if (!nAct) { res.snrDb = (sorted[sorted.length - 1] || -200) - floor; return res; }
    res.snrDb = db(sig / nAct) - floor;
    if (res.activeS < 0.02) { res.label = 'activity'; return res; }
    // discriminator over the active blocks only (joined runs), resampled to 10 samples/symbol
    const f = discriminator(ci, cq, rate), runsF = [];
    for (let b = 0; b < nb;) {
      if (!act[b]) { b++; continue; }
      let e = b; while (e < nb && act[e]) e++;
      if (e - b >= 5) runsF.push(f.subarray((b + 1) * B, (e - 1) * B));     // drop the edge blocks (key-up/down transients)
      b = e;
    }
    if (!runsF.length) { res.label = 'activity'; return res; }
    const fsS = SYMBOL_RATE * 10, mf = rrcTaps(10, 8, 0.2), mfD = (mf.length - 1) >> 1;   // matched RRC filter
    let fourHits = 0, fourTot = 0, outerSum = 0, outerN = 0, lineAcc = 0, refAcc = 0, offAcc = 0, offN = 0;
    const syncPatterns = Object.entries(SYNC_SYMS);
    for (const fr of runsF) {
      let x = resampleLinear(fr, rate, fsS);
      if (x.length < 200 + 2 * mfD) continue;
      // carrier offset = mean frequency (before the matched filter, whose DC gain is not 1)
      let mean = 0; for (const v of x) mean += v; mean /= x.length; offAcc += mean * x.length; offN += x.length;
      for (let k = 0; k < x.length; k++) x[k] -= mean;
      x = new FirDecim(mf, 1).process(concatPad(x, mfD)).subarray(2 * mfD, x.length);     // also drop the filter start-up
      // symbol clock: energy vs sampling phase has a clear maximum for a symbol stream (eye opening), flat for FM audio
      // symbol-rate line: |d/dt x|^2 has a spectral line at 4800 Hz for a symbol stream, none for FM audio
      const d2 = new Float32Array(x.length - 1); let dm = 0;
      for (let k = 1; k < x.length; k++) { const v = x[k] - x[k - 1]; d2[k - 1] = v * v; dm += v * v; }
      dm /= d2.length; for (let k = 0; k < d2.length; k++) d2[k] -= dm;
      lineAcc += goertzelPower(d2, SYMBOL_RATE, fsS) * d2.length;
      refAcc += (goertzelPower(d2, 3500, fsS) + goertzelPower(d2, 4150, fsS) + goertzelPower(d2, 5450, fsS) + goertzelPower(d2, 6100, fsS)) / 4 * d2.length + 1e-12;
      // per 200 ms segment: best sampling phase (max energy), 4-level fit, sync search
      const seg = 2000;
      for (let s0 = 0; s0 + 240 < x.length; s0 += seg) {
        const s1 = Math.min(x.length, s0 + seg);
        let bestP = 0, bestE = -1;
        for (let p = 0; p < 10; p++) { let e = 0; for (let k = s0 + p; k < s1; k += 10) e += x[k] * x[k]; if (e > bestE) { bestE = e; bestP = p; } }
        const sy = []; for (let k = s0 + bestP; k < s1; k += 10) sy.push(x[k]);
        const abs = sy.map(Math.abs).sort((a, b) => a - b), med = abs[abs.length >> 1] || 1;
        const outer = abs.filter(v => v > med); const outerLvl = outer.length ? outer[outer.length >> 1] : med;
        if (!(outerLvl > 0)) continue;
        outerSum += outerLvl * sy.length; outerN += sy.length;
        const u = outerLvl / 3;
        for (const v of sy) { const z = v / u, near = Math.min(Math.abs(z - 3), Math.abs(z - 1), Math.abs(z + 1), Math.abs(z + 3)); fourTot++; if (near < 0.45) fourHits++; }
        // sync: hard decisions on the 24-symbol window
        const hard = sy.map(v => { const z = v / u; return z > 2 ? 3 : z > 0 ? 1 : z > -2 ? -1 : -3; });
        for (let k = 0; k + 24 <= hard.length; k++) {
          for (const [name, pat] of syncPatterns) {
            let e = 0, ei = 0;
            for (let j = 0; j < 24 && (e <= o.maxSyncErrors || ei <= o.maxSyncErrors); j++) { if (hard[k + j] !== pat[j]) e++; if (hard[k + j] !== -pat[j]) ei++; }
            if (e <= o.maxSyncErrors || ei <= o.maxSyncErrors) {
              res.syncHits++; res.syncTypes[name] = (res.syncTypes[name] || 0) + 1; if (ei < e) res.inverted = true;
              k += 23; break;
            }
          }
        }
      }
    }
    res.devHz = outerN ? outerSum / outerN : 0;
    res.fourLevel = fourTot ? fourHits / fourTot : 0;
    res.symLine = refAcc ? lineAcc / refAcc : 0;
    res.freqOffsetHz = offN ? offAcc / offN : 0;
    const devOk = res.devHz > 1944 * 0.7 && res.devHz < 1944 * 1.35;
    const digital = res.symLine > 4;
    if (res.syncHits >= o.minSyncHits && devOk) res.label = 'confirmed_dmr';
    else if (devOk && (res.fourLevel > 0.8 || (digital && res.fourLevel > 0.6))) res.label = 'possible_dmr';
    else if (digital) res.label = 'unknown_digital';
    else res.label = 'activity';
    return res;
  }
  const LABEL_RANK = { none: 0, activity: 1, unknown_digital: 2, possible_dmr: 3, confirmed_dmr: 4 };

  // ------------------------------------------------------------ synthetic signals
  // DMR-like 4FSK at `rate` (complex baseband): 30 ms slots of 144 symbols (12 CACH-like + 54 payload + 24 sync + 54 payload),
  // RRC alpha 0.2, deviation 648 Hz/unit. bursts: [{t0, dur}] in seconds; mode 'continuous' fills every slot, 'tdma'
  // keys up only every other slot (an MS-like inbound burst pattern).
  function synthDmrLike({ rate = ECHO_RATE, seconds = 1, bursts = [{ t0: 0.1, dur: 0.8 }], offsetHz = 0, amp = 0.3, noise = 0.003,
    seed = 1, sync = 'bs_voice', mode = 'continuous', invert = false, devUnit = DEV_UNIT, levels = 4 } = {}) {
    const rnd = mulberry32(seed), n = Math.round(rate * seconds), I = new Float32Array(n), Q = new Float32Array(n);
    // symbol stream at 10 samples/symbol (48 kHz), shaped, as an instantaneous frequency track
    const fsS = SYMBOL_RATE * 10, nS = Math.ceil(seconds * SYMBOL_RATE) + 20, imp = new Float32Array(nS * 10);
    const pat = SYNC_SYMS[sync] || SYNC_SYMS.bs_voice;
    for (let s = 0; s < nS; s++) {
      const slot = Math.floor(s / 144), pos = s % 144;
      let v = levels === 2 ? (rnd() < 0.5 ? 3 : -3) : DIBIT_SYM[Math.floor(rnd() * 4)];
      if (pos >= 66 && pos < 90 && levels === 4) v = pat[pos - 66];
      if (mode === 'tdma' && slot % 2) v = 0;
      imp[s * 10] = invert ? -v : v;
    }
    const rrc = rrcTaps(10, 8, 0.2), shaped = new FirDecim(rrc, 1).process(concatPad(imp, (rrc.length - 1) >> 1)).subarray((rrc.length - 1) >> 1);
    // RRC at both ends (here and in the classifier) gives a raised-cosine response: +-3 lands on +-1944 Hz there
    const gainHz = devUnit;
    // keyed envelope (2 ms ramps)
    const keyed = t => { for (const b of bursts) { if (t >= b.t0 && t < b.t0 + b.dur) { const r = Math.min(t - b.t0, b.t0 + b.dur - t); return Math.min(1, r / 0.002); } } return 0; };
    let ph = 0;
    for (let k = 0; k < n; k++) {
      const t = k / rate, p = t * fsS, i0 = Math.floor(p), u = p - i0;
      const fr = (shaped[i0] + ((shaped[i0 + 1] || 0) - shaped[i0]) * u) * gainHz;
      let a = keyed(t);
      if (mode === 'tdma') { const slot = Math.floor(t * SYMBOL_RATE / 144); if (slot % 2) a = 0; }
      ph += TWO_PI * (fr + offsetHz) / rate;
      if (ph > Math.PI) ph -= TWO_PI; else if (ph < -Math.PI) ph += TWO_PI;
      I[k] = amp * a * Math.cos(ph) + noise * gauss(rnd); Q[k] = amp * a * Math.sin(ph) + noise * gauss(rnd);
    }
    return [I, Q];
  }
  // analogue NFM "voice": a few wandering tones, 2.5 kHz deviation, optional CTCSS
  function synthFm({ rate = ECHO_RATE, seconds = 1, bursts = [{ t0: 0.1, dur: 0.8 }], offsetHz = 0, amp = 0.3, noise = 0.003, seed = 2, devHz = 2500 } = {}) {
    const rnd = mulberry32(seed), n = Math.round(rate * seconds), I = new Float32Array(n), Q = new Float32Array(n);
    const tones = [0, 1, 2].map(() => ({ f: 300 + rnd() * 2000, a: 0.2 + rnd() * 0.4, w: 0.5 + rnd() * 3 }));
    let ph = 0;
    for (let k = 0; k < n; k++) {
      const t = k / rate; let on = 0;
      for (const b of bursts) if (t >= b.t0 && t < b.t0 + b.dur) on = 1;
      let m = 0.15 * Math.sin(TWO_PI * 88.5 * t);
      for (const z of tones) m += z.a * Math.sin(TWO_PI * z.f * t + 3 * Math.sin(TWO_PI * z.w * t));
      ph += TWO_PI * (offsetHz + devHz * m / 1.4) / rate;
      if (ph > Math.PI) ph -= TWO_PI; else if (ph < -Math.PI) ph += TWO_PI;
      I[k] = amp * on * Math.cos(ph) + noise * gauss(rnd); Q[k] = amp * on * Math.sin(ph) + noise * gauss(rnd);
    }
    return [I, Q];
  }
  function synthNoise({ rate = ECHO_RATE, seconds = 1, noise = 0.003, seed = 3 } = {}) {
    const rnd = mulberry32(seed), n = Math.round(rate * seconds), I = new Float32Array(n), Q = new Float32Array(n);
    for (let k = 0; k < n; k++) { I[k] = noise * gauss(rnd); Q[k] = noise * gauss(rnd); }
    return [I, Q];
  }
  // place narrowband signals (each [I,Q] at its own rate) into a wideband int8 capture at `rate`, centre-relative offsets.
  // Narrowband tracks are linearly interpolated (they are heavily oversampled by then) and mixed up.
  let NOISE_TABLE = null;
  function noiseTable() { if (!NOISE_TABLE) { const r = mulberry32(99); NOISE_TABLE = new Float32Array(1 << 20); for (let i = 0; i < NOISE_TABLE.length; i++) NOISE_TABLE[i] = gauss(r); } return NOISE_TABLE; }
  function synthWideband({ rate = 2e6, seconds = 1, signals = [], noise = 0.004, seed = 4, t0 = 0, fastNoise = false } = {}) {
    const rnd = mulberry32(seed), n = Math.round(rate * seconds), out = new Int8Array(n * 2), acc = new Float32Array(n * 2);
    for (const s of signals) {
      const [sI, sQ] = s.iq, r = s.rate, dphi = TWO_PI * s.offsetHz / rate;
      let c = Math.cos(dphi * t0 * rate), sn = Math.sin(dphi * t0 * rate); const dc = Math.cos(dphi), ds = Math.sin(dphi);
      for (let k = 0; k < n; k++) {
        const p = (t0 + k / rate - (s.start || 0)) * r;
        if (p >= 0 && p < sI.length - 1) {
          const i0 = Math.floor(p), u = p - i0, a = sI[i0] + (sI[i0 + 1] - sI[i0]) * u, b = sQ[i0] + (sQ[i0 + 1] - sQ[i0]) * u;
          acc[2 * k] += a * c - b * sn; acc[2 * k + 1] += a * sn + b * c;
        }
        const nc = c * dc - sn * ds; sn = c * ds + sn * dc; c = nc;
        if ((k & 1023) === 1023) { const g = 1 / Math.hypot(c, sn); c *= g; sn *= g; }
      }
    }
    const tab = fastNoise ? noiseTable() : null, msk = tab ? tab.length - 1 : 0; let o = tab ? Math.floor(rnd() * tab.length) : 0;
    for (let k = 0; k < 2 * n; k++) {
      const g = tab ? tab[(o++) & msk] : gauss(rnd);
      const v = Math.round((acc[k] + noise * g) * 128); out[k] = v > 127 ? 127 : v < -128 ? -128 : v;
    }
    return out;
  }

  // ------------------------------------------------------------ TX interpolation
  // 50 ksps complex -> txRate (integer multiple) in two polyphase stages, then an NCO shifts the channel by
  // +offsetHz (the radio is tuned offsetHz below the lab frequency, keeping LO leakage out of the channel),
  // a soft limiter bounds the peak and the result is quantised to int8. Streams in chunks: pull(nOut) -> Int8Array.
  function interpPlan(inRate, outRate) {
    const L = Math.round(outRate / inRate);
    if (L < 1 || Math.abs(L * inRate - outRate) > 1e-6 * outRate) throw new Error(`TX rate ${outRate} is not an integer multiple of ${inRate}`);
    let L1 = 1; for (const d of [5, 4, 2]) if (L % d === 0 && L / d >= 2) { L1 = d; break; }
    return { L, L1, L2: L / L1 };
  }
  class PolyInterp {             // complex polyphase interpolator by L with taps designed at the output rate
    constructor(L, taps) {
      this.L = L; this.P = Math.ceil(taps.length / L); this.ph = [];
      for (let p = 0; p < L; p++) { const h = new Float32Array(this.P); for (let k = 0; k < this.P; k++) { const i = k * L + p; h[k] = i < taps.length ? taps[i] * L : 0; } this.ph.push(h); }
      this.hi = new Float32Array(this.P); this.hq = new Float32Array(this.P);
    }
    process(I, Q) {
      const { L, P, ph, hi, hq } = this, oI = new Float32Array(I.length * L), oQ = new Float32Array(I.length * L);
      for (let n = 0; n < I.length; n++) {
        hi.copyWithin(1, 0, P - 1); hq.copyWithin(1, 0, P - 1); hi[0] = I[n]; hq[0] = Q[n];
        for (let p = 0; p < L; p++) { const h = ph[p]; let a = 0, b = 0; for (let k = 0; k < P; k++) { a += h[k] * hi[k]; b += h[k] * hq[k]; } oI[n * L + p] = a; oQ[n * L + p] = b; }
      }
      return [oI, oQ];
    }
  }
  class TxInterpolator {
    constructor(I, Q, { inRate = ECHO_RATE, outRate = 2e6, offsetHz = 0, level = 0.7, chanCutoff = 9e3, padS = 0.02 } = {}) {
      this.I = I; this.Q = Q; this.inRate = inRate; this.outRate = outRate; this.plan = interpPlan(inRate, outRate);
      const { L1, L2 } = this.plan, r1 = inRate * L1;
      this.st1 = L1 > 1 ? new PolyInterp(L1, lowpassTaps(Math.ceil(5.5 * r1 / (inRate - 2 * chanCutoff)) | 1, chanCutoff + (inRate - 2 * chanCutoff) / 4, r1)) : null;
      this.st2 = L2 > 1 ? new PolyInterp(L2, lowpassTaps(Math.min(255, Math.ceil(5.5 * outRate / (r1 - 2 * chanCutoff)) | 1), Math.min(r1 / 2, chanCutoff * 2), outRate)) : null;
      this.dphi = TWO_PI * offsetHz / outRate; this.c = 1; this.s = 0;
      // normalise so the input peak maps to `level` of int8 full scale
      let pk = 0; for (let k = 0; k < I.length; k++) pk = Math.max(pk, Math.hypot(I[k], Q[k]));
      this.g = pk > 0 ? level / pk : 0; this.level = level;
      this.pos = 0; this.pad = Math.round(padS * inRate); this.queue = null; this.qPos = 0;
      this.totalOut = (I.length + 2 * this.pad) * this.plan.L; this.produced = 0; this.limited = 0; this.peak = 0;
    }
    get done() { return this.produced >= this.totalOut; }
    _block() {               // next 1024 input samples (with silent pads at both ends) -> output floats
      const n = 1024, bi = new Float32Array(n), bq = new Float32Array(n);
      for (let k = 0; k < n; k++) {
        const src = this.pos - this.pad;
        if (src >= 0 && src < this.I.length) { bi[k] = this.I[src] * this.g; bq[k] = this.Q[src] * this.g; }
        this.pos++;
      }
      let r = [bi, bq];
      if (this.st1) r = this.st1.process(r[0], r[1]);
      if (this.st2) r = this.st2.process(r[0], r[1]);
      return r;
    }
    pull(nOut) {             // -> Int8Array interleaved, length 2 * min(nOut, remaining)
      nOut = Math.min(nOut, this.totalOut - this.produced);
      const out = new Int8Array(2 * nOut); let o = 0;
      const dc = Math.cos(this.dphi), ds = Math.sin(this.dphi);
      while (o < nOut) {
        if (!this.queue || this.qPos >= this.queue[0].length) { this.queue = this._block(); this.qPos = 0; }
        const [qi, qq] = this.queue;
        for (; this.qPos < qi.length && o < nOut; this.qPos++, o++) {
          let a = qi[this.qPos] * this.c - qq[this.qPos] * this.s, b = qi[this.qPos] * this.s + qq[this.qPos] * this.c;
          const m = Math.hypot(a, b);
          if (m > this.peak) this.peak = m;
          if (m > 0.98) { const k = 0.98 / m; a *= k; b *= k; this.limited++; }       // hard limiter: never wraps int8
          out[2 * o] = Math.round(a * 127); out[2 * o + 1] = Math.round(b * 127);
          const nc = this.c * dc - this.s * ds; this.s = this.c * ds + this.s * dc; this.c = nc;
        }
        const g = 1 / Math.hypot(this.c, this.s); this.c *= g; this.s *= g;
      }
      this.produced += nOut;
      return out;
    }
  }
  // spectrum check of generated TX samples: power in the channel vs everything else (excluding nothing: digital
  // output has no LO leakage, so any energy outside the channel is a DSP error or an image)
  function txSpectrumCheck(iq8, rate, offsetHz, { n = 4096, chanHalf = 8e3, guard = 12.5e3 } = {}) {
    const len = iq8.length >> 1, f = getFFT(n), acc = new Float64Array(n), frames = Math.max(1, Math.min(32, Math.floor(len / n)));
    const stride = frames > 1 ? Math.floor((len - n) / (frames - 1)) : 0;
    for (let k = 0; k < frames && len >= n; k++) f.powerInto(acc, i => iq8[2 * i] / 128, i => iq8[2 * i + 1] / 128, k * stride);
    const df = rate / n; let inCh = 0, worst = 0, image = 0, total = 0;
    for (let i = 0; i < n; i++) {
      const fr = (i - n / 2) * df, p = acc[i]; total += p;
      if (Math.abs(fr - offsetHz) <= chanHalf) inCh += p;
      else if (Math.abs(fr - offsetHz) > chanHalf + guard && p > worst) worst = p;
      if (Math.abs(fr + offsetHz) <= chanHalf && offsetHz !== 0) image += p;
    }
    const binsCh = Math.max(1, Math.round(2 * chanHalf / df));
    return { inChannelDb: db(inCh), worstSpurDbc: db(worst) - db(inCh / binsCh), imageDbc: offsetHz ? db(image) - db(inCh) : -Infinity, occupiedFrac: total ? inCh / total : 0 };
  }

  // ------------------------------------------------------------ firmware sweep stream
  // Same layout the Scan tab parses: 16 KiB blocks, each 7F 7F + u64 LE tuning frequency, samples at the end.
  // Interleaved style: a block tuned at f carries [f, f+5 MHz] and [f+10, f+15 MHz] (LO at f + 7.5 MHz).
  const SWEEP_BLOCK = 16384, SWEEP_RATE = 20e6, SWEEP_OFFSET = 7.5e6;
  class SweepParser {
    constructor(N, onBins, onPass, firstLoHz) { this.N = N; this.df = SWEEP_RATE / N; this.onBins = onBins; this.onPass = onPass; this.first = firstLoHz; this.rem = null; this.synced = false; this.started = false; this.blocks = 0; }
    push(chunk) {
      let u = new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
      if (this.rem) { const m = new Uint8Array(this.rem.length + u.length); m.set(this.rem); m.set(u, this.rem.length); u = m; this.rem = null; }
      let off = 0;
      if (!this.synced) {
        off = -1;
        for (let i = 0; i + SWEEP_BLOCK + 2 <= u.length; i += 512) if (u[i] === 0x7f && u[i + 1] === 0x7f && u[i + SWEEP_BLOCK] === 0x7f && u[i + SWEEP_BLOCK + 1] === 0x7f) { off = i; break; }
        if (off < 0) { this.rem = u.slice(Math.max(0, u.length - SWEEP_BLOCK - 512)); return; }
        this.synced = true;
      }
      for (; off + SWEEP_BLOCK <= u.length; off += SWEEP_BLOCK) {
        if (u[off] !== 0x7f || u[off + 1] !== 0x7f) { this.synced = false; break; }
        this._block(u, off);
      }
      if (off < u.length) this.rem = u.slice(off);
    }
    _block(u, off) {
      const lo32 = (u[off + 2] | u[off + 3] << 8 | u[off + 4] << 16 | u[off + 5] << 24) >>> 0;
      const hi32 = (u[off + 6] | u[off + 7] << 8 | u[off + 8] << 16 | u[off + 9] << 24) >>> 0;
      const f = hi32 * 4294967296 + lo32;
      if (f === this.first) { if (this.started && this.onPass) this.onPass(); this.started = true; }
      if (!this.started) return;
      this.blocks++;
      const N = this.N, s8 = new Int8Array(u.buffer, u.byteOffset + off + SWEEP_BLOCK - 2 * N, 2 * N);
      let clip = 0; for (let i = 0; i < s8.length; i++) if (s8[i] === 127 || s8[i] === -128) clip++;
      const pw = new Float64Array(N);
      getFFT(N).powerInto(pw, i => s8[2 * i] / 128, i => s8[2 * i + 1] / 128, 0);
      const q = N / 4;
      for (const [k0, fs] of [[N / 2 - 3 * N / 8 + 1, f], [N / 2 + N / 8 + 1, f + 10e6]]) this.onBins(fs + this.df, this.df, pw.subarray(k0, k0 + q), clip > s8.length * 1e-3);
    }
  }
  // build one firmware sweep block (for tests / simulation): psdFn(freqHz) -> linear power per bin
  function makeSweepBlock(fTune, N, psdFn, rnd) {
    const u = new Uint8Array(SWEEP_BLOCK); u[0] = 0x7f; u[1] = 0x7f;
    let f = fTune; for (let i = 0; i < 8; i++) { u[2 + i] = f % 256; f = Math.floor(f / 256); }
    // synthesise N samples whose spectrum follows psdFn around LO = fTune + 7.5 MHz: sum of random-phase bins
    const re = new Float64Array(N), im = new Float64Array(N), df = SWEEP_RATE / N, lo = fTune + SWEEP_OFFSET;
    for (let k = 0; k < N; k++) {
      const fo = (k - N / 2) * df, p = psdFn(lo + fo), a = Math.sqrt(p) * Math.sqrt(-2 * Math.log(rnd() + 1e-12)), ph = TWO_PI * rnd();
      re[k] = a * Math.cos(ph); im[k] = a * Math.sin(ph);
    }
    // inverse DFT via FFT (conjugate trick), bins are fftshifted
    const F = getFFT(N); for (let k = 0; k < N; k++) { const j = (k + N / 2) % N; F.re[j] = re[k]; F.im[j] = -im[k]; }
    F.run();
    const s8 = new Int8Array(u.buffer, SWEEP_BLOCK - 2 * N, 2 * N);
    for (let i = 0; i < N; i++) {
      const v1 = Math.round(F.re[i] * 128), v2 = Math.round(-F.im[i] * 128);
      s8[2 * i] = Math.max(-128, Math.min(127, v1)); s8[2 * i + 1] = Math.max(-128, Math.min(127, v2));
    }
    return u;
  }

  // ------------------------------------------------------------ checksums
  // FNV-1a 32 bit (fast, sync, for in-memory integrity) and SHA-256 when WebCrypto is present
  function fnv1a(bytes) { let h = 0x811c9dc5; for (let i = 0; i < bytes.length; i++) { h ^= bytes[i]; h = Math.imul(h, 0x01000193); } return (h >>> 0).toString(16).padStart(8, '0'); }
  async function sha256(bytes) {
    const c = (typeof crypto !== 'undefined' && crypto.subtle) ? crypto : null;
    if (!c) return 'fnv1a:' + fnv1a(bytes);
    const h = await c.subtle.digest('SHA-256', bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength));
    return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  return { ECHO_RATE, SYMBOL_RATE, DEV_UNIT, DMR_SYNC, SYNC_SYMS, LABEL_RANK, CLASS_DEFAULTS,
    mulberry32, gauss, lowpassTaps, rrcTaps, FirDecim, FirDecimC, firFilterC, channelizerPlan, NarrowChannelizer, ComplexRing, ComplexBuf,
    power, db, toCi8, fromCi8, FFT, getFFT, psd, discriminator, resampleLinear, goertzelPower, classifyDmrLike, hexToSymbols,
    synthDmrLike, synthFm, synthNoise, synthWideband, interpPlan, PolyInterp, TxInterpolator, txSpectrumCheck,
    SWEEP_BLOCK, SWEEP_RATE, SWEEP_OFFSET, SweepParser, makeSweepBlock, fnv1a, sha256 };
});
