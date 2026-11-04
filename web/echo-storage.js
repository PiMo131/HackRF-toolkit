// Echo tab: fragment library. IndexedDB in the browser, an in-memory backend in node tests. Same API either way.
//
// A fragment is compact ci8 narrowband IQ (echo-dsp.toCi8) plus metadata. Export/import is SigMF
// (.sigmf-meta + .sigmf-data) with the rfscan:echo_* extension fields; a round trip is bit-exact on the data.
"use strict";
(function (root, factory) {
  const node = typeof module !== 'undefined' && module.exports;
  const m = factory(node ? require('./echo-dsp.js') : root.EchoDsp);
  if (node) module.exports = m; else root.EchoStorage = m;
})(typeof self !== 'undefined' ? self : this, function (D) {

  const DB_NAME = 'rfscan.echo', STORE = 'fragments', META = 'meta';
  const DEFAULT_QUOTA = { maxClips: 100, maxBytes: 250 * 1024 * 1024 };
  const SIGMF_VERSION = '1.0.0';

  // ------------------------------------------------------------ SigMF
  // meta = the engine's clip metadata (channelCentreHz, iqRate, label, ...). data = Int8Array ci8.
  function toSigmf(meta, data) {
    const g = {
      'core:datatype': 'ci8', 'core:sample_rate': meta.iqRate, 'core:version': SIGMF_VERSION,
      'core:recorder': 'rfscan web Echo', 'core:hw': meta.hw || '', 'core:description': `Echo fragment at ${(meta.channelCentreHz / 1e6).toFixed(5)} MHz (${meta.label})`,
      'rfscan:echo_source': meta.source, 'rfscan:echo_label': meta.label, 'rfscan:echo_stop_reason': meta.stopReason,
      'rfscan:echo_late_lock': !!meta.lateLock, 'rfscan:echo_pre_roll_s': meta.preRollS || 0, 'rfscan:echo_continuation': !!meta.continuation,
      'rfscan:echo_rx_centre_hz': meta.rxCentreHz, 'rfscan:echo_hw_rate': meta.hwRate, 'rfscan:echo_store_gain': meta.storeGain,
      'rfscan:echo_snr_db': meta.snrDb, 'rfscan:echo_profile_ids': meta.profileIds || [], 'rfscan:echo_detect': meta.detect,
      'rfscan:echo_clip_fraction': meta.storeClipFraction, 'rfscan:echo_rx_clip_fraction': meta.rxClipFraction,
      'rfscan:echo_features': meta.features || null, 'rfscan:echo_gain': meta.gain || null, 'rfscan:echo_duration_s': meta.durationS,
    };
    const captures = [{ 'core:sample_start': 0, 'core:frequency': meta.channelCentreHz, 'core:datetime': meta.utc }];
    return { meta: { global: g, captures, annotations: [] }, data };
  }
  function fromSigmf(metaObj, data) {
    const g = metaObj.global || {}, cap = (metaObj.captures || [{}])[0], dt = g['core:datatype'] || 'ci8';
    if (dt !== 'ci8') throw new Error(`unsupported SigMF datatype "${dt}" (Echo imports ci8 only)`);
    const meta = {
      source: g['rfscan:echo_source'] || 'import', utc: cap['core:datetime'] || new Date().toISOString(),
      channelCentreHz: cap['core:frequency'] ?? g['rfscan:echo_channel_centre_hz'] ?? 0, iqRate: g['core:sample_rate'] || D.ECHO_RATE, format: 'ci8',
      rxCentreHz: g['rfscan:echo_rx_centre_hz'] ?? cap['core:frequency'] ?? 0, hwRate: g['rfscan:echo_hw_rate'] || null,
      label: g['rfscan:echo_label'] || 'import', stopReason: g['rfscan:echo_stop_reason'] || null, lateLock: !!g['rfscan:echo_late_lock'],
      preRollS: g['rfscan:echo_pre_roll_s'] || 0, continuation: !!g['rfscan:echo_continuation'], storeGain: g['rfscan:echo_store_gain'] || 1,
      snrDb: g['rfscan:echo_snr_db'], profileIds: g['rfscan:echo_profile_ids'] || [], detect: g['rfscan:echo_detect'] || null,
      storeClipFraction: g['rfscan:echo_clip_fraction'] || 0, rxClipFraction: g['rfscan:echo_rx_clip_fraction'] || 0,
      features: g['rfscan:echo_features'] || null, gain: g['rfscan:echo_gain'] || null, hw: g['core:hw'] || '',
      durationS: g['rfscan:echo_duration_s'] || (data.length >> 1) / (g['core:sample_rate'] || D.ECHO_RATE),
    };
    return meta;
  }
  const safeName = s => String(s).replace(/[^\w.-]+/g, '_').slice(0, 80);
  function fileName(rec) { const m = rec.meta; return `echo_${(m.channelCentreHz / 1e6).toFixed(5)}MHz_${safeName((m.utc || '').replace(/[:.]/g, '-'))}_${rec.id}`; }

  // ------------------------------------------------------------ backends
  class MemoryBackend {
    constructor() { this.map = new Map(); this.meta = { quota: { ...DEFAULT_QUOTA } }; }
    async init() { return this; }
    async put(rec) { this.map.set(rec.id, rec); }
    async get(id) { return this.map.get(id) || null; }
    async delete(id) { this.map.delete(id); }
    async all() { return [...this.map.values()]; }
    async getMeta() { return this.meta; }
    async setMeta(m) { this.meta = m; }
  }
  class IdbBackend {
    constructor(idb = (typeof indexedDB !== 'undefined' ? indexedDB : null)) { this.idb = idb; this.db = null; }
    init() {
      return new Promise((res, rej) => {
        if (!this.idb) return rej(new Error('IndexedDB not available'));
        const r = this.idb.open(DB_NAME, 1);
        r.onupgradeneeded = () => { const db = r.result; if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'id' }); if (!db.objectStoreNames.contains(META)) db.createObjectStore(META); };
        r.onsuccess = () => { this.db = r.result; res(this); };
        r.onerror = () => rej(r.error);
      });
    }
    _tx(store, mode, fn) {
      return new Promise((res, rej) => { const tx = this.db.transaction(store, mode), os = tx.objectStore(store); let out; try { out = fn(os); } catch (e) { rej(e); return; } tx.oncomplete = () => res(out && out.result !== undefined ? out.result : out); tx.onerror = () => rej(tx.error); tx.onabort = () => rej(tx.error || new Error('transaction aborted (quota?)')); });
    }
    put(rec) { return this._tx(STORE, 'readwrite', os => os.put(rec)); }
    get(id) { return this._tx(STORE, 'readonly', os => os.get(id)); }
    delete(id) { return this._tx(STORE, 'readwrite', os => os.delete(id)); }
    all() { return this._tx(STORE, 'readonly', os => os.getAll()); }
    async getMeta() { const m = await this._tx(META, 'readonly', os => os.get('meta')); return m || { quota: { ...DEFAULT_QUOTA } }; }
    setMeta(m) { return this._tx(META, 'readwrite', os => os.put(m, 'meta')); }
  }

  class EchoLibrary {
    constructor(backend) { this.backend = backend || new MemoryBackend(); this.quota = { ...DEFAULT_QUOTA }; this.listeners = new Set(); }
    async init() { await this.backend.init(); const m = await this.backend.getMeta(); if (m && m.quota) this.quota = { ...DEFAULT_QUOTA, ...m.quota }; return this; }
    onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    _emit() { for (const fn of this.listeners) try { fn(); } catch (e) { /* ignore */ } }
    async setQuota(q) { this.quota = { ...this.quota, ...q }; await this.backend.setMeta({ quota: this.quota }); this._emit(); }
    async list() { const all = await this.backend.all(); return all.map(stripData).sort((a, b) => (b.meta.utc || '').localeCompare(a.meta.utc || '')); }
    async stats() { const all = await this.backend.all(); return { count: all.length, bytes: all.reduce((s, r) => s + (r.data ? r.data.byteLength : 0), 0), quota: this.quota }; }
    async get(id) { return this.backend.get(id); }

    // add a fragment; throws a clear quota error instead of silently dropping anything
    async add({ data, meta }, { id, name, notes } = {}) {
      const u8 = data instanceof Int8Array ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
      const st = await this.stats();
      if (st.count + 1 > this.quota.maxClips) throw new QuotaError(`clip limit reached (${this.quota.maxClips}). Delete clips or raise the quota.`, 'clips');
      if (st.bytes + u8.byteLength > this.quota.maxBytes) throw new QuotaError(`storage limit reached (${(this.quota.maxBytes / 1048576).toFixed(0)} MB). Delete clips or raise the quota.`, 'bytes');
      const checksum = D.fnv1a(u8);
      const rec = { id: id || mkId(), name: name || '', notes: notes || '', created: Date.now(), bytes: u8.byteLength, checksum,
        meta: { ...meta, sizeBytes: u8.byteLength, numSamples: u8.byteLength >> 1 }, data: new Int8Array(u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength)) };
      try { await this.backend.put(rec); }
      catch (e) { throw new QuotaError(`could not store fragment: ${e.message}`, 'backend'); }
      this._emit();
      return stripData(rec);
    }
    async rename(id, name) { const r = await this.backend.get(id); if (!r) return; r.name = name; await this.backend.put(r); this._emit(); }
    async setNotes(id, notes) { const r = await this.backend.get(id); if (!r) return; r.notes = notes; await this.backend.put(r); this._emit(); }
    async remove(id) { await this.backend.delete(id); this._emit(); }
    async removeMany(ids) { for (const id of ids) await this.backend.delete(id); this._emit(); }

    // integrity: recompute the checksum of the stored bytes
    async verify(id) { const r = await this.backend.get(id); if (!r) return { ok: false, reason: 'not found' }; const u8 = new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength); const c = D.fnv1a(u8); return { ok: c === r.checksum, checksum: c, stored: r.checksum }; }
    // decoded complex floats for replay / viewing
    async readIq(id) { const r = await this.backend.get(id); if (!r) return null; const [I, Q] = D.fromCi8(r.data, r.meta.storeGain || 1); return { I, Q, meta: r.meta, rec: stripData(r) }; }

    async exportSigmf(id) {
      const r = await this.backend.get(id); if (!r) throw new Error('not found');
      const { meta, data } = toSigmf(r.meta, r.data);
      if (r.notes) meta.global['rfscan:echo_notes'] = r.notes;
      if (r.name) meta.global['rfscan:echo_name'] = r.name;
      return { name: fileName(r), metaJson: JSON.stringify(meta, null, 2), data: new Int8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength) };
    }
    async importSigmf(metaObj, data, { name } = {}) {
      const parsed = typeof metaObj === 'string' ? JSON.parse(metaObj) : metaObj;
      const meta = fromSigmf(parsed, data); meta.source = meta.source === 'live' ? 'import' : (meta.source || 'import');
      const g = parsed.global || {};
      return this.add({ data, meta }, { name: name || g['rfscan:echo_name'] || '', notes: g['rfscan:echo_notes'] || '' });
    }
  }
  class QuotaError extends Error { constructor(msg, kind) { super(msg); this.name = 'QuotaError'; this.kind = kind; } }
  function stripData(r) { const { data, ...rest } = r; return rest; }
  function mkId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  return { EchoLibrary, MemoryBackend, IdbBackend, QuotaError, toSigmf, fromSigmf, fileName, DEFAULT_QUOTA, DB_NAME };
});
