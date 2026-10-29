// Live Vaisala RS41 radiosonde decoder for the browser: rs1729/RS rs41mod built as WebAssembly
// (web/tools/rs41.{js,wasm}, see build_tools.sh) run in IQ mode on 4 s windows of complex baseband.
//   const dec = new Rs41Live(create_rs41, wasmBytes, { rate: 50000, onFrame: f => ... });
//   dec.feed(I, Q);        // Float32Array pairs at `rate`, |x| <= 1, sonde within a few kHz of DC
// Each window (with 1 s overlap) is written to the module FS as an 8-bit stereo WAV and decoded with
// `rs41mod --iq2 --ecc --ptu --crc --json`; JSON lines become onFrame({frame, id, datetime, lat, lon,
// alt, vel_h, heading, vel_v, temp, humidity, pressure, sats, batt, ...}). Frames are de-duplicated by
// (id, frame). A fresh module per window costs ~2 ms; the decode of 4 s takes ~50 ms (0.4 s for 32 s).
// Plain script: importScripts() in a worker or require() in Node.
"use strict";
class Rs41Live {
  constructor(factory, wasmBinary, opts = {}) {
    this.factory = factory; this.wasm = wasmBinary;
    this.rate = opts.rate || 50000; this.windowS = opts.windowS || 4; this.overlapS = opts.overlapS || 1;
    this.onFrame = opts.onFrame || (() => {}); this.onLog = opts.onLog || null;
    this.bufI = new Float32Array(0); this.bufQ = new Float32Array(0);
    this.seen = new Map(); this.stats = { windows: 0, frames: 0, dupes: 0, msLast: 0, msPerWindow: 0 };
  }
  feed(I, Q) {
    const n = Math.min(I.length, Q.length); if (!n) return;
    const bi = new Float32Array(this.bufI.length + n), bq = new Float32Array(bi.length);
    bi.set(this.bufI); bi.set(I.subarray(0, n), this.bufI.length); bq.set(this.bufQ); bq.set(Q.subarray(0, n), this.bufQ.length);
    this.bufI = bi; this.bufQ = bq;
    const W = Math.round(this.windowS * this.rate), O = Math.round(this.overlapS * this.rate);
    while (this.bufI.length >= W) {
      this.decode(this.bufI.subarray(0, W), this.bufQ.subarray(0, W));
      this.bufI = this.bufI.slice(W - O); this.bufQ = this.bufQ.slice(W - O);
    }
  }
  flush() { if (this.bufI.length > this.rate) { this.decode(this.bufI, this.bufQ); this.bufI = new Float32Array(0); this.bufQ = new Float32Array(0); } }
  wav(I, Q) {
    const n = I.length, buf = new Uint8Array(44 + 2 * n), v = new DataView(buf.buffer), str = (o, t) => { for (let i = 0; i < t.length; i++) buf[o + i] = t.charCodeAt(i); };
    str(0, 'RIFF'); v.setUint32(4, 36 + 2 * n, true); str(8, 'WAVE'); str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 2, true);
    v.setUint32(24, this.rate, true); v.setUint32(28, this.rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 8, true); str(36, 'data'); v.setUint32(40, 2 * n, true);
    for (let k = 0, o = 44; k < n; k++) { buf[o++] = 128 + Math.max(-127, Math.min(127, Math.round(I[k] * 127))); buf[o++] = 128 + Math.max(-127, Math.min(127, Math.round(Q[k] * 127))); }
    return buf;
  }
  decode(I, Q) {
    const t0 = (typeof performance !== 'undefined' ? performance : Date).now(), lines = [];
    const M = { wasmBinary: this.wasm, noInitialRun: true, print: l => lines.push(l), printErr: l => { if (this.onLog) this.onLog(l); } };
    this.factory(M);
    M.FS.writeFile('/w.wav', this.wav(I, Q));
    try { M.callMain(['--iq2', '--ecc', '--ptu', '--crc', '--json', '/w.wav']); } catch (e) { if (!e || e.name !== 'ExitStatus') throw e; }
    for (const l of lines) {
      if (l[0] !== '{') continue;
      let f; try { f = JSON.parse(l); } catch (e) { continue; }
      const key = `${f.id}:${f.frame}`;
      if (this.seen.has(key)) { this.stats.dupes++; continue; }
      this.seen.set(key, 1); if (this.seen.size > 5000) this.seen.delete(this.seen.keys().next().value);
      f.t = Date.now(); this.stats.frames++; this.onFrame(f);
    }
    const ms = (typeof performance !== 'undefined' ? performance : Date).now() - t0;
    this.stats.windows++; this.stats.msLast = ms; this.stats.msPerWindow += (ms - this.stats.msPerWindow) * 0.3;
  }
}
if (typeof module !== 'undefined') module.exports = { Rs41Live };
else if (typeof self !== 'undefined') self.Rs41Live = Rs41Live;
