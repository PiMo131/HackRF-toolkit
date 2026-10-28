"""rtl_433 wrapper: ISM sensors, remotes, TPMS, wM-Bus meters (~250 protocols).

Wide recordings are split into frequency clusters of bursty energy; every
cluster is mixed to DC, decimated, reduced to its active time segments
(+padding, short noise gaps between them) and fed to rtl_433 as cf32.
Narrow recordings (already channelised bursts) are fed whole.
"""
import json
import os
import re
import subprocess
from functools import lru_cache

import numpy as np

from . import _audio, result

NAME = "rtl433"
CAPTURE = {"rate": 2e6, "seconds": 30, "center": None}

RTL_433 = "rtl_433"
# disabled-by-default decoders worth enabling: wM-Bus C&T, S, T (dense in NL: water/heat meters)
EXTRA_PROTOCOLS = (104, 105, 238)
CRC_MICS = {"CRC", "CHECKSUM", "PARITY", "DIGEST"}
MAX_DIRECT_RATE = 1.3e6
GAP_S = 0.05            # noise gap inserted between segments


@lru_cache(maxsize=1)
def _protocol_args():
    """-R list = all default-enabled protocols + EXTRA_PROTOCOLS (-R disables the defaults)."""
    try:
        out = subprocess.run([RTL_433, "-R", "help"], capture_output=True, text=True, timeout=20)
        txt = out.stdout + out.stderr
    except Exception:
        return []
    ids = []
    for m in re.finditer(r"^\s*\[(\d+)\](\*?)", txt, re.M):
        n, disabled = int(m.group(1)), m.group(2) == "*"
        if not disabled or n in EXTRA_PROTOCOLS:
            ids.append(n)
    args = []
    for n in ids:
        args += ["-R", str(n)]
    return args


def _out_rate(f_abs, bw):
    # wM-Bus C/T (100 kchip/s) needs ~1.2 Msps; everything else is happy at 1 Msps
    if 868.7e6 <= f_abs <= 869.2e6 or bw > 300e3:
        return 1.2e6
    return 1.0e6


def run_rtl433(path, rate, freq, timeout=180):
    """Run rtl_433 on a cf32 file, return parsed JSON events."""
    cmd = [RTL_433, "-F", "json", "-M", "level", "-M", "protocol", "-M", "time:rel",
           *(_protocol_args()), "-s", str(int(rate)), "-f", str(int(freq)), "-r", f"cf32:{path}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        p = e
    out = []
    for line in (p.stdout or "").splitlines() if isinstance(p.stdout, str) else []:
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _time_s(ev):
    """rtl_433 file mode with -M time:off prints offsets like '@1.234567s'."""
    t = ev.get("time")
    if isinstance(t, str):
        m = re.search(r"([\d.]+)", t)
        if m:
            return float(m.group(1))
    return None


def _to_result(ev, f_abs, t_map=None):
    model = ev.get("model", "unknown")
    ident = ev.get("id", ev.get("channel", ev.get("code", "")))
    mic = str(ev.get("mic", "")).upper()
    crc = mic in CRC_MICS
    t_file = _time_s(ev)
    t = t_map(t_file) if (t_map and t_file is not None) else t_file
    skip = {"time", "model", "mic", "mod", "freq", "freq1", "freq2", "rssi", "snr", "noise", "protocol"}
    data = {k: v for k, v in ev.items() if k not in skip}
    summary = ", ".join(f"{k}={v}" for k, v in data.items() if k not in ("id",))[:120]
    f_rep = f_abs
    if isinstance(ev.get("freq"), (int, float)) and ev["freq"] > 1000:
        f_rep = float(ev["freq"]) * 1e6  # rtl_433 reports MHz, already absolute via -f
    return result(
        NAME, "crc" if crc else "detect", f_rep, f"{model}:{ident}",
        f"{model} id={ident} {summary}", crc_ok=True if crc else None, t_s=t,
        model=model, mic=mic or None, protocol=ev.get("protocol"), mod=ev.get("mod"),
        rssi=ev.get("rssi"), snr=ev.get("snr"), data=data)


def _write_cf32(path, x):
    np.asarray(x, np.complex64).tofile(path)


def decode(rec, ctx):
    tmp = ctx.get("tmpdir", "/tmp")
    os.makedirs(tmp, exist_ok=True)
    results = []
    base = os.path.join(tmp, f"rtl433_{os.getpid()}_{os.path.basename(rec.path)}")

    if rec.rate <= MAX_DIRECT_RATE:
        path = base + ".cf32"
        _write_cf32(path, rec.iq())
        for ev in run_rtl433(path, rec.rate, rec.center_hz):
            results.append(_to_result(ev, rec.center_hz))
        _rm(path)
        return _dedupe(results)

    # wide recording -> clusters of bursty energy
    P, f, blk = _audio.spectrogram(rec, nfft=512, block_s=0.005)
    clusters = _audio.active_clusters(P, f, thr_db=ctx.get("thr_db", 8.0), min_blocks=1)
    clusters = [c for c in clusters if c["peak_db"] >= ctx.get("min_peak_db", 10.0)][: ctx.get("max_clusters", 12)]
    for k, c in enumerate(clusters):
        f_abs = rec.center_hz + c["f_off"]
        orate = _out_rate(f_abs, c["bw"])
        segs = _audio.segments(c["blocks"], blk, pad_blocks=6, max_gap_blocks=20)
        if not segs:
            continue
        pieces, tmap = [], []
        gap = None
        pos = 0
        for a, b in segs:
            y = _audio.channelize(rec.iq(a, b - a), rec.rate, c["f_off"], orate,
                                  bw=max(min(c["bw"] * 2 + 100e3, 0.9 * orate), 200e3))
            if gap is None:
                # noise gap with matching level (rtl_433 needs quiet between packets)
                ns = int(GAP_S * orate)
                lvl = np.sqrt(np.mean(np.abs(y[: max(64, len(y) // 20)]) ** 2) + 1e-12)
                rng = np.random.default_rng(0)
                gap = ((rng.standard_normal(ns) + 1j * rng.standard_normal(ns)) * lvl / np.sqrt(2)).astype(np.complex64)
            tmap.append((pos / orate, a / rec.rate))
            pieces += [y, gap]
            pos += len(y) + len(gap)
        path = f"{base}_{k}_{f_abs/1e6:.4f}M.cf32"
        _write_cf32(path, np.concatenate(pieces))

        def t_map(tf, tmap=tmap):
            st = [p for p in tmap if p[0] <= tf]
            if not st:
                return tf
            p0, a0 = st[-1]
            return a0 + (tf - p0)

        for ev in run_rtl433(path, orate, f_abs):
            results.append(_to_result(ev, f_abs, t_map))
        _rm(path)
    return _dedupe(results)


def _dedupe(results):
    """rtl_433 often prints each repeat of a packet; keep one per (id, payload, ~time)."""
    out, seen = [], set()
    for r in results:
        key = (r["id"], json.dumps(r["fields"].get("data"), sort_keys=True, default=str),
               None if r["t_s"] is None else round(r["t_s"], 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass
