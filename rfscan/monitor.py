"""Long-term monitoring over the surveys of a run (and optionally a baseline run):

  * time-vs-frequency heatmap of all surveys (dB above the local noise floor),
  * occupancy statistics per band-plan band over time,
  * "new signal" alerts: an energy region that shows up in a survey but was never
    seen in the baseline (earlier surveys of the run and/or a baseline run), with a
    tolerance in frequency (F_TOL) and dB (DB_TOL).

Only numpy is used; the heatmap PNG is written with a tiny built-in encoder so the
module works in the bare venv (no matplotlib / PIL).  `Tracker` is incremental and
cheap, so the scheduler calls it right after each survey; `analyze()` replays a
stored run through the same Tracker for the CLI and the report.
"""
import base64
import html
import json
import os
import sqlite3
import struct
import time
import zlib

import numpy as np

from . import bandplan

F_TOL = 150e3        # Hz: a baseline region within this of the new one counts as "seen before"
DB_TOL = 6.0         # dB: the new region must stand this much above anything the baseline had there
MIN_SNR = 10.0       # dB: weaker regions are not alerted on
HEAT_W = 1400        # heatmap columns
HEAT_DB = 40.0       # heatmap colour scale: 0 .. HEAT_DB dB above the floor
CONT_DB = 8.0        # a bin counts as occupied when its mean power is this far above the floor
OUT_DIR = "monitor"
_CAT = {b.service: b.category for b in bandplan.BANDS}


# ----------------------------------------------------------------- PNG output
# one-hue sequential ramp (light -> dark blue), position 0..1
_RAMP = [(0.00, "f4f8fd"), (0.15, "cde2fb"), (0.35, "86b6ef"), (0.55, "3987e5"), (0.75, "1c5cab"),
         (0.90, "0d366b"), (1.00, "061a36")]
_RAMP_X = np.array([p for p, _ in _RAMP])
_RAMP_RGB = np.array([[int(c[i:i + 2], 16) for i in (0, 2, 4)] for _, c in _RAMP], float)

_FONT = {  # 3x5 glyphs for axis labels
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", ":": "000010000010000", ".": "000000000000010",
    "-": "000000111000000", " ": "000000000000000"}


def colorize(v):
    """dB-above-floor array -> uint8 RGB (..., 3) on the sequential ramp."""
    x = np.clip(np.nan_to_num(np.asarray(v, float), nan=0.0) / HEAT_DB, 0, 1)
    return np.stack([np.interp(x, _RAMP_X, _RAMP_RGB[:, i]) for i in range(3)], axis=-1).astype(np.uint8)


def png_bytes(rgb):
    """Encode an (h, w, 3) uint8 array as PNG."""
    h, w, _ = rgb.shape
    raw = np.concatenate([np.zeros((h, 1), np.uint8), rgb.reshape(h, w * 3)], axis=1).tobytes()

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _text(img, x, y, s, color=(60, 60, 66), scale=2):
    """Draw s (digits : . - space) with the 3x5 font, top-left at (x, y)."""
    for ch in s:
        g = _FONT.get(ch, _FONT[" "])
        for r in range(5):
            for c in range(3):
                if g[r * 3 + c] == "1":
                    y0, x0 = y + r * scale, x + c * scale
                    img[y0:y0 + scale, x0:x0 + scale] = color
        x += 4 * scale
    return x


def heat_image(rows):
    """Plot-area image (rows x HEAT_W) as uint8 RGB with a fixed row height."""
    n = len(rows)
    rh = 18 if n <= 40 else max(3, 720 // n)
    return np.repeat(colorize(np.asarray(rows)), rh, axis=0), rh


def heat_png(rows, labels, f0, f1, split=None):
    """Standalone heatmap PNG with time labels (left), MHz ticks (bottom) and a colour bar."""
    plot, rh = heat_image(rows)
    H, W = plot.shape[:2]
    L = 8 * max(len(s) for s in labels) + 8
    T, B, R = 4, 20, 44
    img = np.full((T + H + B, L + W + R, 3), 255, np.uint8)
    img[T:T + H, L:L + W] = plot
    every = max(1, int(np.ceil(12 / rh)))
    for i, s in enumerate(labels):
        if i % every == 0:
            _text(img, 4, T + i * rh + max(0, (rh - 10) // 2), s)
    if split:
        img[T + split * rh - 1:T + split * rh + 1, L:L + W] = (228, 87, 46)
    span = f1 - f0
    step = 500e6 if span > 2e9 else 100e6 if span > 300e6 else 10e6
    for t in np.arange(np.ceil(f0 / step) * step, f1, step):
        x = L + int((t - f0) / span * (W - 1))
        img[T + H:T + H + 4, x] = (100, 100, 106)
        s = f"{t/1e6:.0f}"
        _text(img, max(L, min(x - 4 * len(s), L + W - 8 * len(s))), T + H + 7, s)
    bar = colorize(np.linspace(HEAT_DB, 0, H))[:, None, :]
    img[T:T + H, L + W + 8:L + W + 18] = bar
    _text(img, L + W + 21, T, f"{HEAT_DB:.0f}")
    _text(img, L + W + 21, T + H - 10, "0")
    return png_bytes(img)


# -------------------------------------------------------------- data access
def load_run(run_dir):
    """Surveys of a stored run: [{id, t, path, regions}] with non-spur regions as dicts."""
    db = os.path.join(run_dir, "rfscan.sqlite")
    if not os.path.exists(db):
        raise FileNotFoundError(db)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = []
    for sid, t, sp in c.execute("SELECT id,t,spectrum_path FROM surveys ORDER BY id"):
        rows = c.execute("SELECT f_center,f_lo,f_hi,bw,snr,occupancy,continuous,band FROM regions "
                         "WHERE survey_id=? AND spur=0", (sid,)).fetchall()
        regs = [dict(f_center=r[0], f_lo=r[1], f_hi=r[2], bw=r[3], snr=r[4], occupancy=r[5],
                     continuous=bool(r[6]), band=r[7]) for r in rows]
        path = os.path.join(run_dir, sp) if sp else None
        out.append(dict(id=sid, t=t, path=path if path and os.path.exists(path) else None, regions=regs))
    c.close()
    return out


def load_spec(path):
    d = np.load(path)
    return {k: d[k] for k in ("freqs", "median", "max", "floor", "occupancy")}


def regions_from_objs(regions, spur_flags, band_names):
    """sweep.Region objects (as in the scheduler) -> the dict form used here."""
    return [dict(f_center=r.f_center, f_lo=r.f_lo, f_hi=r.f_hi, bw=r.bw, snr=r.snr_db, occupancy=r.occupancy,
                 continuous=bool(r.continuous), band=b)
            for r, s, b in zip(regions, spur_flags, band_names) if not s]


def excess_of(spec):
    """Mean power above the local noise floor per bin (NaN -> 0)."""
    with np.errstate(invalid="ignore"):
        return np.nan_to_num(spec["median"] - spec["floor"], nan=0.0).astype(np.float32)


def heat_row(exc, width=HEAT_W):
    """Max-preserving decimation of one spectrum to `width` columns."""
    n = len(exc)
    if n <= width:
        return np.interp(np.linspace(0, n - 1, width), np.arange(n), exc).astype(np.float32)
    edges = np.linspace(0, n, width + 1).astype(int)
    return np.maximum.reduceat(exc, edges[:-1]).astype(np.float32)


def band_stats(spec, exc):
    """Per band-plan band: {service: [occ_cont, occ_hits, peak_db]} for one survey."""
    f, occ = spec["freqs"], spec["occupancy"]
    out = {}
    for b in bandplan.BANDS:
        i0, i1 = np.searchsorted(f, b.lo * 1e6), np.searchsorted(f, b.hi * 1e6)
        if i1 <= i0:
            continue
        e, o = exc[i0:i1], occ[i0:i1]
        out[b.service] = [round(float(np.mean(e > CONT_DB)), 4), round(float(np.nanmean(o)), 4),
                          round(float(np.max(e)), 1)]
    return out


# ------------------------------------------------------------------ tracker
class Baseline:
    """What has been seen so far: regions (interval list) and the max excess spectrum."""

    def __init__(self, f_tol=F_TOL, db_tol=DB_TOL):
        self.f_tol, self.db_tol = f_tol, db_tol
        self.lo = np.empty(0)
        self.hi = np.empty(0)
        self.snr = np.empty(0)
        self.freqs = None
        self.excess = None
        self.n = 0

    def add(self, regions, spec=None, exc=None):
        if regions:
            self.lo = np.concatenate([self.lo, [r["f_lo"] for r in regions]])
            self.hi = np.concatenate([self.hi, [r["f_hi"] for r in regions]])
            self.snr = np.concatenate([self.snr, [r["snr"] for r in regions]])
        if spec is not None:
            if exc is None:
                exc = excess_of(spec)
            if self.freqs is None:
                self.freqs, self.excess = spec["freqs"].astype(np.float64), exc.copy()
            else:
                if len(spec["freqs"]) != len(self.freqs) or abs(spec["freqs"][0] - self.freqs[0]) > 1:
                    exc = np.interp(self.freqs, spec["freqs"], exc).astype(np.float32)
                np.maximum(self.excess, exc, out=self.excess)
        self.n += 1

    def baseline_db(self, f_lo, f_hi):
        if self.excess is None:
            return None
        i0 = np.searchsorted(self.freqs, f_lo - self.f_tol)
        i1 = np.searchsorted(self.freqs, f_hi + self.f_tol)
        return float(self.excess[i0:i1].max()) if i1 > i0 else 0.0

    def seen(self, r):
        """True when a baseline region within f_tol of r was not more than db_tol weaker."""
        return bool(np.any((self.lo - self.f_tol <= r["f_hi"]) & (self.hi + self.f_tol >= r["f_lo"])
                           & (self.snr >= r["snr"] - self.db_tol)))

    def new_regions(self, regions, min_snr=MIN_SNR):
        out = []
        for r in regions:
            if r["snr"] < min_snr or self.seen(r):
                continue
            b = self.baseline_db(r["f_lo"], r["f_hi"])
            if b is not None and b >= r["snr"] - self.db_tol:
                continue
            out.append(dict(r, baseline_db=None if b is None else round(b, 1)))
        return out


class Tracker:
    """Incremental monitor: feed surveys in time order, read heatmap rows, band stats and alerts."""

    def __init__(self, baseline_dir=None, f_tol=F_TOL, db_tol=DB_TOL, min_snr=MIN_SNR, width=HEAT_W):
        self.base = Baseline(f_tol, db_tol)
        self.min_snr, self.width = min_snr, width
        self.rows, self.labels, self.surveys, self.bands, self.new = [], [], [], [], []
        self.f0 = self.f1 = None
        self.baseline_dir = baseline_dir
        self.n_baseline = 0
        if baseline_dir:
            for s in load_run(baseline_dir):
                self.add_survey(s["id"], s["t"], s["regions"], load_spec(s["path"]) if s["path"] else None,
                                run="baseline")
            self.n_baseline = len(self.surveys)

    def add_survey(self, sid, t, regions, spec, run="run"):
        """Register one survey; returns the regions that are new w.r.t. everything before it."""
        new = [] if run == "baseline" else self.base.new_regions(regions, self.min_snr)
        exc = excess_of(spec) if spec is not None else None
        if spec is not None:
            self.rows.append(heat_row(exc, self.width))
            self.f0, self.f1 = float(spec["freqs"][0]), float(spec["freqs"][-1])
            self.bands.append(band_stats(spec, exc))
        else:
            self.rows.append(np.zeros(self.width, np.float32))
            self.bands.append({})
        self.labels.append(time.strftime("%H:%M", time.localtime(t)))
        self.surveys.append(dict(run=run, id=sid, t=t, n_regions=len(regions),
                                 regions=[(r["f_lo"], r["f_hi"]) for r in regions]))
        for r in new:
            self.new.append(dict(r, survey_id=sid, t=t))
        self.base.add(regions, spec, exc)
        return new

    def _labels(self):
        days = {time.strftime("%Y-%m-%d", time.localtime(s["t"])) for s in self.surveys}
        fmt = "%m-%d %H:%M" if len(days) > 1 else "%H:%M"
        return [time.strftime(fmt, time.localtime(s["t"])) for s in self.surveys]

    def summary(self):
        """JSON-able summary: surveys, new signals (with persistence), band occupancy over time."""
        tol = self.base.f_tol
        news = []
        for n in self.new:
            later = [s for s in self.surveys if s["run"] == "run" and s["t"] > n["t"]]
            k = sum(1 for s in later if any(lo - tol <= n["f_hi"] and hi + tol >= n["f_lo"] for lo, hi in s["regions"]))
            d = {k2: v for k2, v in n.items()}
            d.update(freq_mhz=round(n["f_center"] / 1e6, 4), bw_khz=round(n["bw"] / 1e3), snr=round(n["snr"], 1),
                     occupancy=round(n["occupancy"], 2), category=_CAT.get(n["band"], "unknown"),
                     persisted=k, later_surveys=len(later), t_str=_ts(n["t"]))
            news.append(d)
        # persistent ones first (seen again later, or no later survey yet), then one-offs; strongest first
        news.sort(key=lambda d: (not (d["persisted"] or not d["later_surveys"]), -d["snr"]))
        bands = []
        for b in bandplan.BANDS:
            series = [bs.get(b.service) for bs in self.bands]
            if not any(series):
                continue
            occ = [s[0] if s else None for s in series]
            vals = [o for o in occ if o is not None]
            bands.append(dict(band=b.service, category=b.category, lo=b.lo, hi=b.hi, occ=occ,
                              hits=[s[1] if s else None for s in series], peak=[s[2] if s else None for s in series],
                              mean=round(float(np.mean(vals)), 4) if vals else 0.0,
                              max=max(vals) if vals else 0.0, min=min(vals) if vals else 0.0,
                              latest=vals[-1] if vals else 0.0))
        return dict(generated=time.time(), baseline=self.baseline_dir, n_baseline_surveys=self.n_baseline,
                    n_surveys=len(self.surveys) - self.n_baseline,
                    params=dict(f_tol_hz=self.base.f_tol, db_tol=self.base.db_tol, min_snr=self.min_snr,
                                cont_db=CONT_DB, heat_db=HEAT_DB),
                    f_lo_hz=self.f0, f_hi_hz=self.f1, heat_rows=len(self.rows),
                    surveys=[dict(run=s["run"], id=s["id"], t=s["t"], t_str=_ts(s["t"]), n_regions=s["n_regions"])
                             for s in self.surveys],
                    new_signals=news, bands=bands)

    def write(self, run_dir, out_dir=None):
        """Write heatmap.png, heatmap.npz and summary.json; returns the summary."""
        out = out_dir or os.path.join(run_dir, OUT_DIR)
        os.makedirs(out, exist_ok=True)
        summ = self.summary()
        labels = self._labels()
        rows = np.asarray(self.rows, np.float32) if self.rows else np.zeros((0, self.width), np.float32)
        f0, f1 = self.f0 or 0.0, self.f1 or 1.0
        with open(os.path.join(out, "heatmap.png"), "wb") as fh:
            fh.write(heat_png(rows, labels, f0, f1, self.n_baseline or None))
        np.savez_compressed(os.path.join(out, "heatmap.npz"), rows=rows, labels=np.array(labels),
                            f0=f0, f1=f1, n_baseline=self.n_baseline)
        with open(os.path.join(out, "summary.json"), "w") as fh:
            json.dump(summ, fh, indent=1)
        return summ


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def analyze(run_dir, baseline_dir=None, f_tol=F_TOL, db_tol=DB_TOL, min_snr=MIN_SNR, write=True):
    """Replay a stored run through a Tracker. Returns (summary, tracker)."""
    tr = Tracker(baseline_dir, f_tol, db_tol, min_snr)
    for s in load_run(run_dir):
        tr.add_survey(s["id"], s["t"], s["regions"], load_spec(s["path"]) if s["path"] else None)
    summ = tr.write(run_dir) if write else tr.summary()
    return summ, tr


def log_events(run_dir, new_signals):
    """Append new_signal events to the run's events.jsonl (what the live scanner does itself)."""
    with open(os.path.join(run_dir, "events.jsonl"), "a") as fh:
        for n in new_signals:
            fh.write(json.dumps(dict(n, type="new_signal", t_logged=time.time()), default=str) + "\n")


# ------------------------------------------------------------------- report
_cache = {}


def load_or_analyze(run_dir, n_surveys):
    """Monitor outputs for the report: reuse monitor/ files when they cover all surveys."""
    out = os.path.join(run_dir, OUT_DIR)
    sp, hp = os.path.join(out, "summary.json"), os.path.join(out, "heatmap.npz")
    key = (os.path.abspath(run_dir), n_surveys)
    try:
        mt = os.path.getmtime(sp)
        if _cache.get(key, (None,))[0] == mt:
            return _cache[key][1], _cache[key][2]
        with open(sp) as fh:
            summ = json.load(fh)
        if summ.get("n_surveys") == n_surveys and os.path.exists(hp):
            d = np.load(hp)
            heat = dict(rows=d["rows"], labels=list(d["labels"]), f0=float(d["f0"]), f1=float(d["f1"]),
                        n_baseline=int(d["n_baseline"]))
            _cache[key] = (mt, summ, heat)
            return summ, heat
    except (OSError, ValueError, KeyError):
        pass
    summ, tr = analyze(run_dir, None)
    heat = dict(rows=np.asarray(tr.rows, np.float32), labels=tr._labels(), f0=tr.f0 or 0.0, f1=tr.f1 or 1.0,
                n_baseline=0)
    _cache[key] = (os.path.getmtime(sp), summ, heat)
    return summ, heat


def _svg_heatmap(heat, news, width=HEAT_W):
    rows = heat["rows"]
    if len(rows) == 0:
        return ""
    plot, rh = heat_image(rows)
    H = plot.shape[0]
    L, B, T = 90, 26, 6
    f0, f1 = heat["f0"], heat["f1"]
    X = lambda v: L + (v - f0) / (f1 - f0) * width
    uri = "data:image/png;base64," + base64.b64encode(png_bytes(plot)).decode()
    p = [f'<svg viewBox="0 0 {L + width + 10} {T + H + B}" class="heat" xmlns="http://www.w3.org/2000/svg">',
         f'<image x="{L}" y="{T}" width="{width}" height="{H}" preserveAspectRatio="none" href="{uri}"/>']
    every = max(1, int(np.ceil(12 / rh)))
    for i, s in enumerate(heat["labels"]):
        if i % every == 0:
            p.append(f'<text x="{L-6}" y="{T + i*rh + rh/2 + 4:.0f}" class="ax" text-anchor="end">{html.escape(s)}</text>')
    nb = heat.get("n_baseline", 0)
    if nb:
        p.append(f'<line x1="{L}" x2="{L+width}" y1="{T+nb*rh}" y2="{T+nb*rh}" stroke="#e4572e" stroke-width="2">'
                 f'<title>above: baseline run, below: this run</title></line>')
    step = 250e6 if (f1 - f0) > 2e9 else 50e6 if (f1 - f0) > 300e6 else 10e6
    for t in np.arange(np.ceil(f0 / step) * step, f1, step):
        p.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{T+H}" y2="{T+H+4}" class="grid"/>'
                 f'<text x="{X(t):.1f}" y="{T+H+18}" class="ax" text-anchor="middle">{t/1e6:.0f}</text>')
    ids = {(s_id): i for i, s_id in enumerate(heat.get("row_ids", []))}
    for n in news:
        i = ids.get(n.get("survey_id"))
        if i is None:
            continue
        x, y = X(n["f_center"]), T + i * rh + rh / 2
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="none" stroke="#e4572e" stroke-width="1.6">'
                 f'<title>{n["freq_mhz"]:.3f} MHz, {n["bw_khz"]:.0f} kHz, {n["snr"]:.1f} dB — new in survey {n["survey_id"]}'
                 f' ({html.escape(n.get("band") or "")})</title></circle>')
    p.append("</svg>")
    return "".join(p)


def _spark(vals, w=4, h=16):
    vals = [0.0 if v is None else float(v) for v in vals]
    top = max(0.01, max(vals))
    bars = "".join(f'<rect x="{i*(w+1)}" y="{h - max(1, v/top*h):.1f}" width="{w}" height="{max(1, v/top*h):.1f}" rx="1"/>'
                   for i, v in enumerate(vals))
    return f'<svg viewBox="0 0 {len(vals)*(w+1)} {h}" width="{len(vals)*(w+1)}" height="{h}" class="spark">{bars}</svg>'


def html_section(run_dir, n_surveys):
    """The monitoring block of report.html: heatmap, new signals, per-band occupancy."""
    try:
        summ, heat = load_or_analyze(run_dir, n_surveys)
    except Exception as e:   # never break the report
        return f'<h2>Monitoring</h2><p class="meta">monitor unavailable: {html.escape(str(e))}</p>'
    heat["row_ids"] = [s["id"] if s["run"] == "run" else None for s in summ["surveys"]]
    news = summ["new_signals"]
    once = [n for n in news if n["later_surveys"] and not n["persisted"]]   # seen in one survey only
    kept = [n for n in news if not (n["later_surveys"] and not n["persisted"])]
    rows_n = []
    for n in kept + once:
        pers = f'{n["persisted"]}/{n["later_surveys"]}' if n["later_surveys"] else "–"
        bdb = "" if n["baseline_db"] is None else f"{n['baseline_db']:.1f}"
        rows_n.append(f"<tr><td class='num'>{n['freq_mhz']:.3f}</td><td class='num'>{n['bw_khz']:.0f}</td>"
                      f"<td class='num'>{n['snr']:.1f}</td><td class='num'>{bdb}</td>"
                      f"<td class='num'>{n['occupancy']:.2f}</td><td>{'cont' if n['continuous'] else 'intermittent'}</td>"
                      f"<td>{html.escape(n.get('band') or '')}</td><td>{n['t_str']}</td><td class='num'>{pers}</td></tr>")
    rows_b = []
    for b in summ["bands"]:
        if b["max"] < 0.005:
            continue
        rows_b.append(f"<tr><td>{html.escape(b['band'])}</td><td>{b['category']}</td><td class='num'>{b['lo']:.3g}–{b['hi']:.4g}</td>"
                      f"<td class='num'>{b['latest']*100:.1f}</td><td class='num'>{b['mean']*100:.1f}</td>"
                      f"<td class='num'>{b['min']*100:.1f}–{b['max']*100:.1f}</td>"
                      f"<td class='num'>{max(p for p in b['peak'] if p is not None):.0f}</td><td>{_spark(b['occ'])}</td></tr>")
    base = summ.get("baseline")
    base_txt = f' &middot; baseline run {html.escape(base)} ({summ["n_baseline_surveys"]} surveys)' if base else \
        " &middot; baseline = earlier surveys of this run"
    prm = summ["params"]
    return f"""<h2>Monitoring</h2>
<div class="cards"><div class="card"><b>{len(news)}</b>new signals</div><div class="card"><b>{summ['n_surveys']}</b>surveys</div>
<div class="card"><b>{sum(1 for n in news if n['later_surveys'] and n['persisted'] == n['later_surveys'])}</b>persisted</div></div>
<p class="meta">Time vs frequency: mean power above the noise floor per survey (0–{prm['heat_db']:.0f} dB, light → dark){base_txt}.
Circles mark the survey in which a new signal first appeared (one-offs not marked).</p>
{_svg_heatmap(heat, kept)}
<h3>New signals</h3><p class="meta">Regions ≥ {prm['min_snr']:.0f} dB with no baseline region within {prm['f_tol_hz']/1e3:.0f} kHz
and standing ≥ {prm['db_tol']:.0f} dB above what the baseline spectrum had there. "persisted" = later surveys of this run that still show it.</p>
<div class="wrap"><table><tr><th>MHz</th><th>BW kHz</th><th>SNR</th><th>baseline dB</th><th>occupancy</th><th>type</th><th>band plan</th><th>first seen</th><th>persisted</th></tr>
{''.join(rows_n[:len(kept)]) or '<tr><td colspan=9 class=meta>none</td></tr>'}</table></div>
{f'<details><summary class="meta">{len(once)} more seen in one survey only (sweep glitches or single bursts)</summary><div class="wrap"><table>{"".join(rows_n[len(kept):])}</table></div></details>' if once else ''}
<h3>Band occupancy over time</h3><p class="meta">Fraction of each band's bins ≥ {prm['cont_db']:.0f} dB above the floor, per survey (bars, oldest left).</p>
<div class="wrap"><table><tr><th>band plan</th><th>category</th><th>MHz</th><th>latest %</th><th>mean %</th><th>min–max %</th><th>peak dB</th><th>trend</th></tr>
{''.join(rows_b) or '<tr><td colspan=8 class=meta>no surveys</td></tr>'}</table></div>"""
