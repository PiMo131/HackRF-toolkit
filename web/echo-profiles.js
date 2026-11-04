// Echo tab: receive-frequency profiles, interval union, sweep ranges and RX window planning.
//
// These are provisional, editable RECEIVE presets. They are not a DMR frequency database and never a
// transmit authorisation: the Lab TX allowlist (echo-lab-tx.js) is a separate list that starts empty and
// is never filled from here.
//
// Loads as a classic script (browser / worker: global EchoProfiles) or with require() in node.
"use strict";
(function (root, factory) {
  const m = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = m; else root.EchoProfiles = m;
})(typeof self !== 'undefined' ? self : this, function () {

  const MHZ = 1e6;
  // Source/revision of the preset table. Bump `revision` when a preset changes; stored selections keep working
  // because they reference profile ids, and the stored copy records which revision it was made against.
  const PRESET_SOURCE = {
    revision: '2026-11-04.1',
    source: 'HackRF Toolkit Echo scope v1.2, section 2 (provisional receive presets, to be tuned per country)',
    note: 'Not a complete DMR frequency database; not a transmit authorisation. See https://www.rdi.nl for Dutch licence conditions.',
  };
  const PRESETS = [
    { id: 'amateur_vhf_rpt', name: 'Amateur DMR VHF — candidate repeater outputs', ranges: [[145.600, 145.800]], defaultOn: true, note: 'Not exclusively DMR' },
    { id: 'amateur_uhf_low', name: 'Amateur DMR UHF — low', ranges: [[430.000, 430.900]], defaultOn: true, note: 'Receive candidates, tune per country' },
    { id: 'amateur_uhf_high', name: 'Amateur DMR UHF — high', ranges: [[438.000, 438.500]], defaultOn: true, note: 'Receive candidates, not exhaustive' },
    { id: 'pmr446', name: 'PMR446 — analogue/digital mixed', ranges: [[446.000, 446.200]], defaultOn: true, note: 'Not everything in this band is DMR' },
    { id: 'business_vhf', name: 'Professional VHF — wide', ranges: [[148.000, 174.000]], defaultOn: false, note: 'Wide energy search space, long revisit time' },
    { id: 'business_uhf', name: 'Professional UHF — wide', ranges: [[400.000, 470.000]], defaultOn: false, note: 'Large overlap with other profiles' },
  ].map(p => ({ ...p, ranges: p.ranges.map(([a, b]) => ({ lo: Math.round(a * MHZ), hi: Math.round(b * MHZ) })), channels: [], step: 12500, builtin: true }));

  const HW_MIN = 1e6, HW_MAX = 6000e6;          // HackRF tuning range used by this tool
  const STORE_KEY = 'rfscan.echo.profiles';
  const STORE_VERSION = 1;
  const CH_HALF = 6250;                         // half width of a 12.5 kHz channel

  const clone = o => JSON.parse(JSON.stringify(o));

  // ------------------------------------------------------------ validation
  function validateProfile(p) {
    const errors = [];
    if (!p || typeof p !== 'object') return ['profile is not an object'];
    if (!p.id || typeof p.id !== 'string') errors.push('missing id');
    if (!p.name || typeof p.name !== 'string') errors.push('missing name');
    const ranges = p.ranges || [], chans = p.channels || [];
    if (!ranges.length && !chans.length) errors.push('profile needs at least one range or one channel');
    for (const r of ranges) {
      if (!Number.isFinite(r.lo) || !Number.isFinite(r.hi)) errors.push('range bounds must be numbers (Hz)');
      else if (r.lo >= r.hi) errors.push(`range ${r.lo / MHZ}–${r.hi / MHZ} MHz: start must be below end`);
      else if (r.lo < HW_MIN || r.hi > HW_MAX) errors.push(`range ${r.lo / MHZ}–${r.hi / MHZ} MHz outside ${HW_MIN / MHZ}–${HW_MAX / MHZ} MHz`);
    }
    for (const f of chans) if (!Number.isFinite(f) || f < HW_MIN || f > HW_MAX) errors.push(`channel ${f} Hz out of range`);
    if (p.step !== undefined && !(p.step > 0)) errors.push('channel step must be positive');
    return errors;
  }

  // Channel list JSON: either [438.0125, 438.025, ...] (MHz) or
  // { name, step_hz, band: {lo_mhz, hi_mhz}, channels: [ {freq_mhz | f_hz, label}, ... ] }
  function parseChannelList(json, { id, name } = {}) {
    let data = json;
    const errors = [], warnings = [];
    if (typeof json === 'string') { try { data = JSON.parse(json); } catch (e) { return { errors: ['not valid JSON: ' + e.message], warnings, profile: null }; } }
    const list = Array.isArray(data) ? data : (data && Array.isArray(data.channels) ? data.channels : null);
    if (!list) return { errors: ['expected an array of channels or an object with a "channels" array'], warnings, profile: null };
    const step = data.step_hz !== undefined ? +data.step_hz : 12500;
    if (!(step > 0)) errors.push('step_hz must be positive');
    const band = data.band ? { lo: +data.band.lo_mhz * MHZ, hi: +data.band.hi_mhz * MHZ } : null;
    if (band && !(band.lo < band.hi)) errors.push('band.lo_mhz must be below band.hi_mhz');
    const seen = new Map(), channels = [], labels = {};
    list.forEach((c, i) => {
      let f = NaN, label = '';
      if (typeof c === 'number') f = c * MHZ;
      else if (c && typeof c === 'object') { f = c.f_hz !== undefined ? +c.f_hz : +c.freq_mhz * MHZ; label = c.label ? String(c.label).slice(0, 60) : ''; }
      f = Math.round(f);
      if (!Number.isFinite(f)) { errors.push(`entry ${i}: no frequency`); return; }
      if (f < HW_MIN || f > HW_MAX) { errors.push(`entry ${i}: ${f / MHZ} MHz outside the HackRF range`); return; }
      if (band && (f < band.lo || f > band.hi)) { errors.push(`entry ${i}: ${f / MHZ} MHz outside band ${band.lo / MHZ}–${band.hi / MHZ} MHz`); return; }
      if (step > 0) {
        const base = band ? band.lo : 0, k = (f - base) / step;
        if (Math.abs(k - Math.round(k)) * step > 1) warnings.push(`entry ${i}: ${f / MHZ} MHz is not on the ${step / 1e3} kHz raster`);
      }
      if (seen.has(f)) { warnings.push(`entry ${i}: duplicate of entry ${seen.get(f)} (${f / MHZ} MHz), dropped`); return; }
      seen.set(f, i); channels.push(f); if (label) labels[f] = label;
    });
    if (!channels.length && !errors.length) errors.push('no channels');
    channels.sort((a, b) => a - b);
    const profile = errors.length ? null : {
      id: id || 'custom_list_' + Date.now().toString(36), name: name || (data && data.name) || `Channel list (${channels.length})`,
      ranges: [], channels, labels, step, builtin: false, defaultOn: false, note: 'imported channel list',
    };
    return { errors, warnings, profile };
  }

  // ------------------------------------------------------------ interval union
  // profiles -> sorted, merged [{lo, hi, profileIds, channels}] (Hz). Overlapping or adjacent (gap <= tol)
  // intervals merge; every merged interval keeps the ids of all profiles that contributed to it.
  function mergeIntervals(profiles, tol = 0) {
    const raw = [];
    for (const p of profiles) {
      for (const r of p.ranges || []) raw.push({ lo: r.lo, hi: r.hi, ids: [p.id], chans: [] });
      for (const f of p.channels || []) raw.push({ lo: f - CH_HALF, hi: f + CH_HALF, ids: [p.id], chans: [f] });
    }
    raw.sort((a, b) => a.lo - b.lo || a.hi - b.hi);
    const out = [];
    for (const r of raw) {
      const last = out[out.length - 1];
      if (last && r.lo <= last.hi + tol) {
        last.hi = Math.max(last.hi, r.hi);
        for (const id of r.ids) if (!last.profileIds.includes(id)) last.profileIds.push(id);
        for (const c of r.chans) if (!last.channels.includes(c)) last.channels.push(c);
      } else out.push({ lo: r.lo, hi: r.hi, profileIds: [...r.ids], channels: [...r.chans] });
    }
    for (const o of out) o.channels.sort((a, b) => a - b);
    return out;
  }
  const totalSpan = ivs => ivs.reduce((s, i) => s + (i.hi - i.lo), 0);
  // which profile ids cover frequency f
  function profilesAt(intervals, f) { for (const i of intervals) if (f >= i.lo && f <= i.hi) return i.profileIds; return []; }

  // ------------------------------------------------------------ firmware sweep ranges
  // The firmware sweep steps 20 MHz from each range's start (whole MHz), max 10 ranges. Expand every interval to
  // whole 20 MHz steps, merge, and if there are more than 10 close the smallest gaps first.
  const SWEEP_STEP_MHZ = 20, MAX_SWEEP_RANGES = 10;
  function sweepRanges(intervals) {
    let rs = intervals.map(i => {
      const lo = Math.max(0, Math.floor(i.lo / MHZ));
      const hi = lo + Math.max(1, Math.ceil((i.hi / MHZ - lo) / SWEEP_STEP_MHZ)) * SWEEP_STEP_MHZ;
      return { loMHz: lo, hiMHz: Math.min(hi, 7250) };
    }).sort((a, b) => a.loMHz - b.loMHz);
    const merged = [];
    for (const r of rs) {
      const last = merged[merged.length - 1];
      if (last && r.loMHz <= last.hiMHz) {
        // re-expand so the merged range is still a whole number of steps from its start
        const hi = Math.max(last.hiMHz, r.hiMHz);
        last.hiMHz = last.loMHz + Math.ceil((hi - last.loMHz) / SWEEP_STEP_MHZ) * SWEEP_STEP_MHZ;
      } else merged.push({ ...r });
    }
    rs = merged;
    while (rs.length > MAX_SWEEP_RANGES) {
      let best = 1, gap = Infinity;
      for (let k = 1; k < rs.length; k++) { const g = rs[k].loMHz - rs[k - 1].hiMHz; if (g < gap) { gap = g; best = k; } }
      const a = rs[best - 1], b = rs[best];
      a.hiMHz = a.loMHz + Math.ceil((b.hiMHz - a.loMHz) / SWEEP_STEP_MHZ) * SWEEP_STEP_MHZ;
      rs.splice(best, 1);
    }
    return rs;
  }
  const sweepSteps = rs => rs.reduce((s, r) => s + (r.hiMHz - r.loMHz) / SWEEP_STEP_MHZ, 0);

  // ------------------------------------------------------------ RX windows
  // Usable part of a capture at `rate`: |f - fc| in [dcGuard + hb, edge*rate/2 - hb]. Keeps signals off the
  // DC spike and off the band edges where the baseband filter rolls off.
  const WIN_DEFAULTS = { rate: 8e6, edge: 0.8, dcGuard: 150e3, hb: CH_HALF + 2e3 };
  function usable(fc, f, o) { const d = Math.abs(f - fc); return d >= o.dcGuard + o.hb && d <= o.edge * o.rate / 2 - o.hb; }
  // centre for a set of channel frequencies, or null if they do not fit in one capture
  function windowFor(freqs, opts = {}) {
    const o = { ...WIN_DEFAULTS, ...opts };
    if (!freqs.length) return null;
    const lo = Math.min(...freqs), hi = Math.max(...freqs), half = o.edge * o.rate / 2 - o.hb;
    if (hi - lo > 2 * half) return null;
    let best = null, bestScore = -Infinity;
    const step = 5e3;
    for (let fc = hi - half; fc <= lo + half; fc += step) {
      let ok = true, score = Infinity;
      for (const f of freqs) {
        if (!usable(fc, f, o)) { ok = false; break; }
        const d = Math.abs(f - fc);
        score = Math.min(score, d - o.dcGuard - o.hb, half - d);
      }
      if (ok && score > bestScore) { bestScore = score; best = fc; }
    }
    return best === null ? null : Math.round(best / 1e3) * 1e3;
  }
  // Greedy cover of the merged intervals by RX windows (for watchlists and the coverage table). Each window
  // reports the parts of the intervals it really samples; whatever no window samples is reported as a gap.
  function planWindows(intervals, opts = {}) {
    const o = { ...WIN_DEFAULTS, ...opts }, half = o.edge * o.rate / 2, inner = o.dcGuard;
    const side = half - inner;                                    // width of one usable side band
    const windows = [];
    for (const iv of intervals) {
      let f = iv.lo;
      while (f < iv.hi) {
        // lower side band [fc - half, fc - inner] starts at f; the upper side band [fc + inner, fc + half] is a bonus.
        // The DC hole between them is covered by the next window, whose lower band starts where this one ended.
        const fc = f + half, parts = [{ lo: f, hi: Math.min(iv.hi, fc - inner) }];
        const a2 = fc + inner, b2 = Math.min(iv.hi, fc + half);
        if (b2 > a2) parts.push({ lo: a2, hi: b2 });
        windows.push({ fc: Math.round(fc), rate: o.rate, parts, profileIds: iv.profileIds });
        f = b2 >= iv.hi && b2 > a2 ? iv.hi : parts[0].hi;
      }
    }
    const covered = windows.reduce((s, w) => s + w.parts.reduce((t, p) => t + p.hi - p.lo, 0), 0);
    return { windows, sampledHz: covered, requestedHz: totalSpan(intervals), side };
  }

  // ------------------------------------------------------------ persistent store
  // storage: anything with getItem/setItem (localStorage in the page, a Map-backed shim in tests)
  class ProfileStore {
    constructor(storage, key = STORE_KEY) {
      this.storage = storage; this.key = key;
      this.state = this._load();
    }
    _fresh() { return { version: STORE_VERSION, presetRevision: PRESET_SOURCE.revision, custom: [], overrides: {}, selected: PRESETS.filter(p => p.defaultOn).map(p => p.id) }; }
    _load() {
      let s = null;
      try { s = JSON.parse(this.storage && this.storage.getItem(this.key) || 'null'); } catch (e) { s = null; }
      if (!s || typeof s !== 'object' || s.version !== STORE_VERSION) return this._fresh();
      s.custom = (s.custom || []).filter(p => !validateProfile(p).length);
      s.overrides = s.overrides || {};
      s.selected = Array.isArray(s.selected) ? s.selected : [];
      const ids = new Set([...PRESETS, ...s.custom].map(p => p.id));
      s.selected = s.selected.filter(id => ids.has(id));
      return s;
    }
    save() { try { this.storage && this.storage.setItem(this.key, JSON.stringify({ ...this.state, presetRevision: PRESET_SOURCE.revision })); } catch (e) { /* quota / private mode */ } }
    all() {
      const st = this.state || { custom: [], overrides: {} };
      return [...PRESETS.map(p => st.overrides[p.id] ? { ...clone(p), ...clone(st.overrides[p.id]), id: p.id, builtin: true, edited: true } : clone(p)), ...clone(st.custom)];
    }
    get(id) { return this.all().find(p => p.id === id) || null; }
    selected() { const s = new Set(this.state.selected); return this.all().filter(p => s.has(p.id)); }
    isSelected(id) { return this.state.selected.includes(id); }
    select(id, on) {
      const s = new Set(this.state.selected);
      if (on) { if (!this.get(id)) throw new Error('unknown profile ' + id); s.add(id); } else s.delete(id);
      this.state.selected = this.all().map(p => p.id).filter(x => s.has(x)); this.save();
    }
    selectAll() { this.state.selected = this.all().map(p => p.id); this.save(); }
    clear() { this.state.selected = []; this.save(); }
    defaults() { this.state.selected = PRESETS.filter(p => p.defaultOn).map(p => p.id); this.save(); }
    // add or replace a custom profile; editing a built-in stores an override (Delete on it restores the preset)
    upsert(p) {
      const errs = validateProfile(p);
      if (errs.length) throw new Error(errs.join('; '));
      const builtin = PRESETS.find(x => x.id === p.id);
      if (builtin) { const { id, builtin: _b, edited: _e, ...rest } = clone(p); this.state.overrides[p.id] = rest; }
      else {
        const i = this.state.custom.findIndex(x => x.id === p.id);
        const v = { ...clone(p), builtin: false };
        if (i >= 0) this.state.custom[i] = v; else this.state.custom.push(v);
      }
      this.save(); return this.get(p.id);
    }
    duplicate(id) {
      const p = this.get(id); if (!p) throw new Error('unknown profile ' + id);
      const copy = { ...p, id: 'custom_' + Date.now().toString(36) + Math.floor(Math.random() * 1e4), name: p.name + ' (copy)', builtin: false, edited: undefined, defaultOn: false };
      delete copy.edited;
      return this.upsert(copy);
    }
    remove(id) {
      if (PRESETS.some(p => p.id === id)) { delete this.state.overrides[id]; this.save(); return 'reset'; }
      this.state.custom = this.state.custom.filter(p => p.id !== id);
      this.state.selected = this.state.selected.filter(x => x !== id);
      this.save(); return 'deleted';
    }
    intervals(tol) { return mergeIntervals(this.selected(), tol); }
  }
  // tiny storage shim for node / private browsing
  class MemoryStorage { constructor() { this.m = new Map(); } getItem(k) { return this.m.has(k) ? this.m.get(k) : null; } setItem(k, v) { this.m.set(k, String(v)); } removeItem(k) { this.m.delete(k); } }

  return { PRESETS, PRESET_SOURCE, HW_MIN, HW_MAX, CH_HALF, SWEEP_STEP_MHZ, MAX_SWEEP_RANGES, WIN_DEFAULTS,
    validateProfile, parseChannelList, mergeIntervals, totalSpan, profilesAt, sweepRanges, sweepSteps, windowFor, planWindows, usable,
    ProfileStore, MemoryStorage };
});
