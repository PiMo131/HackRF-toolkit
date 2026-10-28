"""Wideband survey with hackrf_sweep: auto-gain per 20 MHz segment, noise floor,
energy-region detection.

hackrf_sweep hops the full span ~1-2x per second.  Each bin is a single FFT
(no averaging), so detection uses statistics over many sweeps:
  * median power  -> continuous emitters (broadcast, cellular DL, beacons)
  * hit-rate above floor+10 dB -> intermittent emitters
Short bursts (ISM sensors) are mostly missed by a sweep; the scheduler covers
those with "watch" dwells on known bands instead.
"""
import subprocess
import time
from dataclasses import dataclass, field, asdict

import numpy as np

from .hackrf import tool

SEG_MHZ = 20
# (lna, vga, amp) candidates, low -> high total gain.  amp adds ~11 dB.
GAIN_LADDER = [(8, 12, 0), (16, 20, 0), (24, 24, 0), (32, 28, 0), (40, 30, 0), (40, 32, 1)]
HEADROOM_DB = -12.0   # keep strongest bin below this (hackrf_sweep dB scale)


def gain_total(g):
    lna, vga, amp = g
    return lna + vga + 11 * amp


@dataclass
class Grid:
    f0: float          # Hz, first bin centre
    df: float          # Hz bin width
    n: int

    def freqs(self):
        return self.f0 + self.df * np.arange(self.n)

    def idx(self, f):
        return int(round((f - self.f0) / self.df))


def _sweep_proc(ranges_mhz, gain, bin_w, n_sweeps=None):
    lna, vga, amp = gain
    exe, env = tool("hackrf_sweep")
    cmd = [exe, "-l", str(lna), "-g", str(vga), "-a", str(amp), "-w", str(int(bin_w))]
    for lo, hi in ranges_mhz:
        cmd += ["-f", f"{int(lo)}:{int(hi)}"]
    if n_sweeps:
        cmd += ["-N", str(n_sweeps)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            bufsize=1 << 20, env=env)


def iter_sweeps(ranges_mhz, gain, bin_w, n_sweeps=None, duration=None):
    """Yield dict{hz_low: np.array(dB)} per completed sweep."""
    p = _sweep_proc(ranges_mhz, gain, bin_w, n_sweeps)
    t_end = time.time() + duration if duration else None
    cur_ts, blocks = None, {}
    meta = {}
    try:
        for line in p.stdout:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            ts = parts[0] + parts[1]
            lo = int(parts[2])
            meta["df"] = float(parts[4])
            if ts != cur_ts:
                if blocks:
                    yield blocks, meta["df"]
                cur_ts, blocks = ts, {}
                if t_end and time.time() > t_end:
                    break
            blocks[lo] = np.array(parts[6:], dtype=np.float32)
        else:
            if blocks:
                yield blocks, meta["df"]
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


class Accumulator:
    """Places sweep blocks onto a fixed grid and keeps per-bin statistics."""

    def __init__(self, f_lo_mhz, f_hi_mhz, bin_w):
        self.f_lo, self.f_hi, self.req_bw = f_lo_mhz * 1e6, f_hi_mhz * 1e6, bin_w
        self.grid = None
        self.rows = []        # list of float32 arrays (one per sweep, NaN where not covered)
        self.gain_db = None   # per-bin total gain used

    def _ensure_grid(self, df):
        if self.grid is None:
            n = int(np.ceil((self.f_hi - self.f_lo) / df))
            self.grid = Grid(self.f_lo + df / 2, df, n)
            self.gain_db = np.full(n, np.nan, np.float32)

    def add(self, blocks, df, gain=None):
        self._ensure_grid(df)
        row = np.full(self.grid.n, np.nan, np.float32)
        for lo, db in blocks.items():
            i0 = int(round((lo - self.f_lo) / self.grid.df))
            i1 = i0 + len(db)
            a, b = max(i0, 0), min(i1, self.grid.n)
            if a < b:
                row[a:b] = db[a - i0:b - i0]
                if gain is not None:
                    self.gain_db[a:b] = gain_total(gain)
        self.rows.append(row)

    def matrix(self):
        return np.vstack(self.rows) if self.rows else np.empty((0, 0), np.float32)


def calibrate_gain(f_lo_mhz, f_hi_mhz, bin_w=500e3, sweeps_per_gain=3, log=print):
    """Pick per-20MHz-segment gain: highest ladder step that stays linear and below headroom."""
    segs = np.arange(f_lo_mhz, f_hi_mhz, SEG_MHZ)
    peak = {}   # gain -> per-seg peak dB
    floor = {}
    for g in GAIN_LADDER:
        acc = Accumulator(f_lo_mhz, f_hi_mhz, bin_w)
        for blocks, df in iter_sweeps([(f_lo_mhz, f_hi_mhz)], g, bin_w, n_sweeps=sweeps_per_gain):
            acc.add(blocks, df)
        m = acc.matrix()
        if m.size == 0:
            continue
        f = acc.grid.freqs() / 1e6
        mx = np.nanmax(m, axis=0)
        md = np.nanmedian(m, axis=0)
        pk, fl = [], []
        for s in segs:
            sel = (f >= s) & (f < s + SEG_MHZ)
            pk.append(np.nanmax(mx[sel]) if sel.any() else np.nan)
            fl.append(np.nanpercentile(md[sel], 20) if sel.any() else np.nan)
        peak[g], floor[g] = np.array(pk), np.array(fl)
        log(f"  gain {g}: median floor {np.nanmedian(fl):6.1f} dB, max peak {np.nanmax(pk):6.1f} dB")
    choice = []
    g0 = GAIN_LADDER[0]
    for i, s in enumerate(segs):
        best = g0
        for g in GAIN_LADDER[1:]:
            if g not in peak:
                continue
            dg = gain_total(g) - gain_total(g0)
            # linearity only meaningful where a real signal stands out at the lowest gain
            strong = peak[g0][i] - floor[g0][i] > 15.0
            linear = (not strong) or (peak[g][i] - peak[g0][i]) >= dg - 4.0
            if peak[g][i] < HEADROOM_DB and linear:
                best = g
            else:
                break
        choice.append(best)
    return [(float(s), float(min(s + SEG_MHZ, f_hi_mhz)), g) for s, g in zip(segs, choice)]


def group_ranges(plan, max_ranges=10):
    """Merge contiguous segments with the same gain -> {gain: [(lo,hi),...]} (<=10 ranges each)."""
    out = {}
    for lo, hi, g in plan:
        rs = out.setdefault(g, [])
        if rs and abs(rs[-1][1] - lo) < 1e-6:
            rs[-1] = (rs[-1][0], hi)
        else:
            rs.append((lo, hi))
    # hackrf_sweep accepts at most 10 ranges; split into several jobs if needed
    jobs = []
    for g, rs in out.items():
        for k in range(0, len(rs), max_ranges):
            jobs.append((g, rs[k:k + max_ranges]))
    return jobs


@dataclass
class Region:
    f_lo: float
    f_hi: float
    f_center: float
    bw: float
    peak_db: float
    median_db: float
    floor_db: float
    snr_db: float
    occupancy: float     # fraction of sweeps above floor+10 dB
    continuous: bool
    gain: tuple = field(default=(16, 20, 0))

    def to_dict(self):
        d = asdict(self)
        d["gain"] = list(self.gain)
        return d


def noise_floor(med, gain_db, df, win_hz=50e6, pct=30):
    """Rolling low percentile of the median spectrum, computed per gain group."""
    floor = np.full_like(med, np.nan)
    half = max(1, int(win_hz / df / 2))
    step = max(1, half // 8)
    for g in np.unique(gain_db[~np.isnan(gain_db)]):
        idx = np.where(gain_db == g)[0]
        vals = med[idx]
        n = len(idx)
        pts, fv = [], []
        for c in range(0, n, step):
            a, b = max(0, c - half), min(n, c + half)
            seg = vals[a:b]
            seg = seg[~np.isnan(seg)]
            if len(seg):
                pts.append(c)
                fv.append(np.percentile(seg, pct))
        if pts:
            floor[idx] = np.interp(np.arange(n), pts, fv)
    return floor


def detect_regions(acc, plan, cont_db=8.0, hit_db=14.0, min_hits=3, gap_bins=1):
    m = acc.matrix()
    f = acc.grid.freqs()
    df = acc.grid.df
    # linear mean over sweeps has far lower variance than a per-bin median
    with np.errstate(all="ignore"):
        med = 10 * np.log10(np.nanmean(10 ** (m / 10), axis=0))
    mx = np.nanmax(m, axis=0)
    floor = noise_floor(med, acc.gain_db, df)
    n_sw = np.sum(~np.isnan(m), axis=0).clip(min=1)
    hits = np.nansum(m > (floor + hit_db)[None, :], axis=0)
    occ = hits / n_sw
    cont = med > floor + cont_db
    inter = hits >= max(min_hits, 0.02 * n_sw.max())
    mask = (cont | inter) & ~np.isnan(floor)
    # close small gaps
    if gap_bins:
        idx = np.where(mask)[0]
        for a, b in zip(idx[:-1], idx[1:]):
            if 1 < b - a <= gap_bins + 1:
                mask[a:b] = True
    regions = []
    gain_at = {}
    for lo, hi, g in plan:
        gain_at[(lo, hi)] = g
    i = 0
    n = len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        sl = slice(i, j + 1)
        lin = 10 ** (mx[sl] / 10)
        fc = float(np.sum(f[sl] * lin) / np.sum(lin))
        fl = float(np.nanmedian(floor[sl]))
        g = next((gg for (lo, hi), gg in gain_at.items() if lo * 1e6 <= fc < hi * 1e6), (16, 20, 0))
        regions.append(Region(
            f_lo=float(f[i] - df / 2), f_hi=float(f[j] + df / 2), f_center=fc,
            bw=float((j - i + 1) * df), peak_db=float(np.nanmax(mx[sl])),
            median_db=float(np.nanmax(med[sl])), floor_db=fl,
            snr_db=float(np.nanmax(med[sl]) - fl), occupancy=float(np.nanmax(occ[sl])),
            continuous=bool(cont[sl].any()), gain=g))
        i = j + 1
    return regions, dict(freqs=f, median=med, max=mx, floor=floor, occupancy=occ)


def survey(f_lo_mhz=1, f_hi_mhz=6000, bin_w=100e3, duration=30.0, plan=None, log=print):
    """Full survey: gain calibration (unless plan given) + duration seconds of sweeping."""
    if plan is None:
        log(f"[survey] calibrating gain {f_lo_mhz}-{f_hi_mhz} MHz")
        plan = calibrate_gain(f_lo_mhz, f_hi_mhz, log=log)
    jobs = group_ranges(plan)
    acc = Accumulator(f_lo_mhz, f_hi_mhz, bin_w)
    t_end = time.time() + duration
    per_job = max(2.0, duration / max(1, len(jobs)) / 3)
    n = 0
    while time.time() < t_end:
        for g, ranges in jobs:
            for blocks, df in iter_sweeps(ranges, g, bin_w, duration=per_job):
                acc.add(blocks, df, g)
                n += 1
            if time.time() >= t_end:
                break
    log(f"[survey] {n} partial sweeps collected over {duration:.0f}s in {len(jobs)} gain groups")
    regions, spec = detect_regions(acc, plan)
    return regions, spec, plan
