// Echo tab: Lab TX. A real RF transmit pipeline, deliberately boxed in so it can ONLY send the user's own
// synthetic or lab-recorded fragments, to a frequency on a separate allowlist, after an explicit two-step arm.
//
// Hard rules enforced here (not just in the UI):
//   * RF TX is OFF after every page load, fault or reconnect. Nothing in Auto Echo can arm or transmit.
//   * The TX centre frequency comes ONLY from the Lab TX allowlist. The allowlist starts EMPTY, is edited
//     separately from the RX presets, and is never written from RX discovery or candidates.
//   * Only 'synthetic' or 'owned_lab_recording' sources transmit. 'live' and imported-unknown fragments never do.
//   * A confirmed lab test setup (dummy load / attenuator / shielded) must be ticked; the code records that a
//     checkbox is not physical proof.
//   * Arming expires. One transmission at a time. A hard timeout independent of the UI queue. Any fault -> RF OFF
//     and re-arm required. The sequence ALWAYS ends with MODE.OFF, including on exception / stop / disconnect.
//
// `transport` abstracts the device so this is testable with a mock (see tests). It must provide the real firmware
// TX endpoint and mode from the live USB descriptor, not an assumed endpoint number — echo.js builds that from WebUSB.
"use strict";
(function (root, factory) {
  const node = typeof module !== 'undefined' && module.exports;
  const m = factory(node ? require('./echo-dsp.js') : root.EchoDsp);
  if (node) module.exports = m; else root.EchoLabTx = m;
})(typeof self !== 'undefined' ? self : this, function (D) {

  const STATES = ['OFF', 'ARM_REQUESTED', 'ARMED', 'TRANSMITTING', 'COOLDOWN', 'FAULT'];
  const TX_SOURCES = new Set(['synthetic', 'owned_lab_recording']);     // the only transmittable sources, ever
  const LAB_DEFAULTS = {
    armWindowS: 120,          // arming expires this long after Arm
    maxClipS: 10,             // max single-clip duration that may be transmitted
    maxRepeats: 5,            // max repeats in one lab Auto Echo burst
    cooldownS: 3,             // forced RX re-acquire gap after any TX
    txGain: 0,               // TXVGA 0..47 dB, starts at minimum
    hardTimeoutS: 20,         // absolute cap on one transmission, independent of the UI queue
    chunkMs: 40, aheadMs: 120,
    allowlist: [],            // EMPTY by default; { centreHz, label, maxGain }
  };
  const MHZ = 1e6;

  // ------------------------------------------------------------ allowlist (separate store, empty by default)
  class TxAllowlist {
    constructor(storage, key = 'rfscan.echo.labtx.allowlist') { this.storage = storage; this.key = key; this.entries = this._load(); }
    _load() { try { const a = JSON.parse(this.storage && this.storage.getItem(this.key) || '[]'); return Array.isArray(a) ? a.filter(e => Number.isFinite(e.centreHz)) : []; } catch (e) { return []; } }
    _save() { try { this.storage && this.storage.setItem(this.key, JSON.stringify(this.entries)); } catch (e) { /* ignore */ } }
    list() { return this.entries.map(e => ({ ...e })); }
    has(centreHz) { return this.entries.some(e => e.centreHz === centreHz); }
    get(centreHz) { return this.entries.find(e => e.centreHz === centreHz) || null; }
    add({ centreHz, label = '', maxGain = 47 }) {
      centreHz = Math.round(centreHz);
      if (!Number.isFinite(centreHz) || centreHz < 1e6 || centreHz > 6000e6) throw new Error('allowlist frequency out of range');
      if (this.has(centreHz)) throw new Error('already in the allowlist');
      this.entries.push({ centreHz, label: String(label).slice(0, 60), maxGain: Math.max(0, Math.min(47, maxGain | 0)) }); this._save();
    }
    remove(centreHz) { this.entries = this.entries.filter(e => e.centreHz !== centreHz); this._save(); }
    clear() { this.entries = []; this._save(); }
  }

  // reasons a clip may not be transmitted (returns [] when it may, given an armed, verified setup)
  function sourceEligible(meta) {
    const r = [];
    if (!meta) return ['no metadata'];
    if (!TX_SOURCES.has(meta.source)) r.push(`source "${meta.source}" is not transmittable (only synthetic or owned_lab_recording)`);
    return r;
  }

  class LabTxController {
    constructor({ transport, allowlist, clock, onState, onLog, onStatus, maxClipS } = {}) {
      this.transport = transport; this.allowlist = allowlist || new TxAllowlist(memStore());
      this.clock = clock || { now: () => Date.now() / 1000, sleep: ms => new Promise(r => setTimeout(r, ms)) };
      this.onState = onState || (() => {}); this.onLog = onLog || (() => {}); this.onStatus = onStatus || (() => {});
      this.cfg = { ...LAB_DEFAULTS }; if (maxClipS) this.cfg.maxClipS = maxClipS;
      this.state = 'OFF'; this.enabled = false; this.arm = null; this.labVerified = false; this.txId = 0; this.busy = false;
      this.fault = null; this.events = [];
    }
    _set(s, info) { this.state = s; this.onState(s, info || {}); this._status(); }
    _log(e) { const rec = { t: this.clock.now(), ...e }; this.events.push(rec); if (this.events.length > 500) this.events.shift(); this.onLog(rec); }
    _status() { this.onStatus(this.status()); }
    status() {
      const now = this.clock.now();
      return { state: this.state, enabled: this.enabled, labVerified: this.labVerified, busy: this.busy,
        armed: this.state === 'ARMED' || this.state === 'TRANSMITTING',
        arm: this.arm ? { centreHz: this.arm.centreHz, expiresInS: Math.max(0, this.arm.expires - now), txGain: this.arm.txGain, maxClipS: this.arm.maxClipS, maxRepeats: this.arm.maxRepeats } : null,
        fault: this.fault, allowlistCount: this.allowlist.list().length };
    }

    // Step 1: enable the subsystem (a conscious switch). Still cannot transmit until setup is verified AND armed.
    enable(on) {
      this.enabled = !!on;
      if (!on) this.disarm('disabled');
      this._log({ kind: 'enable', on: this.enabled });
      this._status();
    }
    // The user confirms a lab setup (dummy load / attenuator / shielded, or wired second receiver). A checkbox is
    // not physical proof: this only records the user's confirmation; the README documents the required verification.
    setLabVerified(on, note = '') { this.labVerified = !!on; this._log({ kind: 'lab_verified', on: this.labVerified, note: String(note).slice(0, 200) }); this._status(); }

    // Step 2: arm one centre frequency from the allowlist, for a bounded window. Fails closed on any precondition.
    armFor(centreHz, { txGain, maxClipS, maxRepeats } = {}) {
      centreHz = Math.round(centreHz);
      const why = this._whyCannotArm(centreHz);
      if (why) { this._log({ kind: 'arm_refused', centreHz, reason: why }); throw new Error(why); }
      const entry = this.allowlist.get(centreHz), now = this.clock.now();
      this._set('ARM_REQUESTED', { centreHz });
      this.arm = { centreHz, txGain: Math.max(0, Math.min(entry.maxGain, txGain != null ? txGain : this.cfg.txGain)),
        maxClipS: Math.min(this.cfg.maxClipS, maxClipS || this.cfg.maxClipS), maxRepeats: Math.min(this.cfg.maxRepeats, maxRepeats || this.cfg.maxRepeats),
        expires: now + this.cfg.armWindowS, armedAt: now };
      this._set('ARMED', { centreHz, expiresInS: this.cfg.armWindowS });
      this._log({ kind: 'armed', centreHz, txGain: this.arm.txGain, maxClipS: this.arm.maxClipS, maxRepeats: this.arm.maxRepeats, windowS: this.cfg.armWindowS });
      return { ...this.arm };
    }
    _whyCannotArm(centreHz) {
      if (!this.enabled) return 'Lab TX is not enabled';
      if (this.busy || this.state === 'TRANSMITTING') return 'a transmission is in progress';
      if (this.fault) return `clear the fault first: ${this.fault}`;
      if (!this.transport) return 'no TX transport (device not connected)';
      if (!this.allowlist.list().length) return 'Lab TX allowlist is empty';
      if (!this.allowlist.has(centreHz)) return 'frequency is not in the Lab TX allowlist';
      if (!this.labVerified) return 'lab test setup not confirmed';
      return null;
    }
    armValid() { return this.state === 'ARMED' && this.arm && this.clock.now() < this.arm.expires; }
    disarm(reason = 'disarmed') { if (this.arm || this.state !== 'OFF') { this.arm = null; if (this.state !== 'FAULT') this._set('OFF', { reason }); this._log({ kind: 'disarmed', reason }); } }
    fault_(msg) { this.fault = msg; this.arm = null; this._set('FAULT', { error: msg }); this._log({ kind: 'fault', error: msg }); }
    clearFault() { if (this.state === 'FAULT') { this.fault = null; this._set('OFF', { reason: 'fault cleared' }); this._log({ kind: 'fault_cleared' }); } }

    // Transmit one fragment `repeat` times. Validates EVERYTHING again at call time (arming, allowlist, source,
    // duration, rate). `fragment` = { I, Q, rate, meta }. rxGuard(before/after) lets the engine free the radio.
    async transmit(fragment, { repeat = 1, pauseMs = 500, rxGuard } = {}) {
      const id = ++this.txId;
      const pre = this._whyCannotTransmit(fragment, repeat);
      if (pre) { this._log({ kind: 'tx_refused', id, reason: pre }); throw new Error(pre); }
      const arm = this.arm, centreHz = arm.centreHz;
      this.busy = true;
      const rep = Math.max(1, Math.min(arm.maxRepeats, repeat | 0));
      let txRate, offset, prepared;
      try {
        // prepare OUTSIDE the mode change: resample the owned/synthetic baseband to a HackRF TX rate, LO offset so
        // the channel sits off DC. The arm is consumed now; re-arm is required for the next transmission.
        txRate = await this.transport.pickTxRate(fragment.rate);
        offset = Math.min(txRate * 0.25, 500e3);
        prepared = this._prepare(fragment, txRate, offset, arm.txGain);
      } catch (e) { this.busy = false; this.fault_('TX prepare failed: ' + e.message); throw e; }
      this._set('TRANSMITTING', { id, centreHz, repeat: rep });
      this._log({ kind: 'tx_start', id, centreHz, txRate, offsetHz: offset, txGain: arm.txGain, repeat: rep, durationS: fragment.I.length / fragment.rate, checksum: prepared.checksum, source: fragment.meta.source });
      const t0 = this.clock.now();
      let sent = 0, ok = false, err = null;
      try {
        if (rxGuard) await rxGuard.before();                         // half-duplex: stop RX, hand over the device
        await this.transport.setFreq(centreHz - offset);             // tune below the lab freq by `offset`
        await this.transport.setTxRate(txRate);
        await this.transport.setTxGain(arm.txGain);
        for (let r = 0; r < rep; r++) {
          if (!this.busy) break;                                     // emergency stop
          await this._stream(id, prepared, txRate, t0);
          sent++;
          if (r + 1 < rep && this.busy) await this.clock.sleep(Math.max(0, Math.min(5000, pauseMs)));
        }
        ok = true;
      } catch (e) { err = e; }
      finally {
        // UNCONDITIONAL RF OFF, even on exception / stop / disconnect, as far as the device can still be reached
        try { await this.transport.modeOff(); } catch (e2) { err = err || e2; }
        this.busy = false; this.arm = null;
      }
      const dt = this.clock.now() - t0;
      if (err) { this.fault_(`TX fault: ${err.message}`); this._log({ kind: 'tx_fault', id, error: err.message, sentRepeats: sent, durationS: dt }); if (rxGuard) try { await rxGuard.after(); } catch (e) {} throw err; }
      this._set('COOLDOWN', { id }); this._log({ kind: 'tx_done', id, sentRepeats: sent, durationS: dt });
      await this.clock.sleep(this.cfg.cooldownS * 1000);             // forced RX re-acquire gap
      if (rxGuard) try { await rxGuard.after(); } catch (e) {}        // restore RX / AGC
      this._set('OFF', { reason: 'transmission complete; re-arm for another' });
      return { id, sentRepeats: sent, durationS: dt };
    }
    _whyCannotTransmit(fragment, repeat) {
      if (!this.armValid()) return this.state === 'ARMED' ? 'arming has expired; re-arm' : 'not armed';
      if (!fragment || !fragment.I || !fragment.I.length) return 'empty fragment';
      const elig = sourceEligible(fragment.meta); if (elig.length) return elig.join('; ');
      const durS = fragment.I.length / fragment.rate;
      if (durS > this.arm.maxClipS + 1e-6) return `fragment ${durS.toFixed(1)} s exceeds the ${this.arm.maxClipS} s lab limit`;
      if (repeat > this.arm.maxRepeats) return `repeat ${repeat} exceeds the ${this.arm.maxRepeats} lab limit`;
      if (!this.transport) return 'device not connected';
      return null;
    }
    _prepare(fragment, txRate, offset, txGain) {
      const interp = new D.TxInterpolator(fragment.I, fragment.Q, { inRate: fragment.rate, outRate: txRate, offsetHz: offset, level: 0.7 });
      // render once into memory so streaming cannot stall on DSP; cap to the hard-timeout worth of samples
      const cap = Math.ceil(this.cfg.hardTimeoutS * txRate), parts = []; let total = 0;
      while (!interp.done && total < cap * 2) { const p = interp.pull(1 << 16); parts.push(p); total += p.length; }
      const data = new Int8Array(total); let o = 0; for (const p of parts) { data.set(p, o); o += p.length; }
      return { data, checksum: D.fnv1a(new Uint8Array(data.buffer, data.byteOffset, data.byteLength)), limited: interp.limited, peak: interp.peak, samples: total >> 1 };
    }
    async _stream(id, prepared, txRate, t0) {
      const chunk = Math.max(1, Math.round(txRate * this.cfg.chunkMs / 1000)) * 2;
      const ahead = Math.max(chunk, Math.round(txRate * this.cfg.aheadMs / 1000) * 2);
      let p = 0, queued = 0;
      while (p < prepared.data.length) {
        if (!this.busy) throw new Error('stopped');                                // emergency stop mid-stream
        if (this.clock.now() - t0 > this.cfg.hardTimeoutS) throw new Error('hard TX timeout');
        const end = Math.min(prepared.data.length, p + ahead - queued);
        const slice = prepared.data.subarray(p, Math.min(prepared.data.length, p + chunk));
        const r = await this.transport.sendTx(slice);                              // pace on transfer completion
        if (r && r.status && r.status !== 'ok') throw new Error('USB transfer ' + r.status);
        p += slice.length; queued = Math.max(0, (end - p));
      }
      await this.transport.flushTx();                                              // ensure the tail reaches the air before MODE.OFF
    }
    emergencyStop() { const was = this.busy || this.state === 'TRANSMITTING'; this.busy = false; this._log({ kind: 'emergency_stop', was }); return was; }
    // wholesale shutdown on device loss: RF off, drop arming, no false "armed" state survives
    onDeviceLost() { this.busy = false; this.arm = null; this.transport = null; if (this.state === 'TRANSMITTING') this.fault_('device lost during TX'); else this.disarm('device lost'); }
  }
  function memStore() { const m = new Map(); return { getItem: k => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; }

  return { LabTxController, TxAllowlist, STATES, TX_SOURCES, LAB_DEFAULTS, sourceEligible };
});
