"""Passive positioning from 5G NR (TDD, GPS phase-synchronised) SSB arrival times, plus an LTE
frame-timing check.

For every detected NR cell the SSB/PSS arrival times are measured with sub-sample precision
(nr.decode with ctx["timing"]), the SSB index (PBCH DMRS) gives the SSB position in the
half-frame (case C: symbols {2, 8} + 14 n), so each SSB implies a half-frame start time.  TS 38.133
requires TDD cells to be phase aligned within 3 us, so differences of these frame starts between
cells are TDOAs (+- the sync error).  The half-frame bit is in the PBCH payload (MIB not decoded),
so frame starts are known modulo 5 ms; that is harmless because synchronised cells differ by
microseconds only.

Usage:
    python -m rfscan.tdoa_nr runs/x/n78_3575.sigmf-meta [...] [--sites rfscan/data/nr_sites_willemstad.json]
    python -m rfscan.tdoa_nr --lte runs/x/lte_b8_950.sigmf-meta [...]      # LTE frame-offset check

Prints per capture: PCI, SSB frequency, power, SSB indices, frame-start offset (us) relative to the
strongest cell, its standard deviation over all SSB periods, and (with >= 3 positioned cells) the
TDOA fix as [{"src":"nrtdoa","lat":..,"lon":..,"acc":<m>,"n":<cells>,"note":".."}].

Each capture has its own receiver clock offset, so offsets are only compared within one capture
(the solver differences per capture group).  The receiver sample-clock error (~0.3-1 ppm for the
HackRF) is common to all cells of a capture and removed by fitting a drift line to the reference
cell.
"""
import argparse
import json
import os
import sys

import numpy as np

from . import recording, tdoa
from .decoders import lte, nr
from .decoders._ofdm_util import Channelizer, pmap

HF = nr.HALF_FRAME_S
DEFAULT_SITES = os.path.join(os.path.dirname(__file__), "data", "nr_sites_willemstad.json")


# ---------------------------------------------------------------- shared: offsets relative to a reference cell
def _unwrap_periods(t, period_s):
    """Integer period numbers for a series of (sorted) periodic event times, robust to clock-rate error."""
    t = np.asarray(t)
    if len(t) == 0:
        return np.array([], int)
    n = np.concatenate([[0], np.cumsum(np.round(np.diff(t) / period_s))]).astype(int)
    return n


def relative_offsets(cells, period_s=HF, skip_s=0.0):
    """cells: list of dicts with t_hf_s (frame/half-frame start per occurrence, s) and power_dbfs.

    The strongest cell is the reference.  Its frame starts define the receiver clock: a cell's frame
    start t is compared with the reference's frame-start series interpolated at t (so receiver
    sample-clock drift and wander, common to all cells of one capture, cancel), modulo period_s
    (wrapped to +-period/2).  Occurrences before skip_s (HackRF start-up transient) are ignored.
    Adds offset_us, offset_std_us (std over all periods = sync stability), offset_sem_us, n_used,
    rel_drift_ppb (frequency difference vs the reference) and drift_ppm_rx; returns the reference index."""
    if not cells:
        return None
    for c in cells:
        t = np.asarray(c["t_hf_s"], float)
        c["_t"] = np.sort(t[t >= skip_s]) if np.sum(t >= skip_s) >= 2 else np.sort(t)
    # strongest = highest PSS SNR (power_dbfs is the in-window power of the whole channel, data included)
    ref = int(np.argmax([c.get("snr", 0.0) for c in cells]))
    tr = cells[ref]["_t"]
    # one value per reference period (several SSB beams of one burst give the same frame start)
    nr_ = _unwrap_periods(tr, period_s)
    un = np.unique(nr_)
    trm = np.array([np.median(tr[nr_ == u]) for u in un])
    if len(un) >= 2:
        pf = np.polyfit(un * period_s, trm, 1)
        drift = (pf[0] - 1) * 1e6
    else:
        drift = 0.0
    for c in cells:
        t = c.pop("_t")
        if len(un) >= 2:
            u = np.interp(t, trm, un.astype(float), left=np.nan, right=np.nan)
            # extrapolate linearly outside the reference span
            out = np.isnan(u)
            if out.any():
                u[out] = np.polyval(np.polyfit(trm, un, 1), t[out])
            frac = u - np.round(u)
            d = frac * period_s * (1 + drift * 1e-6)
        else:
            d = t - trm[0]
        d = (d + period_s / 2) % period_s - period_s / 2
        med = np.median(d)
        mad = np.median(np.abs(d - med)) + 1e-12
        keep = np.abs(d - med) < max(5 * 1.4826 * mad, 0.2e-6)      # drop gross outliers
        dk = d[keep]
        c["offset_us"] = float(np.mean(dk) * 1e6)
        c["offset_std_us"] = float(np.std(dk) * 1e6)
        c["offset_sem_us"] = float(np.std(dk) / np.sqrt(max(len(dk), 1)) * 1e6)
        c["n_used"] = int(len(dk))
        tk = t[keep]
        if len(dk) >= 3 and np.ptp(tk) > 0:
            c["rel_drift_ppb"] = float(np.polyfit(tk, dk, 1)[0] * 1e9)
        c["drift_ppm_rx"] = float(drift)
    return ref


# ---------------------------------------------------------------- NR
def _resolve_issb_by_sync(cells, ref, fc_hz):
    """SSB occurrences whose index PBCH DMRS could not resolve (weak or co-timed cells): pick the SSB
    candidate whose implied half-frame start is closest to the reference cell's (modulo 5 ms).  This
    assumes the cells are synchronised (the candidates are >= 0.2 ms apart, sync errors are us), so
    such cells are flagged `issb_by_sync`."""
    offs = [nr.ssb_offset_s(i, 30e3, "C", fc_hz) for i in range(len(nr.ssb_start_symbols("C", fc_hz)))]
    r = cells[ref]
    tr = np.asarray(r["t_hf_s"])
    if not len(tr):
        return
    ph = np.angle(np.mean(np.exp(2j * np.pi * tr / HF))) / (2 * np.pi) * HF
    for i, c in enumerate(cells):
        if i == ref or all(c["_dmrs"]):
            continue
        t_hf, iss, n_sync = [], [], 0
        for t, dm, j in zip(c["_t_pss"], c["_dmrs"], c["_issb"]):
            if not dm:
                err = [abs(((t - o - ph + HF / 2) % HF) - HF / 2) for o in offs]
                j = int(np.argmin(err))
                n_sync += 1
            iss.append(j)
            t_hf.append(t - offs[j])
        c["t_hf_s"] = t_hf
        c["issb"] = sorted(set(iss))
        c["issb_by_sync"] = n_sync
        c["period_ms"] = nr._period_ms(c["_t_pss"], iss)
        c["index_ambiguous"] = False


def measure_nr(path, ctx=None):
    rec = recording.load(path)
    c = dict(timing=True, usable=0.9)
    c.update(ctx or {})
    res = nr.decode(rec, c)
    cells = []
    for r in res:
        f = r["fields"]
        tm = f.get("timing")
        if not tm:
            continue
        base = dict(kind="nr", pci=f["pci"], freq_hz=f["ss_ref_hz"], power_dbfs=f["pss_power_dbfs"],
                    sss_z=f["sss_z"], issb=sorted(set(tm["issb"])), issb_dmrs=sum(tm["issb_from_dmrs"]),
                    period_ms=tm["period_ms"], index_ambiguous=tm["index_ambiguous"], t_hf_s=tm["t_hf_s"],
                    snr=float(np.median(tm["snr"])) if tm["snr"] else 0.0, resid_std_us=tm.get("resid_std_s", 0) * 1e6,
                    _t_pss=tm["t_pss_s"], _dmrs=tm["issb_from_dmrs"], _issb=tm["issb"])
        cells.append(base)
        for sc in f.get("co_channel", []):
            cells.append(dict(base, pci=sc["pci"], power_dbfs=base["power_dbfs"] - 10, sss_z=sc["sss_z"],
                              t_hf_s=sc["t_hf_s"], co_channel_of=f["pci"], index_ambiguous=sc["index_ambiguous"],
                              snr=float(np.median(sc["snr"])) if sc["snr"] else 0.0,
                              _t_pss=sc["t_pss_s"], _dmrs=sc["issb_from_dmrs"], _issb=sc["issb"]))
    skip = float((ctx or {}).get("skip_s", 0.15))
    ref = relative_offsets(cells, HF, skip)
    if ref is not None:
        _resolve_issb_by_sync(cells, ref, rec.center_hz)
        ref = relative_offsets(cells, HF, skip)
    for c in cells:
        for k in ("_t_pss", "_dmrs", "_issb"):
            c.pop(k, None)
    return dict(path=path, center_hz=rec.center_hz, seconds=rec.n_samples() / rec.rate, cells=cells, ref=ref)


# ---------------------------------------------------------------- LTE (FDD) frame timing
LTE_PSS_USEFUL_S = (10 + 128 + 5 * 137 + 9) / lte.FS     # slot 0, symbol 6 (normal CP), from the frame start


def _lte_refine(y, nid2, p):
    """Sub-sample PSS time (samples): delay-profile peak over the 62 PSS subcarriers (DC excluded)."""
    t = lte._PSS_T[nid2] if hasattr(lte, "_PSS_T") else lte.pss_time(nid2)
    best, q0 = -1, p
    for d in range(-2, 3):
        q = p + d
        if q < 0 or q + lte.NFFT > len(y):
            continue
        v = abs(np.vdot(t, y[q:q + lte.NFFT]))
        if v > best:
            best, q0 = v, q
    Y = np.fft.fft(y[q0:q0 + lte.NFFT])
    H = Y[lte.sc_bins()] * np.conj(lte.pss_freq(nid2))
    ks = np.concatenate([np.arange(-31, 0), np.arange(1, 32)])
    snr = float(abs(np.sum(H)) ** 2 / 62 / (np.sum(np.abs(Y) ** 2) * 62 / 128 + 1e-20))
    return q0 + nr.fine_delay([H], ks, lte.NFFT), snr


def measure_lte(path, ctx=None):
    """LTE cell search with the whole capture folded modulo 5 ms (more sensitive than lte.decode's
    0.1 s window), then per-cell PSS timing and 10 ms frame starts (SSS gives subframe 0 vs 5)."""
    ctx = ctx or {}
    rec = recording.load(path)
    fs, fc = rec.rate, rec.center_hz
    x = rec.iq()
    ch = Channelizer(x, fs, (lte.FS,))
    half_bw = float(ctx.get("usable", 0.85)) * fs / 2 - 0.5e6
    raster = np.arange(np.ceil((fc - half_bw) / 100e3) * 100e3, np.floor((fc + half_bw) / 100e3) * 100e3 + 1, 100e3)
    span = float(ctx.get("ppm_max", 3.0)) * 1e-6 * fc
    cfos = np.arange(-np.ceil(span / 5e3), np.ceil(span / 5e3) + 1) * 5e3
    jobs = [(f - fc + c, f) for f in raster for c in cfos]
    dets = []
    for (off, f), r in zip(jobs, pmap(lambda j: lte._pss_search(ch, j[0]), jobs, int(ctx.get("threads", 24)))):
        for rho, nid2, i, foff, med in r:
            dets.append((rho, nid2, i, foff, f, 0.0, med))
    dets = lte._nms(dets)
    cells, seen = [], set()
    for rho, nid2, t0, foff, f_r, _, med in dets[:40]:
        y = ch.get(foff, lte.FS)
        occ = lte._occurrences(y, nid2, t0 % lte.HALF)
        if len(occ) < 4:
            continue
        dcfo = lte._cfo_fine(y, nid2, occ)
        y = ch.get(foff + dcfo, lte.FS)
        occ = lte._occurrences(y, nid2, occ[0])
        s = lte._sss(y, nid2, occ)
        if s is None or s[0] < lte.SSS_Z:
            continue
        z, mode, nid1, sf5_first, n_used = s
        if not mode.startswith("FDD"):
            continue
        pci = 3 * nid1 + nid2
        key = (pci, int(round(f_r / 1e5)))
        if key in seen:
            continue
        seen.add(key)
        tp, snr = [], []
        for p in occ:
            a, b = _lte_refine(y, nid2, p)
            tp.append(a / lte.FS)
            snr.append(b)
        tp = np.array(tp)
        hf = np.round((tp - tp[0]) / HF).astype(int)
        sf5 = (hf + (1 if sf5_first else 0)) % 2
        t_frame = tp - LTE_PSS_USEFUL_S - sf5 * HF
        pwr = float(np.mean([np.mean(np.abs(y[p:p + lte.NFFT]) ** 2) for p in occ]))
        cells.append(dict(kind="lte", pci=pci, freq_hz=float(f_r), measured_hz=float(fc + foff + dcfo),
                          power_dbfs=float(10 * np.log10(pwr + 1e-20)), sss_z=float(z), mode=mode,
                          t_hf_s=[float(v) for v in t_frame], snr=float(np.median(snr)), issb=[], period_ms=5,
                          pss_rho=float(rho)))
    # frame starts are known modulo 10 ms (SSS -> subframe 0 / 5)
    ref = relative_offsets(cells, 2 * HF, float(ctx.get("skip_s", 0.15)))
    return dict(path=path, center_hz=fc, seconds=rec.n_samples() / fs, cells=cells, ref=ref)


# ---------------------------------------------------------------- sites + fix
def load_sites(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        d = json.load(f)
    return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, dict) and "lat" in v}


def load_candidates(path):
    """Operator -> candidate mast list and the n78 band plan from the sites file ('_candidates')."""
    if not path or not os.path.exists(path):
        return {}, {}
    with open(path) as f:
        d = json.load(f)
    plan = {k: v for k, v in d.get("_band_plan_n78", {}).items() if not k.startswith("_")}
    return d.get("_candidates", {}), plan


def operator_of(freq_hz, plan):
    f = freq_hz / 1e6
    for op, (lo, hi) in plan.items():
        if lo <= f < hi:
            return op
    return None


def fix_by_assignment(captures, cands, plan, sigma_sync_us=0.5, max_combos=200000):
    """No PCI->site table: try every assignment of detected cells to candidate masts of the operator
    that owns the SSB frequency and keep the smallest-residual one.  Needs >= 4 cells (with 3 the fit is
    exact for any assignment).  Returns (paste list or None, assignment description)."""
    cells = []
    for gi, cap in enumerate(captures):
        for c in cap["cells"]:
            op = operator_of(c["freq_hz"], plan)
            if op and cands.get(op) and c.get("n_used", 0) >= 2:
                cells.append(dict(key=c["pci"], t=c["offset_us"] * 1e-6, group=gi,
                                  sigma=max(c.get("offset_sem_us", 0.05), 0.02) * 1e-6, cands=cands[op], op=op))
    if len(cells) < 4:
        return None, f"{len(cells)} cells with operator candidates (need >= 4 for assignment search)"
    import itertools
    best = None
    n = 0
    for combo in itertools.product(*[c["cands"] for c in cells]):
        n += 1
        if n > max_combos:
            break
        tx = [dict(lat=m["lat"], lon=m["lon"], h=m.get("h", 25.0), t=c["t"], sigma=c["sigma"], group=c["group"])
              for c, m in zip(cells, combo)]
        sol = tdoa.solve(tx, sigma_sync_s=sigma_sync_us * 1e-6, grid_step_m=1000.0)
        if sol and (best is None or sol["cost"] < best[0]["cost"]):
            best = (sol, combo)
    if best is None:
        return None, "no solution"
    sol, combo = best
    desc = ", ".join(f"PCI{c['key']}->{m['site']}" for c, m in zip(cells, combo))
    note = (f"NR TDOA {len(cells)} cells, PCI->site by min residual over {n} assignments ({desc}); LOW confidence")
    return [dict(src="nrtdoa", lat=round(sol["lat"], 6), lon=round(sol["lon"], 6), acc=round(sol["acc_m"]),
                 n=len(cells), note=note)], desc


def _site_for(sites, cell):
    f = cell["freq_hz"] / 1e6
    for k in (f"{cell['pci']}@{f:.2f}", f"{cell['pci']}@{f:.1f}", f"{cell['pci']}@{f:.0f}", str(cell["pci"])):
        if k in sites:
            s = sites[k]
            fr = s.get("freq_mhz")
            if fr is None or abs(float(fr) - f) < 1.0:
                return s
    return None


def fix(captures, sites, sigma_sync_us=0.5):
    """TDOA fix from all positioned cells (one clock group per capture). Returns the paste-able list."""
    tx, used = [], []
    for gi, cap in enumerate(captures):
        for c in cap["cells"]:
            s = _site_for(sites, c)
            if s is None or c.get("n_used", 0) < 2:
                continue
            tx.append(dict(lat=s["lat"], lon=s["lon"], h=s.get("h", 25.0), t=c["offset_us"] * 1e-6,
                           sigma=max(c.get("offset_sem_us", 0.05), 0.02) * 1e-6, group=gi))
            used.append((c["pci"], s.get("site", "?")))
    groups = {}
    for t in tx:
        groups[t["group"]] = groups.get(t["group"], 0) + 1
    if len(tx) < 3 or sum(n - 1 for n in groups.values() if n >= 2) < 2:
        return None, used
    sol = tdoa.solve(tx, sigma_sync_s=sigma_sync_us * 1e-6)
    if sol is None:
        return None, used
    note = (f"NR TDOA {len(tx)} cells ({', '.join(f'PCI{p}' for p, _ in used)}), sync sigma {sigma_sync_us} us, "
            f"rms resid {sol['rms_ns']:.0f} ns" + ("; AMBIGUOUS (mirror solution fits too)" if sol["ambiguous"] else "")
            + ("; exactly determined (3 cells): no residual check" if sol["dof"] == 0 else ""))
    out = [dict(src="nrtdoa", lat=round(sol["lat"], 6), lon=round(sol["lon"], 6), acc=round(sol["acc_m"]),
                n=len(tx), note=note)]
    return out, used


# ---------------------------------------------------------------- printing
def print_capture(cap, out=sys.stdout):
    print(f"== {cap['path']}  fc {cap['center_hz']/1e6:.3f} MHz  {cap['seconds']:.2f} s  cells {len(cap['cells'])}", file=out)
    if not cap["cells"]:
        print("   no cells detected", file=out)
        return
    print("   kind  PCI  freq MHz     pwr dBFS  PSS SNR  SSS z  SSB idx      per ms  n    offset us   std us  rel ppb", file=out)
    for i, c in enumerate(sorted(cap["cells"], key=lambda c: -c["power_dbfs"])):
        ref = c is cap["cells"][cap["ref"]]
        idx = ",".join(map(str, c.get("issb", []))) or "-"
        print(f"   {c['kind']:4s} {c['pci']:4d}  {c['freq_hz']/1e6:10.4f}  {c['power_dbfs']:7.1f}  {c.get('snr', 0):7.2f}  {c['sss_z']:5.1f}  "
              f"{idx:12s} {str(c.get('period_ms')):>6s}  {c.get('n_used', 0):3d}  {c['offset_us']:10.3f}  "
              f"{c['offset_std_us']:7.3f}  {c.get('rel_drift_ppb', 0):7.2f}{'  (ref)' if ref else ''}"
              f"{'  co-channel of ' + str(c['co_channel_of']) if c.get('co_channel_of') is not None else ''}"
              f"{'  [SSB index unresolved]' if c.get('index_ambiguous') else ''}"
              f"{'  [SSB index of ' + str(c['issb_by_sync']) + ' occ. from sync assumption]' if c.get('issb_by_sync') else ''}", file=out)
    print(f"   receiver clock drift (from ref cell): {cap['cells'][cap['ref']].get('drift_ppm_rx', 0):.3f} ppm", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m rfscan.tdoa_nr", description="NR SSB TDOA positioning / LTE frame-sync check")
    ap.add_argument("captures", nargs="+")
    ap.add_argument("--sites", default=DEFAULT_SITES, help="JSON {pci or 'pci@MHz': {lat, lon, site, operator, confidence}}")
    ap.add_argument("--lte", action="store_true", help="LTE frame-offset measurement instead of NR")
    ap.add_argument("--sync-us", type=float, default=0.5, help="1-sigma gNB phase-sync error (us)")
    ap.add_argument("--usable", type=float, default=None, help="usable fraction of the capture bandwidth")
    ap.add_argument("--json", action="store_true", help="also dump all measurements as JSON")
    ap.add_argument("--assign", action="store_true",
                    help="without a PCI->site table, search PCI->mast assignments (operator from SSB frequency)")
    a = ap.parse_args(argv)
    ctx = {} if a.usable is None else {"usable": a.usable}
    caps = []
    for p in a.captures:
        rec = recording.load(p)
        is_lte = a.lte or rec.center_hz < 3000e6
        cap = measure_lte(p, ctx) if is_lte else measure_nr(p, ctx)
        print_capture(cap)
        caps.append(cap)
    sites = load_sites(a.sites)
    nr_caps = [c for c in caps if any(x["kind"] == "nr" for x in c["cells"])]
    res, used = fix(nr_caps, sites, a.sync_us) if nr_caps else (None, [])
    if not res and nr_caps and a.assign:
        cands, plan = load_candidates(a.sites)
        res, desc = fix_by_assignment(nr_caps, cands, plan, a.sync_us)
        if not res:
            print(f"assignment search: {desc}")
    if res:
        print(json.dumps(res))
    else:
        npos = len(used)
        print(f"no TDOA fix: {npos} detected NR cell(s) have a site position in {a.sites} (need >= 3, "
              f"with >= 2 differences inside the same capture)")
    if a.json:
        print(json.dumps(caps, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
