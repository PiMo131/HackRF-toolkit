// node tests/web/echo_capture.js — trigger: pre-roll, hang, cap/truncation, late_lock, separate bursts, detect gating.
"use strict";
const D = require('../../web/echo-dsp.js'), C = require('../../web/echo-capture.js');
const { ok, eq, near, run } = require('./echo_assert.js');

function feed(iq, opts) {
  const [I, Q] = iq, n0 = 2 * 0.003 ** 2 / 50e3, tr = new C.ChannelTrigger(0, { n0, ...opts }), clips = [];
  for (let o = 0; o < I.length; o += 2500) for (const e of tr.process(I.subarray(o, o + 2500), Q.subarray(o, o + 2500))) if (e.event === 'clip') clips.push(e.clip);
  const a = tr.abort('stopped'); if (a && a.event === 'clip') clips.push(a.clip);
  return clips;
}

run('echo-capture', t => {
  t('two separate bursts -> two clips with pre-roll and end-of-transmission', () => {
    const clips = feed(D.synthDmrLike({ seconds: 4, bursts: [{ t0: 0.6, dur: 1.0 }, { t0: 2.5, dur: 0.7 }] }));
    eq(clips.length, 2);
    for (const c of clips) { eq(c.stopReason, 'end_of_transmission'); near(c.preRollS, 0.3, 0.12, 'pre-roll'); ok(!c.lateLock); }
  });

  t('signal already on at dwell start is flagged late_lock with no pre-roll', () => {
    const [c] = feed(D.synthDmrLike({ seconds: 2, bursts: [{ t0: 0, dur: 1.2 }] }));
    ok(c.lateLock); near(c.preRollS, 0, 0.05);
  });

  t('hard cap truncates and splits a continuous signal', () => {
    const clips = feed(D.synthDmrLike({ seconds: 4, bursts: [{ t0: 0.5, dur: 3.3 }] }), { maxS: 1, maxSplits: 1 });
    ok(clips.length >= 2, 'split into >=2');
    eq(clips[0].stopReason, 'truncated_by_limit');
    for (const c of clips) ok(c.durationS <= 1.0001, 'within cap');
    ok(clips[1].continuation);
  });

  t('hang time ends the clip ~500 ms after the signal stops', () => {
    const [c] = feed(D.synthDmrLike({ seconds: 2, bursts: [{ t0: 0.4, dur: 0.6 }] }), { hangMs: 500 });
    // duration = pre-roll + signal + hang
    near(c.durationS, 0.3 + 0.6 + 0.5, 0.2, 'duration incl hang');
  });

  t('detect=confirmed does not record mere activity (FM rejected)', () => {
    eq(feed(D.synthFm({ seconds: 2, bursts: [{ t0: 0.5, dur: 1 }] }), { detect: 'confirmed' }).length, 0);
  });

  t('detect=any records analogue FM activity', () => {
    const clips = feed(D.synthFm({ seconds: 2, bursts: [{ t0: 0.5, dur: 1 }] }), { detect: 'any' });
    ok(clips.length >= 1); eq(clips[0].label, 'activity');
  });

  t('TDMA (keyed every other slot) confirmed when asked', () => {
    const clips = feed(D.synthDmrLike({ seconds: 2, mode: 'tdma', bursts: [{ t0: 0.4, dur: 1 }] }), { detect: 'confirmed' });
    ok(clips.length >= 1); eq(clips[0].label, 'confirmed_dmr');
  });

  t('pure noise never triggers', () => eq(feed(D.synthNoise({ seconds: 2 })).length, 0));

  t('noiseDensity estimate is close to the true noise floor', () => {
    const wb = D.synthWideband({ rate: 2e6, seconds: 0.3, signals: [], noise: 0.01 });
    const n0 = C.noiseDensity(wb, 2e6);
    const expected = 2 * 0.01 ** 2 / 2e6;      // variance / rate
    ok(n0 > expected * 0.4 && n0 < expected * 2.5, `n0 ${n0.toExponential(2)} vs ${expected.toExponential(2)}`);
  });
});
