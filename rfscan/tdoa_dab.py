"""Passive receiver positioning from DAB single-frequency networks (TII + TDOA).

Every transmitter of a DAB SFN sends the same OFDM signal, GPS-locked, and marks its
null symbol with a Transmitter Identification Information code (EN 300 401 §14.8):
in mode I, 4 of 8 carrier-pair groups (pattern = main id 0..69) at comb offset
2*sub (sub id 0..23), repeated in the 4 quarters of the 1536-carrier spectrum.

Per ensemble this module
  1. channelises the block to exactly 2.048 Msps (one shared FFT overlap-save pass for all
     blocks), corrects CFO (CP correlation + integer search on the PRS) and times every
     frame on its own PRS correlation peak (the HackRF captures drop samples, so no
     straight-line frame clock is assumed);
  2. aligns all frames to sub-sample precision (power-delay-profile matching), puts the
     strongest composite path at window sample REF_POS with phase 0, so null symbols
     can be averaged coherently: Z(k) = mean_f Y_null,f(k) * conj(P(k_first_of_pair));
  3. detects TII codes by successive coherent detection over all 24 x 70 codes (groups
     already owned by a stronger code are excluded); TII is carried on alternate frames,
     the right parity is selected automatically (+3 dB);
  4. estimates each transmitter's delay from the oversampled coherent sparse impulse
     response of its 32 carriers. The layout gives strong lobes every 2048/384 = 5.33 and
     2048/48 = 42.7 samples (6.25 km); the lobe is chosen with the composite PRS impulse
     response (full 1.5 MHz resolution), alternatives are kept for the solver;
     optionally weak transmitters are tested only at unexplained composite paths
     against the codes known for the block ("tentative", database prior);
  5. matches codes to sites (rfscan/data/dab_tx.json) and solves hyperbolic TDOA
     (Gauss-Newton in a WGS84 local ENU frame around the transmitters' centroid, one clock
     offset per ensemble = differencing), trying the lobe alternatives.

welle-cli was not used: its TII decoder (welle.io tii-decoder.cpp) gives integer-sample
delays only and it cannot even decode the FIC at the -5..-8 dB channel SNR of the captures.
Delays are relative to the strongest path; known static SFN transmitter offsets can be put
in the database as offset_us (they are NOT zero in practice, see docs/notes-dab-tii.md).

CLI:  python -m rfscan.tdoa_dab runs/x/dab_224.sigmf-meta [--blocks 12C,11C] [--json-only]
Output ends with a JSON list [{"src":"dabtii","lat":..,"lon":..,"acc":..,"n":..,"note":..}].
"""
import argparse
import itertools
import json
import math
import os
import sys

import numpy as np

FS = 2.048e6
TU, TG, TNULL, TS, TFRAME = 2048, 504, 2656, 2552, 196608
C = 299792458.0
M_PER_SAMPLE = C / FS                      # 146.4 m
REF_POS = 150                              # strongest composite path placed here in the window
SEARCH = (0, 620)                          # window positions (samples) searched for TII delays
OS = 8                                     # delay-profile oversampling
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "dab_tx.json")

# ---------------------------------------------------------------- tables (EN 300 401)
_PRS_TABLE = [(-768, 0, 1), (-736, 1, 2), (-704, 2, 0), (-672, 3, 1), (-640, 0, 3), (-608, 1, 2),
              (-576, 2, 2), (-544, 3, 3), (-512, 0, 2), (-480, 1, 1), (-448, 2, 2), (-416, 3, 3),
              (-384, 0, 1), (-352, 1, 2), (-320, 2, 3), (-288, 3, 3), (-256, 0, 2), (-224, 1, 2),
              (-192, 2, 2), (-160, 3, 1), (-128, 0, 1), (-96, 1, 3), (-64, 2, 1), (-32, 3, 2),
              (1, 0, 3), (33, 3, 1), (65, 2, 1), (97, 1, 1), (129, 0, 2), (161, 3, 2), (193, 2, 1),
              (225, 1, 0), (257, 0, 2), (289, 3, 2), (321, 2, 3), (353, 1, 3), (385, 0, 0),
              (417, 3, 2), (449, 2, 1), (481, 1, 3), (513, 0, 3), (545, 3, 3), (577, 2, 3),
              (609, 1, 0), (641, 0, 3), (673, 3, 0), (705, 2, 1), (737, 1, 1)]
_H = [[0, 2, 0, 0, 0, 0, 1, 1, 2, 0, 0, 0, 2, 2, 1, 1],
      [0, 3, 2, 3, 0, 1, 3, 0, 2, 1, 2, 3, 2, 3, 3, 0],
      [0, 0, 0, 2, 0, 2, 1, 3, 2, 2, 0, 2, 2, 0, 1, 3],
      [0, 1, 2, 1, 0, 3, 3, 2, 2, 3, 2, 1, 2, 1, 3, 2]]


def prs_spectrum():
    """Phase reference symbol P[bin] (length 2048, bin = k mod 2048), |P| = 1 on 1536 carriers."""
    P = np.zeros(TU, np.complex128)
    for kp, i, n in _PRS_TABLE:
        for k in range(kp, kp + 32):
            P[k % TU] = np.exp(1j * np.pi / 2 * (_H[i][(k - kp) % 16] + n))
    return P


def tii_patterns():
    """70 x 8 pattern table: main id -> 4-of-8 group bits (EN 300 401 table 22, lexicographic)."""
    pats = [c for c in itertools.product((0, 1), repeat=8) if sum(c) == 4]
    return np.array(pats, np.int8)          # lexicographic order == the standard's order


PATTERNS = tii_patterns()
_PAT_INDEX = {tuple(np.nonzero(p)[0]): i for i, p in enumerate(PATTERNS)}


def tii_carriers(main, sub):
    """Carrier indices k (sorted) of TII (main, sub) and, for each, the first carrier of its pair."""
    ks, k1 = [], []
    for b in np.nonzero(PATTERNS[main])[0]:
        k0 = 1 + 2 * sub + 48 * int(b)            # 1..383 (positive-first-quarter convention)
        for base in (k0 - 769, k0 - 385, k0, k0 + 384):
            ks += [base, base + 1]
            k1 += [base, base]
    o = np.argsort(ks)
    return np.array(ks)[o], np.array(k1)[o]


def _group_bins():
    """bins[c, b] -> the 8 FFT bins (4 quarters x pair) of comb c, group b."""
    out = np.zeros((24, 8, 8), int)
    for c in range(24):
        for b in range(8):
            k0 = 1 + 2 * c + 48 * b
            ks = [k0 - 769, k0 - 768, k0 - 385, k0 - 384, k0, k0 + 1, k0 + 384, k0 + 385]
            out[c, b] = np.array(ks) % TU
    return out


GROUP_BINS = _group_bins()


# ---------------------------------------------------------------- signal processing
def channelise_many(rec, freqs_mhz, max_s=12.0, bw=1.62e6):
    """Channelise several DAB blocks at once to exactly 2.048 Msps (FFT overlap-save,
    one big FFT per input block shared by all channels). Works for input rates that are
    a multiple of 2.048e6 * k / 2**m (20 Msps: ratio 64/625); falls back to resample_poly."""
    from fractions import Fraction
    from scipy import fft as sfft
    fs = rec.rate
    fr = Fraction(FS / fs).limit_denominator(100000)
    up, down = fr.numerator, fr.denominator           # 20e6 -> 64/625
    n_tot = int(min(rec.n_samples(), max_s * fs))
    if up > 4096 or down > 100000:
        from .decoders.dab import _channel
        return [_channel(rec, f * 1e6 - rec.center_hz, max_s=max_s) for f in freqs_mhz]
    E = down * max(1, int(round(100000 / down)))      # discarded edge, input samples
    N = down * max(8, int(round(1_250_000 / down)))   # FFT size, input samples
    while N <= 2 * E:
        N *= 2
    H = N - 2 * E
    M = N * up // down                                # output FFT size
    Eo = E * up // down
    df = fs / N
    f = sfft.fftfreq(M, 1 / FS)
    taper = np.clip((bw / 2 + 40e3 - np.abs(f)) / 80e3, 0, 1).astype(np.float32)
    keep = taper > 0
    offs = [int(round((fm * 1e6 - rec.center_hz) / df)) for fm in freqs_mhz]
    fo = sfft.fftfreq(M, 1 / M).astype(np.int64)[keep]
    tk = taper[keep]
    outs = [[] for _ in freqs_mhz]
    a = -E
    while a + E < n_tot:
        lo, hi = max(0, a), min(n_tot, a + N)
        x = np.zeros(N, np.complex64)
        x[lo - a: hi - a] = rec.iq(lo, hi - lo)
        X = sfft.fft(x, workers=-1)
        for ci, b in enumerate(offs):
            Y = np.zeros(M, np.complex64)
            Y[keep] = X[(fo + b) % N] * tk
            yb = sfft.ifft(Y, workers=-1) * (M / N)
            # integer-bin shift by b in a block starting at input sample a: phase-continuity term
            yb *= np.exp(-2j * np.pi * ((b * a) % N) / N)
            n_valid = min(H, n_tot - (a + E))
            outs[ci].append(yb[Eo: Eo + n_valid * up // down])
        a += H
    return [np.concatenate(o).astype(np.complex64) for o in outs]


def channelise(rec, fmhz, max_s=12.0):
    return channelise_many(rec, [fmhz], max_s=max_s)[0]


def _frac_cfo(y):
    r = np.sum(y[TU:] * np.conj(y[:-TU]))
    return float(np.angle(r)) * FS / (2 * np.pi * TU)


def prs_time(P):
    """Time-domain PRS useful part (2048 samples, unit power)."""
    return np.fft.ifft(P) * np.sqrt(TU / 1536.0) * np.sqrt(TU)


def _xcorr_prs(y, tmpl, step=1 << 20):
    """|corr(y, tmpl)| for every lag (overlap-save via FFT)."""
    from scipy.signal import fftconvolve
    k = np.conj(tmpl[::-1]).astype(np.complex64)
    out = np.empty(max(0, len(y) - len(tmpl) + 1), np.float32)
    for a in range(0, len(out), step):
        seg = y[a: a + step + len(tmpl) - 1]
        c = fftconvolve(seg, k, mode="valid")
        out[a: a + len(c)] = np.abs(c)
    return out


def _int_cfo(y, P, span=10, seg_s=0.3):
    """Integer carrier offset: PRS time correlation peak-to-median over carrier shifts."""
    x = y[: int(seg_s * FS)]
    n = np.arange(len(x))
    best = (0, 0.0)
    for m in range(-span, span + 1):
        tm = prs_time(np.roll(P, m))
        c = _xcorr_prs(x, tm)
        v = float(c.max() / (np.median(c) + 1e-30))
        if v > best[1]:
            best = (m, v)
    return best


def _prs_peaks(y, P):
    """Start of the PRS useful part (strongest path) in every complete frame."""
    c = _xcorr_prs(y, prs_time(P))
    first = int(np.argmax(c[:TFRAME]))
    peaks = []
    n = first
    while n - 3000 < len(c):
        a, b = max(0, n - 3000), min(len(c), n + 3000)
        if b - a < 1000:
            break
        m = a + int(np.argmax(c[a:b]))
        q = float(c[m] / (np.median(c[a:b]) + 1e-30))
        peaks.append((m, q))
        n = m + TFRAME
    return peaks


def _cir(Y, P, os_=1):
    """Power delay profile of a channel estimate on the 1536 carriers, oversampled."""
    H = np.zeros(TU * os_, np.complex128)
    k = np.r_[1:769, -768:0]
    H[k % (TU * os_)] = Y[k % TU] * np.conj(P[k % TU])
    return np.fft.ifft(H) * TU * os_


def analyse_block(y, P=None):
    """Frame sync + coherent alignment of one channelised block.

    Returns {"Zn": (F, 2048) aligned null-symbol spectra divided by the PRS phase of the first
    carrier of each TII pair, "Hc": (F, 2048) aligned composite channel from the PRS, ...}.
    Alignment: frame positions follow a straight line (common sample-clock drift), the
    composite main path is put at exactly REF_POS and its phase at 0 in every frame, so
    frames can be averaged coherently."""
    if P is None:
        P = prs_spectrum()
    n = np.arange(len(y), dtype=np.float64)
    f1 = _frac_cfo(y)
    y = (y * np.exp(-2j * np.pi * f1 / FS * n)).astype(np.complex64)
    m, q = _int_cfo(y, P)
    if q < 8:
        return None
    cfo = f1 + m * FS / TU
    if m:
        y = (y * np.exp(-2j * np.pi * m / TU * n)).astype(np.complex64)
    peaks = _prs_peaks(y, P)
    if len(peaks) < 3:
        return None
    # The capture may drop samples (USB): every frame is timed on its own PRS peak, then
    # refined to sub-sample precision by matching its power delay profile to the average.
    kk = np.fft.fftfreq(TU, 1 / TU)
    Yn_l, Yp_l, fidx = [], [], []
    for pk, q in peaks:
        t0 = pk - TNULL - TG - REF_POS
        if q < 6 or t0 < 0 or t0 + TNULL + TS > len(y):
            continue
        fidx.append(int(round((pk - peaks[0][0]) / TFRAME)))
        Yn_l.append(np.fft.fft(y[t0 + TNULL - TU: t0 + TNULL]))
        Yp_l.append(np.fft.fft(y[t0 + TNULL + TG: t0 + TNULL + TG + TU]))
    if len(Yp_l) < 3:
        return None
    Yn, Yp = np.array(Yn_l), np.array(Yp_l)
    Hc = Yp * np.conj(P)[None, :]
    k = np.r_[1:769, -768:0]
    mask = np.zeros(TU)
    mask[k % TU] = 1
    os2 = 4
    N2 = TU * os2

    def pdps(H):
        G = np.zeros((len(H), N2), np.complex128)
        G[:, k % N2] = H[:, k % TU]
        return np.abs(np.fft.ifft(G, axis=1)) ** 2

    W = 40 * os2                                         # +-40 samples search
    sh = np.zeros(len(Hc))
    for _ in range(2):
        ramp = np.exp(2j * np.pi * np.outer(sh, kk) / TU)
        pd = pdps(Hc * ramp)
        tmpl = np.median(pd, axis=0)
        Tf = np.conj(np.fft.fft(tmpl))
        xc = np.real(np.fft.ifft(np.fft.fft(pd, axis=1) * Tf[None, :], axis=1))
        lag = np.r_[0:W + 1, -W:0] % N2
        for f in range(len(Hc)):
            v = xc[f, lag]
            i_ = int(np.argmax(v))
            ii = (i_ if i_ <= W else i_ - (2 * W + 1))
            sh[f] += _peak_interp(xc[f], ii) / os2
        sh -= np.median(sh)
    ramp = np.exp(2j * np.pi * np.outer(sh, kk) / TU)
    Yn, Hc = Yn * ramp, Hc * ramp
    # quality: correlation of each frame's profile with the template
    pd = pdps(Hc)
    tmpl = np.median(pd, axis=0)
    rho = np.array([np.corrcoef(p_, tmpl)[0, 1] for p_ in pd])
    ok = rho > max(0.3, np.median(rho) - 3 * np.std(rho))
    Yn, Hc, fidx = Yn[ok], Hc[ok], np.array(fidx)[ok]
    # global sub-sample offset of the main path (from the mean power delay profile)
    pdp = np.mean(pdps(Hc), axis=0)
    i = int(np.argmax(pdp))
    i = i - len(pdp) if i > len(pdp) // 2 else i
    g = _peak_interp(pdp, i) / os2 - REF_POS
    ramp = np.exp(2j * np.pi * kk * g / TU)[None, :]
    Yn, Hc = Yn * ramp, Hc * ramp
    # per-frame common phase of the main path
    k = np.r_[1:769, -768:0]
    ph = np.angle(np.sum(Hc[:, k % TU] * np.exp(2j * np.pi * k * REF_POS / TU)[None, :], axis=1))
    rot = np.exp(-1j * ph)[:, None]
    Yn, Hc = Yn * rot, Hc * rot
    Zn = Yn * np.conj(P[K_FIRST])[None, :]
    # SNR from the composite channel: coherent mean vs frame-to-frame scatter
    sig = np.mean(np.abs(np.mean(Hc[:, k % TU], 0)) ** 2)
    noise = np.mean(np.abs(Hc[:, k % TU] - np.mean(Hc[:, k % TU], 0)) ** 2)
    return {"Zn": Zn, "Hc": Hc, "cfo_hz": cfo, "frames": len(Hc), "fidx": fidx, "rejected": int((~ok).sum()),
            "snr_db": float(10 * np.log10(sig / noise + 1e-12)), "P": P}


def _peak_interp(a, i):
    """Parabolic sub-bin peak position around index i (circular)."""
    n = len(a)
    y0, y1, y2 = a[(i - 1) % n], a[i % n], a[(i + 1) % n]
    d = y0 - 2 * y1 + y2
    return i + (0.5 * (y0 - y2) / d if d < 0 else 0.0)


def _first_of_pair():
    """bin -> bin of the first carrier of its TII pair (pairs (2j, 2j+1) for k<0, (2j+1, 2j+2) for k>0)."""
    out = np.arange(TU)
    for k in range(-768, 769):
        if k == 0:
            continue
        f = (k if k % 2 == 0 else k - 1) if k < 0 else (k if k % 2 == 1 else k - 1)
        out[k % TU] = f % TU
    return out


K_FIRST = _first_of_pair()


def _profile(Z, bins, os_=1, n=None):
    """|sum_k Z(k) e^{j 2 pi k tau / TU}|^2 for tau = 0..TU-1 in steps of 1/os_."""
    N = TU * os_
    H = np.zeros(N, np.complex128)
    kk = np.where(bins >= TU // 2, bins - TU, bins)
    H[kk % N] = Z[bins]
    return np.abs(np.fft.ifft(H) * N) ** 2


def detect_tii(a, thr=30.0, max_codes=16):
    """Successive detection of TII codes on the coherently averaged null spectrum.

    Detection statistic per code: peak of the coherent delay profile over the search window,
    normalised by its noise power (from frame-to-frame scatter), using only groups not yet
    claimed by stronger codes."""
    Zn = a["Zn"]
    F = len(Zn)
    Zm = Zn.mean(0)
    var = np.mean(np.abs(Zn - Zm) ** 2, 0) / F               # noise variance of the mean
    lo, hi = SEARCH
    # complex per-group delay responses G[c, b, tau] and their noise powers
    Gr = np.zeros((24, 8, hi - lo), np.complex128)
    Nv = np.zeros((24, 8))
    for c in range(24):
        for b in range(8):
            bins = GROUP_BINS[c, b]
            kk = np.where(bins >= TU // 2, bins - TU, bins)
            tau = np.arange(lo, hi)
            Gr[c, b] = np.exp(2j * np.pi * np.outer(tau, kk) / TU) @ Zm[bins]
            Nv[c, b] = np.sum(var[bins])
    used = np.zeros((24, 8), bool)
    found = []
    while len(found) < max_codes:
        best = None
        for c in range(24):
            for p in range(70):
                free = [b for b in np.nonzero(PATTERNS[p])[0] if not used[c, b]]
                if len(free) < 2:
                    continue
                prof = np.abs(np.sum(Gr[c, free], axis=0)) ** 2
                s = float(prof.max() / np.sum(Nv[c, free]))
                if best is None or s > best[0]:
                    best = (s, c, p, free)
        if best is None or best[0] < thr:
            break
        s, c, p, free = best
        found.append({"main": p, "sub": c, "snr": float(10 * np.log10(s)), "free_groups": len(free),
                      "shared": len(free) < 4})
        used[c, np.nonzero(PATTERNS[p])[0]] = True
    return found


def estimate_delays(a, codes):
    """Per code: candidate delays (window samples, sub-sample) from the oversampled coherent
    profile of its (unshared) carriers, scored with the coherent composite impulse response."""
    Zm = a["Zn"].mean(0)
    Hm = a["Hc"].mean(0)
    comp = _profile(Hm, np.r_[1:769, -768:0] % TU, OS)
    comp_n = comp / comp.max()
    lo, hi = SEARCH[0] * OS, SEARCH[1] * OS
    claimed = {}
    for cd in codes:
        for b in np.nonzero(PATTERNS[cd["main"]])[0]:
            claimed[(cd["sub"], b)] = claimed.get((cd["sub"], b), 0) + 1
    out = []
    taken = []                                   # delays (samples) explained by stronger codes
    for cd in sorted(codes, key=lambda d: -d["snr"]):
        gb = [b for b in np.nonzero(PATTERNS[cd["main"]])[0]]
        own = [b for b in gb if claimed[(cd["sub"], b)] == 1]
        use = own if len(own) >= 2 else gb
        bins = GROUP_BINS[cd["sub"], use].ravel()
        prof = _profile(Zm, bins, OS)
        seg = prof[lo:hi]
        pmax = seg.max()
        idx = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] > seg[i + 1]
               and seg[i] > 0.4 * pmax]
        cands = []
        for i in idx:
            j = lo + i
            tau = _peak_interp(prof, j) / OS
            w = int(1.0 * OS)
            c = float(comp_n[max(0, j - w): j + w + 1].max())
            if any(abs(tau - t) < 6.0 for t in taken):
                c = 0.0                 # composite path already owned by a stronger transmitter
            cands.append({"tau": tau, "tii": float(seg[i] / pmax), "comp": c,
                          "score": float(seg[i] / pmax) * max(min(1.0, c / 0.02), 0.05) ** 0.5})
        cands.sort(key=lambda d: -d["score"])
        if cands:
            taken.append(cands[0]["tau"])
        out.append(dict(cd, groups_used=len(use), cands=cands[:6]))
    return out, comp_n


def assisted_detect(a, est, comp_n, known, thr_db=10.0, min_path_db=-22.0, min_frames=30):
    """Second pass for weak transmitters: strong composite paths not explained by a detected
    TII are tested against the TII codes known for this block (database prior), at that
    delay only (no look-elsewhere over all delays/codes, hence a lower threshold).
    Returns extra entries flagged tentative=True."""
    if len(a["Zn"]) < min_frames:          # noise estimate too uncertain for a lowered threshold
        return []
    Zm = a["Zn"].mean(0)
    var = np.mean(np.abs(a["Zn"] - Zm) ** 2, 0) / len(a["Zn"])
    lo, hi = SEARCH[0] * OS, SEARCH[1] * OS
    have = {(e["main"], e["sub"]) for e in est}
    used = {(e["sub"], b) for e in est for b in np.nonzero(PATTERNS[e["main"]])[0]}
    taken = [e["cands"][0]["tau"] for e in est if e["cands"]]
    thr = 10 ** (min_path_db / 10)
    peaks = [j for j in range(lo + 1, hi - 1) if comp_n[j] > thr and comp_n[j] >= comp_n[j - 1]
             and comp_n[j] > comp_n[j + 1]]
    extra = []
    for j in peaks:
        tau = _peak_interp(comp_n, j) / OS
        if any(abs(tau - t) < 6.0 for t in taken):     # main-path sidelobes / filter ringing
            continue
        res = []
        for m, sb in known:
            if (m, sb) in have:
                continue
            free = [b for b in np.nonzero(PATTERNS[m])[0] if (sb, b) not in used]
            if len(free) < 3:          # mostly carriers of an already detected code: cross-talk
                continue
            bins = GROUP_BINS[sb, free].ravel()
            prof = _profile(Zm, bins, OS)
            w = OS                                           # +-1 sample around the path
            seg = prof[j - w: j + w + 1]
            res.append((float(seg.max() / np.sum(var[bins])), m, sb, j - w + int(np.argmax(seg)), free))
        if not res:
            continue
        res.sort(reverse=True)
        s0, m, sb, jj, free = res[0]
        s1 = res[1][0] if len(res) > 1 else 0.0
        if 10 * np.log10(s0) >= thr_db and s0 > 1.5 * s1:
            t2 = _peak_interp(_profile(Zm, GROUP_BINS[sb, free].ravel(), OS), jj) / OS
            extra.append({"main": m, "sub": sb, "snr": float(10 * np.log10(s0)), "free_groups": len(free),
                          "shared": len(free) < 4, "groups_used": len(free), "tentative": True,
                          "cands": [{"tau": t2, "tii": 1.0, "comp": float(comp_n[j]), "score": 1.0}]})
            have.add((m, sb))
            taken.append(t2)
            used.update((sb, b) for b in np.nonzero(PATTERNS[m])[0])
    return extra


# ---------------------------------------------------------------- geometry
R_E = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def _ecef(lat, lon, h=0.0):
    la, lo = math.radians(lat), math.radians(lon)
    n = R_E / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return np.array([(n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo),
                     (n * (1 - _E2) + h) * math.sin(la)])


def _rot(lat0, lon0):
    la, lo = math.radians(lat0), math.radians(lon0)
    return np.array([[-math.sin(lo), math.cos(lo), 0],
                     [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
                     [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]])


def _enu3(lat, lon, lat0, lon0, h=0.0):
    """WGS84 local east/north/up (m) of (lat, lon, h) around (lat0, lon0, 0)."""
    return _rot(lat0, lon0) @ (_ecef(lat, lon, h) - _ecef(lat0, lon0))


def _enu(lat, lon, lat0, lon0):
    e = _enu3(lat, lon, lat0, lon0)
    return float(e[0]), float(e[1])


def _geodetic(xyz):
    x, y, z = xyz
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _E2))
    for _ in range(6):
        n = R_E / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon)


def _rx_point(x, y, lat0, lon0):
    """Tangent-plane (east x, north y) -> point on the ellipsoid below it (ENU 3-vector, lat, lon)."""
    R = _rot(lat0, lon0)
    o = _ecef(lat0, lon0)
    lat, lon = _geodetic(o + R.T @ np.array([x, y, 0.0]))
    return R @ (_ecef(lat, lon) - o), lat, lon


def _llh(x, y, lat0, lon0):
    _, lat, lon = _rx_point(x, y, lat0, lon0)
    return lat, lon


def solve_tdoa(tx, x0=None, iters=30):
    """Hyperbolic TDOA by weighted Gauss-Newton.

    tx: list of dicts {lat, lon, tau_s, sigma_s?, group, h?} (tau = arrival time in seconds with an
    arbitrary offset per group: receiver clock / FFT-window reference of that ensemble).
    Unknowns: receiver east/north in a local ENU frame around the transmitters' centroid
    (receiver on the WGS84 ellipsoid, distances computed in 3-D) and one clock offset per group,
    which is equivalent to differencing against a reference transmitter of each ensemble.
    Needs n_tx - n_groups >= 2. Returns dict (lat, lon, acc, rms_m, res_m, dof, cost) or None."""
    groups = sorted({t.get("group", 0) for t in tx}, key=str)
    if len(tx) - len(groups) < 2:
        return None
    lat0 = float(np.mean([t["lat"] for t in tx]))
    lon0 = float(np.mean([t["lon"] for t in tx]))
    S = np.array([_enu3(t["lat"], t["lon"], lat0, lon0, t.get("h") or 0.0) for t in tx])
    r = np.array([t["tau_s"] * C for t in tx])                  # pseudo-ranges (m)
    w = np.array([1.0 / max(t.get("sigma_s", 1e-6) * C, 30.0) for t in tx])
    gi = np.array([groups.index(t.get("group", 0)) for t in tx])
    ng = len(groups)
    n = len(tx)

    def model(p):
        q, _, _ = _rx_point(p[0], p[1], lat0, lon0)
        dv = q[None, :] - S
        d = np.linalg.norm(dv, axis=1) + 1e-9
        J = np.zeros((n, 2 + ng))
        J[:, 0] = dv[:, 0] / d
        J[:, 1] = dv[:, 1] / d
        J[np.arange(n), 2 + gi] = 1.0
        return d + p[2 + gi], J

    best = None
    starts = [x0] if x0 is not None else [(0.0, 0.0)] + [(dx, dy) for dx in (-60e3, 0, 60e3)
                                                            for dy in (-60e3, 0, 60e3) if dx or dy]
    for st in starts:
        p = np.zeros(2 + ng)
        p[:2] = st
        pred, _ = model(p)
        for g in range(ng):
            p[2 + g] = np.mean((r - pred + p[2 + gi])[gi == g])
        for _ in range(iters):
            pred, J = model(p)
            res = r - pred
            dp, *_ = np.linalg.lstsq(J * w[:, None], res * w, rcond=None)
            step = np.hypot(*dp[:2])
            if step > 50e3:                                       # damp wild first steps
                dp *= 50e3 / step
            p += dp
            if np.hypot(*dp[:2]) < 0.01:
                break
        pred, J = model(p)
        res = r - pred
        cost = float(np.sum((res * w) ** 2))
        if best is None or cost < best[0] - 1e-9:
            best = (cost, p.copy(), res, J)
    cost, p, res, J = best
    dof = n - (2 + ng)
    sig_m = float(np.sqrt(np.sum(res ** 2) / dof)) if dof > 0 else None
    # 1-sigma horizontal accuracy: geometry (covariance) scaled by the larger of the residual
    # rms and the a-priori range sigma
    s0 = max(sig_m or 0.0, float(np.median(1 / w)))
    try:
        cov = np.linalg.inv(J.T @ J) * s0 ** 2
        acc = float(np.sqrt(cov[0, 0] + cov[1, 1]))
    except np.linalg.LinAlgError:
        acc = float("inf")
    lat, lon = _llh(p[0], p[1], lat0, lon0)
    return {"lat": lat, "lon": lon, "acc": acc, "rms_m": sig_m, "res_m": res.tolist(), "dof": dof,
            "cost": cost}


# ---------------------------------------------------------------- database
def load_db(path=DATA_PATH):
    with open(path) as f:
        return json.load(f)


def match_sites(db, block, codes, eid=None):
    """Map detected TII codes of a block to database sites."""
    out = []
    ens = [e for e in db.get("ensembles", []) if e.get("block") == block
           and (eid is None or not e.get("eid") or e["eid"].upper() == eid.upper())]
    for cd in codes:
        hits = []
        for e in ens:
            for t in e.get("transmitters", []):
                if t.get("tii_main") == cd["main"] and t.get("tii_sub") == cd["sub"] \
                        and t.get("lat") is not None:
                    hits.append(dict(t, ensemble=e.get("label"), eid=e.get("eid")))
        out.append(hits)
    return out


# ---------------------------------------------------------------- top level
def process_y(y, block="?", fmhz=0.0, thr=30.0, known=None):
    a = analyse_block(y)
    if a is None or a["frames"] < 3:
        return None
    codes = detect_tii(a, thr=thr)
    a["tii_frames"], a["tii_alternate"] = a["frames"], False
    if codes:
        # TII may be carried in alternate frames only: test the frame parity on the strongest code
        bins = tii_carriers(codes[0]["main"], codes[0]["sub"])[0] % TU
        Zn = a["Zn"]
        ref = Zn[:, bins].mean(0)
        cf = np.real(Zn[:, bins] @ np.conj(ref)) / np.real(np.vdot(ref, ref))
        fidx = a["fidx"]
        par = [float(np.mean(cf[fidx % 2 == r])) if np.any(fidx % 2 == r) else 0.0 for r in (0, 1)]
        r_best = int(np.argmax(par))
        if min(par) < 0.5 * max(par):
            a["Zn"] = Zn[fidx % 2 == r_best]
            a["tii_frames"], a["tii_alternate"] = len(a["Zn"]), True
            codes = detect_tii(a, thr=thr)
    est, comp = estimate_delays(a, codes)
    if known and codes:
        est += assisted_detect(a, est, comp, known)
    return {"block": block, "fmhz": fmhz, "cfo_hz": a["cfo_hz"], "frames": a["frames"],
            "tii_frames": a["tii_frames"], "tii_alternate": a["tii_alternate"], "snr_db": a["snr_db"], "rejected": a["rejected"], "tii": est}


def process(rec, block, fmhz, max_s=12.0):
    return process_y(channelise(rec, fmhz, max_s=max_s), block, fmhz)


def _plausible(sel, max_extra_m=10000.0):
    """Physical constraint within one SFN: |c*dtau_ij| <= baseline_ij (+ margin for static
    transmitter offsets, which were observed to reach ~9 km equivalent)."""
    for a, b in itertools.combinations(sel, 2):
        base = math.hypot(*_enu(a["lat"], a["lon"], b["lat"], b["lon"]))
        if abs(a["tau_s"] - b["tau_s"]) * C > base + max_extra_m:
            return False
    return True


def _plausible_groups(tx):
    return all(_plausible([t for t in tx if t["group"] == g]) for g in {t["group"] for t in tx})


def solve_with_ambiguity(rows, max_combos=20000):
    """rows: dicts with site lat/lon, group, cands (candidate delays in samples with scores).
    Tries the grating-lobe alternatives of each TII (6.25 km apart), keeps physically plausible
    combinations and returns (solution, chosen candidates) of the best one: lowest weighted
    cost plus a penalty for using lobes the TII/composite evidence did not prefer."""
    choices = [[c for c in r["cands"] if c["score"] > 0.5 * r["cands"][0]["score"]][:3] for r in rows]
    if math.prod(len(c) for c in choices) > max_combos:
        choices = [c[:1] for c in choices]
    best = None
    for combo in itertools.product(*choices):
        tx = [dict(lat=r["lat"], lon=r["lon"], group=r["group"],
                   tau_s=c["tau"] / FS - (r.get("offset_us") or 0.0) * 1e-6,
                   sigma_s=r.get("sigma_s", 0.25 / FS)) for r, c in zip(rows, combo)]
        if not _plausible_groups(tx):
            continue
        sol = solve_tdoa(tx)
        if sol is None:
            continue
        pen = sum(-math.log(max(c["score"], 1e-3) / r["cands"][0]["score"]) for r, c in zip(rows, combo))
        key = sol["cost"] + 4.0 * pen
        if best is None or key < best[0]:
            best = (key, sol, combo)
    return (best[1], best[2]) if best else (None, None)


MAX_ACC_M = 20000.0


def _estimate(sol, rows, note):
    """JSON estimate object for the browser 'Where am I' feature, or None if not credible."""
    acc = sol["acc"]
    if sol["dof"] <= 0:            # exactly determined: no residual check, inflate
        acc = max(acc, 1000.0)
        note += " (exactly determined, unverified)"
    if not math.isfinite(acc) or acc > MAX_ACC_M:
        return None
    return {"src": "dabtii", "lat": round(sol["lat"], 5), "lon": round(sol["lon"], 5),
            "acc": int(round(min(acc, 99999))), "n": len(rows), "note": note}


def main(argv=None):
    from . import recording
    from .decoders.dab import BLOCKS
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture", nargs="+", help="SigMF .sigmf-meta file(s)")
    ap.add_argument("--blocks", help="comma list, e.g. 12C,11C (default: all blocks in the usable span)")
    ap.add_argument("--db", default=DATA_PATH)
    ap.add_argument("--max-s", type=float, default=12.0, help="seconds of capture to use")
    ap.add_argument("--thr-db", type=float, default=14.8, help="TII detection threshold (coherent peak SNR)")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)
    db = load_db(args.db)
    say = (lambda *a: None) if args.json_only else print
    rows_all, estimates = [], []
    for cap in args.capture:
        rec = recording.load(cap)
        usable = 0.5 * rec.rate * 0.80                    # HackRF baseband filter: ~75-80 % usable
        want = args.blocks.split(",") if args.blocks else list(BLOCKS)
        blocks = [b for b in want if b in BLOCKS and abs(BLOCKS[b] * 1e6 - rec.center_hz) + 0.77e6 <= usable]
        for b in want:
            if args.blocks and b not in blocks:
                say(f"{b}: outside the usable capture span")
        if not blocks:
            continue
        ys = channelise_many(rec, [BLOCKS[b] for b in blocks], max_s=args.max_s)
        for blk, y in zip(blocks, ys):
            known = sorted({(t["tii_main"], t["tii_sub"]) for e in db.get("ensembles", []) if e.get("block") == blk
                            for t in e.get("transmitters", []) if t.get("tii_main") is not None
                            and t.get("tii_sub") is not None and t.get("lat") is not None})
            r = process_y(y, blk, BLOCKS[blk], thr=10 ** (args.thr_db / 10), known=known)
            if r is None:
                say(f"{os.path.basename(cap)} {blk}: no DAB frame sync")
                continue
            say(f"\n== {os.path.basename(cap)} block {blk} {BLOCKS[blk]:.3f} MHz: {r['frames']} frames "
                f"({r['tii_frames']} used for TII{', alternate frames' if r['tii_alternate'] else ''}), "
                f"channel SNR {r['snr_db']:.1f} dB, CFO {r['cfo_hz']:+.0f} Hz")
            if not r["tii"]:
                say("  no TII above threshold")
            matches = match_sites(db, blk, r["tii"])
            rows = []
            for t, hits in zip(r["tii"], matches):
                c0 = t["cands"][0] if t["cands"] else None
                d = (c0["tau"] - REF_POS) if c0 else float("nan")
                alts = " ".join(f"{c['tau'] - REF_POS:+.1f}" for c in t["cands"][1:4])
                sites = sorted({h["site"] for h in hits})
                site = "; ".join(f"{h['site']} [{h.get('ensemble')}]" for h in hits) or "-- not in database --"
                say(f"  TII {t['main']:2d}-{t['sub']:02d}  {t['snr']:5.1f} dB  delay {d:+8.2f} smp "
                    f"({d * M_PER_SAMPLE / 1e3:+6.2f} km){'  shared-groups' if t['shared'] else ''}"
                    f"{'  TENTATIVE(composite-assisted)' if t.get('tentative') else ''}"
                    f"  lobes[{alts}]  -> {site}")
                if len(sites) == 1 and c0:
                    h = hits[0]
                    rows.append({"lat": h["lat"], "lon": h["lon"], "site": h["site"], "group": f"{cap}:{blk}",
                                 "cands": t["cands"], "block": blk, "tii": f"{t['main']}-{t['sub']:02d}",
                                 "offset_us": h.get("offset_us")})
            if len(rows) >= 3:
                sol, combo = solve_with_ambiguity(rows)
                if sol:
                    say(f"  fix {blk}: {sol['lat']:.5f} {sol['lon']:.5f}  acc {sol['acc']:.0f} m  "
                        f"rms {sol['rms_m'] if sol['rms_m'] is not None else float('nan'):.0f} m  n={len(rows)}")
                    e = _estimate(sol, rows, f"DAB {blk} TII TDOA: " + ",".join(x["site"] for x in rows))
                    if e:
                        estimates.append(e)
                    else:
                        say(f"  fix rejected: accuracy {sol['acc']:.0f} m (geometry / wrong lobe / SFN offsets)")
            else:
                say(f"  {len(rows)} identified transmitter(s) with position: need >= 3 for a fix")
            rows_all += rows
    # combined solution: only ensembles contributing >= 2 transmitters carry TDOA information;
    # need >= 3 distinct sites and at least one redundant measurement
    multi = [r for r in rows_all if sum(1 for q in rows_all if q["group"] == r["group"]) >= 2]
    groups = {r["group"] for r in multi}
    nsites = len({(round(r["lat"], 3), round(r["lon"], 3)) for r in multi})
    if len(groups) > 1 and nsites >= 3 and len(multi) - len(groups) - 2 >= 1:
        sol, combo = solve_with_ambiguity(multi)
        if sol:
            say(f"\ncombined fix: {sol['lat']:.5f} {sol['lon']:.5f}  acc {sol['acc']:.0f} m  n={len(multi)}")
            e = _estimate(sol, multi, "DAB TII TDOA combined " + ",".join(sorted({r['block'] for r in multi})))
            if e:
                estimates.insert(0, e)
    if not estimates:
        say(f"\nno position fix: {len(rows_all)} identified transmitter(s) at {nsites} distinct site(s) in "
            f"{len({r['group'] for r in rows_all})} ensemble(s); TDOA needs >= 3 sites in one SFN "
            f"(or >= 3 sites and a redundant measurement across SFNs)")
    print(json.dumps(estimates))
    return estimates


if __name__ == "__main__":
    main()
