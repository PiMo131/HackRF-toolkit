// Echo tab controller (main thread). Owns the HackRF on behalf of the scan worker and Lab TX, and drives the UI,
// the fragment library (echo-storage), offline replay (echo-replay) and the guarded Lab TX engine (echo-lab-tx).
//
// The heavy scan DSP runs in echo-worker.js; this file only configures the radio, forwards USB bytes to the worker
// and relays worker results to the UI. It uses globals from the main inline script: $, hrf, REQ, MODE, XFER,
// READERS, fmtHz, showTab, splitGain.
"use strict";
window.Echo = (function () {
  const Prof = window.EchoProfiles, Store = window.EchoStorage, Replay = window.EchoReplay, LabTx = window.EchoLabTx;
  const SWEEP_RATE = 20e6, SWEEP_OFFSET = 7.5e6, SWEEP_BLOCK = 16384, BIN_N = 4096;

  const store = new Prof.ProfileStore(window.localStorage);
  const allowlist = new LabTx.TxAllowlist(window.localStorage);
  const lib = new Store.EchoLibrary(typeof indexedDB !== 'undefined' ? new Store.IdbBackend() : new Store.MemoryBackend());
  let libReady = lib.init().then(renderLib).catch(e => console.warn('Echo library init failed', e));

  let worker = null, scanning = false, status = null, mode = 'off', selected = new Set(), autoEcho = false;
  let txEndpoint = 2, viewer = null;

  // ---------------------------------------------------------------- device driver for the worker
  function fwd(tag) { return buf => { if (!worker) return; const b = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength); worker.postMessage({ usb: b, mode: tag }, [b]); }; }
  async function doStartSweep({ ranges, gains }) {
    await hrf.setMode(MODE.OFF); hrf.onData = null;
    await hrf.setSampleRate(SWEEP_RATE);
    await hrf.out(REQ.BBF, 15e6 & 0xffff, 15e6 >>> 16);
    await hrf.setLna(gains.lna); await hrf.setVga(gains.vga); await hrf.setAmp(gains.amp);
    // multi-range INIT_SWEEP payload: step(4) offset(4) style(1) then u16 lo,hi MHz per range
    const payload = new DataView(new ArrayBuffer(9 + ranges.length * 4));
    payload.setUint32(0, SWEEP_RATE / BIN_N * BIN_N, true); payload.setUint32(4, SWEEP_OFFSET, true); payload.setUint8(8, 1);
    ranges.forEach((r, i) => { payload.setUint16(9 + i * 4, r.loMHz, true); payload.setUint16(11 + i * 4, r.hiMHz, true); });
    await hrf.out(REQ.INIT_SWEEP, SWEEP_BLOCK & 0xffff, SWEEP_BLOCK >>> 16, payload.buffer);
    hrf.skip = READERS; hrf.onData = fwd('sweep');
    await hrf.setMode(MODE.SWEEP);
  }
  async function doStartRx({ fc, rate, gains }) {
    await hrf.setMode(MODE.OFF); hrf.onData = null;
    await hrf.setSampleRate(rate); await hrf.setFreq(fc);
    await hrf.setLna(gains.lna); await hrf.setVga(gains.vga); await hrf.setAmp(gains.amp);
    hrf.skip = READERS; hrf.onData = fwd('rx');
    await hrf.setMode(MODE.RX);
  }
  async function doStop() { hrf.onData = null; await hrf.setMode(MODE.OFF); }
  async function handleCtl(m) {
    try {
      if (!hrf) throw new Error('device not connected');
      if (m.ctl === 'startSweep') await doStartSweep(m);
      else if (m.ctl === 'startRx') await doStartRx(m);
      else if (m.ctl === 'stopSweep' || m.ctl === 'stopRx') await doStop();
      worker.postMessage({ ctlDone: m.id, result: true });
    } catch (e) { worker.postMessage({ ctlDone: m.id, error: e.message }); }
  }

  // ---------------------------------------------------------------- scan lifecycle
  function startScan() {
    if (scanning) return stopScan();
    const ivs = store.intervals();
    if (!ivs.length) { setStatus('select at least one receive range first', 'warn'); return; }
    // hand the device to Echo
    try { if (typeof liveStop === 'function') { /* main script stops others via releaseDevice path */ } } catch (e) {}
    releaseOthersForEcho();
    worker = new Worker('echo-worker.js');
    worker.onmessage = onWorker;
    worker.onerror = e => { setStatus('scan worker error: ' + e.message, 'bad'); stopScan(); };
    const gains = splitGain(44);
    worker.postMessage({ cmd: 'start', intervals: ivs, source: hrf && hrf.kind === 'sim' ? 'synthetic' : 'live',
      sched: { mode: $('#echoMode').value, rxRate: 8e6 }, capture: { detect: $('#echoDetect').value, thresholdDb: +$('#echoThr').value, maxS: Prof_clampMax($('#echoMax').value) }, gains });
    scanning = true; $('#echoStart').textContent = 'Stop scanning';
    setBanner();
  }
  function Prof_clampMax(s) { return Math.max(1, Math.min(60, +s || 30)); }
  async function stopScan() {
    if (!worker) { scanning = false; return; }
    const w = worker;
    await new Promise(res => { let done = false; w.onmessage = e => { if (e.data.ev === 'stopped') { done = true; res(); } else onWorker(e); }; w.postMessage({ cmd: 'stop' }); setTimeout(() => { if (!done) res(); }, 3000); });
    try { w.terminate(); } catch (e) {}
    if (worker === w) worker = null;
    scanning = false; mode = 'off';
    if (hrf) { hrf.onData = null; try { await hrf.setMode(MODE.OFF); } catch (e) {} }
    $('#echoStart').textContent = 'Start scanning'; $('#echoStateLine').textContent = '';
    setBanner();
  }
  function onWorker(e) {
    const m = e.data;
    if (m.ctl) return handleCtl(m);
    if (m.ev === 'state') { renderState(m.state, m.info); return; }
    if (m.ev === 'status') { status = m.status; renderStatus(); return; }
    if (m.ev === 'log') { if (m.log && m.log.kind === 'dwell') {} return; }
    if (m.ev === 'clip') { onClip(m); return; }
    if (m.ev === 'stopped') { scanning = false; return; }
  }
  async function onClip(m) {
    try {
      await libReady;
      const rec = await lib.add({ data: new Int8Array(m.data), meta: m.meta });
      await renderLib();
      if (autoEcho) { try { replay.enqueue(rec.id, { repeat: +$('#echoRepeat').value, pauseMs: +$('#echoPause').value, name: rec.meta.label }); } catch (e) {} }
    } catch (e) { setStatus('library: ' + e.message, 'bad'); }
    finally { if (worker) worker.postMessage({ clipStored: m.id }); }
  }
  function releaseOthersForEcho() {
    try { if (typeof live !== 'undefined' && live.running) liveStop(); } catch (e) {}
    try { if (typeof scan !== 'undefined' && scan.running) scanStop(); } catch (e) {}
    try { if (typeof audio !== 'undefined' && audio.running && audio.source === 'device') audioStop(); } catch (e) {}
  }

  // ---------------------------------------------------------------- Lab TX transport (WebUSB)
  const txTransport = {
    async pickTxRate() { return 2e6; },               // 2 Msps = 40 * 50 ksps, a HackRF-supported TX rate
    async setFreq(hz) { await hrf.setFreq(hz); },
    async setTxRate(rate) { await hrf.setSampleRate(rate); this._rate = rate; this._mode = 'off'; },
    async setTxGain(g) { await hrf.in(REQ.SET_TXVGA_GAIN !== undefined ? REQ.SET_TXVGA_GAIN : 21, 0, Math.max(0, Math.min(47, g)) & 0xffff, 1); },
    async sendTx(buf) {
      if (this._mode !== 'tx') { await hrf.setMode(MODE.TX !== undefined ? MODE.TX : 2); this._mode = 'tx'; }
      const b = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
      const r = await hrf.dev.transferOut(txEndpoint, b);
      return { status: r.status };
    },
    async flushTx() { try { const z = new Uint8Array(SWEEP_BLOCK); await hrf.dev.transferOut(txEndpoint, z); } catch (e) {} },
    async modeOff() { this._mode = 'off'; if (hrf) await hrf.setMode(MODE.OFF); },
  };
  const labTx = new LabTx.LabTxController({ transport: null, allowlist, onState: renderTx, onLog: logTx, onStatus: renderTx });

  // ---------------------------------------------------------------- replay
  const replay = new Replay.ReplayQueue({ provider: id => lib.readIq(id), onFrame: onReplayFrame, onEvent: onReplayEvent });
  function onReplayFrame(f) {
    const sp = f.frame.spectrum; if (sp && sp.length) drawFragmentRow(sp);
    $('#echoReplayStat').innerHTML = `<span class="echo-banner">OFFLINE REPLAY — NO RF TRANSMISSION</span> <span>${esc(f.job.name)} · repeat ${f.repeat} · t ${f.frame.t1.toFixed(2)} s</span>`;
  }
  function onReplayEvent(ev) {
    if (ev.kind === 'drained' || ev.kind === 'stopped') { $('#echoStopPlay').disabled = true; $('#echoReplayStat').innerHTML = ev.kind === 'stopped' ? '<span class="note">replay stopped</span>' : '<span class="note">replay finished</span>'; }
    else $('#echoStopPlay').disabled = false;
  }

  // ---------------------------------------------------------------- UI: profiles popover + chips
  function renderProfiles() {
    const pop = $('#echoProfPop'); pop.innerHTML = '';
    const bar = document.createElement('div'); bar.style.cssText = 'display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px';
    for (const [lbl, fn] of [['All', () => store.selectAll()], ['Clear', () => store.clear()], ['Defaults', () => store.defaults()], ['Custom range…', addCustomRange]]) {
      const b = document.createElement('button'); b.className = 'sec'; b.textContent = lbl; b.onclick = () => { fn(); renderProfiles(); renderChips(); }; bar.append(b);
    }
    pop.append(bar);
    for (const p of store.all()) {
      const row = document.createElement('div'); row.className = 'pf';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = store.isSelected(p.id);
      cb.onchange = () => { store.select(p.id, cb.checked); renderChips(); renderProfBtn(); };
      const nm = document.createElement('div'); nm.className = 'nm';
      const rngTxt = (p.ranges || []).map(r => `${(r.lo / 1e6).toFixed(3)}–${(r.hi / 1e6).toFixed(3)}`).join(', ') + ((p.channels || []).length ? ` +${p.channels.length} ch` : '');
      nm.innerHTML = `${esc(p.name)}${p.edited ? ' <span class="note">(edited)</span>' : ''}<div class="rg">${rngTxt} MHz · ${esc(p.note || '')}</div>`;
      const acts = document.createElement('div');
      const dup = mkBtn('Duplicate', () => { store.duplicate(p.id); renderProfiles(); });
      const edit = mkBtn('Edit', () => editProfile(p));
      const del = mkBtn(p.builtin ? 'Reset' : 'Delete', () => { store.remove(p.id); renderProfiles(); renderChips(); renderProfBtn(); });
      acts.append(edit, dup, del);
      row.append(cb, nm, acts); pop.append(row);
    }
    const foot = document.createElement('div'); foot.className = 'note'; foot.style.marginTop = '6px';
    foot.textContent = `presets rev ${Prof.PRESET_SOURCE.revision}; editable receive presets, not a DMR database and never a TX authorisation`;
    pop.append(foot);
    // channel-list import
    const imp = document.createElement('label'); imp.className = 'note'; imp.style.display = 'block'; imp.style.marginTop = '4px';
    imp.textContent = 'Import channel-list JSON: ';
    const f = document.createElement('input'); f.type = 'file'; f.accept = '.json'; f.onchange = () => importChannelList(f.files[0]); imp.append(f); pop.append(imp);
    renderProfBtn();
  }
  function mkBtn(t, fn) { const b = document.createElement('button'); b.className = 'sec'; b.textContent = t; b.onclick = fn; return b; }
  function renderProfBtn() { const n = store.selected().length; $('#echoProfBtn').textContent = `Frequency ranges (${n} selected) ▾`; }
  function renderChips() {
    const c = $('#echoChips'); c.innerHTML = '';
    for (const p of store.selected()) {
      const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = p.name;
      const x = document.createElement('button'); x.textContent = '×'; x.title = 'remove'; x.onclick = () => { store.select(p.id, false); renderChips(); renderProfBtn(); if ($('#echoProfPop').style.display !== 'none') renderProfiles(); };
      chip.append(x); c.append(chip);
    }
    renderProfBtn();
  }
  function addCustomRange() {
    const lo = parseFloat(prompt('Custom range start MHz:', '438.0')); if (!(lo > 0)) return;
    const hi = parseFloat(prompt('Custom range end MHz:', '438.5')); if (!(hi > lo)) { alert('end must be above start'); return; }
    const name = prompt('Name:', `Custom ${lo}–${hi} MHz`) || `Custom ${lo}–${hi} MHz`;
    try { const p = store.upsert({ id: 'custom_' + Date.now().toString(36), name, ranges: [{ lo: Math.round(lo * 1e6), hi: Math.round(hi * 1e6) }], channels: [], step: 12500, builtin: false }); store.select(p.id, true); }
    catch (e) { alert(e.message); }
  }
  function editProfile(p) {
    const lo = parseFloat(prompt(`${p.name}\nStart MHz:`, (p.ranges[0] ? p.ranges[0].lo / 1e6 : 438).toString())); if (!(lo > 0)) return;
    const hi = parseFloat(prompt('End MHz:', (p.ranges[0] ? p.ranges[0].hi / 1e6 : 438.5).toString())); if (!(hi > lo)) return;
    const name = prompt('Name:', p.name) || p.name;
    try { store.upsert({ ...p, name, ranges: [{ lo: Math.round(lo * 1e6), hi: Math.round(hi * 1e6) }] }); renderProfiles(); renderChips(); }
    catch (e) { alert(e.message); }
  }
  async function importChannelList(file) {
    if (!file) return;
    const txt = await file.text();
    const res = Prof.parseChannelList(txt, { name: file.name.replace(/\.json$/, '') });
    if (res.errors.length) { alert('Channel list errors:\n' + res.errors.join('\n')); return; }
    if (res.warnings.length) console.warn('channel list warnings', res.warnings);
    try { const p = store.upsert(res.profile); store.select(p.id, true); renderProfiles(); renderChips(); if (res.warnings.length) setStatus(`imported ${res.profile.channels.length} channels (${res.warnings.length} warning(s), see console)`, 'warn'); }
    catch (e) { alert(e.message); }
  }

  // ---------------------------------------------------------------- UI: status / state
  function setBanner() {
    const tx = labTx.status().armed || labTx.state === 'TRANSMITTING';
    const b = $('#echoBanner');
    b.textContent = tx ? 'LAB TX — ARMED' : 'OFFLINE REPLAY — NO RF TRANSMISSION';
    b.classList.toggle('tx', tx);
  }
  const STATE_TXT = { IDLE: 'idle', DISCOVERY: 'energy survey', RANK_CANDIDATES: 'ranking candidates', FOCUSED_DWELL: 'focused dwell', CAPTURING: 'recording clip', FINALIZING: 'saving clip', SAVED: 'clip saved', MAINTENANCE: 'adaptive revisit', PAUSED: 'paused (TX)', STOPPING: 'stopping', ERROR: 'error' };
  function renderState(s, info) {
    let t = STATE_TXT[s] || s;
    if (info && info.fc) t += ` @ ${fmtHz(info.fc)}`;
    if (info && info.reason) t += ` — ${info.reason}`;
    if (info && info.error) t += ` — ${info.error}`;
    $('#echoStateLine').innerHTML = `<b>${s}</b>: ${esc(t)}`;
    if (s === 'ERROR') setStatus('scan error: ' + (info.error || ''), 'bad');
  }
  function renderStatus() {
    if (!status) return;
    const st = status.stats || {};
    $('#echoStatus').innerHTML = [
      `<span>mode <b>${esc(status.mode)}</b></span>`,
      `<span>surveys <b>${status.surveys || 0}</b></span>`,
      `<span>dwells <b>${st.dwells || 0}</b></span>`,
      `<span>clips <b>${st.clips || 0}</b></span>`,
      `<span>candidates <b>${(status.candidates || []).length}</b></span>`,
      st.rxPausedForTx ? `<span class="warn">RX paused for TX ×${st.rxPausedForTx}</span>` : '',
      st.usbStalls ? `<span class="bad">USB stalls ${st.usbStalls}</span>` : '',
      st.clipWarnings ? `<span class="warn">clipping on ${st.clipWarnings} survey(s)</span>` : '',
    ].join(' ');
    const cb = $('#echoCand tbody'); cb.innerHTML = '';
    for (const c of (status.candidates || []).slice(0, 30)) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="mono">${fmtHz(c.f)}</td><td class="tier-${c.tier}">${c.tier}</td><td>${labelTxt(c.bestLabel)}</td>` +
        `<td class="mono">${Number.isFinite(c.snrDb) ? c.snrDb.toFixed(0) : '–'}</td><td class="mono">${c.dwells}</td>` +
        `<td class="mono">${Number.isFinite(c.revisitS) && c.revisitS < 1e6 ? c.revisitS.toFixed(0) + ' s' : '–'}</td><td class="mono">${c.score.toFixed(2)}</td>`;
      cb.append(tr);
    }
    const ov = $('#echoCov tbody'); ov.innerHTML = '';
    for (const c of status.coverage || []) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${esc(c.profileId)}</td><td class="mono">${c.lastVisitAgoS == null ? 'never' : c.lastVisitAgoS.toFixed(0) + ' s ago'}</td>` +
        `<td class="mono">${Number.isFinite(c.estimatedRevisitS) ? c.estimatedRevisitS.toFixed(0) + ' s' : '–'}</td>` +
        `<td class="mono">${c.missedWhileRecordingS.toFixed(0)}/${c.missedWhileTxS.toFixed(0)} s</td><td class="note">${esc(c.coverageWarning)}</td>`;
      ov.append(tr);
    }
  }
  function labelTxt(l) { return { energy_candidate: 'energy', activity: 'activity', unknown_digital: 'unknown digital', possible_dmr: 'possible DMR', confirmed_dmr: 'confirmed DMR' }[l] || l || ''; }

  // ---------------------------------------------------------------- UI: library
  async function renderLib() {
    const list = await lib.list(), st = await lib.stats();
    $('#echoLibStat').textContent = `${st.count}/${st.quota.maxClips} clips · ${(st.bytes / 1048576).toFixed(1)}/${(st.quota.maxBytes / 1048576).toFixed(0)} MB`;
    $('#echoCount').textContent = st.count ? `(${st.count})` : '';
    const tb = $('#echoLib tbody'); tb.innerHTML = '';
    for (const r of list) {
      const m = r.meta, tr = document.createElement('tr');
      const flags = [m.lateLock ? 'late' : '', m.stopReason === 'truncated_by_limit' ? 'truncated' : '', m.continuation ? 'cont' : ''].filter(Boolean).join(' ');
      tr.innerHTML = `<td><input type="checkbox" data-id="${r.id}"></td><td>${esc(r.name || '(unnamed)')}</td><td class="mono">${fmtHz(m.channelCentreHz)}</td>` +
        `<td>${labelTxt(m.label)}</td><td>${esc(m.source)}</td><td class="mono">${(m.utc || '').replace('T', ' ').replace(/\..*/, '')}</td>` +
        `<td class="mono">${(m.durationS || 0).toFixed(1)}s</td><td class="mono">${(r.bytes / 1e3).toFixed(0)}k</td><td class="note">${flags}</td>`;
      tr.querySelector('input').onchange = updateSel;
      tr.onclick = ev => { if (ev.target.tagName !== 'INPUT') viewFragment(r.id); };
      tb.append(tr);
    }
    updateSel();
    renderTxArmOptions();
  }
  function selectedIds() { return [...document.querySelectorAll('#echoLib tbody input:checked')].map(c => c.dataset.id); }
  function updateSel() {
    const ids = selectedIds();
    $('#echoPlay').disabled = ids.length === 0;
    $('#echoExport').disabled = ids.length === 0;
    $('#echoDelete').disabled = ids.length === 0;
    $('#echoTxSend').disabled = !(ids.length === 1 && labTx.armValid());
  }

  async function viewFragment(id) {
    const iq = await lib.readIq(id); if (!iq) return;
    viewer = { id, I: iq.I, Q: iq.Q, meta: iq.meta };
    $('#echoViewWrap').style.display = 'block';
    const v = await lib.verify(id);
    $('#echoViewMeta').innerHTML = `${fmtHz(iq.meta.channelCentreHz)} · ${labelTxt(iq.meta.label)} · ${(iq.meta.durationS || 0).toFixed(2)} s · SNR ${Number.isFinite(iq.meta.snrDb) ? iq.meta.snrDb.toFixed(0) : '?'} dB · stop: ${esc(iq.meta.stopReason || '?')} · integrity ${v.ok ? 'OK' : 'FAILED'}` +
      (iq.meta.features && iq.meta.features.syncHits ? ` · sync hits ${iq.meta.features.syncHits}` : '') + ` · <span class="note">viewing only, no audio/decode</span>`;
    drawFragmentWaterfall(iq.I, iq.Q, iq.meta.iqRate);
  }
  function drawFragmentWaterfall(I, Q, rate) {
    const c = $('#echoView'); if (!c) return;
    const D2 = window.EchoDsp, N = 512, hop = Math.max(N, Math.floor(I.length / 220));
    const rows = [];
    for (let o = 0; o + N <= I.length; o += hop) { const sp = D2.psd(I.subarray(o, o + N), Q.subarray(o, o + N), N, 1); rows.push(toDbRow(sp)); }
    c._rows = rows; c._lo = rate / -2 + viewer.meta.channelCentreHz; drawRows(c, rows);
  }
  function toDbRow(sp) { const o = new Float32Array(sp.length); for (let i = 0; i < sp.length; i++) o[i] = 10 * Math.log10(sp[i] + 1e-20); return o; }
  function drawRows(c, rows) {
    if (typeof fitCanvas === 'function') fitCanvas(c);
    const ctx = c.getContext('2d'), W = c.width, H = c.height; ctx.fillStyle = '#07090c'; ctx.fillRect(0, 0, W, H);
    if (!rows.length) return;
    let lo = Infinity, hi = -Infinity; for (const r of rows) for (const v of r) { if (v < lo) lo = v; if (v > hi) hi = v; }
    const img = ctx.createImageData(W, H), N = rows[0].length;
    for (let y = 0; y < H; y++) { const r = rows[Math.min(rows.length - 1, Math.floor(y / H * rows.length))];
      for (let x = 0; x < W; x++) { const v = r[Math.floor(x / W * N)], t = Math.max(0, Math.min(1, (v - lo) / (hi - lo + 1e-9))); const k = (y * W + x) * 4; const col = heat(t); img.data[k] = col[0]; img.data[k + 1] = col[1]; img.data[k + 2] = col[2]; img.data[k + 3] = 255; } }
    ctx.putImageData(img, 0, 0);
  }
  function drawFragmentRow(sp) { const c = $('#echoView'); if (!c || $('#echo').className.indexOf('on') < 0) return; if (typeof scrollRow === 'function') { const row = toDbRow(sp); let lo = Infinity, hi = -Infinity; for (const v of row) { if (v < lo) lo = v; if (v > hi) hi = v; } scrollRow(c, row, lo, hi); } }
  function heat(t) { const r = Math.round(255 * Math.min(1, t * 1.6)), g = Math.round(255 * Math.max(0, Math.min(1, t * 1.6 - 0.4))), b = Math.round(255 * Math.max(0, 0.5 - Math.abs(t - 0.25))); return [r, g, b]; }

  // ---------------------------------------------------------------- UI: Lab TX
  function renderTx() {
    const s = labTx.status(), el = $('#echoTxState');
    el.className = 'echo-tx-state' + (s.state === 'TRANSMITTING' ? ' tx' : s.armed ? ' armed' : s.state === 'FAULT' ? ' fault' : '');
    el.textContent = s.state === 'TRANSMITTING' ? '⚡ TRANSMITTING' : s.armed ? `ARMED @ ${fmtHz(s.arm.centreHz)} (${s.arm.expiresInS.toFixed(0)} s left, ${s.arm.txGain} dB)` : s.state === 'FAULT' ? 'FAULT — ' + (s.fault || '') + ' (clear & re-arm)' : 'RF OFF';
    $('#echoTxEnable').checked = s.enabled; $('#echoTxVerified').checked = s.labVerified;
    $('#echoTxArm').disabled = !(s.enabled && s.labVerified && s.allowlistCount && !s.busy && s.state !== 'FAULT');
    $('#echoTxSend').disabled = !(selectedIds().length === 1 && labTx.armValid());
    setBanner();
    renderTxList(); renderTxArmOptions();
  }
  function renderTxList() {
    const tb = $('#echoTxList tbody'); if (!tb) return; tb.innerHTML = '';
    for (const e of allowlist.list()) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="mono">${fmtHz(e.centreHz)}</td><td>${esc(e.label || '')}</td><td class="mono">${e.maxGain} dB</td><td></td>`;
      const b = mkBtn('Remove', () => { allowlist.remove(e.centreHz); renderTx(); }); tr.lastChild.append(b); tb.append(tr);
    }
  }
  function renderTxArmOptions() {
    const sel = $('#echoTxArmFreq'); if (!sel) return; const cur = sel.value; sel.innerHTML = '';
    for (const e of allowlist.list()) { const o = document.createElement('option'); o.value = e.centreHz; o.textContent = `${(e.centreHz / 1e6).toFixed(5)} MHz ${e.label ? '— ' + e.label : ''}`; sel.append(o); }
    if (cur) sel.value = cur;
  }
  function logTx(e) {
    const box = $('#echoTxLog'); if (!box) return;
    const line = document.createElement('div');
    line.textContent = `[${new Date().toLocaleTimeString()}] ${e.kind}${e.centreHz ? ' @ ' + (e.centreHz / 1e6).toFixed(5) + ' MHz' : ''}${e.reason ? ' — ' + e.reason : ''}${e.error ? ' — ' + e.error : ''}${e.checksum ? ' cksum ' + e.checksum : ''}${e.sentRepeats != null ? ' reps ' + e.sentRepeats : ''}`;
    box.prepend(line); while (box.childNodes.length > 60) box.lastChild.remove();
  }

  async function doTxSend() {
    const ids = selectedIds(); if (ids.length !== 1) return;
    if (scanning) { setStatus('stop scanning before Lab TX (half-duplex)', 'warn'); return; }
    const iq = await lib.readIq(ids[0]); if (!iq) return;
    const elig = LabTx.sourceEligible(iq.meta);
    if (elig.length) { alert('This fragment may not be transmitted:\n' + elig.join('\n')); return; }
    labTx.transport = txTransport;
    const rxGuard = { before: async () => { if (hrf) { hrf.onData = null; await hrf.setMode(MODE.OFF); } }, after: async () => {} };
    try {
      await labTx.transmit({ I: iq.I, Q: iq.Q, rate: iq.meta.iqRate, meta: iq.meta }, { repeat: Math.min(+$('#echoTxMaxRep').value, +$('#echoRepeat').value || 1), pauseMs: +$('#echoPause').value, rxGuard });
      setStatus('Lab TX complete; RF off', 'ok');
    } catch (e) { setStatus('Lab TX: ' + e.message, 'bad'); }
    renderTx();
  }

  // ---------------------------------------------------------------- wiring
  function wire() {
    $('#echoProfBtn').onclick = () => { const p = $('#echoProfPop'); const show = p.style.display === 'none'; p.style.display = show ? 'block' : 'none'; if (show) renderProfiles(); };
    document.addEventListener('click', e => { if (!$('#echoProfPop').contains(e.target) && e.target !== $('#echoProfBtn')) $('#echoProfPop').style.display = 'none'; });
    $('#echoStart').onclick = startScan;
    for (const id of ['#echoMode', '#echoDetect', '#echoThr', '#echoMax']) $(id).onchange = () => { if (worker) worker.postMessage({ cmd: 'config', capture: { detect: $('#echoDetect').value, thresholdDb: +$('#echoThr').value, maxS: Prof_clampMax($('#echoMax').value) }, sched: { mode: $('#echoMode').value } }); };
    $('#echoAuto').onchange = () => { autoEcho = $('#echoAuto').checked; };
    $('#echoPlay').onclick = () => { const ids = selectedIds(); for (const id of ids) { try { replay.enqueue(id, { repeat: +$('#echoRepeat').value, pauseMs: +$('#echoPause').value }); } catch (e) { setStatus(e.message, 'warn'); } } $('#echoStopPlay').disabled = false; };
    $('#echoStopPlay').onclick = () => replay.stop();
    $('#echoExport').onclick = exportSel;
    $('#echoDelete').onclick = async () => { const ids = selectedIds(); if (ids.length && confirm(`Delete ${ids.length} fragment(s)?`)) { await lib.removeMany(ids); await renderLib(); } };
    $('#echoImport').onchange = () => importFiles($('#echoImport').files);
    $('#echoQuota').onclick = async () => { const st = await lib.stats(); const mc = parseInt(prompt('Max clips:', st.quota.maxClips)); const mb = parseInt(prompt('Max MB:', (st.quota.maxBytes / 1048576).toFixed(0))); if (mc > 0 && mb > 0) { await lib.setQuota({ maxClips: mc, maxBytes: mb * 1048576 }); await renderLib(); } };
    $('#echoAll').onchange = () => { for (const cb of document.querySelectorAll('#echoLib tbody input')) cb.checked = $('#echoAll').checked; updateSel(); };
    // Lab TX
    $('#echoTxEnable').onchange = () => { labTx.enable($('#echoTxEnable').checked); };
    $('#echoTxVerified').onchange = () => { labTx.setLabVerified($('#echoTxVerified').checked); };
    $('#echoTxAdd').onclick = () => { try { allowlist.add({ centreHz: Math.round(parseFloat($('#echoTxAddFreq').value) * 1e6), label: $('#echoTxAddLabel').value, maxGain: +$('#echoTxAddGain').value }); renderTx(); } catch (e) { alert(e.message); } };
    $('#echoTxArm').onclick = () => { try { labTx.armFor(+$('#echoTxArmFreq').value, { txGain: +$('#echoTxGain').value, maxClipS: +$('#echoTxMaxClip').value, maxRepeats: +$('#echoTxMaxRep').value }); } catch (e) { alert(e.message); } renderTx(); };
    $('#echoTxSend').onclick = doTxSend;
    $('#echoTxStop').onclick = () => { labTx.emergencyStop(); setStatus('Emergency Stop: RF off', 'warn'); renderTx(); };
    setInterval(() => { if (labTx.armValid() || labTx.state === 'ARMED') renderTx(); }, 1000);
    renderChips(); renderTx();
  }
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  async function exportSel() {
    for (const id of selectedIds()) {
      const ex = await lib.exportSigmf(id);
      saveText(ex.name + '.sigmf-meta', ex.metaJson);
      saveBlobLocal(ex.name + '.sigmf-data', new Blob([ex.data], { type: 'application/octet-stream' }));
    }
  }
  async function importFiles(files) {
    const byBase = {};
    for (const f of files) { const base = f.name.replace(/\.sigmf-(meta|data)$/, ''); (byBase[base] = byBase[base] || {})[f.name.endsWith('meta') ? 'meta' : 'data'] = f; }
    for (const [base, pair] of Object.entries(byBase)) {
      if (!pair.meta || !pair.data) { setStatus(`skip ${base}: need both .sigmf-meta and .sigmf-data`, 'warn'); continue; }
      try { const meta = await pair.meta.text(); const data = new Int8Array(await pair.data.arrayBuffer()); await lib.importSigmf(meta, data, { name: base }); }
      catch (e) { setStatus('import ' + base + ': ' + e.message, 'bad'); }
    }
    await renderLib(); $('#echoImport').value = '';
  }
  function saveText(name, text) { saveBlobLocal(name, new Blob([text], { type: 'application/json' })); }
  function saveBlobLocal(name, blob) { const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 60000); }

  // ---------------------------------------------------------------- public hooks
  function onAttach(h) {
    $('#echoStart').disabled = false;
    // discover the TX (OUT) endpoint from the live USB descriptor instead of assuming a number
    try { const alt = h.dev.configuration.interfaces[0].alternate; const out = alt.endpoints.find(e => e.direction === 'out'); if (out) txEndpoint = out.endpointNumber; } catch (e) {}
    renderTx();
  }
  function onDeviceLost() { if (scanning) stopScan(); labTx.onDeviceLost(); $('#echoStart').disabled = true; renderTx(); }
  function onShow() { renderChips(); renderStatus(); renderLib(); renderTx(); }
  async function releaseDevice() { if (scanning) await stopScan(); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire); else wire();

  return { onAttach, onDeviceLost, onShow, releaseDevice, scanning: () => scanning, stopScan,
    _store: store, _lib: lib, _labTx: labTx, _replay: replay };
})();
