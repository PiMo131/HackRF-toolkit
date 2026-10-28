"""DAB / DAB+ (Band III) ensemble identification.

Per Band III block inside the recording span:
  1. channelise to 2.048 Msps (DAB mode I native rate)
  2. native detection: CP autocorrelation at Tu = 2048 samples (1 ms) + null-symbol
     dips every 96 ms  -> level="detect"
  3. if detected: write <name>.cf32.iq and run welle-cli on it (it loops the file in
     real time) -> ensemble id/label + services; FIC CRC-verified -> level="crc"
welle-cli needs a few seconds of signal for labels; CAPTURE asks for 6 s per block.
"""
import os
import re
import subprocess

import numpy as np

from . import _audio, result

NAME = "dab"
CAPTURE = {"rate": 2.048e6, "seconds": 6, "center": None}
FS_DAB = 2.048e6
TU, TS, TNULL, TFRAME = 2048, 2552, 2656, 196608
WELLE = "welle-cli"

BLOCKS = {"5A": 174.928, "5B": 176.640, "5C": 178.352, "5D": 180.064, "6A": 181.936, "6B": 183.648,
          "6C": 185.360, "6D": 187.072, "7A": 188.928, "7B": 190.640, "7C": 192.352, "7D": 194.064,
          "8A": 195.936, "8B": 197.648, "8C": 199.360, "8D": 201.072, "9A": 202.928, "9B": 204.640,
          "9C": 206.352, "9D": 208.064, "10A": 209.936, "10N": 210.096, "10B": 211.648, "10C": 213.360,
          "10D": 215.072, "11A": 216.928, "11B": 218.640, "11C": 220.352, "11D": 222.064, "12A": 223.936,
          "12B": 225.648, "12C": 227.360, "12D": 229.072, "13A": 230.784, "13B": 232.496, "13C": 234.208,
          "13D": 235.776, "13E": 237.488, "13F": 239.200}


def _channel(rec, f_off, max_s=8.0):
    count = int(min(rec.n_samples(), max_s * rec.rate))
    if abs(rec.rate - FS_DAB) < 1 and abs(f_off) < 1:
        return rec.iq(0, count)
    x, r1 = _audio.mix_decimate(rec, f_off, min_rate=2 * FS_DAB, count=count)
    return _audio.channelize(x, r1, 0.0, FS_DAB, bw=1.6e6)


def ofdm_metrics(y):
    """CP autocorrelation at Tu (vs off-lags) and null-symbol periodicity."""
    y = y - np.mean(y)
    p = np.mean(np.abs(y) ** 2) + 1e-20

    def rho(lag):
        return float(np.abs(np.mean(y[lag:] * np.conj(y[:-lag]))) / p)

    r_tu = rho(TU)
    r_off = max(rho(TU - 150), rho(TU + 150), 1e-6)
    # null symbol: 1.3 ms power dip repeating every 96 ms
    k = TNULL
    e = np.convolve(np.abs(y) ** 2, np.ones(k) / k, mode="valid")[::64]
    null_hits = 0
    n_frames = len(y) // TFRAME
    if n_frames >= 1:
        med = np.median(e)
        dips = e < 0.35 * med
        if dips.any():
            idx = np.where(dips)[0] * 64
            # fold dip positions modulo the frame length: real DAB piles up in one phase
            ph = np.round((idx % TFRAME) / 512).astype(int)
            # count distinct frames that show a dip at the dominant phase
            dom = np.bincount(ph).argmax()
            null_hits = len({i // TFRAME for i, q in zip(idx, ph) if abs(q - dom) <= 1})
    return {"cp_rho": r_tu, "cp_ratio": r_tu / r_off, "null_frames": null_hits, "frames": n_frames}


def coherence_screen(rec, max_s=0.5, nfft=1024):
    """Per-bin coherence between x[n] and x[n+1 ms] on the wide recording.
    DAB (Tu = 1 ms) shows ~0.1-0.2 across its 1.536 MHz; noise/other signals ~0."""
    fs = rec.rate
    L = int(round(1e-3 * fs))
    n = int(min(rec.n_samples(), max_s * fs))
    x = rec.iq(0, n)
    x = x - np.mean(x)
    m = (n - L) // nfft
    if m < 8:
        return None, None
    a = x[: m * nfft].reshape(m, nfft)
    b = x[L: L + m * nfft].reshape(m, nfft)
    A, B = np.fft.fft(a, axis=1), np.fft.fft(b, axis=1)
    c = np.abs(np.sum(A * np.conj(B), axis=0)) / np.sqrt(np.sum(np.abs(A) ** 2, 0) * np.sum(np.abs(B) ** 2, 0))
    f = np.fft.fftfreq(nfft, 1 / fs)
    return np.fft.fftshift(c), np.fft.fftshift(f)


def _detected(m):
    cp = m["cp_rho"] > 0.03 and m["cp_ratio"] > 4
    null = m["frames"] >= 2 and m["null_frames"] >= max(2, int(0.6 * m["frames"]))
    return cp or null, cp, null


_RE_EID = re.compile(r"Ensemble (?:name id|id is):\s*(?:0x)?([0-9a-fA-F]+)")
_RE_ELABEL = re.compile(r"Ensemble label:\s*(.*)")
_RE_SVC = re.compile(r"New Service:\s*0x([0-9a-fA-F]+)\s*(.*)")
_RE_LIST = re.compile(r"^\s*\[?0x([0-9a-fA-F]{4,8})\]?\s+(.+)$")


def run_welle(path, timeout=20):
    try:
        p = subprocess.run([WELLE, "-f", path, "-T"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
    txt = out.decode("utf-8", "replace")
    info = {"eid": None, "label": None, "services": {}, "crc_errors": txt.count("FIB CRC"),
            "synced": "Wait for service list" in txt or "Ensemble" in txt}
    in_list = False
    for line in txt.splitlines():
        if m := _RE_EID.search(line):
            info["eid"] = m.group(1).upper()
        elif m := _RE_ELABEL.search(line):
            info["label"] = m.group(1).strip()
        elif m := _RE_SVC.search(line):
            info["services"].setdefault(m.group(1).upper(), m.group(2).strip())
        elif line.startswith("Service list"):
            in_list = True
        elif in_list and (m := _RE_LIST.match(line)):
            info["services"][m.group(1).upper()] = m.group(2).strip()
    return info, txt


def decode(rec, ctx):
    tmp = ctx.get("tmpdir", "/tmp")
    os.makedirs(tmp, exist_ok=True)
    half = 0.5 * rec.rate - 0.8e6
    out = []
    coh, cf = coherence_screen(rec) if rec.rate > 2.5e6 else (None, None)
    floor = float(np.median(coh)) if coh is not None else 0.0
    for blk, fmhz in BLOCKS.items():
        f_off = fmhz * 1e6 - rec.center_hz
        if abs(f_off) > max(half, 1.0):
            continue
        if coh is not None:
            sel = np.abs(cf - f_off) < 0.65e6
            if not sel.any() or float(np.mean(coh[sel])) < max(0.04, 3 * floor):
                continue
        y = _channel(rec, f_off)
        if len(y) < 4 * TS:
            continue
        m = ofdm_metrics(y)
        ok, cp, null = _detected(m)
        if not ok:
            continue
        f_abs = fmhz * 1e6
        det = result(NAME, "detect", f_abs, f"DAB:{blk}", f"DAB OFDM on block {blk} ({fmhz:.3f} MHz)",
                     block=blk, **m, cp_detect=cp, null_detect=null)
        if ctx.get("run_welle", True) and shutil_which(WELLE):
            path = os.path.join(tmp, f"dab_{os.getpid()}_{blk}.cf32.iq")
            yy = (y / (np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-12) * 0.1).astype(np.complex64)
            yy.tofile(path)
            dur = len(y) / FS_DAB
            info, _txt = run_welle(path, timeout=ctx.get("welle_timeout", max(12, 3 * dur)))
            try:
                os.remove(path)
            except OSError:
                pass
            if info["eid"] or info["label"] or info["services"]:
                lab = info["label"] or "?"
                svcs = ", ".join(v or k for k, v in info["services"].items())
                out.append(result(NAME, "crc", f_abs, f"DAB:{info['eid'] or blk}",
                                  f"DAB {blk} ensemble {lab} (EId {info['eid']}) services: {svcs}"[:200],
                                  crc_ok=True, block=blk, ensemble_id=info["eid"], ensemble_label=info["label"],
                                  services=info["services"], fib_crc_errors=info["crc_errors"], **m))
                continue
            det["fields"]["welle_synced"] = info["synced"]
        out.append(det)
    return out


def shutil_which(x):
    import shutil
    return shutil.which(x)
