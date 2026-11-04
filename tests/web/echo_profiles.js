// node tests/web/echo_profiles.js — profiles, interval union, sweep ranges, RX windows, channel-list import, store.
"use strict";
const P = require('../../web/echo-profiles.js');
const { ok, eq, near, run } = require('./echo_assert.js');

run('echo-profiles', t => {
  t('defaults are the four standard-on presets', () => {
    const s = new P.ProfileStore(new P.MemoryStorage());
    eq(s.selected().map(p => p.id).sort(), ['amateur_uhf_high', 'amateur_uhf_low', 'amateur_vhf_rpt', 'pmr446'].sort());
  });

  t('interval union merges overlapping/adjacent and keeps all profile ids', () => {
    const profs = [
      { id: 'a', ranges: [{ lo: 430e6, hi: 431e6 }], channels: [] },
      { id: 'b', ranges: [{ lo: 430.5e6, hi: 432e6 }], channels: [] },     // overlaps a
      { id: 'c', ranges: [{ lo: 438e6, hi: 439e6 }], channels: [] },       // separate
    ];
    const iv = P.mergeIntervals(profs);
    eq(iv.length, 2);
    eq(iv[0].lo, 430e6); eq(iv[0].hi, 432e6); eq(iv[0].profileIds.sort(), ['a', 'b']);
    eq(iv[1].profileIds, ['c']);
  });

  t('no double-visit from overlapping presets (union has one interval)', () => {
    const s = new P.ProfileStore(new P.MemoryStorage());
    s.upsert({ id: 'x', name: 'X', ranges: [{ lo: 446.0e6, hi: 446.1e6 }], channels: [], builtin: false });
    s.clear(); s.select('pmr446', true); s.select('x', true);
    const iv = s.intervals();
    const pmr = iv.filter(i => i.lo < 446.2e6 && i.hi > 446.0e6);
    eq(pmr.length, 1);  // merged, not two overlapping intervals
  });

  t('sweep ranges: whole 20 MHz steps, <=10 ranges, merged', () => {
    const iv = P.mergeIntervals([
      { id: 'v', ranges: [{ lo: 145.6e6, hi: 145.8e6 }], channels: [] },
      { id: 'u', ranges: [{ lo: 430e6, hi: 470e6 }], channels: [] },
    ]);
    const rs = P.sweepRanges(iv);
    ok(rs.length <= P.MAX_SWEEP_RANGES);
    for (const r of rs) ok((r.hiMHz - r.loMHz) % P.SWEEP_STEP_MHZ === 0, 'whole 20 MHz step');
    eq(rs[0].loMHz, 145);
    ok(rs.some(r => r.loMHz <= 430 && r.hiMHz >= 470));
  });

  t('sweep ranges collapse to <=10 when many intervals', () => {
    const profs = [];
    for (let i = 0; i < 20; i++) profs.push({ id: 'p' + i, ranges: [{ lo: (100 + i * 30) * 1e6, hi: (105 + i * 30) * 1e6 }], channels: [] });
    const rs = P.sweepRanges(P.mergeIntervals(profs));
    ok(rs.length <= P.MAX_SWEEP_RANGES, `got ${rs.length}`);
  });

  t('RX window keeps channels off DC and off the edges', () => {
    const fc = P.windowFor([438.1e6, 438.15e6], { rate: 8e6 });
    ok(fc !== null);
    for (const f of [438.1e6, 438.15e6]) ok(P.usable(fc, f, { ...P.WIN_DEFAULTS, rate: 8e6 }), 'usable');
  });

  t('planWindows samples the band and reports what it cannot (no 100% claim)', () => {
    const iv = [{ lo: 446.0e6, hi: 446.2e6, profileIds: ['pmr446'], channels: [] }];
    const plan = P.planWindows(iv, { rate: 8e6 });
    ok(plan.windows.length >= 1);
    ok(plan.sampledHz > 0 && plan.sampledHz <= plan.requestedHz + 1);
  });

  t('channel-list JSON import: validate raster, dedupe, bounds', () => {
    const res = P.parseChannelList({ name: 'test', step_hz: 12500, band: { lo_mhz: 438, hi_mhz: 439 },
      channels: [{ freq_mhz: 438.0125 }, { freq_mhz: 438.025 }, { freq_mhz: 438.0125 }] });
    eq(res.errors, []);
    eq(res.profile.channels.length, 2);          // duplicate dropped
    ok(res.warnings.some(w => /duplicate/.test(w)));
  });

  t('channel-list rejects out-of-band / bad frequencies', () => {
    const res = P.parseChannelList([438.0, 9000.0]);   // 9 GHz is out of HackRF range
    ok(res.errors.length > 0);
  });

  t('store persists and restores selection and custom profiles', () => {
    const mem = new P.MemoryStorage();
    const s1 = new P.ProfileStore(mem);
    s1.clear(); s1.select('pmr446', true);
    s1.upsert({ id: 'mine', name: 'Mine', ranges: [{ lo: 433e6, hi: 434e6 }], channels: [], builtin: false });
    s1.select('mine', true);
    const s2 = new P.ProfileStore(mem);
    ok(s2.isSelected('pmr446')); ok(s2.isSelected('mine'));
    eq(s2.get('mine').name, 'Mine');
  });

  t('editing a built-in stores an override; Delete restores the preset', () => {
    const s = new P.ProfileStore(new P.MemoryStorage());
    s.upsert({ id: 'pmr446', name: 'PMR edited', ranges: [{ lo: 446e6, hi: 446.2e6 }], channels: [], builtin: true });
    eq(s.get('pmr446').name, 'PMR edited'); ok(s.get('pmr446').edited);
    eq(s.remove('pmr446'), 'reset');
    eq(s.get('pmr446').name, P.PRESETS.find(p => p.id === 'pmr446').name);
  });

  t('zero selected profiles -> empty intervals (scan cannot start)', () => {
    const s = new P.ProfileStore(new P.MemoryStorage());
    s.clear();
    eq(s.intervals().length, 0);
  });
});
