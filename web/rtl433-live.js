// Live rtl_433 for the browser: ISM 433 / 868 MHz sensors, remotes, TPMS and wM-Bus meters decoded
// from a continuous complex-baseband stream instead of per recording.
//
// Plain script: importScripts('rtl433-live.js') in a worker, or require() in Node (tests).
//
//   const dec = new Rtl433Live(create_rtl433, wasmBytes, { rate: 250000, centerHz: 433.92e6,
//                              onMessage: m => console.log(m.model, m.id, m) });
//   dec.feed(I, Q);          // Float32Array pairs at `rate`, |x| <= 1, any length, call repeatedly
//   dec.flush();             // decode whatever is left (end of stream)
//   dec.stats                // { chunks, decodes, dupes, msPerChunk, msLast, seconds }
//
// How it works: samples are accumulated for `chunkS` seconds (default 2), written as one file into
// a fresh rtl_433 module instance (MEMFS) and decoded with `rtl_433 -r <file> -s rate -f centerHz
// -F json -M level -M protocol -M time:rel <-R list>`. JSON lines from Module.print are parsed and
// handed to onMessage with the rtl_433 fields plus {t, ts, freqHz, chunk}. The last `mergeWindow`
// seconds of each chunk are prepended to the next one so packets on a chunk border still decode
// once; duplicates that this (or a sensor repeating itself) produces are dropped within `dedupeS`.
//
// A fresh module per chunk is required: the build has EXIT_RUNTIME=1 and rtl_433 ends with exit(),
// which closes the FS streams (a second callMain on the same instance prints nothing). Measured:
// instantiating the 620 kB module costs ~2 ms (V8 caches the compiled code by bytes), so it is
// negligible next to the decode itself.
//
// Sample rate per band (rtl_433 25.02):
//   433 MHz OOK/FSK sensors + remotes:  rate 250000, centerHz 433.92e6       (250 k is enough)
//   868 MHz wM-Bus C/T (100 kchip/s):   rate 1000000 (1.2e6 better), centerHz 868.95e6
//   868 MHz wM-Bus S/T downlink, misc:  rate 1000000, centerHz 868.3e6
//   TPMS 433.92 (FSK, 10 kbps..):       rate 250000 is fine; 1000000 for 20 kbps FSK TPMS
// `format`: 'cs16' (default: 16-bit, keeps weak signals; 8 MB per 2 s at 1 Msps), 'cu8' (4 MB, 8-bit,
// optional autoGain scales each chunk so the peak hits full scale) or 'cf32' (16 MB).
"use strict";
(function (root) {

const SKIP_KEYS = new Set(['time', 'mod', 'freq', 'freq1', 'freq2', 'rssi', 'snr', 'noise', 'protocol']);
// wM-Bus modes rtl_433 disables by default but which are dense in NL/BE (water, heat, gas meters)
const DEFAULT_EXTRA_PROTOCOLS = [104, 105, 238];

class Rtl433Live {
  constructor(factory, wasmBinary, opts = {}) {
    if (typeof factory !== 'function') throw new Error('Rtl433Live: factory must be the create_rtl433 function');
    this.factory = factory; this.wasm = wasmBinary;
    this.rate = Math.round(opts.rate || 250000);
    this.centerHz = Math.round(opts.centerHz || 433.92e6);
    this.chunkS = opts.chunkS || 2;
    this.mergeWindow = opts.mergeWindow === undefined ? 0.25 : opts.mergeWindow;
    this.dedupeS = opts.dedupeS === undefined ? 1.0 : opts.dedupeS;
    this.format = opts.format || 'cs16';
    this.autoGain = opts.autoGain === undefined ? this.format === 'cu8' : !!opts.autoGain;
    this.extraArgs = Array.from(opts.extraArgs || []);
    this.extraProtocols = opts.extraProtocols || DEFAULT_EXTRA_PROTOCOLS;
    this.protocols = opts.protocols || null;       // null: default-enabled + extraProtocols; 'default': rtl_433 defaults; array: explicit
    this.onMessage = opts.onMessage || (() => {});
    this.onLog = opts.onLog || null;               // non-JSON output lines (stderr), for debugging
    this.stats = { chunks: 0, decodes: 0, dupes: 0, msPerChunk: 0, msLast: 0, msTotal: 0, seconds: 0, lines: 0 };

    this.chunkN = Math.round(this.chunkS * this.rate);
    this.tailN = Math.round(this.mergeWindow * this.rate);
    this.bufI = new Float32Array(this.chunkN); this.bufQ = new Float32Array(this.chunkN); this.fill = 0;
    this.tailI = new Float32Array(0); this.tailQ = new Float32Array(0);
    this.streamN = 0;                 // samples fed so far (stream clock)
    this.wallAtEnd = 0;               // Date.now() when the last fed sample arrived
    this.seen = new Map();            // dedupe key -> last ts
    this.rArgs = null;
  }

  // ------------------------------------------------------------ input
  feed(I, Q) {
    const n = Math.min(I.length, Q.length);
    this.wallAtEnd = Date.now();
    let off = 0;
    while (off < n) {
      const take = Math.min(n - off, this.chunkN - this.fill);
      this.bufI.set(I.subarray(off, off + take), this.fill);
      this.bufQ.set(Q.subarray(off, off + take), this.fill);
      this.fill += take; off += take;
      if (this.fill === this.chunkN) this.runChunk(this.bufI, this.bufQ);
    }
  }
  flush(minS = 0.1) {
    if (this.fill >= minS * this.rate) this.runChunk(this.bufI.subarray(0, this.fill), this.bufQ.subarray(0, this.fill));
    else { this.streamN += this.fill; this.fill = 0; }
  }

  // ------------------------------------------------------------ one chunk -> rtl_433
  runChunk(I, Q) {
    const t0 = Date.now();
    const n = I.length, m = this.tailI.length, total = m + n;
    const fileStartS = (this.streamN - m) / this.rate;      // stream time of the first sample in the file
    // gain for cu8: scale the chunk so its peak hits ~0.9 full scale (max +36 dB), reported back in gainDb
    let g = 1;
    if (this.autoGain) {
      let pk = 0;
      for (let k = 0; k < n; k += 4) { const a = Math.abs(I[k]), b = Math.abs(Q[k]); if (a > pk) pk = a; if (b > pk) pk = b; }
      for (let k = 0; k < m; k += 4) { const a = Math.abs(this.tailI[k]), b = Math.abs(this.tailQ[k]); if (a > pk) pk = a; if (b > pk) pk = b; }
      g = Math.min(64, 0.9 / Math.max(pk, 1e-6));
    }
    const gainDb = 20 * Math.log10(g);
    const file = this.format === 'cu8' ? '/in.cu8' : this.format === 'cf32' ? '/in.cf32' : '/in.cs16';
    const bytes = this.pack(this.tailI, this.tailQ, I, Q, g, total);

    const lines = [];
    const M = { wasmBinary: this.wasm, noInitialRun: true, print: l => lines.push(l),
      printErr: l => { if (this.onLog) this.onLog(l); } };
    this.factory(M);
    if (typeof M.callMain !== 'function') throw new Error('rtl_433 module did not initialise synchronously');
    M.FS.writeFile(file, bytes);
    const args = ['-r', file, '-s', String(this.rate), '-f', String(this.centerHz), '-F', 'json',
      '-M', 'level', '-M', 'protocol', '-M', 'time:rel', ...this.protocolArgs(), ...this.extraArgs];
    try { M.callMain(args); } catch (e) { if (!e || e.name !== 'ExitStatus') throw e; }

    // keep the tail for the next chunk
    if (this.tailN > 0) {
      const keep = Math.min(this.tailN, n);
      this.tailI = I.slice(n - keep); this.tailQ = Q.slice(n - keep);
    }
    this.streamN += n; this.fill = 0;
    const chunkIdx = this.stats.chunks++;
    this.stats.seconds += n / this.rate;
    this.stats.lines += lines.length;

    const streamEndS = this.streamN / this.rate;
    for (const l of lines) {
      if (l.charCodeAt(0) !== 123) { if (this.onLog) this.onLog(l); continue; }   // '{'
      let o; try { o = JSON.parse(l); } catch (e) { continue; }
      const rel = relSeconds(o.time);
      const ts = rel === null ? fileStartS : fileStartS + rel;
      if (this.dedupeS > 0 && this.isDupe(o, ts)) { this.stats.dupes++; continue; }
      o.ts = ts;
      o.t = this.wallAtEnd - (streamEndS - ts) * 1000;
      o.chunk = chunkIdx;
      if (typeof o.freq === 'number' && o.freq > 1000) o.freqHz = Math.round(o.freq * 1e6);
      else if (typeof o.freq1 === 'number' && o.freq1 > 1000) o.freqHz = Math.round(o.freq1 * 1e6);
      else o.freqHz = this.centerHz;
      if (g !== 1) { o.gainDb = +gainDb.toFixed(1); for (const k of ['rssi', 'noise']) if (typeof o[k] === 'number') o[k] = +(o[k] - gainDb).toFixed(1); }
      this.stats.decodes++;
      this.onMessage(o);
    }
    const ms = Date.now() - t0;
    this.stats.msLast = ms; this.stats.msTotal += ms; this.stats.msPerChunk = this.stats.msTotal / this.stats.chunks;
  }

  pack(tI, tQ, I, Q, g, total) {
    const m = tI.length, n = I.length;
    if (this.format === 'cu8') {
      const b = new Uint8Array(total * 2);
      let j = 0;
      for (let k = 0; k < m; k++) { b[j++] = u8(tI[k] * g); b[j++] = u8(tQ[k] * g); }
      for (let k = 0; k < n; k++) { b[j++] = u8(I[k] * g); b[j++] = u8(Q[k] * g); }
      return b;
    }
    if (this.format === 'cf32') {
      const f = new Float32Array(total * 2);
      let j = 0;
      for (let k = 0; k < m; k++) { f[j++] = tI[k] * g; f[j++] = tQ[k] * g; }
      for (let k = 0; k < n; k++) { f[j++] = I[k] * g; f[j++] = Q[k] * g; }
      return new Uint8Array(f.buffer);
    }
    const s = new Int16Array(total * 2);
    const sc = 32767 * g;
    let j = 0;
    for (let k = 0; k < m; k++) { s[j++] = s16(tI[k] * sc); s[j++] = s16(tQ[k] * sc); }
    for (let k = 0; k < n; k++) { s[j++] = s16(I[k] * sc); s[j++] = s16(Q[k] * sc); }
    return new Uint8Array(s.buffer);
  }

  // ------------------------------------------------------------ protocol list
  // "-R n" disables everything not listed, so enabling the wM-Bus extras needs the full default list.
  protocolArgs() {
    if (this.rArgs) return this.rArgs;
    let ids;
    if (this.protocols === 'default') ids = [];
    else if (Array.isArray(this.protocols)) ids = this.protocols;
    else {
      const list = Rtl433Live.protocolList(this.factory, this.wasm);
      ids = list.filter(p => !p.disabled || this.extraProtocols.includes(p.id)).map(p => p.id);
    }
    this.rArgs = [];
    for (const n of ids) this.rArgs.push('-R', String(n));
    return this.rArgs;
  }
  static protocolList(factory, wasm) {
    const cache = Rtl433Live._protoCache || (Rtl433Live._protoCache = new WeakMap());
    const key = wasm && typeof wasm === 'object' ? wasm : factory;
    if (cache.has(key)) return cache.get(key);
    const out = [];
    const M = { wasmBinary: wasm, noInitialRun: true, print: l => out.push(l), printErr: l => out.push(l) };
    factory(M);
    try { M.callMain(['-R', 'help']); } catch (e) { /* exits */ }
    const list = [];
    for (const l of out) {
      const m = /^\s*\[(\d+)\](\*?)\s+(.*)$/.exec(l.replace(/\x1b\[[0-9;]*m/g, ''));
      if (m) list.push({ id: +m[1], disabled: m[2] === '*', name: m[3].trim() });
    }
    cache.set(key, list);
    return list;
  }

  // ------------------------------------------------------------ dedupe
  isDupe(o, ts) {
    const parts = [];
    for (const k of Object.keys(o).sort()) if (!SKIP_KEYS.has(k)) parts.push(k + '=' + JSON.stringify(o[k]));
    const key = parts.join('|');
    const last = this.seen.get(key);
    this.seen.set(key, ts);
    if (this.seen.size > 4096) for (const [k, v] of this.seen) { if (ts - v > this.dedupeS) this.seen.delete(k); }
    return last !== undefined && ts - last < this.dedupeS;
  }
}

function u8(v) { v = (v * 127.5 + 128) | 0; return v < 0 ? 0 : v > 255 ? 255 : v; }
function s16(v) { return v < -32768 ? -32768 : v > 32767 ? 32767 : v; }
function relSeconds(t) {       // "@1.234567s" (time:rel in file mode)
  if (typeof t !== 'string') return null;
  const m = /([\d.]+)/.exec(t);
  return m ? parseFloat(m[1]) : null;
}

// ---------------------------------------------------------------- channelizer
// Wide stream (HackRF int8 IQ at inRate) -> complex baseband at outRate centred on offsetHz:
// NCO mix, then one or two FIR decimation stages (windowed sinc). inRate/outRate must be an integer.
//   const ch = new IqChannelizer(10e6, 433.92e6 - 434.4e6, 250e3);  const [I, Q] = ch.process(int8IQ);
function lowpassTaps(n, cutoff, fs) {
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
class FirDecim {
  constructor(taps, D) { this.h = taps; this.D = D; this.n = taps.length; this.hist = new Float32Array(this.n - 1 + 65536); this.have = this.n - 1; }
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
class IqChannelizer {
  constructor(inRate, offsetHz, outRate, bwHz) {
    const F = Math.round(inRate / outRate);
    if (F < 1 || Math.abs(inRate / outRate - F) > 1e-6) throw new Error(`IqChannelizer: ${inRate}/${outRate} is not an integer`);
    this.inRate = inRate; this.outRate = outRate; this.bw = bwHz || 0.8 * outRate;
    this.phase = 0; this.setOffset(offsetHz);
    let D1 = F;
    for (const d of [8, 6, 5, 4, 3, 2]) if (F % d === 0 && F / d > 1) { D1 = d; break; }
    if (F <= 8) D1 = F;
    const D2 = F / D1, r1 = inRate / D1;
    this.stages = [];
    if (D2 === 1) { if (D1 > 1) this.stages.push([new FirDecim(lowpassTaps(8 * D1 + 1, this.bw / 2, inRate), D1), new FirDecim(lowpassTaps(8 * D1 + 1, this.bw / 2, inRate), D1)]); }
    else {
      const t1 = lowpassTaps(6 * D1 + 1, Math.min(0.35 * r1, 0.5 * r1 - this.bw / 2), inRate), t2 = lowpassTaps(Math.max(16, 12 * D2) + 1, this.bw / 2, r1);
      this.stages.push([new FirDecim(t1, D1), new FirDecim(t1, D1)], [new FirDecim(t2, D2), new FirDecim(t2, D2)]);
    }
  }
  setOffset(hz) { this.dphi = -2 * Math.PI * hz / this.inRate; }
  // iq: Int8Array interleaved (scaled by 1/128) or [Float32Array I, Float32Array Q]
  process(iq) {
    let I, Q;
    if (Array.isArray(iq)) { I = iq[0].slice(); Q = iq[1].slice(); }
    else { const n = iq.length >> 1; I = new Float32Array(n); Q = new Float32Array(n); for (let i = 0, j = 0; i < n; i++, j += 2) { I[i] = iq[j] / 128; Q[i] = iq[j + 1] / 128; } }
    if (this.dphi !== 0) {
      const n = I.length; let c = Math.cos(this.phase), sn = Math.sin(this.phase);
      const dc = Math.cos(this.dphi), ds = Math.sin(this.dphi);
      for (let i = 0; i < n; i++) {
        const a = I[i], b = Q[i];
        I[i] = a * c - b * sn; Q[i] = a * sn + b * c;
        const nc = c * dc - sn * ds; sn = c * ds + sn * dc; c = nc;
        if ((i & 1023) === 1023) { const g = 1 / Math.sqrt(c * c + sn * sn); c *= g; sn *= g; }
      }
      this.phase = Math.atan2(sn, c);
    }
    for (const [fi, fq] of this.stages) { I = fi.process(I); Q = fq.process(Q); }
    return [I, Q];
  }
}

const api = { Rtl433Live, IqChannelizer };
if (typeof module !== 'undefined' && module.exports) module.exports = api;
else { root.Rtl433Live = Rtl433Live; root.IqChannelizer = IqChannelizer; }
})(typeof self !== 'undefined' ? self : this);
