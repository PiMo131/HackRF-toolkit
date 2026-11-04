// Echo tab: offline virtual replay. Plays a stored fragment back through the SAME analysis chain (classifier /
// spectrum), NOT the radio: no RF energy leaves the machine. This is the default for Play and for Auto Echo.
//
// A ReplayQueue holds up to `maxItems` jobs; each job replays one fragment `repeat` times with `pauseMs` between
// repeats. Stop / Emergency Stop clear it at once. The engine here is transport-agnostic: the UI gives it a
// `render` callback (draw a spectrum frame) and a `clock` (so tests run without real timers).
"use strict";
(function (root, factory) {
  const node = typeof module !== 'undefined' && module.exports;
  const m = factory(node ? require('./echo-dsp.js') : root.EchoDsp);
  if (node) module.exports = m; else root.EchoReplay = m;
})(typeof self !== 'undefined' ? self : this, function (D) {

  const REPLAY_DEFAULTS = { repeat: 2, pauseMs: 500, maxItems: 20, frameMs: 40 };
  const clampRepeat = n => Math.max(1, Math.min(10, Math.round(+n || 1)));
  const clampPause = n => Math.max(0, Math.min(5000, Math.round(+n || 0)));

  // one virtual playback: steps a cursor through the fragment at real-time speed (frameMs chunks), reporting
  // a spectrum + running classification for each frame. Never touches WebUSB.
  class VirtualReplay {
    constructor(I, Q, rate, { frameMs = 40 } = {}) { this.I = I; this.Q = Q; this.rate = rate; this.frame = Math.max(1, Math.round(rate * frameMs / 1000)); this.pos = 0; }
    get done() { return this.pos >= this.I.length; }
    get progress() { return this.I.length ? this.pos / this.I.length : 1; }
    step() {
      if (this.done) return null;
      const a = this.pos, b = Math.min(this.I.length, a + this.frame); this.pos = b;
      const I = this.I.subarray(a, b), Q = this.Q.subarray(a, b);
      const n = Math.min(1024, 1 << Math.floor(Math.log2(Math.max(2, b - a))));
      return { spectrum: n >= 2 ? D.psd(I, Q, n, 8) : new Float64Array(0), rms: Math.sqrt(D.power(I, Q)), t0: a / this.rate, t1: b / this.rate };
    }
    reset() { this.pos = 0; }
  }

  // queue state machine. `provider(id)` -> Promise<{I, Q, meta}> (EchoLibrary.readIq). TRANSMITS NOTHING.
  class ReplayQueue {
    constructor({ provider, clock, onFrame, onEvent, maxItems = REPLAY_DEFAULTS.maxItems } = {}) {
      this.provider = provider; this.clock = clock || defaultClock(); this.onFrame = onFrame || (() => {}); this.onEvent = onEvent || (() => {});
      this.maxItems = maxItems; this.items = []; this.running = false; this.current = null; this.seq = 0; this.cancelled = false;
    }
    get length() { return this.items.length + (this.current ? 1 : 0); }
    enqueue(id, { repeat = REPLAY_DEFAULTS.repeat, pauseMs = REPLAY_DEFAULTS.pauseMs, name } = {}) {
      if (this.length >= this.maxItems) throw new Error(`replay queue is full (${this.maxItems}); Stop or let it drain first`);
      const job = { jid: ++this.seq, id, repeat: clampRepeat(repeat), pauseMs: clampPause(pauseMs), name: name || id };
      this.items.push(job); this.onEvent({ kind: 'enqueued', job, length: this.length });
      if (!this.running) this._run();
      return job.jid;
    }
    enqueueMany(list) { return list.map(x => this.enqueue(x.id, x)); }
    async _run() {
      if (this.running) return; this.running = true; this.cancelled = false;
      try {
        while (this.items.length && !this.cancelled) {
          const job = this.items.shift(); this.current = job;
          this.onEvent({ kind: 'job_start', job, length: this.length });
          let data;
          try { data = await this.provider(job.id); } catch (e) { this.onEvent({ kind: 'job_error', job, error: e.message }); this.current = null; continue; }
          if (!data) { this.onEvent({ kind: 'job_error', job, error: 'fragment not found' }); this.current = null; continue; }
          for (let rep = 0; rep < job.repeat && !this.cancelled; rep++) {
            const vr = new VirtualReplay(data.I, data.Q, data.meta.iqRate, { frameMs: REPLAY_DEFAULTS.frameMs });
            this.onEvent({ kind: 'replay_start', job, repeat: rep + 1, of: job.repeat, meta: data.meta });
            let fr;
            while ((fr = vr.step()) && !this.cancelled) { this.onFrame({ job, repeat: rep + 1, frame: fr, meta: data.meta, mode: 'OFFLINE_REPLAY' }); await this.clock.sleep(REPLAY_DEFAULTS.frameMs); }
            this.onEvent({ kind: 'replay_end', job, repeat: rep + 1, of: job.repeat });
            if (rep + 1 < job.repeat && !this.cancelled && job.pauseMs) await this.clock.sleep(job.pauseMs);
          }
          this.current = null;
        }
      } finally { this.running = false; this.current = null; if (this.cancelled) this.onEvent({ kind: 'stopped' }); else this.onEvent({ kind: 'drained' }); }
    }
    stop() { this.cancelled = true; this.items = []; this.onEvent({ kind: 'stopping' }); }
    emergencyStop() { this.stop(); }      // replay transmits nothing; Emergency Stop is the same as Stop here
    clear() { this.items = []; }
    status() { return { running: this.running, queued: this.items.length, current: this.current, mode: 'OFFLINE_REPLAY', rf: false }; }
  }
  function defaultClock() { return { sleep: ms => new Promise(r => setTimeout(r, ms)), now: () => Date.now() / 1000 }; }

  return { VirtualReplay, ReplayQueue, REPLAY_DEFAULTS, clampRepeat, clampPause };
});
