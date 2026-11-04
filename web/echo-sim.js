// Echo tab: simulated radio for offline development, CI and the "synthetic source" demo mode.
//
// SimRadio implements the same interface as the WebUSB radio in echo.js (survey / startRx / stopRx / now / sleep) on a
// scripted scenario of emitters, with its own simulated clock. Clips recorded from it carry source 'synthetic'.
// Emitters: { f, kind: 'dmr' | 'dmr_tdma' | 'fm' | 'carrier', amp, bursts: [{t0, dur}] } (seconds of simulated time).
"use strict";
(function (root, factory) {
  const node = typeof module !== 'undefined' && module.exports;
  const m = factory(node ? require('./echo-dsp.js') : root.EchoDsp);
  if (node) module.exports = m; else root.EchoSim = m;
})(typeof self !== 'undefined' ? self : this, function (D) {

  const STEP_S = 0.0027, SETUP_S = 0.04, NB_RATE = D.ECHO_RATE;

  class SimRadio {
    constructor({ emitters = [], spurs = [], noise = 0.004, seed = 7, start = Date.UTC(2026, 10, 4, 12), realtime = 0, blockS = 0.02 } = {}) {
      this.kind = 'sim'; this.t = 0; this.noise = noise; this.rnd = D.mulberry32(seed); this.startMs = start; this.realtime = realtime; this.blockS = blockS;
      this.emitters = emitters.map((e, i) => ({ ...e, id: i, tracks: (e.bursts || []).map(b => ({ ...b, iq: null })) }));
      this.spurs = spurs; this.rx = null; this.log = []; this.txCalls = 0; this.surveys = 0; this.dwells = 0;
    }
    now() { return this.t; }
    wallClock() { return new Date(this.startMs + this.t * 1000); }
    async _yield(dt) { this.t += dt; if (this.realtime) await new Promise(r => setTimeout(r, dt * 1000 / this.realtime)); else await new Promise(r => setImmediate ? setImmediate(r) : setTimeout(r, 0)); }
    async sleep(ms) { await this._yield(ms / 1000); }
    setGains() { return Promise.resolve(); }
    active(e, t) { return e.tracks.some(b => t >= b.t0 && t < b.t0 + b.dur); }
    _track(e, b) {
      if (b.iq) return b.iq;
      const o = { rate: NB_RATE, seconds: b.dur + 0.005, bursts: [{ t0: 0, dur: b.dur }], amp: e.amp, noise: 0, seed: 11 + e.id * 101 + Math.round(b.t0 * 10) };
      b.iq = e.kind === 'fm' ? D.synthFm(o) : e.kind === 'carrier' ? carrier(o) : D.synthDmrLike({ ...o, mode: e.kind === 'dmr_tdma' ? 'tdma' : 'continuous', sync: e.sync || 'bs_voice' });
      return b.iq;
    }
    // firmware-sweep equivalent: per-bin dB (same normalisation as the sweep FFT: a tone of amplitude a -> a^2)
    async survey({ ranges, passes = 4, binN = 4096 }) {
      this.surveys++; const t0 = this.t;
      await this._yield(SETUP_S);
      const df = D.SWEEP_RATE / binN, floorLin = this.noise * this.noise * 2 * 1.5 / binN;
      const out = ranges.map(r => ({ loHz: r.loMHz * 1e6, df, passes: [] }));
      for (let p = 0; p < passes; p++) {
        for (const [ri, r] of ranges.entries()) {
          const nb = Math.round((r.hiMHz - r.loMHz) * 1e6 / df), a = new Float32Array(nb), lo = r.loMHz * 1e6;
          const steps = (r.hiMHz - r.loMHz) / 20, tp = this.t;
          for (let k = 0; k < nb; k++) {
            const f = lo + k * df, tb = tp + (k / nb) * steps * STEP_S;
            let p0 = floorLin * -Math.log(this.rnd() + 1e-12);
            for (const e of this.emitters) if (Math.abs(f - e.f) < 6e3 && this.active(e, tb)) p0 += e.amp * e.amp / Math.max(1, 12e3 / df) * (0.6 + 0.8 * this.rnd());
            for (const s of this.spurs) if (Math.abs(f - s.f) < df && this.rnd() < (s.passFrac ?? 1)) p0 += 10 ** (s.db / 10);
            a[k] = 10 * Math.log10(p0);
          }
          out[ri].passes.push(a);
          this.t += steps * STEP_S;
        }
      }
      this.log.push({ kind: 'survey', t0, t1: this.t });
      return { ranges: out, clipped: [], ms: (this.t - t0) * 1000 };
    }
    async startRx({ fc, rate }, onBlock) {
      if (this.rx) await this.stopRx();
      this.dwells++;
      const rx = this.rx = { fc, rate, on: true, t0: this.t };
      await this._yield(0.015);                                   // retune / mode switch
      rx.pump = (async () => {
        while (rx.on) {
          const dur = this.blockS, t = this.t, signals = [];
          for (const e of this.emitters) {
            if (Math.abs(e.f - fc) > rate / 2) continue;
            for (const b of e.tracks) if (b.t0 < t + dur && b.t0 + b.dur > t) signals.push({ iq: this._track(e, b), rate: NB_RATE, offsetHz: e.f - fc, start: b.t0 });
          }
          const buf = D.synthWideband({ rate, seconds: dur, signals, noise: this.noise, seed: Math.floor(this.rnd() * 1e9), t0: t, fastNoise: true });
          await this._yield(dur);
          if (!rx.on) break;
          onBlock(buf);
        }
      })();
    }
    async stopRx() {
      const rx = this.rx; if (!rx) return;
      rx.on = false; this.rx = null; await rx.pump;
      this.log.push({ kind: 'rx', fc: rx.fc, t0: rx.t0, t1: this.t });
    }
    // the sim radio has no transmitter: Lab TX tests use the USB mock in echo-lab-tx tests instead
  }
  function carrier({ rate, seconds, amp }) { const n = Math.round(rate * seconds), I = new Float32Array(n).fill(amp), Q = new Float32Array(n); return [I, Q]; }

  // a small demo scenario on the selected intervals: a DMR-like repeater, a TDMA burst user, analogue FM and a spur
  function demoScenario(intervals, { seed = 1, durationS = 600 } = {}) {
    const rnd = D.mulberry32(seed), em = [];
    const pick = iv => Math.round((iv.lo + 25e3 + rnd() * Math.max(0, iv.hi - iv.lo - 50e3)) / 12500) * 12500;
    const bursts = (every, len) => { const b = []; for (let t = 2 + rnd() * every; t < durationS; t += every * (0.6 + 0.8 * rnd())) b.push({ t0: t, dur: len * (0.5 + rnd()) }); return b; };
    if (intervals[0]) em.push({ f: pick(intervals[0]), kind: 'dmr', amp: 0.05, bursts: bursts(25, 6), label: 'DMR-like repeater (synthetic)' });
    const iv2 = intervals[1] || intervals[0];
    if (iv2) em.push({ f: pick(iv2), kind: 'dmr_tdma', amp: 0.03, bursts: bursts(40, 4), label: 'DMR-like TDMA bursts (synthetic)' });
    const iv3 = intervals[intervals.length - 1];
    if (iv3) em.push({ f: pick(iv3), kind: 'fm', amp: 0.04, bursts: bursts(30, 5), label: 'analogue FM (synthetic)' });
    return { emitters: em, spurs: [], note: 'synthetic DMR-like signals: public sync words, random payload, not valid DMR frames' };
  }

  return { SimRadio, demoScenario };
});
