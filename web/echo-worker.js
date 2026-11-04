// Echo tab worker: hosts the adaptive scan engine (scheduler + capture + DSP) off the UI thread.
//
// The HackRF itself lives on the main thread (one WebUSB owner). This worker drives it through a message protocol:
// it asks the main thread to sweep or to receive, the main thread streams raw USB bytes back tagged with the mode,
// and this worker parses them (firmware sweep stream -> survey bins; RX stream -> narrowband capture). Clips are
// posted to the main thread, which stores them. Lab TX is NOT here: it runs on the main thread, which owns the device.
"use strict";
importScripts('echo-dsp.js', 'echo-profiles.js', 'echo-capture.js', 'echo-scheduler.js');
const D = self.EchoDsp, P = self.EchoProfiles, S = self.EchoScheduler;

// radio proxy: turns engine calls into messages to the main thread and consumes the byte stream it sends back
class ProxyRadio {
  constructor(post) { this.kind = 'hackrf'; this.post = post; this.mode = 'off'; this.onBlock = null; this.sweepQ = []; this.sweepWaiter = null; this.lastData = perf(); }
  now() { return perf() / 1000; }
  wallClock() { return new Date(); }
  sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  setGains() { return Promise.resolve(); }
  feed(mode, buf) {                               // called from onmessage with raw USB bytes
    this.lastData = perf();
    if (mode === 'sweep') { this.sweepQ.push(buf); if (this.sweepWaiter) { const w = this.sweepWaiter; this.sweepWaiter = null; w(); } }
    else if (mode === 'rx' && this.onBlock) this.onBlock(buf);
  }
  _ctl(cmd, extra) { return new Promise((res, rej) => { const id = ++CTL; pending[id] = { res, rej }; this.post({ ctl: cmd, id, ...extra }); }); }

  async survey({ ranges, passes, binN, gains }) {
    const t0 = this.now();
    this.sweepQ = []; this.mode = 'sweep';
    await this._ctl('startSweep', { ranges, binN, gains });
    const firstLo = ranges[0].loMHz * 1e6;
    const acc = ranges.map(r => ({ loHz: r.loMHz * 1e6, df: D.SWEEP_RATE / binN, nb: Math.round((r.hiMHz - r.loMHz) * 1e6 / (D.SWEEP_RATE / binN)), passes: [], sum: null, cnt: null }));
    const rangeAt = f => { for (const a of acc) if (f >= a.loHz && f < a.loHz + a.nb * a.df) return a; return null; };
    let passCount = 0, cur = freshPass(acc);
    const parser = new D.SweepParser(binN,
      (f0, df, pw) => { const a = rangeAt(f0); if (!a) return; const base = Math.round((f0 - a.loHz) / df); const p = cur.get(a); for (let k = 0; k < pw.length; k++) { const g = base + k; if (g >= 0 && g < a.nb) { p.sum[g] += pw[k]; p.cnt[g]++; } } },
      () => { commitPass(acc, cur); cur = freshPass(acc); passCount++; }, firstLo);
    const clipped = new Set();
    while (passCount < passes) {
      if (!this.sweepQ.length) { await Promise.race([new Promise(r => { this.sweepWaiter = r; }), this.sleep(250)]); if (perf() - this.lastData > 4000) { this.mode = 'off'; await this._ctl('stopSweep'); throw new Error('USB stream stalled during the survey (device dropped off USB?)'); } continue; }
      const q = this.sweepQ.splice(0); for (const b of q) parser.push(b);
    }
    this.mode = 'off'; await this._ctl('stopSweep');
    return { ranges: acc.map(a => ({ loHz: a.loHz, df: a.df, passes: a.passes })), clipped: [...clipped], ms: (this.now() - t0) * 1000 };
  }
  async startRx({ fc, rate, gains }, onBlock) { this.onBlock = onBlock; this.mode = 'rx'; await this._ctl('startRx', { fc, rate, gains }); }
  async stopRx() { this.onBlock = null; this.mode = 'off'; await this._ctl('stopRx'); }
}
function freshPass(acc) { const m = new Map(); for (const a of acc) m.set(a, { sum: new Float64Array(a.nb), cnt: new Uint32Array(a.nb) }); return { get: a => m.get(a) }; }
function commitPass(acc, cur) { for (const a of acc) { const p = cur.get(a), db = new Float32Array(a.nb); for (let k = 0; k < a.nb; k++) db[k] = p.cnt[k] ? 10 * Math.log10(p.sum[k] / p.cnt[k] + 1e-20) : -200; a.passes.push(db); } }

const perf = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
let CTL = 0; const pending = {};
let engine = null, radio = null, clipSeq = 0, pendingClips = {};

const post = m => postMessage(m);
onmessage = e => {
  const m = e.data;
  if (m.ctlDone !== undefined) { const p = pending[m.ctlDone]; if (p) { delete pending[m.ctlDone]; m.error ? p.rej(new Error(m.error)) : p.res(m.result); } return; }
  if (m.usb) { if (radio) radio.feed(m.mode, new Int8Array(m.usb)); return; }
  if (m.clipStored !== undefined) { const p = pendingClips[m.clipStored]; if (p) { delete pendingClips[m.clipStored]; p(); } return; }
  if (m.cmd === 'start') {
    radio = new ProxyRadio(post);
    const intervals = m.intervals;
    engine = new S.EchoEngine({ radio, intervals, sched: m.sched, capture: m.capture, gains: m.gains, source: m.source || 'live',
      onState: (s, info) => post({ ev: 'state', state: s, info }),
      onLog: l => post({ ev: 'log', log: l }),
      onStatus: st => post({ ev: 'status', status: st }),
      onClip: async clip => { const id = ++clipSeq; await new Promise(res => { pendingClips[id] = res; post({ ev: 'clip', id, meta: clip.meta, data: clip.data.buffer.slice(clip.data.byteOffset, clip.data.byteOffset + clip.data.byteLength) }, [clip.data.buffer.slice(clip.data.byteOffset, clip.data.byteOffset + clip.data.byteLength)]); }); } });
    engine.start();
    // periodic status so the UI updates even during a long survey
    statusTimer = setInterval(() => { if (engine) post({ ev: 'status', status: engine.status() }); }, 1000);
    return;
  }
  if (m.cmd === 'pauseForTx') { if (engine) engine.pause('tx').then(() => post({ ev: 'paused' })); return; }
  if (m.cmd === 'resume') { if (engine) engine.resume(); return; }
  if (m.cmd === 'stop') { clearInterval(statusTimer); if (engine) engine.stop().then(() => post({ ev: 'stopped' })); engine = null; return; }
  if (m.cmd === 'config') { if (engine) { if (m.capture) Object.assign(engine.cap, m.capture); if (m.gains) Object.assign(engine.gains, m.gains); if (m.sched) Object.assign(engine.sched.o, m.sched); } return; }
};
let statusTimer = null;
