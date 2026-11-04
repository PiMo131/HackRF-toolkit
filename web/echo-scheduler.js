// Echo tab: adaptive scan scheduler (pure planning, testable) and the engine that drives a radio with it.
//
//   IDLE -> DISCOVERY -> RANK_CANDIDATES -> FOCUSED_DWELL -> [CAPTURING -> FINALIZING -> SAVED] -> MAINTENANCE -> DISCOVERY
//   plus STOPPING / PAUSED (half-duplex: the radio is handed to Lab TX) / ERROR.
//
// DISCOVERY is the firmware sweep over the union of the selected ranges (fast energy survey, several passes so
// one-pass blips and spurs do not become candidates). Energy only makes an `energy_candidate`; a FOCUSED_DWELL
// (normal RX at a stable hardware rate, channelised to 50 ksps) decides activity / unknown digital / possible DMR /
// confirmed DMR. Candidates are hot (recent DMR-like), warm (recent energy) or cold; every survey revisits all
// segments, and a survey is forced after `maxNoSurveyS` however busy the dwell queue is (no starvation of quiet bands).
//
// Numbers in SCHED_DEFAULTS are starting points to tune empirically, not guaranteed response times.
"use strict";
(function (root, factory) {
  const node = typeof module !== 'undefined' && module.exports;
  const m = factory(node ? require('./echo-dsp.js') : root.EchoDsp, node ? require('./echo-profiles.js') : root.EchoProfiles, node ? require('./echo-capture.js') : root.EchoCapture);
  if (node) module.exports = m; else root.EchoScheduler = m;
})(typeof self !== 'undefined' ? self : this, function (D, P, C) {

  const SCHED_DEFAULTS = {
    mode: 'adaptive',            // 'adaptive' | 'thorough' | 'watchlist'
    hotRevisitS: 2, warmRevisitS: 10, surveyS: 60, maxNoSurveyS: 120,
    hotWindowS: 120, warmWindowS: 300, candidateTtlS: 1800,
    surveyPasses: 4, binN: 4096, thresholdDb: 10, minHitFrac: 0.5, wideHz: 60e3,
    segmentHz: 1e6, raster: 6250, maxCandidates: 96, maxChannelsPerDwell: 3,
    rxRate: 8e6, probeMs: 400, inconclusiveMs: 1200, settleMs: 30,
    alwaysOnDwells: 3,
  };
  const MODE_OVERRIDES = {
    adaptive: {},
    thorough: { surveyPasses: 10, surveyS: 30, maxNoSurveyS: 60, probeMs: 600 },
    watchlist: { warmRevisitS: 6, surveyS: 90, maxNoSurveyS: 180 },
  };
  const RANK = D.LABEL_RANK;
  const clamp01 = x => Math.max(0, Math.min(1, x));
  const lin = db => 10 ** (db / 10);
  // HackRF clock harmonics (same rule as the Scan tab's spurNote)
  const isSpur = f => Math.abs(f - Math.round(f / 40e6) * 40e6) < 60e3;

  class EchoScheduler {
    constructor(intervals, opts = {}) {
      const mode = opts.mode || SCHED_DEFAULTS.mode;
      this.o = { ...SCHED_DEFAULTS, ...MODE_OVERRIDES[mode], ...opts, mode };
      this.intervals = intervals;
      this.ranges = P.sweepRanges(intervals);
      this.segments = [];
      for (const iv of intervals) {
        for (let a = iv.lo; a < iv.hi; a += this.o.segmentHz) {
          const b = Math.min(iv.hi, a + this.o.segmentHz);
          this.segments.push({ id: `${Math.round(a / 1e3)}k`, lo: a, hi: b, profileIds: iv.profileIds, channels: iv.channels.filter(c => c >= a && c < b),
            lastVisit: -Infinity, lastSurvey: -Infinity, visits: 0, revisitEwma: NaN, floorDb: NaN, hits: 0, wide: 0, dwellS: 0,
            missedRecording: 0, missedTx: 0 });
        }
      }
      this.cands = new Map();
      this.lastSurvey = -Infinity; this.surveys = 0; this.dwells = 0; this.lastDwellId = null;
      if (this.o.mode === 'watchlist') for (const s of this.segments) for (const f of s.channels) this._cand(f, s, 0).watch = true;
    }
    _cand(f, seg, now) {
      let c = this.cands.get(f);
      if (!c) {
        c = { id: f, f, profileIds: seg.profileIds, firstSeen: now, lastSeen: -Infinity, lastDwell: -Infinity, dmrSeen: -Infinity,
          hits: 0, surveysSeen: 0, misses: 0, snrDb: NaN, label: 'energy_candidate', bestLabel: 'energy_candidate', dwells: 0, dwellS: 0,
          inconclusive: 0, spur: isSpur(f), alwaysOn: 0, activeDwells: 0, clips: 0, watch: false, score: 0, tier: 'cold' };
        this.cands.set(f, c);
      }
      return c;
    }
    // an existing candidate within one 6.25 kHz raster step is the same emitter (centroid jitter between surveys)
    _near(f) { let best = null; for (const c of this.cands.values()) if (Math.abs(c.f - f) <= this.o.raster * 1.3 && (!best || Math.abs(c.f - f) < Math.abs(best.f - f))) best = c; return best; }
    snap(f, seg) {
      for (const ch of seg.channels) if (Math.abs(ch - f) <= P.CH_HALF) return ch;
      return Math.round(f / this.o.raster) * this.o.raster;
    }
    // ---------------------------------------------------------- scoring
    tier(c, now) {
      if (c.dmrSeen > -Infinity && now - c.dmrSeen < this.o.hotWindowS && RANK[c.bestLabel] >= RANK.possible_dmr) return 'hot';
      if (now - c.lastSeen < this.o.warmWindowS || c.watch) return 'warm';
      return 'cold';
    }
    score(c, now) {
      const snr = Number.isFinite(c.snrDb) ? clamp01(c.snrDb / 30) : 0;
      const rec = c.lastSeen > -Infinity ? Math.exp(-(now - c.lastSeen) / 60) : 0;
      const dmr = c.dmrSeen > -Infinity ? ({ confirmed_dmr: 1, possible_dmr: 0.6, unknown_digital: 0.2 }[c.bestLabel] || 0) * Math.exp(-(now - c.dmrSeen) / 300) : 0;
      return 0.35 * snr + 0.2 * clamp01(c.hits / 10) + 0.25 * rec + 0.4 * dmr - (c.spur ? 0.5 : 0) - 0.3 * clamp01(c.inconclusive / 3) - 0.6 * (c.alwaysOn >= this.o.alwaysOnDwells ? 1 : 0);
    }
    interval(c, now) {
      const t = this.tier(c, now);
      let s = t === 'hot' ? this.o.hotRevisitS : t === 'warm' ? this.o.warmRevisitS : Infinity;
      s *= 1 + Math.min(4, c.inconclusive);                                    // inconclusive carriers: back off
      if (c.alwaysOn >= this.o.alwaysOnDwells) s *= 6;                        // always-on signals: separate, slow policy
      if (c.spur) s *= 4;
      return s;
    }
    eligible(c) { return c.watch || c.dwells === 0 ? (c.hits > 0 || c.watch) : true; }
    due(now) {
      const out = [];
      for (const c of this.cands.values()) {
        c.tier = this.tier(c, now); c.score = this.score(c, now);
        if (!this.eligible(c)) continue;
        const iv = this.interval(c, now), since = now - c.lastDwell;
        // never-dwelled energy candidates are due at once; everything else by revisit interval
        if (c.dwells === 0 && c.lastSeen > -Infinity) out.push({ c, key: 1e6 + c.score });
        else if (Number.isFinite(iv) && since >= iv) out.push({ c, key: (since / iv) * (0.5 + Math.max(0, c.score)) });
      }
      return out.sort((a, b) => b.key - a.key || a.c.lastDwell - b.c.lastDwell).map(x => x.c);
    }
    // ---------------------------------------------------------- planning
    next(now) {
      if (!this.ranges.length) return { type: 'idle', reason: 'no ranges selected' };
      const sinceSurvey = now - this.lastSurvey;
      if (sinceSurvey >= this.o.maxNoSurveyS) return this._survey('forced: background survey overdue');
      const due = this.due(now);
      if (due.length) {
        const first = due[0], group = [first];
        for (const c of due.slice(1)) {
          if (group.length >= this.o.maxChannelsPerDwell) break;
          if (P.windowFor([...group, c].map(x => x.f), { rate: this.o.rxRate }) !== null) group.push(c);
        }
        const fc = P.windowFor(group.map(x => x.f), { rate: this.o.rxRate });
        if (fc !== null) {
          const inconclusive = group.every(c => c.inconclusive > 0);
          return { type: 'dwell', fc, rate: this.o.rxRate, channels: group.map(c => ({ f: c.f, candId: c.id, tier: c.tier })),
            probeMs: this.o.probeMs, inconclusiveMs: inconclusive ? this.o.probeMs : this.o.inconclusiveMs, reason: `${first.tier} ${first.label}` };
        }
      }
      if (sinceSurvey >= this.o.surveyS || !due.length) return this._survey(due.length ? 'periodic survey' : 'nothing due: background survey');
      return { type: 'idle', reason: 'waiting' };
    }
    _survey(reason) { return { type: 'survey', ranges: this.ranges, passes: this.o.surveyPasses, binN: this.o.binN, reason }; }

    // ---------------------------------------------------------- results
    // result: { ranges: [{ loHz, df, passes: [Float32Array dB per bin] }], clipped: [tuning freq Hz] }
    onSurvey(result, now) {
      this.lastSurvey = now; this.surveys++;
      const thr = this.o.thresholdDb, found = [];
      for (const seg of this.segments) {
        const bins = [];               // [{f, mean, perPass[]}]
        for (const r of result.ranges) {
          const nb = r.passes[0] ? r.passes[0].length : 0;
          const k0 = Math.max(0, Math.ceil((seg.lo - r.loHz) / r.df)), k1 = Math.min(nb, Math.floor((seg.hi - r.loHz) / r.df));
          for (let k = k0; k < k1; k++) {
            let m = 0; const pp = r.passes.map(p => p[k]);
            for (const v of pp) m += lin(v); m /= pp.length;
            bins.push({ f: r.loHz + k * r.df, mean: 10 * Math.log10(m), pp, df: r.df });
          }
        }
        if (!bins.length) continue;
        const prevVisit = seg.lastVisit;
        seg.lastSurvey = now; this._visit(seg, now, prevVisit);
        const sorted = bins.map(b => b.mean).filter(Number.isFinite).sort((a, b) => a - b);
        seg.floorDb = sorted[Math.floor(sorted.length * 0.3)];
        const minHits = Math.max(1, Math.ceil(this.o.minHitFrac * result.ranges[0].passes.length));
        // bins above threshold in the averaged spectrum or in enough individual passes
        const mask = bins.map(b => b.mean > seg.floorDb + thr || b.pp.filter(v => v > seg.floorDb + thr).length >= minHits);
        for (let a = 0; a < bins.length;) {
          if (!mask[a]) { a++; continue; }
          let e = a; while (e + 1 < bins.length && (mask[e + 1] || (e + 2 < bins.length && mask[e + 2]))) e++;
          const run = bins.slice(a, e + 1), width = run.length * run[0].df;
          a = e + 1;
          const passesHit = run[0].pp.map((_, i) => run.some(b => b.pp[i] > seg.floorDb + thr)).filter(Boolean).length;
          if (passesHit < minHits) continue;                                   // one-pass blip: not persistent
          if (width > this.o.wideHz) { seg.wide++; continue; }                // wider than a narrowband channel
          let w = 0, fw = 0, pk = -Infinity;
          for (const b of run) { const p = lin(b.mean); w += p; fw += p * (b.f + b.df / 2); if (b.mean > pk) pk = b.mean; }
          const f = this.snap(fw / w, seg), near = this._near(f), c = near || this._cand(f, seg, now);
          c.hits += passesHit; c.surveysSeen++; c.lastSeen = now;
          c.snrDb = Number.isFinite(c.snrDb) ? 0.6 * c.snrDb + 0.4 * (pk - seg.floorDb) : pk - seg.floorDb;
          seg.hits++; found.push(c);
        }
      }
      // candidates covered by this survey but not seen
      const seen = new Set(found.map(c => c.id));
      for (const c of this.cands.values()) if (!seen.has(c.id)) c.misses++;
      this._prune(now);
      if (result.clipped && result.clipped.length) this.clipWarning = { at: now, steps: result.clipped.length };
      return found;
    }
    _visit(seg, now, prev) {
      if (Number.isFinite(prev) && prev > -Infinity) { const dt = now - prev; seg.revisitEwma = Number.isFinite(seg.revisitEwma) ? 0.7 * seg.revisitEwma + 0.3 * dt : dt; }
      seg.lastVisit = now; seg.visits++;
    }
    _prune(now) {
      for (const c of this.cands.values()) if (!c.watch && now - Math.max(c.lastSeen, c.lastDwell, c.firstSeen) > this.o.candidateTtlS) this.cands.delete(c.id);
      if (this.cands.size > this.o.maxCandidates) {
        const order = [...this.cands.values()].filter(c => !c.watch).sort((a, b) => this.score(a, now) - this.score(b, now));
        for (const c of order.slice(0, this.cands.size - this.o.maxCandidates)) this.cands.delete(c.id);
      }
    }
    // report: { fc, rate, start, end, channels: [{ candId, active, cls, clips, alwaysOn, activeS }] }
    onDwell(report, now) {
      this.dwells++;
      const dur = Math.max(0, report.end - report.start), lo = report.fc - report.rate * 0.4, hi = report.fc + report.rate * 0.4;
      for (const seg of this.segments) if (seg.hi > lo && seg.lo < hi) { this._visit(seg, now, seg.lastVisit); seg.dwellS += dur; }
      for (const ch of report.channels) {
        const c = this.cands.get(ch.candId); if (!c) continue;
        c.lastDwell = now; c.dwells++; c.dwellS += dur;
        if (ch.active) { c.activeDwells++; c.lastSeen = now; }
        if (ch.cls) {
          c.label = ch.cls.label;
          if (RANK[ch.cls.label] > (RANK[c.bestLabel] || 0)) c.bestLabel = ch.cls.label;
          if (RANK[ch.cls.label] >= RANK.possible_dmr) { c.dmrSeen = now; c.inconclusive = 0; }
          else if (ch.active && !ch.clips) c.inconclusive++;
        } else if (!ch.active) c.label = c.label === 'energy_candidate' ? 'energy_candidate' : c.label;
        if (ch.clips) c.clips += ch.clips;
        if (ch.alwaysOn || (ch.active && ch.activeS >= dur * 0.95 && dur > 0.3)) c.alwaysOn++; else if (!ch.active) c.alwaysOn = 0;
      }
      this.lastDwellId = report.channels.map(c => c.candId).join(',');
    }
    // time the radio was busy elsewhere (recording a clip, or transmitting): every segment outside `win` missed it
    noteBusy(kind, seconds, win) {
      for (const seg of this.segments) {
        if (win && seg.hi > win.lo && seg.lo < win.hi) continue;
        if (kind === 'tx') seg.missedTx += seconds; else seg.missedRecording += seconds;
      }
    }
    // ---------------------------------------------------------- reporting
    coverage(now) {
      const per = new Map();
      for (const s of this.segments) for (const id of s.profileIds) {
        const p = per.get(id) || { profileId: id, segments: 0, hz: 0, lastVisit: -Infinity, revisit: [], dwellS: 0, missedRecording: 0, missedTx: 0, hits: 0, wide: 0 };
        p.segments++; p.hz += s.hi - s.lo; p.lastVisit = Math.max(p.lastVisit, s.lastVisit); if (Number.isFinite(s.revisitEwma)) p.revisit.push(s.revisitEwma);
        p.dwellS += s.dwellS; p.missedRecording = Math.max(p.missedRecording, s.missedRecording); p.missedTx = Math.max(p.missedTx, s.missedTx); p.hits += s.hits; p.wide += s.wide;
        per.set(id, p);
      }
      return [...per.values()].map(p => {
        const est = p.revisit.length ? Math.max(...p.revisit) : NaN;
        const warn = [];
        if (!(p.lastVisit > -Infinity)) warn.push('not visited yet');
        else if (now - p.lastVisit > this.o.maxNoSurveyS) warn.push('stale');
        if (Number.isFinite(est) && est > this.o.surveyS) warn.push(`revisit ${est.toFixed(0)} s`);
        if (p.hz > 20e6) warn.push('wide profile: long revisit for dwells');
        if (p.wide) warn.push(`${p.wide} wideband region(s) skipped`);
        warn.push('survey = energy only; no 100% coverage claim');
        return { profileId: p.profileId, lastVisitAgoS: p.lastVisit > -Infinity ? now - p.lastVisit : null, estimatedRevisitS: est, dwellS: p.dwellS,
          missedWhileRecordingS: p.missedRecording, missedWhileTxS: p.missedTx, hits: p.hits, coverageWarning: warn.join('; ') };
      });
    }
    candidates(now) {
      return [...this.cands.values()].map(c => ({ ...c, tier: this.tier(c, now), score: this.score(c, now), revisitS: this.interval(c, now) }))
        .sort((a, b) => b.score - a.score);
    }
    coldList(now) { return this.segments.map(s => ({ id: s.id, lo: s.lo, hi: s.hi, sinceVisitS: s.lastVisit > -Infinity ? now - s.lastVisit : null, floorDb: s.floorDb, hits: s.hits })); }
  }

  // ------------------------------------------------------------------ engine
  // radio interface (WebUSB in echo.js, simulation in echo-sim.js):
  //   kind: 'hackrf' | 'sim' ;  now() -> seconds ;  wallClock(t) -> Date ;  sleep(ms)
  //   survey({ranges, passes, binN, gains}) -> { ranges: [{loHz, df, passes}], clipped: [] }
  //   startRx({fc, rate, gains}, onBlock(Int8Array)) ; stopRx() ; setGains(g)
  const STATES = ['IDLE', 'DISCOVERY', 'RANK_CANDIDATES', 'FOCUSED_DWELL', 'CAPTURING', 'FINALIZING', 'SAVED', 'MAINTENANCE', 'PAUSED', 'STOPPING', 'ERROR'];
  class EchoEngine {
    constructor({ radio, intervals, sched = {}, capture = {}, gains = { lna: 24, vga: 20, amp: 0 }, onState, onClip, onLog, onStatus, source }) {
      this.radio = radio; this.intervals = intervals; this.sched = new EchoScheduler(intervals, sched);
      this.cap = { ...C.CAPTURE_DEFAULTS, ...capture }; this.cap.maxS = C.clampMax(this.cap.maxS);
      this.gains = { ...gains }; this.state = 'IDLE'; this.running = false; this.pauseReq = null; this.paused = false;
      this.onState = onState || (() => {}); this.onClip = onClip || (() => {}); this.onLog = onLog || (() => {}); this.onStatus = onStatus || (() => {});
      this.source = source || (radio.kind === 'sim' ? 'synthetic' : 'live');
      this.stats = { surveys: 0, dwells: 0, clips: 0, rxPausedForTx: 0, rxPausedS: 0, usbStalls: 0, sampleDrops: 0, clipWarnings: 0, lastError: null };
      this.current = null;
    }
    _set(s, info) { this.state = s; this.onState(s, info || {}); }
    async start() {
      if (this.running) return;
      this.running = true; this.loop = this._run();
      return this.loop;
    }
    async stop() {
      if (!this.running) return;
      this._set('STOPPING'); this.running = false; this.stopReq = true;
      if (this.wake) this.wake();
      try { await this.loop; } catch (e) { /* reported */ }
      this.stopReq = false; this._set('IDLE');
    }
    // half-duplex hand-over: resolves once the current capture has finished and the radio is off
    pause(reason = 'tx') {
      if (this.paused) return Promise.resolve();
      return new Promise(res => { this.pauseReq = { reason, res }; if (this.wake) this.wake(); });
    }
    resume() { if (!this.paused) return; this.paused = false; if (this.wake) this.wake(); }
    async _run() {
      const r = this.radio;
      try {
        while (this.running) {
          if (this.pauseReq) {
            const p = this.pauseReq; this.pauseReq = null; this.paused = true; const t0 = r.now();
            this._set('PAUSED', { reason: p.reason }); p.res();
            while (this.paused && this.running) await new Promise(w => { this.wake = w; });
            this.wake = null;
            const dt = r.now() - t0; this.stats.rxPausedS += dt;
            if (p.reason === 'tx') { this.stats.rxPausedForTx++; this.sched.noteBusy('tx', dt, null); }
            if (!this.running) break;
            this.sched.lastSurvey = -Infinity;          // after TX: restore RX and start with a fresh survey
            continue;
          }
          const now = r.now(), act = this.sched.next(now);
          if (act.type === 'survey') {
            this._set('DISCOVERY', { reason: act.reason });
            const res = await r.survey({ ranges: act.ranges, passes: act.passes, binN: act.binN, gains: this.gains });
            if (!this.running) break;
            this.stats.surveys++; if (res.clipped && res.clipped.length) this.stats.clipWarnings++;
            this._set('RANK_CANDIDATES');
            const found = this.sched.onSurvey(res, r.now());
            this.onLog({ t: r.now(), kind: 'survey', found: found.length, reason: act.reason, ms: res.ms });
          } else if (act.type === 'dwell') {
            await this._dwell(act);
            this._set('MAINTENANCE');
          } else {
            this._set('MAINTENANCE', { reason: act.reason });
            await r.sleep(200);
          }
          this.onStatus(this.status());
        }
      } catch (e) {
        this.stats.lastError = e.message || String(e); this._set('ERROR', { error: this.stats.lastError });
        this.running = false;
        try { await r.stopRx(); } catch (e2) { /* already off */ }
        throw e;
      }
    }
    async _dwell(act) {
      const r = this.radio, o = this.cap, rate = act.rate;
      this._set('FOCUSED_DWELL', { fc: act.fc, channels: act.channels.map(c => c.f), reason: act.reason });
      const chans = act.channels.map(c => ({ ...c, ch: new D.NarrowChannelizer(rate, c.f - act.fc), trig: null, clips: 0, lastCls: null }));
      let settle = Math.round(rate * this.sched.o.settleMs / 1000) * 2, n0 = NaN, samples = 0, clipped = 0, capStartT = null, capWin = 0;
      const t0 = r.now(), probeS = act.probeMs / 1000, budgetS = (act.probeMs + act.inconclusiveMs) / 1000;
      const hardS = probeS + o.maxS * (o.maxSplits + 1) + o.hangMs / 1000 + 2;
      const pending = [];
      let reason = null, waiter = null, lastBlockT = r.now();
      const signal = why => { if (!reason) reason = why; if (waiter) { const w = waiter; waiter = null; w(); } };
      const onBlock = buf => {
        if (reason) return;                                                   // dwell over, radio about to stop
        const capturing0 = chans.some(c => c.trig && c.trig.state === 'capturing');
        if (!this.running || (this.pauseReq && !capturing0)) { signal('interrupted'); return; }
        lastBlockT = r.now();
        if (settle > 0) { settle -= buf.length; return; }                    // PLL / gain settle after the retune
        if (!Number.isFinite(n0)) {
          n0 = C.noiseDensity(buf, rate);
          for (const c of chans) c.trig = new C.ChannelTrigger(c.f, { ...o, n0 });
        }
        for (let i = 0; i < buf.length; i += 2) { const a = buf[i], b = buf[i + 1]; if (a === 127 || a === -128 || b === 127 || b === -128) clipped++; }
        samples += buf.length >> 1;
        let capturing = false, busy = false;
        for (const c of chans) {
          const [I, Q] = c.ch.process(buf);
          for (const e of c.trig.process(I, Q)) {
            if (e.event === 'clip') { c.clips++; pending.push(e.clip); }
            else if (e.event === 'rejected') c.lastCls = e.cls;
          }
          if (c.trig.lastClass) c.lastCls = c.trig.lastClass;
          if (c.trig.state === 'capturing') capturing = true;
          if (c.trig.busy) busy = true;
        }
        const el = samples / rate;
        if (capturing && capStartT === null) { capStartT = el; this._set('CAPTURING', { fc: act.fc }); }
        if (!capturing && capStartT !== null) { capWin += el - capStartT; capStartT = null; }
        if (el >= hardS) signal('hard cap');
        else if (el >= budgetS && !capturing) signal('budget');
        else if (el >= probeS && !busy) signal('probe done');
        else if (pending.length && waiter) { const w = waiter; waiter = null; w(); }   // hand finished clips over now
      };
      await r.startRx({ fc: act.fc, rate, gains: this.gains }, onBlock);
      const rxClip = () => samples ? clipped / samples / 2 : 0;
      while (!reason) {
        if (pending.length) { await this._emitClips(pending.splice(0), act, rate, rxClip()); continue; }
        await new Promise(res => { waiter = res; setTimeout(() => { if (waiter === res) waiter = null; res(); }, 250); });
        if (!reason && r.now() - lastBlockT > 3) { this.stats.usbStalls++; signal('usb stall'); }
      }
      await r.stopRx();
      for (const c of chans) if (c.trig) { const e = c.trig.abort(this.running ? 'dwell_aborted' : 'stopped'); if (e) { c.clips++; pending.push(e.clip); } }
      if (pending.length) await this._emitClips(pending.splice(0), act, rate, rxClip());
      if (capStartT !== null) capWin += samples / rate - capStartT;
      const end = r.now();
      this.stats.dwells++;
      this.sched.onDwell({ fc: act.fc, rate, start: t0, end, channels: chans.map(c => ({ candId: c.candId, active: !!(c.trig && c.trig.everActive), cls: c.lastCls,
        clips: c.clips, alwaysOn: !!(c.trig && c.trig.alwaysOn), activeS: c.trig ? c.trig.activeBlocks * 0.01 : 0 })) }, end);
      if (capWin > 0) this.sched.noteBusy('recording', capWin, { lo: act.fc - rate * 0.4, hi: act.fc + rate * 0.4 });
      this.onLog({ t: end, kind: 'dwell', fc: act.fc, why: reason, channels: chans.map(c => ({ f: c.f, label: c.lastCls ? c.lastCls.label : null, clips: c.clips })) });
      if (reason === 'usb stall') throw new Error('USB stream stalled during a dwell (device dropped off USB? check power/cable)');
    }
    async _emitClips(list, act, rate, rxClip) {
      for (const clip of list) {
        this._set('FINALIZING', { f: clip.freq });
        const q = D.toCi8(clip.I, clip.Q);
        const startT = this.radio.wallClock ? this.radio.wallClock() : new Date();
        const meta = {
          source: this.source, utc: new Date(startT.getTime() - clip.durationS * 1000).toISOString(),
          profileIds: P.profilesAt(this.intervals, clip.freq), rxCentreHz: act.fc, channelCentreHz: clip.freq, iqRate: clip.rate, format: 'ci8',
          hwRate: rate, gain: { ...this.gains }, rxClipFraction: rxClip, storeGain: q.gain, storeClipFraction: q.clipFraction,
          snrDb: clip.snrDb, label: clip.label, features: summarise(clip.features), stopReason: clip.stopReason, lateLock: clip.lateLock,
          preRollS: clip.preRollS, continuation: clip.continuation, durationS: clip.durationS, detect: this.cap.detect,
        };
        this.stats.clips++;
        await this.onClip({ data: q.data, meta });
        this._set('SAVED', { f: clip.freq });
      }
    }
    status() {
      const now = this.radio.now();
      return { state: this.state, now, stats: { ...this.stats }, candidates: this.sched.candidates(now).slice(0, 40), coverage: this.sched.coverage(now),
        cold: this.sched.coldList(now), ranges: this.sched.ranges, surveys: this.sched.surveys, mode: this.sched.o.mode };
    }
  }
  function summarise(f) { if (!f) return null; return { label: f.label, snrDb: +f.snrDb.toFixed(1), duty: +f.duty.toFixed(2), devHz: Math.round(f.devHz), fourLevel: +f.fourLevel.toFixed(2), symLine: +f.symLine.toFixed(1), syncHits: f.syncHits, syncTypes: f.syncTypes, freqOffsetHz: Math.round(f.freqOffsetHz) }; }

  return { EchoScheduler, EchoEngine, SCHED_DEFAULTS, MODE_OVERRIDES, STATES, isSpur, summarise };
});
