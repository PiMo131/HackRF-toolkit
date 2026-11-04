// node tests/web/echo_storage.js — library size, SigMF round trip, quota, integrity, and offline replay (no TX).
"use strict";
const D = require('../../web/echo-dsp.js'), St = require('../../web/echo-storage.js'), R = require('../../web/echo-replay.js');
const { ok, eq, near, run } = require('./echo_assert.js');

function mkClip(src = 'synthetic', seconds = 1, label = 'confirmed_dmr') {
  const [I, Q] = D.synthDmrLike({ seconds }); const q = D.toCi8(I, Q);
  return { data: q.data, meta: { source: src, utc: new Date().toISOString(), channelCentreHz: 446.03125e6, rxCentreHz: 445.5e6, iqRate: 50e3,
    format: 'ci8', hwRate: 2e6, storeGain: q.gain, snrDb: 30, label, profileIds: ['pmr446'], stopReason: 'end_of_transmission',
    lateLock: false, preRollS: 0.3, durationS: seconds, detect: 'likely', features: { label, syncHits: 20 }, gain: { lna: 24, vga: 20, amp: 0 }, storeClipFraction: q.clipFraction, rxClipFraction: 0 } };
}

run('echo-storage', t => {
  t('50 ksps ci8 fragment is ~3 MB for 30 s', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip('synthetic', 30));
    near(rec.bytes, 3e6, 2e5, '30 s size');
  });

  t('SigMF export -> import is bit-exact and carries the echo_* fields', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip(), { name: 'clipA', notes: 'bench' });
    const ex = await lib.exportSigmf(rec.id);
    ok(/rfscan:echo_label/.test(ex.metaJson), 'has echo_ fields');
    const lib2 = await new St.EchoLibrary().init();
    const back = await lib2.importSigmf(ex.metaJson, ex.data, {});
    const r1 = await lib.get(rec.id), r2 = await lib2.get(back.id);
    eq(r1.data.length, r2.data.length);
    let same = true; for (let i = 0; i < r1.data.length; i++) if (r1.data[i] !== r2.data[i]) { same = false; break; }
    ok(same, 'data bit-exact'); eq(r2.meta.label, 'confirmed_dmr'); eq(r2.meta.channelCentreHz, 446.03125e6);
  });

  t('imported fragments are marked source=import (not live), so they cannot be transmitted', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip('live'));
    const ex = await lib.exportSigmf(rec.id);
    const lib2 = await new St.EchoLibrary().init();
    const back = await lib2.importSigmf(ex.metaJson, ex.data, {});
    eq((await lib2.get(back.id)).meta.source, 'import');
  });

  t('quota is enforced with a clear error, nothing silently dropped', async () => {
    const lib = await new St.EchoLibrary().init();
    await lib.setQuota({ maxClips: 2, maxBytes: 1e9 });
    await lib.add(mkClip()); await lib.add(mkClip());
    let err = null; try { await lib.add(mkClip()); } catch (e) { err = e; }
    ok(err && err.name === 'QuotaError', 'quota error'); eq((await lib.stats()).count, 2);
  });

  t('byte quota enforced independently', async () => {
    const lib = await new St.EchoLibrary().init();
    await lib.setQuota({ maxClips: 100, maxBytes: 150000 });   // ~1.5 clips of 1 s
    await lib.add(mkClip());
    let err = null; try { await lib.add(mkClip()); } catch (e) { err = e; }
    ok(err && err.kind === 'bytes');
  });

  t('integrity check detects a good fragment', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip());
    ok((await lib.verify(rec.id)).ok);
  });

  t('rename / notes / delete', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip());
    await lib.rename(rec.id, 'renamed'); await lib.setNotes(rec.id, 'note');
    eq((await lib.get(rec.id)).name, 'renamed'); eq((await lib.get(rec.id)).notes, 'note');
    await lib.remove(rec.id); eq((await lib.stats()).count, 0);
  });

  t('offline replay runs N repeats with pauses and calls NO transmit path', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip('synthetic', 0.5));
    let txCalls = 0;
    const clock = { sleep: () => Promise.resolve(), now: () => 0 };
    const frames = [], events = [];
    const q = new R.ReplayQueue({ provider: id => lib.readIq(id), clock, onFrame: f => { frames.push(f); if (f.mode !== 'OFFLINE_REPLAY') txCalls++; }, onEvent: e => events.push(e) });
    q.enqueue(rec.id, { repeat: 3, pauseMs: 500 });
    await new Promise(r => setTimeout(r, 60));
    eq(events.filter(e => e.kind === 'replay_start').length, 3, 'three repeats');
    ok(frames.length > 0 && frames.every(f => f.mode === 'OFFLINE_REPLAY'), 'all offline');
    eq(txCalls, 0, 'no RF transmit in replay');
    ok(events.some(e => e.kind === 'drained'));
    ok(typeof q.emergencyStop === 'function');
  });

  t('replay queue is bounded (no unbounded growth)', async () => {
    const lib = await new St.EchoLibrary().init();
    const rec = await lib.add(mkClip('synthetic', 0.1));
    const q = new R.ReplayQueue({ provider: () => new Promise(() => {}), clock: { sleep: () => new Promise(() => {}), now: () => 0 }, maxItems: 3 });
    let full = false; try { for (let i = 0; i < 10; i++) q.enqueue(rec.id); } catch (e) { full = true; }
    ok(full, 'queue cap enforced');
  });

  t('repeat clamps to 1..10, pause to 0..5000', () => {
    eq(R.clampRepeat(0), 1); eq(R.clampRepeat(99), 10); eq(R.clampPause(-5), 0); eq(R.clampPause(99999), 5000);
  });
});
