// Echo tab: per-channel trigger and compact IQ capture (pre-roll, classification, hang time, hard cap, splitting).
//
// A ChannelTrigger watches one narrowband channel (50 ksps complex from NarrowChannelizer) for the duration of a
// focused dwell. Pre-roll only exists for samples this channel really received during the dwell: if the channel was
// already active when the dwell started, the clip is flagged late_lock (the start may be missing).
"use strict";
(function (root, factory) {
  const dsp = (typeof module !== 'undefined' && module.exports) ? require('./echo-dsp.js') : root.EchoDsp;
  const m = factory(dsp);
  if (typeof module !== 'undefined' && module.exports) module.exports = m; else root.EchoCapture = m;
})(typeof self !== 'undefined' ? self : this, function (D) {

  const DETECT = { any: 'activity', likely: 'possible_dmr', confirmed: 'confirmed_dmr' };
  const CAPTURE_DEFAULTS = {
    detect: 'likely',          // 'any' | 'likely' | 'confirmed'
    thresholdDb: 10,           // above the channel noise floor
    hysteresisDb: 3,
    debounceBlocks: 2,         // 10 ms blocks above threshold before an onset counts
    preRollMs: 300,
    hangMs: 500,
    stopAtEnd: true,
    maxS: 30,                  // 1..60
    maxSplits: 1,              // continuation clips after truncation before the channel is released
    classifyMs: 300,           // activity needed before the first classification
    rate: D.ECHO_RATE,
  };
  const clampMax = s => Math.max(1, Math.min(60, +s || 30));

  // noise density estimate (power per Hz, full-scale units) from a wideband int8 block: median of a Hann-windowed
  // periodogram over the central part of the band (avoids DC and the baseband filter skirts)
  function noiseDensity(iq8, rate, n = 2048) {
    const len = iq8.length >> 1; if (len < n) return NaN;
    const f = D.getFFT(n), acc = new Float64Array(n), frames = Math.min(16, Math.floor(len / n));
    for (let k = 0; k < frames; k++) f.powerInto(acc, i => iq8[2 * i] / 128, i => iq8[2 * i + 1] / 128, k * n);
    const v = []; for (let i = 0; i < n; i++) { const off = Math.abs(i - n / 2); if (off > n * 0.02 && off < n * 0.35) v.push(acc[i] / frames); }
    v.sort((a, b) => a - b);
    let sw = 0, sw2 = 0; for (let i = 0; i < n; i++) { sw += f.win[i]; sw2 += f.win[i] ** 2; }
    const sigma2 = v[v.length >> 1] / Math.LN2 * sw * sw / sw2;     // median of an exponential = ln2 * mean
    return sigma2 / rate;
  }

  class ChannelTrigger {
    constructor(freq, opts = {}) {
      this.o = { ...CAPTURE_DEFAULTS, ...opts }; this.o.maxS = clampMax(this.o.maxS);
      const o = this.o, r = o.rate;
      this.freq = freq; this.block = Math.round(r * 0.01);
      this.chan = new D.FirDecimC(D.lowpassTaps(101, 6.5e3, r), 1);       // channel power filter (12.5 kHz)
      let s2 = 0; for (const h of this.chan.i.h) s2 += h * h; this.noiseBw = r * s2;
      this.floorDb = Number.isFinite(o.n0) ? D.db(o.n0 * this.noiseBw) : NaN;
      this.ring = new D.ComplexRing(Math.round(r * (o.preRollMs + 50) / 1000));
      this.pend = { I: null, Q: null };                                 // samples not yet a whole block
      this.t = 0;                                                       // samples seen
      this.state = 'idle';                                              // idle | active | capturing | ignoring
      this.onRun = 0; this.offRun = 0; this.activeBlocks = 0; this.peakDb = -Infinity;
      this.buf = null; this.onsetSample = 0; this.lateLock = false; this.label = null; this.features = null;
      this.clips = []; this.splits = 0; this.lastClass = null; this.classifiedAt = 0; this.levelHist = [];
      this.everActive = false; this.firstBlockActive = null;
    }
    get busy() { return this.state === 'capturing' || this.state === 'active'; }
    get wanted() { return DETECT[this.o.detect] || 'possible_dmr'; }
    process(I, Q) {
      if (this.pend.I && this.pend.I.length) { I = cat(this.pend.I, I); Q = cat(this.pend.Q, Q); }
      const B = this.block, nb = Math.floor(I.length / B), evs = [];
      for (let b = 0; b < nb; b++) { const e = this._block(I.subarray(b * B, (b + 1) * B), Q.subarray(b * B, (b + 1) * B)); if (e) evs.push(e); }
      this.pend.I = I.slice(nb * B); this.pend.Q = Q.slice(nb * B);
      return evs;                     // [{event: 'clip', clip} | {event: 'rejected', cls}]
    }
    _block(I, Q) {
      const o = this.o, [ci, cq] = this.chan.process(I, Q), lvl = D.db(D.power(ci, cq));
      if (!Number.isFinite(this.floorDb)) this.floorDb = lvl;                       // no estimate: first block (refined below)
      const on = lvl > this.floorDb + o.thresholdDb, off = lvl < this.floorDb + o.thresholdDb - o.hysteresisDb;
      if (this.firstBlockActive === null) this.firstBlockActive = on;
      if (!on && lvl < this.floorDb + 3) this.floorDb = 0.97 * this.floorDb + 0.03 * lvl;   // track the floor while quiet
      else if (lvl < this.floorDb) this.floorDb = lvl;
      if (on) { this.onRun++; this.offRun = 0; } else if (off) { this.offRun++; this.onRun = 0; }
      const t0 = this.t; this.t += I.length;
      if (this.state === 'idle' || this.state === 'ignoring') {
        if (this.state === 'ignoring' && this.offRun * 10 >= o.hangMs) this.state = 'idle';
        if (this.state === 'idle' && this.onRun >= o.debounceBlocks) {
          // onset: pre-roll = what the ring holds (it only holds samples this dwell really received)
          this.state = 'active'; this.everActive = true; this.activeBlocks = this.onRun; this.peakDb = lvl;
          const onset = t0 - (this.onRun - 1) * I.length;
          const pre = Math.round(o.preRollMs * o.rate / 1000);
          const [rI, rQ] = this.ring.read(pre + (this.onRun - 1) * I.length);
          this.buf = new D.ComplexBuf(Math.round(o.rate * 2)); this.buf.push(rI, rQ);
          this.onsetSample = onset; this.bufStart = t0 - (rI.length - 0);
          this.preRollS = Math.max(0, (onset - this.bufStart) / o.rate);
          this.lateLock = !!this.firstBlockActive && onset <= this.block * o.debounceBlocks;
        }
        this.ring.push(I, Q);
        if (this.state !== 'active') return null;
        this.buf.push(I, Q);
        return this._afterActiveBlock(on, off, lvl);
      }
      this.ring.push(I, Q);
      this.buf.push(I, Q);
      if (on) { this.activeBlocks++; if (lvl > this.peakDb) this.peakDb = lvl; }
      return this._afterActiveBlock(on, off, lvl);
    }
    _afterActiveBlock(on, off, lvl) {
      const o = this.o, ended = this.offRun * 10 >= o.hangMs, dur = this.buf.n / o.rate;
      if (this.state === 'active') {
        const actMs = this.activeBlocks * 10;
        const decide = actMs >= o.classifyMs * (this.lastClass ? 3 : 1) || ended;
        if (decide) {
          const [I, Q] = this.buf.take();
          this.lastClass = D.classifyDmrLike(I, Q, o.rate, { floorDb: this.floorDb, thresholdDb: o.thresholdDb });
          this.classifiedAt = this.t;
          if (D.LABEL_RANK[this.lastClass.label] >= D.LABEL_RANK[this.wanted]) {
            this.state = 'capturing'; this.label = this.lastClass.label; this.features = this.lastClass;
          } else if (ended || this.activeBlocks * 10 >= o.classifyMs * 3) {
            this.state = ended ? 'idle' : 'ignoring'; this.buf = null;
            return { event: 'rejected', cls: this.lastClass };
          }
        }
        if (this.state === 'active' && dur >= o.maxS) { this.state = 'ignoring'; this.buf = null; return { event: 'rejected', cls: this.lastClass }; }
        if (this.state !== 'capturing') return null;
      }
      // capturing: re-label as more data arrives (a possible -> confirmed upgrade is common)
      if (o.stopAtEnd && ended) return this._finish('end_of_transmission');
      if (dur >= o.maxS) return this._finish('truncated_by_limit', !ended);
      return null;
    }
    _finish(reason, stillActive = false) {
      const o = this.o, [I, Q] = this.buf.take();
      const maxN = Math.round(o.maxS * o.rate), n = Math.min(I.length, maxN);
      const cls = D.classifyDmrLike(I.subarray(0, n), Q.subarray(0, n), o.rate, { floorDb: this.floorDb, thresholdDb: o.thresholdDb });
      if (D.LABEL_RANK[cls.label] > D.LABEL_RANK[this.label]) this.label = cls.label;
      const clip = {
        freq: this.freq, I: I.slice(0, n), Q: Q.slice(0, n), rate: o.rate, stopReason: reason, lateLock: this.lateLock || this.splits > 0,
        continuation: this.splits > 0, preRollS: this.splits > 0 ? 0 : this.preRollS, label: this.label, features: cls,
        snrDb: this.peakDb - this.floorDb, floorDb: this.floorDb, durationS: n / o.rate, startSample: this.bufStart,
      };
      this.clips.push(clip);
      if (stillActive && this.splits < o.maxSplits) {
        // continue in a new clip without a gap
        this.splits++; this.buf = new D.ComplexBuf(Math.round(o.rate * 2)); this.bufStart = this.t;
        if (I.length > n) this.buf.push(I.subarray(n), Q.subarray(n));
      } else if (stillActive) {
        this.state = 'ignoring'; this.buf = null; this.alwaysOn = true;     // continuous carrier: release it
      } else { this.state = 'idle'; this.buf = null; this.splits = 0; }
      return { event: 'clip', clip };
    }
    // the dwell is ending: close an open capture
    abort(reason = 'dwell_aborted') { if (this.state === 'capturing' && this.buf && this.buf.n) { const r = this._finish(reason); this.state = 'idle'; return r; } this.state = 'idle'; this.buf = null; return null; }
  }
  function cat(a, b) { const o = new Float32Array(a.length + b.length); o.set(a); o.set(b, a.length); return o; }

  return { ChannelTrigger, CAPTURE_DEFAULTS, DETECT, noiseDensity, clampMax };
});
