"""Live scanner: survey -> plan targets -> dwell (AGC + capture) -> async analysis.

The radio is used by one thing at a time (hackrf_sweep for surveys, libhackrf for
dwells); analysis/decoding runs in a process pool so the radio keeps moving.
"""
import json
import os
import signal as _signal
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from . import agc, bandplan, decoders, pipeline, recording, sweep
from .db import DB
from .hackrf import HackRF

WIDE_DECODERS = {"lte", "nr", "gsm", "dab", "ble"}
WINDOW_RATE = 20e6
WINDOW_USABLE = 0.8 * WINDOW_RATE


@dataclass
class Target:
    name: str
    center: float
    rate: float
    seconds: float
    decoders: list
    kind: str = "window"          # "window" (continuous carriers) | "watch" (bursty band)
    priority: float = 1.0
    bursty: bool = False
    keep_wide: bool = False
    regions: list = field(default_factory=list)
    last_visit: float = 0.0
    gain_hint: tuple = None


# Fixed watch list: bands where emitters are bursty and invisible to a sweep.
# Centres are offset so the channels of interest avoid the HackRF DC spike.
WATCH = [
    Target("ism433", 434.4e6, 4e6, 20, ["rtl433", "lora"], "watch", 3, True),
    Target("srd868", 868.0e6, 4e6, 20, ["rtl433", "lora"], "watch", 3, True),
    Target("vhf169", 169.95e6, 2e6, 30, ["pager", "rtl433"], "watch", 3, True),
    Target("adsb", 1089.0e6, 4e6, 10, ["adsb"], "watch", 3, True),
    Target("ais", 162.3e6, 2e6, 15, ["ais"], "watch", 2, True),
    Target("ble38", 2425.0e6, 8e6, 3, ["ble"], "watch", 2, True),
    Target("pmr446", 446.4e6, 2e6, 10, [], "watch", 1, True),
]


def is_spur(r):
    """HackRF sweep artefacts: narrow, never intermittent, at 20 MHz tuning steps or weak."""
    narrow = r.bw <= 2.5 * 98e3
    near_step = abs(((r.f_center + 10e6) % 20e6) - 10e6) < 120e3
    return narrow and ((near_step and r.occupancy < 0.5) or (r.occupancy < 0.05 and r.snr_db < 12))


# --focus odd: well-understood traffic that is skipped so time goes to the unusual
COMMON_CATEGORIES = {"cellular", "cellular_ul", "wifi", "broadcast", "rail"}
COMMON_DECODERS = {"lte", "nr", "gsm", "ble", "dab", "adsb"}
COMMON_WATCH = {"ble38", "adsb"}


def plan_windows(regions, f_lo, f_hi, skip_categories=(), skip_decoders=()):
    """Cluster survey regions into <=16 MHz capture windows at 20 Msps."""
    regs = sorted([r for r in regions if not is_spur(r)
                   and bandplan.best(r.f_center).category not in skip_categories], key=lambda r: r.f_lo)
    wins = []
    cur = []
    for r in regs:
        if cur and r.f_hi - cur[0].f_lo > WINDOW_USABLE:
            wins.append(cur)
            cur = []
        cur.append(r)
    if cur:
        wins.append(cur)
    out = []
    for w in wins:
        lo, hi = min(r.f_lo for r in w), max(r.f_hi for r in w)
        c = (lo + hi) / 2
        if hi - lo > WINDOW_USABLE:      # one region wider than a window: centre on its peak
            c = max(w, key=lambda r: r.snr_db).f_center
        # keep narrow signals off the DC spike
        if any(abs(r.f_center - c) < 100e3 and r.bw < 1e6 for r in w):
            c += 300e3
        decs = [d for d in bandplan.decoders_for(c - WINDOW_RATE * 0.4, c + WINDOW_RATE * 0.4)
                if d in WIDE_DECODERS and d not in skip_decoders]
        secs = 0.3
        for d in decs:
            mod = decoders.get(d)
            cap = getattr(mod, "CAPTURE", None) if mod else None
            if cap and cap.get("rate", WINDOW_RATE) >= 10e6:
                secs = max(secs, min(2.0, cap.get("seconds", 0.3)))
        prio = max(bandplan.best(r.f_center).priority for r in w) + max(r.snr_db for r in w) / 20
        band = bandplan.best(c)
        out.append(Target(f"win{c/1e6:.1f}", c, WINDOW_RATE, secs, decs, "window", prio,
                          bursty=band.category in ("ism", "wifi", "dect"),
                          keep_wide=bool(set(decs) & {"lte", "nr", "gsm"}), regions=w))
    return out


class Scanner:
    def __init__(self, out_dir, f_lo=1, f_hi=6000, survey_s=30, resurvey_s=900, duration=None,
                 watch=True, watch_names=None, storage_gb=10.0, workers=None, bin_w=100e3,
                 keep_wide=True, focus="all", log=print):
        self.out = out_dir
        os.makedirs(os.path.join(out_dir, "iq"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "surveys"), exist_ok=True)
        self.tmp_root = "/dev/shm/rfscan" if os.path.isdir("/dev/shm") else os.path.join(out_dir, "tmp")
        os.makedirs(self.tmp_root, exist_ok=True)
        self.db = DB(os.path.join(out_dir, "rfscan.sqlite"))
        self.events = open(os.path.join(out_dir, "events.jsonl"), "a")
        self.f_lo, self.f_hi = f_lo, f_hi
        self.survey_s, self.resurvey_s, self.duration = survey_s, resurvey_s, duration
        self.focus = focus
        self.skip_cats = COMMON_CATEGORIES if focus == "odd" else set()
        self.skip_decs = COMMON_DECODERS if focus == "odd" else set()
        self.watch = [w for w in WATCH if watch and (not watch_names or w.name in watch_names)
                      and not (focus == "odd" and w.name in COMMON_WATCH)]
        self.storage = storage_gb * 1e9
        self.bin_w = bin_w
        self.keep_wide = keep_wide
        self.log = log
        nw = workers or max(2, (os.cpu_count() or 4) // 2)
        self.pool = ProcessPoolExecutor(max_workers=nw)
        self.pending = {}
        self.max_pending = nw + 2
        self.plan = None
        self.stop = False
        self.targets = []
        self.t0 = time.time()
        self.wide_kept = set()

    # ------------------------------------------------------------ helpers
    def _event(self, etype, **kw):
        kw = dict(kw, type=etype, t_logged=time.time())
        self.events.write(json.dumps(kw, default=str) + "\n")
        self.events.flush()

    def _time_left(self):
        return self.duration is None or time.time() - self.t0 < self.duration

    # ------------------------------------------------------------- survey
    def do_survey(self):
        self.log(f"[survey] {self.f_lo}-{self.f_hi} MHz for {self.survey_s:.0f}s"
                 f"{' (gain calibration first)' if self.plan is None else ''}")
        regions, spec, self.plan = sweep.survey(self.f_lo, self.f_hi, self.bin_w, self.survey_s, self.plan,
                                                log=lambda m: self.log("  " + m.strip()))
        spurs = [is_spur(r) for r in regions]
        bands = [bandplan.best(r.f_center).service for r in regions]
        path = os.path.join(self.out, "surveys", time.strftime("survey_%Y%m%dT%H%M%S.npz"))
        np.savez_compressed(path, **{k: np.asarray(v, np.float32) for k, v in spec.items()})
        sid = self.db.add_survey(self.f_lo, self.f_hi, regions, os.path.relpath(path, self.out),
                                 [(lo, hi, list(g)) for lo, hi, g in self.plan], spurs, bands)
        real = [r for r, s in zip(regions, spurs) if not s]
        self.log(f"[survey] {len(regions)} energy regions ({len(regions) - len(real)} spur-like dropped)")
        by_cat = {}
        for r in real:
            by_cat.setdefault(bandplan.best(r.f_center).service, []).append(r)
        for svc, rs in sorted(by_cat.items(), key=lambda kv: -max(r.snr_db for r in kv[1]))[:25]:
            self.log(f"    {len(rs):3d} x  {svc[:55]:55s} best {max(r.snr_db for r in rs):4.1f} dB")
        self._event("survey", id=sid, n_regions=len(regions), n_real=len(real))
        wins = plan_windows(regions, self.f_lo * 1e6, self.f_hi * 1e6, self.skip_cats, self.skip_decs)
        self.targets = sorted(wins, key=lambda t: -t.priority) + list(self.watch)
        self.log(f"[plan] {len(wins)} capture windows + {len(self.watch)} watch bands"
                 f"{' (focus: odd ones out; cellular/Wi-Fi/BLE/broadcast/ADS-B skipped)' if self.focus == 'odd' else ''}")
        return sid

    # -------------------------------------------------------------- dwell
    def dwell(self, dev, tgt):
        dev.set_rate(tgt.rate)
        dev.tune(tgt.center)
        start = tgt.gain_hint or (tuple(tgt.regions[0].gain) if tgt.regions else (24, 20, 0))
        lna, vga, amp, p, clip = agc.auto_gain(dev, start=start, bursty=tgt.bursty)
        t = time.time()
        n = int(tgt.rate * tgt.seconds)
        _, raw = dev.capture(n, timeout=tgt.seconds + 5)
        a = np.abs(raw[: min(len(raw), 4_000_000)].astype(np.int16))
        clip_cap = float(np.mean(a >= 126))
        if clip_cap > 1e-4:          # a strong burst arrived after the probe: start lower next visit
            tgt.gain_hint = agc.split(max(0, agc.total(lna, vga, amp) - 12))
        else:
            tgt.gain_hint = (lna, vga, amp)
        base = os.path.join(self.tmp_root, f"cap_{int(t*1000)}_{tgt.name}")
        recording.write(base, tgt.center, tgt.rate, raw, "ci8", tgt.name,
                        extra={"rfscan:lna": lna, "rfscan:vga": vga, "rfscan:amp": amp,
                               "rfscan:clip_frac": clip_cap})
        del raw
        cid = self.db.add_capture(t, tgt.kind, tgt.name, tgt.center, tgt.rate, tgt.seconds,
                                  [lna, vga, amp], tgt.decoders)
        keep = self.keep_wide and tgt.keep_wide and tgt.name not in self.wide_kept
        job = {"path": base, "out_dir": self.out, "decoders": tgt.decoders, "bursts": tgt.kind == "watch",
               "t": t, "name": tgt.name, "save": self.db.saved_bytes() < self.storage, "keep_wide": keep,
               "delete_source": True, "tmp_root": self.tmp_root}
        fut = self.pool.submit(pipeline.analyze, job)
        self.pending[fut] = (cid, tgt, keep)
        tgt.last_visit = t
        self.log(f"[dwell] {tgt.name:12s} {tgt.center/1e6:9.3f} MHz {tgt.rate/1e6:4.1f} Msps {tgt.seconds:4.1f}s "
                 f"gain=({lna},{vga},{amp}) clip={clip_cap:.0e} -> {','.join(tgt.decoders) or 'classify'}")

    # ------------------------------------------------------------ results
    def collect(self, block=False):
        while self.pending:
            done = [f for f in self.pending if f.done()]
            if not done:
                if not block:
                    return
                time.sleep(0.2)
                continue
            for f in done:
                cid, tgt, keep = self.pending.pop(f)
                try:
                    res = f.result()
                except Exception as e:
                    self.log(f"[error] analysis of {tgt.name} failed: {e}")
                    continue
                try:
                    self._handle(cid, tgt, res)
                except Exception as e:   # never let bookkeeping kill a long scan
                    self.log(f"[error] storing results of {tgt.name}: {e!r}")
            if not block:
                return

    def _handle(self, cid, tgt, res):
        for d in res["decodes"]:
            if not self.db.known_ident(d["decoder"], d["id"]):
                tag = {"crc": "CRC ", "sync": "SYNC", "detect": "DET "}[d["level"]]
                self.log(f"  NEW {tag} {d['decoder']:7s} {d['freq_hz']/1e6:10.4f} MHz  {d['label'][:100]}")
            self._event("decode", capture=cid, **d)
        if res.get("wide_path"):
            self.wide_kept.add(tgt.name)
        interesting = [s for s in res["signals"] if s["snr"] > 12][:6]
        for s in interesting:
            self._event("signal", capture=cid, **s)
        if interesting and tgt.kind == "watch":
            self.log(f"  [{tgt.name}] " + "; ".join(f"{s['freq']/1e6:.3f} {s['label'][:40]} ({s.get('n_bursts', 0)}x)"
                                                 for s in interesting[:4]))
        for e in res.get("errors", []):
            self.log(f"  [warn] {tgt.name}: {e.strip().splitlines()[-1]}")
        self.db.finish_capture(cid, res)

    def _open(self, wait_s=120):
        """Open the HackRF, retrying for wait_s (USB drop-outs, power saving)."""
        t_end = time.time() + wait_s
        while not self.stop:
            try:
                return HackRF()
            except Exception as e:
                if time.time() > t_end:
                    self.log(f"[error] HackRF unavailable for {wait_s}s ({e}); stopping")
                    self.stop = True
                    return None
                self.log(f"[warn] HackRF not available ({e}); retrying")
                time.sleep(5)
        return None

    # --------------------------------------------------------------- loop
    def run(self):
        def _sig(*a):
            self.stop = True
            self.log("[stop] finishing current step (Ctrl-C again to force)")
            _signal.signal(_signal.SIGINT, _signal.default_int_handler)
        _signal.signal(_signal.SIGINT, _sig)
        avail = sorted(decoders.registry())
        self.log(f"[rfscan] output {self.out}  decoders: {', '.join(avail)}")
        last_survey = 0
        try:
            while not self.stop and self._time_left():
                if time.time() - last_survey > self.resurvey_s or not self.targets:
                    self.collect(block=False)
                    self.do_survey()
                    last_survey = time.time()
                todo = sorted(self.targets, key=lambda t: (t.last_visit, -t.priority))
                dev = self._open()
                if dev is None:
                    break
                try:
                    for tgt in todo:
                        if self.stop or not self._time_left():
                            break
                        if time.time() - last_survey > self.resurvey_s:
                            break
                        while len(self.pending) >= self.max_pending:
                            self.collect(block=False)
                            time.sleep(0.1)
                        for attempt in (1, 2):
                            try:
                                self.dwell(dev, tgt)
                                break
                            except Exception as e:
                                self.log(f"[error] dwell {tgt.name}: {e}")
                                if not any(k in str(e) for k in ("No such device", "not found", "timeout", "disconnected")):
                                    break
                                # USB drop-out: reopen the device and give this target one more try
                                dev.close()
                                dev = self._open()
                                if dev is None:
                                    break
                                if attempt == 1:
                                    self.log(f"[warn] HackRF reopened, retrying {tgt.name}")
                        if dev is None:
                            break
                        self.collect(block=False)
                finally:
                    if dev is not None:
                        dev.close()
        finally:
            self.log("[rfscan] waiting for analysis to finish...")
            self.collect(block=True)
            self.pool.shutdown()
            self.events.close()
            from . import report
            path = report.write(self.out)
            self.log(f"[rfscan] report: {path}")
