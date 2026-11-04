// node tests/web/echo_dsp.js — classifier, channelizer, ci8 storage, TX interpolation + spectrum, sweep parser.
"use strict";
const D = require('../../web/echo-dsp.js');
const { ok, eq, near, run } = require('./echo_assert.js');

const snrAmp = (snr, noise = 0.003, bwFrac = 14e3 / 50e3) => noise * Math.SQRT2 / Math.sqrt(bwFrac) * 10 ** (snr / 20);

run('echo-dsp', t => {
  t('continuous DMR-like is confirmed (sync words, ~1944 Hz deviation, 4-level)', () => {
    const r = D.classifyDmrLike(...D.synthDmrLike({ seconds: 1 }), 50e3);
    eq(r.label, 'confirmed_dmr');
    ok(r.syncHits >= 2, 'sync hits'); near(r.devHz, 1944, 250, 'deviation'); ok(r.fourLevel > 0.8);
  });

  t('confirmed down to ~9 dB SNR', () => {
    const r = D.classifyDmrLike(...D.synthDmrLike({ seconds: 1, amp: snrAmp(9), seed: 9 }), 50e3);
    ok(D.LABEL_RANK[r.label] >= D.LABEL_RANK.possible_dmr, 'label ' + r.label); ok(r.snrDb < 25);
  });

  t('inverted sync still confirmed and flagged', () => {
    const r = D.classifyDmrLike(...D.synthDmrLike({ seconds: 1, invert: true }), 50e3);
    eq(r.label, 'confirmed_dmr'); ok(r.inverted);
  });

  t('carrier offset is measured', () => {
    const r = D.classifyDmrLike(...D.synthDmrLike({ seconds: 1, offsetHz: 1200 }), 50e3);
    near(r.freqOffsetHz, 1200, 400);
  });

  t('analogue FM is activity, not DMR (no false confirm)', () => {
    const r = D.classifyDmrLike(...D.synthFm({ seconds: 1 }), 50e3);
    ok(D.LABEL_RANK[r.label] < D.LABEL_RANK.possible_dmr, 'label ' + r.label); eq(r.syncHits, 0);
  });

  t('noise is none', () => eq(D.classifyDmrLike(...D.synthNoise({ seconds: 1 }), 50e3).label, 'none'));

  t('2-level FSK is not labelled DMR', () => {
    const r = D.classifyDmrLike(...D.synthDmrLike({ seconds: 1, levels: 2, devUnit: 1000 }), 50e3);
    ok(r.label !== 'confirmed_dmr' && r.label !== 'possible_dmr', 'label ' + r.label);
  });

  t('two signals in one window: channelize each, adjacent is not confused', () => {
    const dmr = D.synthDmrLike({ seconds: 0.6, bursts: [{ t0: 0.05, dur: 0.5 }], amp: 0.1, noise: 0 });
    const fm = D.synthFm({ seconds: 0.6, bursts: [{ t0: 0.05, dur: 0.5 }], amp: 0.1, noise: 0 });
    const wb = D.synthWideband({ rate: 8e6, seconds: 0.6, signals: [{ iq: dmr, rate: 50e3, offsetHz: -600e3 }, { iq: fm, rate: 50e3, offsetHz: -587.5e3 }], noise: 0.01 });
    const chD = new D.NarrowChannelizer(8e6, -600e3), bd = new D.ComplexBuf();
    const chF = new D.NarrowChannelizer(8e6, -587.5e3), bf = new D.ComplexBuf();
    for (let o = 0; o < wb.length; o += 262144) { const c = wb.subarray(o, o + 262144); const a = chD.process(c); bd.push(a[0], a[1]); const b = chF.process(c); bf.push(b[0], b[1]); }
    eq(D.classifyDmrLike(...bd.take(), 50e3).label, 'confirmed_dmr');
    ok(D.classifyDmrLike(...bf.take(), 50e3).syncHits === 0, 'adjacent FM not DMR');
  });

  t('ci8 round trip is ~45 dB SQNR and records gain + clip', () => {
    const [I, Q] = D.synthDmrLike({ seconds: 0.2 });
    const q = D.toCi8(I, Q);
    const [I2] = D.fromCi8(q.data, q.gain);
    let e = 0, p = 0; for (let k = 0; k < I.length; k++) { e += (I[k] - I2[k]) ** 2; p += I[k] ** 2; }
    ok(10 * Math.log10(p / e) > 40, 'SQNR'); ok(q.gain > 1); eq(q.clipped, 0);
  });

  t('50 ksps ci8 is ~100 kB/s (3 MB / 30 s)', () => {
    const [I, Q] = D.synthDmrLike({ seconds: 1 });
    eq(D.toCi8(I, Q).data.length, 100000);
  });

  for (const rate of [2e6, 4e6, 8e6]) t(`TX interpolate to ${rate / 1e6} Msps: clean spectrum, image-suppressed, loops back to DMR`, () => {
    const src = D.synthDmrLike({ seconds: 0.3, bursts: [{ t0: 0, dur: 0.3 }], noise: 0 });
    const off = 300e3, tx = new D.TxInterpolator(src[0], src[1], { outRate: rate, offsetHz: off });
    const parts = []; let n = 0; while (!tx.done) { const p = tx.pull(1 << 16); parts.push(p); n += p.length; }
    const all = new Int8Array(n); let o = 0; for (const p of parts) { all.set(p, o); o += p.length; }
    eq(n / 2, (src[0].length + 2 * tx.pad) * tx.plan.L);            // produced == planned (input + pads, interpolated by L)
    const chk = D.txSpectrumCheck(all, rate, off);
    ok(chk.occupiedFrac > 0.98, 'occupied ' + chk.occupiedFrac.toFixed(3));
    ok(chk.imageDbc < -30, 'image ' + chk.imageDbc.toFixed(0) + ' dBc');
    ok(tx.peak <= 0.99 && tx.limited === 0, 'within limiter');
    // channelize back and confirm the recovered RMS deviation matches the source (the signal survives TX)
    const ch = new D.NarrowChannelizer(rate, off), b = new D.ComplexBuf();
    for (let p = 0; p < all.length; p += 262144) { const [a, bq] = ch.process(all.subarray(p, p + 262144)); b.push(a, bq); }
    const [li, lq] = b.take(), pad = tx.pad + 400;
    const fSrc = D.discriminator(src[0], src[1], 50e3), fLoop = D.discriminator(li.subarray(pad), lq.subarray(pad), 50e3);
    const rms = x => { let s = 0, m = 2000; for (let k = m; k < x.length - m; k++) s += x[k] * x[k]; return Math.sqrt(s / (x.length - 2 * m)); };
    near(rms(fLoop), rms(fSrc), rms(fSrc) * 0.2, 'recovered deviation');
  });

  t('sweep parser reads bins for two tones across passes', () => {
    const rnd = D.mulberry32(5), N = 4096;
    const psd = f => 1e-7 + (Math.abs(f - 446.00625e6) < 5e3 ? 1e-4 : 0) + (Math.abs(f - 438.1e6) < 5e3 ? 1e-5 : 0);
    const hits = {}, passes = [];
    const p = new D.SweepParser(N, (f0, df, pw) => { for (let k = 0; k < pw.length; k++) { const f = f0 + k * df; for (const tgt of [446.00625e6, 438.1e6]) if (Math.abs(f - tgt) < 3e3) hits[tgt] = Math.max(hits[tgt] || -200, 10 * Math.log10(pw[k])); } }, () => passes.push(1), 430e6);
    const blocks = []; for (let pass = 0; pass < 2; pass++) for (const f of [430e6, 435e6, 450e6, 455e6]) blocks.push(D.makeSweepBlock(f, N, psd, rnd));
    const all = new Uint8Array(blocks.length * 16384 + 512); blocks.forEach((b, i) => all.set(b, 512 + i * 16384));
    for (let o = 0; o < all.length; o += 50000) p.push(new Int8Array(all.buffer, o, Math.min(50000, all.length - o)));
    ok(hits[446.00625e6] > -60, 'strong tone seen'); ok(hits[438.1e6] > -70, 'weak tone seen'); ok(passes.length >= 1);
  });
});
