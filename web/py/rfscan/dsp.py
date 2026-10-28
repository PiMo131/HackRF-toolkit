"""Signal-level DSP: PSD/carrier finding inside a capture, time-frequency burst
detection, channel extraction, feature extraction and a heuristic classifier.
"""
from dataclasses import dataclass, asdict

import numpy as np
from scipy import ndimage, signal


# ---------------------------------------------------------------- spectrum
def remove_dc(iq, block=None):
    """Subtract the running DC (HackRF LO leakage) in blocks; in place-safe copy."""
    block = block or 65536
    x = np.array(iq, dtype=np.complex64, copy=True)
    for a in range(0, len(x), block):
        x[a:a + block] -= x[a:a + block].mean()
    return x


def psd(iq, rate, nfft=4096, max_frames=2000, maxhold=False):
    """Averaged periodogram (Hann), fftshifted.  Returns (freq_offsets_hz, power_db)
    or (f, mean_db, max_db) with maxhold=True."""
    n = len(iq) // nfft
    if n == 0:
        nfft = 1 << int(np.log2(max(16, len(iq))))
        n = 1
    step = max(1, n // max_frames)
    x = iq[: n * nfft].reshape(n, nfft)[::step]
    w = np.hanning(nfft).astype(np.float32)
    a = np.abs(np.fft.fft(x * w, axis=1)) ** 2
    norm = nfft * np.sum(w ** 2)
    p = np.fft.fftshift(np.mean(a, axis=0)) / norm
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rate))
    if maxhold:
        m = np.fft.fftshift(np.max(a, axis=0)) / norm
        return f, 10 * np.log10(p + 1e-20), 10 * np.log10(m + 1e-20)
    return f, 10 * np.log10(p + 1e-20)


def _regions(mask, f, p_db, floor, close_hz=0.0):
    df = f[1] - f[0]
    w = max(3, int(close_hz / df) | 1)
    lab, k = ndimage.label(ndimage.binary_closing(mask, structure=np.ones(w)))
    out = []
    for i in range(1, k + 1):
        idx = np.where(lab == i)[0]
        lin = 10 ** (p_db[idx] / 10)
        out.append({
            "f_off": float(np.sum(f[idx] * lin) / np.sum(lin)),
            "f_lo": float(f[idx[0]] - df / 2), "f_hi": float(f[idx[-1]] + df / 2),
            "bw": float(len(idx) * df), "peak_db": float(p_db[idx].max()),
            "snr_db": float(p_db[idx].max() - floor), "nbins": len(idx),
        })
    return out


def find_carriers(f, p_db, rate, p_max=None, thr_db=8.0, max_thr_db=10.0, edge_frac=0.08, min_bins=2):
    """Spectral regions above the in-capture noise floor, from the mean PSD
    (continuous signals) and, if given, the max-hold PSD (bursty signals).
    Skips band edges (anti-alias roll-off) and the DC bin.  Returns (list, floor_db)."""
    n = len(p_db)
    e = int(edge_frac * n)
    valid = np.ones(n, bool)
    valid[:e] = valid[n - e:] = False
    valid[n // 2 - 3:n // 2 + 4] = False
    floor = float(np.percentile(p_db[valid], 10))
    close = 150e3 if rate >= 10e6 else 0.0      # wideband captures: join OFDM resource-block fragments
    cont = _regions((p_db > floor + thr_db) & valid, f, p_db, floor, close)
    for c in cont:
        c["kind"] = "continuous"
    out = [c for c in cont if c["nbins"] >= min_bins]
    if p_max is not None:
        mfloor = float(np.percentile(p_max[valid], 10))
        cmask = np.zeros(n, bool)
        for c in out:
            cmask |= (f >= c["f_lo"]) & (f <= c["f_hi"])
        for c in _regions((p_max > mfloor + max_thr_db) & valid & ~cmask, f, p_max, mfloor):
            if c["nbins"] >= min_bins:
                c["kind"] = "bursty"
                out.append(c)
    return out, floor


# ------------------------------------------------------------------ bursts
@dataclass
class Burst:
    t0: float          # s from capture start
    t1: float
    f_lo: float        # Hz offset from capture centre
    f_hi: float
    f_peak: float
    snr_db: float

    @property
    def dur(self):
        return self.t1 - self.t0

    @property
    def bw(self):
        return self.f_hi - self.f_lo

    def to_dict(self):
        return asdict(self)


def detect_bursts(iq, rate, nfft=256, chunk_s=0.5, thr_db=10.0, min_dur=0.4e-3,
                  max_frac=0.9, edge_frac=0.06):
    """Energy bursts in the time-frequency plane (STFT + connected components).
    Components present in > max_frac of the chunk are treated as continuous carriers
    and not returned as bursts (they are handled by find_carriers)."""
    hop = nfft
    df = rate / nfft
    dt = hop / rate
    chunk = int(chunk_s * rate) // nfft * nfft
    w = np.hanning(nfft).astype(np.float32)
    e = int(edge_frac * nfft)
    bursts = []
    for c0 in range(0, len(iq) - nfft, chunk):
        x = iq[c0:c0 + chunk]
        m = len(x) // nfft
        if m < 8:
            break
        s = np.fft.fftshift(np.fft.fft(x[: m * nfft].reshape(m, nfft) * w, axis=1), axes=1)
        p = 10 * np.log10(np.abs(s) ** 2 + 1e-20).astype(np.float32)
        floor = np.median(p, axis=0)                       # per-bin floor over time
        floor = np.minimum(floor, np.percentile(floor, 30) + 3)  # continuous carriers don't raise it
        mask = p > (floor + thr_db)[None, :]
        mask[:, :e] = mask[:, nfft - e:] = False
        mask[:, nfft // 2 - 1:nfft // 2 + 2] = False         # DC
        mask = ndimage.binary_opening(mask, structure=np.ones((1, 2)))
        mask = ndimage.binary_closing(mask, structure=np.ones((5, 3)))
        lab, k = ndimage.label(mask)
        if k == 0:
            continue
        objs = ndimage.find_objects(lab)
        for i, sl in enumerate(objs, 1):
            ts, fs = sl
            n_t = ts.stop - ts.start
            if n_t * dt < min_dur or n_t > max_frac * m:
                continue
            sub = np.where(lab[sl] == i, p[sl], -np.inf)
            fi = np.unravel_index(np.argmax(sub), sub.shape)[1] + fs.start
            snr = float(np.max(sub) - floor[fi])
            bursts.append(Burst(
                t0=(c0 / rate) + ts.start * dt, t1=(c0 / rate) + ts.stop * dt,
                f_lo=(fs.start - nfft / 2) * df, f_hi=(fs.stop - nfft / 2) * df,
                f_peak=(fi - nfft / 2) * df, snr_db=snr))
    return merge_bursts(bursts)


def merge_bursts(bursts, gap_s=2e-3, f_tol=30e3):
    """Join fragments of the same transmission (split by chunking or fading)."""
    bursts = sorted(bursts, key=lambda b: (b.t0, b.f_lo))
    out = []
    for b in bursts:
        for o in reversed(out[-20:]):
            if b.t0 - o.t1 <= gap_s and b.f_lo <= o.f_hi + f_tol and b.f_hi >= o.f_lo - f_tol:
                o.t1 = max(o.t1, b.t1)
                o.f_lo, o.f_hi = min(o.f_lo, b.f_lo), max(o.f_hi, b.f_hi)
                if b.snr_db > o.snr_db:
                    o.snr_db, o.f_peak = b.snr_db, b.f_peak
                break
        else:
            out.append(Burst(**asdict(b)))
    return out


def cluster_bursts_by_freq(bursts, tol=50e3):
    """Group bursts into frequency channels -> list of (f_center, [bursts])."""
    groups = []
    for b in sorted(bursts, key=lambda b: (b.f_lo + b.f_hi) / 2):
        fc = (b.f_lo + b.f_hi) / 2
        if groups and abs(fc - groups[-1][0]) < tol + groups[-1][2] / 2:
            g = groups[-1]
            g[1].append(b)
            fcs = [(x.f_lo + x.f_hi) / 2 for x in g[1]]
            g[0] = float(np.median(fcs))
            g[2] = max(g[2], b.bw)
        else:
            groups.append([fc, [b], b.bw])
    return [(g[0], g[1]) for g in groups]


# ------------------------------------------------------- channel extraction
def extract(iq, rate, f_off, bw, t0=0.0, t1=None, min_rate=48e3, os=4.0):
    """Mix f_off to DC, low-pass to bw, decimate to >= max(os*bw/2*2, min_rate).
    Returns (baseband, new_rate)."""
    a = max(0, int(t0 * rate))
    b = len(iq) if t1 is None else min(len(iq), int(t1 * rate))
    x = iq[a:b]
    if len(x) == 0:
        return x, rate
    target = max(min_rate, os * bw)
    dec = max(1, int(rate // target))
    x = remove_dc(x)
    n = np.arange(a, a + len(x))
    x = x * np.exp(-2j * np.pi * f_off / rate * n).astype(np.complex64)
    if dec > 1:
        # staged polyphase decimation keeps filters short
        while dec > 1:
            d = next(k for k in (5, 4, 3, 2) if dec % k == 0) if any(dec % k == 0 for k in (5, 4, 3, 2)) else dec
            x = signal.resample_poly(x, 1, d, window=("kaiser", 8.0))
            rate /= d
            dec //= d
    cutoff = min(0.95, bw / rate)
    if cutoff < 0.9:
        taps = signal.firwin(129, cutoff, window=("kaiser", 8.0))
        x = signal.fftconvolve(x, taps, mode="same")
    return x.astype(np.complex64), rate


# ---------------------------------------------------------------- features
OFDM_CANDIDATES = {
    # name: (Tu seconds, nominal CP fraction)
    "LTE/NR-15k": (66.667e-6, 0.07),
    "NR-30k": (33.333e-6, 0.07),
    "WiFi": (3.2e-6, 0.25),
    "DAB": (1.0e-3, 0.246),
    "DVB-T 2k": (224e-6, 0.125),
    "DVB-T/T2 8k": (896e-6, 0.125),
    "DVB-T2 32k": (3584e-6, 0.0078),
}

TDMA_PERIODS = {"GSM frame 4.615ms": 4.615e-3, "DECT 10ms": 10e-3, "TETRA 14.17ms": 14.167e-3,
                "LTE-TDD/NR 5ms": 5e-3}


OFDM_MIN_BW = {"LTE/NR-15k": 1.0e6, "NR-30k": 4e6, "WiFi": 15e6, "DAB": 1.3e6,
               "DVB-T 2k": 6e6, "DVB-T/T2 8k": 6e6, "DVB-T2 32k": 6e6}


def ofdm_cp_scores(x, rate, bw=None, max_n=400_000):
    """Normalised CP autocorrelation |R(Tu)| vs. surrounding lags, per candidate."""
    # gate on active blocks so idle time between bursts (Wi-Fi, TDD) doesn't dilute
    blk = 4096
    nb = len(x) // blk
    if nb >= 8:
        bp = np.mean(np.abs(x[: nb * blk].reshape(nb, blk)) ** 2, axis=1)
        act = bp > 2 * np.median(bp)
        if 0 < act.sum() < 0.7 * nb:
            x = x[: nb * blk].reshape(nb, blk)[act].ravel()
    x = x[:max_n]
    out = {}
    for name, (tu, _) in OFDM_CANDIDATES.items():
        if bw is not None and bw < OFDM_MIN_BW[name]:
            continue
        # resample so that Tu is an exact integer number of samples; a fractional
        # lag decorrelates the cyclic prefix across the band
        L = int(2 ** np.ceil(np.log2(max(8, tu * rate))))
        new_rate = L / tu
        n_out = int(len(x) * new_rate / rate)
        if n_out < 4 * L or n_out > 3_000_000:
            continue
        y = signal.resample(x, n_out) if abs(new_rate - rate) > 1 else x
        p = np.mean(np.abs(y) ** 2) + 1e-20

        def r(lag):
            return np.abs(np.mean(y[lag:] * np.conj(y[:-lag]))) / p
        rc = r(L)
        ref = np.median([r(int(L * k)) for k in (0.83, 0.91, 1.09, 1.17)])
        out[name] = float(rc / (ref + 1e-9)), float(rc)
    return out


def features(x, rate, bw=None):
    """Feature dict for a channelised signal (baseband, roughly its own bandwidth)."""
    if len(x) < 256:
        return {}
    env = np.abs(x)
    p = env ** 2
    mean_p = float(np.mean(p)) + 1e-20
    papr = float(10 * np.log10(np.max(p) / mean_p))
    env_cv = float(np.std(env) / (np.mean(env) + 1e-12))
    # duty cycle in 0.5 ms blocks
    blk = max(1, int(rate * 0.5e-3))
    nb = len(p) // blk
    bp = p[: nb * blk].reshape(nb, blk).mean(axis=1) if nb else p
    bfloor = np.percentile(bp, 5) + 1e-20
    b95 = np.percentile(bp, 95)
    if b95 < 4 * bfloor:             # < 6 dB envelope dynamics -> continuous
        duty = 1.0
    else:
        duty = float(np.mean(bp > np.sqrt(bfloor * b95)))
    # occupied bandwidth 99%
    f, pd = psd(x, rate, nfft=min(2048, 1 << int(np.log2(len(x)))))
    lin = 10 ** (pd / 10)
    lin_n = lin - np.percentile(lin, 10)
    lin_n[lin_n < 0] = 0
    c = np.cumsum(lin_n) / (np.sum(lin_n) + 1e-20)
    obw = float(f[np.searchsorted(c, 0.995)] - f[np.searchsorted(c, 0.005)]) if c[-1] > 0 else 0.0
    # spectral flatness within OBW (OFDM/noise-like ~1, FSK/CW peaky)
    sel = (f >= f[np.searchsorted(c, 0.05)]) & (f <= f[min(len(f) - 1, np.searchsorted(c, 0.95))])
    v = lin[sel] if sel.sum() > 4 else lin
    flat = float(np.exp(np.mean(np.log(v + 1e-20))) / (np.mean(v) + 1e-20))
    # instantaneous frequency spread (constant envelope FM/FSK -> narrow env, wide inst freq)
    xa = x[p > 2 * bfloor] if np.sum(p > 2 * bfloor) > 256 else x
    ifreq = np.angle(xa[1:] * np.conj(xa[:-1])) * rate / (2 * np.pi)
    hist, edges = np.histogram(ifreq, bins=64, range=(-obw - 1, obw + 1) if obw > 0 else None)
    peaks, _ = signal.find_peaks(np.r_[0, hist, 0], prominence=0.15 * hist.max() if hist.max() else 1)
    # TDMA periodicity from the block envelope
    tdma = {}
    if nb > 64:
        e0 = bp - bp.mean()
        ac = np.fft.irfft(np.abs(np.fft.rfft(e0, 2 * nb)) ** 2)[:nb]
        ac /= ac[0] + 1e-20
        for name, T in TDMA_PERIODS.items():
            k = int(round(T / (blk / rate)))
            if 2 <= k < nb // 3:
                tdma[name] = float(ac[k])
    return {
        "papr_db": papr, "env_cv": env_cv, "duty": duty, "obw_hz": obw, "flatness": flat,
        "ifreq_std_hz": float(np.std(ifreq)), "fsk_levels": int(len(peaks)),
        "ofdm": ofdm_cp_scores(x, rate, bw or obw), "tdma": tdma,
        "power_db": float(10 * np.log10(mean_p)),
    }


def classify(feat, band=None, bw=None):
    """Heuristic label from features + band plan.  Returns (label, confidence 0..1, hints[])."""
    if not feat:
        return "unknown", 0.0, []
    hints = []
    bw = bw or feat.get("obw_hz", 0)
    best_ofdm = max(feat.get("ofdm", {}).items(), key=lambda kv: kv[1][0], default=None)
    is_ofdm = best_ofdm and best_ofdm[1][0] > 3.0 and best_ofdm[1][1] > 0.02
    cat = band.category if band else "unknown"
    svc = band.service if band else ""
    tdma = feat.get("tdma", {})
    t_best = max(tdma.items(), key=lambda kv: kv[1], default=(None, 0))
    if is_ofdm:
        hints.append(f"OFDM ({best_ofdm[0]}, CP score {best_ofdm[1][0]:.1f})")
    if t_best[1] > 0.35:
        hints.append(f"TDMA periodicity {t_best[0]} (r={t_best[1]:.2f})")
    if feat["duty"] < 0.6:
        hints.append(f"bursty (duty {feat['duty']:.0%})")
    if feat["env_cv"] < 0.25:
        hints.append("constant envelope (FM/FSK/GMSK/CW)")
    if feat["fsk_levels"] >= 2 and feat["env_cv"] < 0.35:
        hints.append(f"{feat['fsk_levels']}-level FSK-like")

    label, conf = "unknown", 0.3
    name = best_ofdm[0] if is_ofdm else ""
    if is_ofdm and name.startswith("LTE") and cat == "cellular":
        label, conf = "LTE downlink (OFDM 15 kHz)", 0.8
    elif is_ofdm and name == "NR-30k":
        label, conf = "5G NR (OFDM 30 kHz)", 0.8
    elif is_ofdm and name == "WiFi":
        label, conf = "Wi-Fi OFDM (802.11a/g/n/ac/ax)", 0.8
    elif is_ofdm and name == "DAB":
        label, conf = "DAB/DAB+ ensemble", 0.85
    elif is_ofdm and name.startswith("DVB"):
        label, conf = f"DVB-T/T2 ({name})", 0.8
    elif is_ofdm:
        label, conf = f"OFDM ({name})", 0.6
    elif t_best[0] == "GSM frame 4.615ms" and t_best[1] > 0.35 and bw < 400e3:
        label, conf = "GSM carrier (TDMA)", 0.7
    elif t_best[0] == "DECT 10ms" and t_best[1] > 0.35 and cat == "dect":
        label, conf = "DECT base/handset (TDMA 10 ms)", 0.75
    elif cat == "tetra" and bw < 40e3:
        label, conf = "TETRA carrier (25 kHz pi/4-DQPSK)", 0.6
    elif svc == "FM broadcast" and bw > 80e3 and feat["env_cv"] < 0.3:
        label, conf = "FM broadcast", 0.8
    elif bw < 3e3 and feat["env_cv"] < 0.2:
        label, conf = "CW carrier / beacon (or receiver spur)", 0.5
    elif feat["duty"] < 0.6 and feat["fsk_levels"] >= 2 and feat["env_cv"] < 0.4:
        label, conf = "FSK burst transmitter", 0.55
    elif cat in ("cellular", "cellular_ul") and feat["env_cv"] > 0.6:
        label, conf = f"cellular {'uplink ' if cat == 'cellular_ul' else ''}activity (scheduled resource blocks)", 0.45
    elif feat["duty"] < 0.6 and feat["env_cv"] > 0.6 and bw < 500e3:
        label, conf = "OOK/ASK burst transmitter", 0.5
    elif feat["env_cv"] < 0.25 and bw < 30e3:
        label, conf = "narrowband FM / FSK (voice or data)", 0.5
    elif feat["flatness"] > 0.6 and bw > 1e6:
        label, conf = "wideband noise-like digital signal", 0.4
    if label == "unknown" and svc:
        label = f"unidentified signal in '{svc}'"
    return label, conf, hints
