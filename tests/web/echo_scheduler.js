// node tests/web/echo_scheduler.js — the adaptive scheduler scenario from the spec, driven by the simulated radio.
"use strict";
const P = require('../../web/echo-profiles.js'), S = require('../../web/echo-scheduler.js'), Sim = require('../../web/echo-sim.js');
const { ok, eq, near, run } = require('./echo_assert.js');

function engineOn(radio, intervals, extra = {}) {
  const clips = [], logs = [];
  const eng = new S.EchoEngine({ radio, intervals, sched: { rxRate: 2e6, ...extra.sched }, capture: { detect: 'likely', ...extra.capture },
    onClip: m => clips.push(m.meta), onLog: l => logs.push(l) });
  return { eng, clips, logs };
}
async function runUntil(radio, eng, tEnd) { eng.start(); while (radio.t < tEnd && eng.running) await new Promise(r => setTimeout(r, 15)); await eng.stop(); }

run('echo-scheduler', t => {
  const store = new P.ProfileStore(new P.MemoryStorage());
  const ivs = store.intervals();   // the four defaults

  t('scheduler pure-planning: unit of intervals, segments and sweep ranges', () => {
    const sch = new S.EchoScheduler(ivs, { rxRate: 2e6 });
    ok(sch.ranges.length >= 1 && sch.ranges.length <= 10);
    ok(sch.segments.length >= 4);
    const act = sch.next(0);
    eq(act.type, 'survey');   // nothing known yet -> survey first
  });

  t('energy -> candidate, dwell promotes to confirmed DMR', async () => {
    const radio = new Sim.SimRadio({ emitters: [{ f: 446.03125e6, kind: 'dmr', amp: 0.05, bursts: [{ t0: 2, dur: 3 }, { t0: 10, dur: 3 }] }] });
    const { eng, clips } = engineOn(radio, ivs);
    await runUntil(radio, eng, 16);
    const st = eng.status();
    const c = st.candidates.find(c => Math.abs(c.f - 446.03125e6) < 7e3);
    ok(c, 'candidate created'); eq(c.bestLabel, 'confirmed_dmr');
    ok(clips.length >= 1, 'recorded at least one clip');
    ok(clips.every(m => m.label === 'confirmed_dmr'));
  });

  t('scenario: silent band, one hot candidate 3× then gone, later new cold-band activity', async () => {
    // hot candidate in pmr446 appears at t=3,13,23 then stops; new activity in the VHF band at t=40
    const radio = new Sim.SimRadio({ emitters: [
      { f: 446.08125e6, kind: 'dmr', amp: 0.05, bursts: [{ t0: 3, dur: 2.5 }, { t0: 13, dur: 2.5 }, { t0: 23, dur: 2.5 }] },
      { f: 145.6875e6, kind: 'dmr', amp: 0.05, bursts: [{ t0: 42, dur: 3 }] },
    ] });
    const { eng, clips } = engineOn(radio, ivs, { sched: { surveyS: 20, maxNoSurveyS: 30 } });
    await runUntil(radio, eng, 55);
    const st = eng.status();
    const hot = st.candidates.find(c => Math.abs(c.f - 446.08125e6) < 7e3);
    const late = st.candidates.find(c => Math.abs(c.f - 145.6875e6) < 7e3);
    ok(hot, 'hot candidate exists'); ok(late, 'late VHF candidate found by a revisit survey');
    ok(hot.dwellS > 0.5, 'hot candidate got real dwell time');
    // cold segments were still surveyed: every profile has been visited
    const unvisited = st.coverage.filter(c => c.lastVisitAgoS == null);
    eq(unvisited.length, 0, 'every profile visited at least once');
    ok(clips.some(m => Math.abs(m.channelCentreHz - 446.08125e6) < 7e3), 'hot clips');
    ok(clips.some(m => Math.abs(m.channelCentreHz - 145.6875e6) < 7e3), 'late VHF clip');
  });

  t('no duplicate candidates from overlapping presets; centroid jitter merges', () => {
    const iv = P.mergeIntervals([
      { id: 'a', ranges: [{ lo: 446.0e6, hi: 446.2e6 }], channels: [] },
      { id: 'b', ranges: [{ lo: 446.1e6, hi: 446.3e6 }], channels: [] },
    ]);
    const sch = new S.EchoScheduler(iv, {});
    const now = 0;
    const r1 = { ranges: [{ loHz: 446.0e6, df: 5e3, passes: [bump(441, 446.15e6, 446.0e6, 5e3), bump(441, 446.1505e6, 446.0e6, 5e3)] }], clipped: [] };
    sch.onSurvey(r1, now);
    eq(sch.cands.size, 1, 'one candidate despite two overlapping presets + jitter');
  });

  t('forced background survey happens even with a continuously-active source (no starvation)', async () => {
    const radio = new Sim.SimRadio({ emitters: [{ f: 446.08125e6, kind: 'dmr', amp: 0.05, bursts: [{ t0: 0, dur: 60 }] }] });
    const { eng } = engineOn(radio, ivs, { sched: { surveyS: 15, maxNoSurveyS: 20, alwaysOnDwells: 2 }, capture: { maxS: 2, maxSplits: 1 } });
    await runUntil(radio, eng, 55);
    ok(eng.sched.surveys >= 2, `surveys ran despite always-on source (${eng.sched.surveys})`);
    const c = eng.sched.candidates(radio.t).find(c => Math.abs(c.f - 446.08125e6) < 7e3);
    ok(c && c.alwaysOn >= 1, 'continuously-active source detected and de-prioritised');
  });

  t('coverage reports missed-while-recording and an honest no-100% note', async () => {
    const radio = new Sim.SimRadio({ emitters: [{ f: 446.08125e6, kind: 'dmr', amp: 0.05, bursts: [{ t0: 2, dur: 5 }] }] });
    const { eng } = engineOn(radio, ivs);
    await runUntil(radio, eng, 12);
    const cov = eng.status().coverage;
    ok(cov.every(c => /no 100% coverage claim/.test(c.coverageWarning)), 'honest coverage note on every profile');
  });
});

// helper: a single survey pass with a bump at freq `f` within a range starting at `lo`
function bump(nb, f, lo, df) { const a = new Float32Array(nb).fill(-100); const k = Math.round((f - lo) / df); if (k >= 0 && k < nb) { a[k] = -40; if (k + 1 < nb) a[k + 1] = -45; } return a; }
