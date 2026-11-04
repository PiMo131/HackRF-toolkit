// node tests/web/echo_browser.js — headless integration test. Serves web/, loads the page, injects a MOCK HackRF
// (no real USB) that answers the control transfers and streams synthetic firmware-sweep + RX bytes, then drives the
// Echo tab: Start -> survey finds a candidate -> focused dwell records a confirmed-DMR clip -> Auto Echo replays it
// offline (no transferOut). Also checks the Lab TX arming gates in the real DOM.
"use strict";
const http = require('http'), fs = require('fs'), path = require('path');
let chromium; try { chromium = require('playwright').chromium; } catch (e) { console.log('echo-browser: playwright not installed, skipping'); process.exit(0); }

const root = path.resolve(__dirname, '../../web');
const types = { '.js': 'text/javascript', '.html': 'text/html', '.css': 'text/css', '.json': 'application/json' };
const server = http.createServer((req, res) => {
  let p = path.join(root, req.url.split('?')[0]); if (req.url === '/' || req.url === '') p = path.join(root, 'index.html');
  fs.readFile(p, (e, d) => { if (e) { res.writeHead(404); res.end('nf'); return; } res.writeHead(200, { 'content-type': types[path.extname(p)] || 'application/octet-stream' }); res.end(d); });
});

(async () => {
  await new Promise(r => server.listen(8097, r));
  const browser = await chromium.launch({ args: ['--no-sandbox'], executablePath: '/opt/pw-browsers/chromium' }).catch(() => chromium.launch({ args: ['--no-sandbox'] }));
  const page = await browser.newContext().then(c => c.newPage());
  const fails = [];
  page.on('pageerror', e => fails.push('PAGEERROR ' + e.message));
  const assert = (cond, msg) => { if (!cond) fails.push('FAIL: ' + msg); else console.log('  ✓', msg); };

  await page.goto('http://127.0.0.1:8097/', { waitUntil: 'domcontentloaded' });
  // wait for Echo + the DSP lib the mock needs to synthesise signals
  await page.waitForFunction(() => window.Echo && window.EchoDsp && window.EchoProfiles, null, { timeout: 10000 });

  // ---- mock the HackRF at the WebUSB USBDevice level and go through the page's real attach(), so the real HackRF
  // class, control-transfer plumbing and reader loop are all exercised. The device answers transferIn with a
  // synthetic firmware-sweep or RX stream depending on the current transceiver mode.
  await page.evaluate(() => {
    const D = window.EchoDsp, XFER = 262144;
    const EMIT = { f: 430.5e6, amp: 0.1 };               // in the amateur_uhf_low default band
    // one long continuous DMR-like signal, sliced by absolute time -> phase/slot continuity across USB chunks
    EMIT.iq = D.synthDmrLike({ rate: 50e3, seconds: 60, bursts: [{ t0: 0, dur: 60 }], amp: EMIT.amp, noise: 0 });
    let mode = 0, fc = 0, rate = 2e6, sweepRanges = [], t0 = Date.now();
    const now = () => (Date.now() - t0) / 1000;
    function sweepBytes() {
      const N = 4096, blocks = [];
      for (const r of sweepRanges) for (let f = r.lo; f < r.hi; f += 20e6) {
        const psd = x => 1e-7 + (Math.abs(x - EMIT.f) < 6e3 ? EMIT.amp * EMIT.amp : 0);
        blocks.push(D.makeSweepBlock(f, N, psd, Math.random));
      }
      if (!blocks.length) return new Uint8Array(XFER);
      const u = new Uint8Array(XFER); for (let i = 0, o = 0; o + 16384 <= XFER; i++, o += 16384) u.set(blocks[i % blocks.length], o);
      return u;
    }
    function rxBytes() {
      const samples = XFER >> 1, dur = samples / rate, sig = [];
      if (Math.abs(EMIT.f - fc) < rate / 2) sig.push({ iq: EMIT.iq, rate: 50e3, offsetHz: EMIT.f - fc, start: 0 });
      return new Uint8Array(D.synthWideband({ rate, seconds: dur, signals: sig, noise: 0.01, fastNoise: true, t0: now() }).buffer);
    }
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const dev = {
      opened: false, serialNumber: 'MOCK01', deviceVersionMajor: 1, deviceVersionMinor: 1, deviceVersionSubminor: 0,
      configuration: { configurationValue: 1, interfaces: [{ alternate: { endpoints: [{ direction: 'in', endpointNumber: 1 }, { direction: 'out', endpointNumber: 2 }] } }] },
      async open() { this.opened = true; }, async selectConfiguration() {}, async claimInterface() {}, async reset() {}, async clearHalt() {},
      async controlTransferOut(setup, data) {
        if (setup.request === 1) { mode = setup.value; }                       // SET_TRANSCEIVER_MODE
        else if (setup.request === 16 && data) { const dv = new DataView(data.buffer || data); fc = dv.getUint32(0, true) * 1e6 + dv.getUint32(4, true); }
        else if (setup.request === 6 && data) { const dv = new DataView(data.buffer || data); rate = dv.getUint32(0, true); }
        else if (setup.request === 26 && data) { const dv = new DataView(data.buffer || data); sweepRanges = []; for (let o = 9; o + 4 <= dv.byteLength; o += 4) sweepRanges.push({ lo: dv.getUint16(o, true) * 1e6, hi: dv.getUint16(o + 2, true) * 1e6 }); }
        return { status: 'ok' };
      },
      async controlTransferIn(setup, len) {
        let b = new Uint8Array(len);
        if (setup.request === 14) b[0] = 5;                                     // BOARD_ID = HackRF Pro
        else if (setup.request === 15) b = new TextEncoder().encode('mock-fw-2026.01\0');  // VERSION
        return { status: 'ok', data: new DataView(b.buffer, 0, Math.min(len, b.length)) };
      },
      async transferIn(ep, len) {
        await sleep(20);
        if (mode === 5) return { status: 'ok', data: new DataView(sweepBytes().buffer) };
        if (mode === 1) return { status: 'ok', data: new DataView(rxBytes().buffer) };
        await sleep(40); return { status: 'ok', data: new DataView(new Uint8Array(0).buffer) };
      },
      async transferOut(ep, data) { window.__txBytes = (window.__txBytes || 0) + (data.byteLength || data.length || 0); return { status: 'ok', bytesWritten: data.byteLength }; },
    };
    window.attach(dev);          // the page's real attach(): builds a real HackRF, enables buttons, calls Echo.onAttach
  });
  await page.waitForFunction(() => { const s = document.querySelector('#echoStart'); return s && !s.disabled; }, null, { timeout: 8000 });

  // select defaults, lower max clip so the test is quick, start scanning
  await page.evaluate(() => { window.Echo._store.defaults(); document.querySelector('#echoMode').value = 'thorough'; document.querySelector('#echoMax').value = '3'; });
  await page.click('nav button[data-tab="echo"]');
  await page.check('#echoAuto');
  await page.click('#echoStart');

  // wait up to 30 s for a clip to land in the library
  let libCount = 0;
  for (let i = 0; i < 60; i++) { libCount = await page.evaluate(async () => (await window.Echo._lib.stats()).count); if (libCount >= 1) break; await page.waitForTimeout(500); }
  assert(libCount >= 1, `recorded a clip via the worker pipeline (got ${libCount})`);

  const info = await page.evaluate(async () => {
    const list = await window.Echo._lib.list();
    return { label: list[0] && list[0].meta.label, source: list[0] && list[0].meta.source, cands: window.Echo && document.querySelectorAll('#echoCand tbody tr').length };
  });
  assert(info.label === 'confirmed_dmr' || info.label === 'possible_dmr', `clip classified DMR-like (${info.label})`);
  assert(info.source === 'live', 'clip source is live (not transmittable)');

  // Auto Echo should have replayed offline: no transferOut bytes at all
  await page.waitForTimeout(1500);
  const txBytes = await page.evaluate(() => window.__txBytes || 0);
  assert(txBytes === 0, 'Auto Echo replayed offline with ZERO WebUSB TX bytes');

  // stop scanning, then check Lab TX gating + that a live clip is refused for TX
  await page.click('#echoStart'); await page.waitForTimeout(500);
  const tx = await page.evaluate(() => {
    const r = {};
    r.armDisabledInitially = document.querySelector('#echoTxArm').disabled;
    document.querySelector('#echoTxEnable').click(); document.querySelector('#echoTxVerified').click();
    r.armDisabledNoAllowlist = document.querySelector('#echoTxArm').disabled;
    document.querySelector('#echoTxAddFreq').value = '433.5'; document.querySelector('#echoTxAdd').click();
    r.armEnabled = !document.querySelector('#echoTxArm').disabled;
    r.banner = document.querySelector('#echoBanner').textContent;
    return r;
  });
  assert(tx.armDisabledInitially && tx.armDisabledNoAllowlist && tx.armEnabled, 'Lab TX arm gated by enable+verify+allowlist');
  assert(/NO RF TRANSMISSION/.test(tx.banner), 'banner shows offline replay by default');

  await browser.close(); server.close();
  if (fails.length) { console.error('echo-browser FAILED:\n' + fails.join('\n')); process.exit(1); }
  console.log('echo-browser: all integration checks passed');
})().catch(e => { console.error(e); process.exit(1); });
