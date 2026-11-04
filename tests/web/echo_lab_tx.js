// node tests/web/echo_lab_tx.js — Lab TX safety. Every path must end RF-OFF and refuse unless all gates pass.
// A mock USB transport records calls and lets us prove the device mode is OFF after every scenario.
"use strict";
const D = require('../../web/echo-dsp.js'), T = require('../../web/echo-lab-tx.js');
const { ok, eq, near, run } = require('./echo_assert.js');

function mockTransport(opts = {}) {
  let mode = 'OFF', sent = 0;
  const calls = [], drop = opts.dropAfter || Infinity;
  return {
    calls, get mode() { return mode; }, get sent() { return sent; },
    pickTxRate: async r => { let x = r; while (x < 2e6) x *= 2; return x; },
    setFreq: async f => { calls.push(['freq', f]); },
    setTxRate: async r => { calls.push(['rate', r]); },
    setTxGain: async g => { calls.push(['gain', g]); },
    sendTx: async buf => { if (opts.clock && opts.sendAdvance) opts.clock.t += opts.sendAdvance; sent += buf.length >> 1; mode = 'TX'; calls.push(['send', buf.length]); if (sent > drop) throw new Error('USB disconnect'); return { status: 'ok' }; },
    flushTx: async () => { calls.push(['flush']); },
    modeOff: async () => { mode = 'OFF'; calls.push(['modeoff']); },
  };
}
function mem() { const m = new Map(); return { getItem: k => m.has(k) ? m.get(k) : null, setItem: (k, v) => m.set(k, String(v)) }; }
function frag(source, durS = 2) { const [I, Q] = D.synthDmrLike({ seconds: durS, bursts: [{ t0: 0, dur: durS }] }); return { I, Q, rate: 50e3, meta: { source, channelCentreHz: 433.5e6 } }; }
function controller(clock, mt) {
  const al = new T.TxAllowlist(mem());
  const tx = new T.LabTxController({ transport: mt, allowlist: al, clock });
  return { tx, al };
}
const CLK = () => ({ t: 0, now() { return this.t; }, sleep(ms) { this.t += ms / 1000; return Promise.resolve(); } });

run('echo-lab-tx', t => {
  t('allowlist starts empty and never auto-fills', () => {
    const al = new T.TxAllowlist(mem());
    eq(al.list().length, 0);
  });

  t('default controller is OFF, disabled, unverified', () => {
    const { tx } = controller(CLK(), mockTransport());
    eq(tx.state, 'OFF'); eq(tx.enabled, false); eq(tx.labVerified, false);
  });

  t('arming refused: disabled / empty allowlist / unverified', () => {
    const clk = CLK(); const { tx, al } = controller(clk, mockTransport());
    ok(thrown(() => tx.armFor(433.5e6)), 'disabled');
    tx.enable(true);
    ok(/allowlist is empty/.test(err(() => tx.armFor(433.5e6))), 'empty allowlist');
    al.add({ centreHz: 433.5e6, label: 'bench', maxGain: 20 });
    ok(/setup/.test(err(() => tx.armFor(433.5e6))), 'unverified');
    tx.setLabVerified(true);
    ok(!thrown(() => tx.armFor(433.5e6)), 'arms once all gates pass');
  });

  t('TX centre must come from the allowlist, never an arbitrary frequency', () => {
    const clk = CLK(); const { tx, al } = controller(clk, mockTransport());
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    ok(/not in the Lab TX allowlist/.test(err(() => tx.armFor(446.03125e6))), 'discovered freq refused');
  });

  t('not armed -> transmit refused, RF stays OFF', async () => {
    const clk = CLK(), mt = mockTransport(); const { tx, al } = controller(clk, mt);
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    ok(await rejects(() => tx.transmit(frag('synthetic'), { repeat: 1 })));
    eq(mt.mode, 'OFF'); eq(mt.sent, 0);
  });

  t('live and imported sources never transmit', async () => {
    const clk = CLK(), mt = mockTransport(); const { tx, al } = controller(clk, mt);
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    for (const bad of ['live', 'import', 'imported_unknown', undefined]) {
      tx.armFor(433.5e6);
      ok(await rejects(() => tx.transmit(frag(bad), { repeat: 1 })), 'refuse ' + bad);
      eq(mt.mode, 'OFF');
    }
  });

  t('clip longer than the lab limit is refused', async () => {
    const clk = CLK(), mt = mockTransport(); const { tx, al } = controller(clk, mt);
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6, { maxClipS: 3 });
    ok(await rejects(() => tx.transmit(frag('synthetic', 6), { repeat: 1 })));
    eq(mt.mode, 'OFF');
  });

  t('gain is capped to the allowlist entry maximum', () => {
    const clk = CLK(); const { tx, al } = controller(clk, mockTransport());
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 12 });
    eq(tx.armFor(433.5e6, { txGain: 47 }).txGain, 12);
  });

  t('successful transmit: N repeats, ends OFF, arming consumed, flush + modeOff called', async () => {
    const clk = CLK(), mt = mockTransport(); const { tx, al } = controller(clk, mt);
    tx.cfg.cooldownS = 0;
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6, { txGain: 10, maxRepeats: 3 });
    const r = await tx.transmit(frag('synthetic'), { repeat: 3, pauseMs: 0 });
    eq(r.sentRepeats, 3); eq(tx.state, 'OFF'); eq(mt.mode, 'OFF');
    ok(mt.calls.filter(c => c[0] === 'modeoff').length >= 1, 'modeOff called');
    ok(mt.calls.filter(c => c[0] === 'flush').length === 3, 'flushed each repeat');
    ok(tx.arm === null, 're-arm required for next TX');
    ok(await rejects(() => tx.transmit(frag('synthetic'), { repeat: 1 })), 'cannot transmit again without re-arm');
  });

  t('arming expires', async () => {
    const clk = CLK(), mt = mockTransport(); const { tx, al } = controller(clk, mt);
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6); clk.t += tx.cfg.armWindowS + 1;
    ok(!tx.armValid()); ok(await rejects(() => tx.transmit(frag('synthetic'), { repeat: 1 })));
  });

  t('USB drop mid-TX -> FAULT, RF OFF, re-arm blocked until fault cleared', async () => {
    const clk = CLK(), mt = mockTransport({ dropAfter: 20000, clock: clk }); const { tx, al } = controller(clk, mt);
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6);
    ok(await rejects(() => tx.transmit(frag('synthetic'), { repeat: 2 })));
    eq(tx.state, 'FAULT'); eq(mt.mode, 'OFF');
    ok(/fault/.test(err(() => tx.armFor(433.5e6))), 're-arm blocked');
    tx.clearFault(); eq(tx.state, 'OFF');
  });

  t('hard timeout independent of UI -> RF OFF', async () => {
    const clk = CLK(), mt = mockTransport({ clock: clk, sendAdvance: 0.2 }); const { tx, al } = controller(clk, mt);
    tx.cfg.hardTimeoutS = 0.1;                 // one chunk takes 0.2 s of (simulated) wall time -> backstop trips
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6);
    ok(await rejects(() => tx.transmit(frag('synthetic', 5), { repeat: 1 })), 'timed out');
    eq(mt.mode, 'OFF'); eq(tx.state, 'FAULT');
  });

  t('emergency stop halts the stream and leaves RF OFF', async () => {
    const clk = CLK(), mt = mockTransport({ clock: clk }); const { tx, al } = controller(clk, mt);
    tx.cfg.cooldownS = 0;
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6, { maxRepeats: 5 });
    const p = tx.transmit(frag('synthetic', 4), { repeat: 5, pauseMs: 0 });
    await new Promise(r => setTimeout(r, 2)); tx.emergencyStop();
    await p.catch(() => {});
    eq(mt.mode, 'OFF');
  });

  t('device lost drops arming and never leaves a false armed state', () => {
    const clk = CLK(); const { tx, al } = controller(clk, mockTransport());
    tx.enable(true); tx.setLabVerified(true); al.add({ centreHz: 433.5e6, maxGain: 20 });
    tx.armFor(433.5e6); tx.onDeviceLost();
    ok(!tx.armValid()); ok(tx.arm === null);
  });

  t('Auto Echo cannot arm: nothing but armFor() sets ARMED', () => {
    const clk = CLK(); const { tx } = controller(clk, mockTransport());
    tx.enable(true);
    // no API other than armFor transitions to ARMED; enabling + verifying does not arm
    tx.setLabVerified(true);
    eq(tx.state, 'OFF');
  });
});

function thrown(fn) { try { fn(); return false; } catch (e) { return true; } }
function err(fn) { try { fn(); return ''; } catch (e) { return e.message; } }
async function rejects(fn) { try { await fn(); return false; } catch (e) { return true; } }
