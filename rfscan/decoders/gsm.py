"""GSM broadcast cell identification (same scope as grgsm_scanner).

Pipeline on a wideband recording (typically 20 Msps, ~1.2 s):
  1. FCCH search for every ARFCN in span with one wideband STFT: an FCCH burst is a
     pure tone at +67.7 kHz (1625/24 kHz) lasting one burst, repeating every 10/11
     TDMA frames.  -> candidate BCCH carriers (level "detect")
  2. Native SCH decode one frame after each FCCH: GMSK differential demod, extended
     training sequence sync, Viterbi (K=5, r=1/2), 10-bit fire parity -> BSIC and
     frame number (level "crc").
  3. BCCH system information (SI3/SI4: MCC, MNC, LAC, CI) via gr-gsm's grgsm_decode on
     the channelised carrier (level "crc", gr-gsm checks the FIRE code).
Only broadcast control information is decoded; no traffic channels, no ciphering.
"""
import os
import re
import subprocess

import numpy as np
from scipy import signal

from . import result

NAME = "gsm"
CAPTURE = {"rate": 20e6, "seconds": 2.0}

SYM_RATE = 1625e3 / 6            # 270.833 kbaud
OSR = 4
CH_RATE = SYM_RATE * OSR         # 1.0833 Msps
FCCH_TONE = SYM_RATE / 4         # 67.708 kHz
FRAME_S = 60e-3 / 13             # 4.615 ms
BURST_PERIOD_SYM = 156.25

NL_OPERATORS = {(206, 1): "Proximus", (206, 2): "Infrabel GSM-R", (206, 10): "Orange Belgium",
                (206, 20): "BASE (Telenet)", (206, 5): "Telenet", (206, 25): "Digi Belgium",
                (204, 4): "VodafoneZiggo", (204, 8): "KPN", (204, 16): "Odido (T-Mobile)",
                (204, 20): "Odido (ex-Tele2)", (204, 21): "ProRail GSM-R", (204, 2): "Odido (ex-Tele2)",
                (204, 12): "Odido/Telfort", (204, 69): "KPN"}


# ------------------------------------------------------------------ ARFCN
def arfcn_list(f_lo, f_hi):
    """(arfcn, downlink_hz, band) for all DL channels inside [f_lo, f_hi]."""
    out = []
    for n in range(0, 125):                       # P/E-GSM 900
        out.append((n, 935e6 + 0.2e6 * n, "GSM900"))
    for n in range(955, 1024):                    # R-GSM / E-GSM
        out.append((n, 935e6 + 0.2e6 * (n - 1024), "E/R-GSM900"))
    for n in range(512, 886):                     # DCS 1800
        out.append((n, 1805.2e6 + 0.2e6 * (n - 512), "DCS1800"))
    return [a for a in out if f_lo <= a[1] <= f_hi]


# ------------------------------------------------------------- channelizer
class Channelizer:
    """One big FFT of the recording; cheap extraction of any 200 kHz channel at OSR 4."""

    def __init__(self, x, rate):
        self.rate = rate
        # output rate must be CH_RATE exactly: N * CH_RATE / rate must be an integer
        q = 240 if abs(rate - 20e6) < 1 else None
        if q is None:
            # generic: resample whole capture is too slow; pick N so that M is integral
            frac = CH_RATE / rate
            N = len(x)
            self.M = int(round(N * frac))
            self.frac_exact = False
        else:
            N = (len(x) // q) * q
            self.M = N * 13 // q
            self.frac_exact = True
        self.N = N
        from scipy import fft as sfft
        self.X = sfft.fft(np.asarray(x[:N], np.complex64), workers=-1)
        self.out_rate = self.M * rate / N

    def get(self, f_off, bw=270e3):
        N, M = self.N, self.M
        k0 = int(round(f_off / (self.rate / N)))
        half = M // 2
        idx = (np.arange(-half, M - half) + k0) % N
        Y = self.X[idx].copy()
        fr = np.arange(-half, M - half) * (self.rate / N)
        Y *= np.clip((bw / 2 + 20e3 - np.abs(fr)) / 40e3, 0, 1)      # smooth edge
        from scipy import fft as sfft
        y = sfft.ifft(np.fft.ifftshift(Y), workers=-1) * (M / N)
        # residual from rounding the centre to an FFT bin
        resid = f_off - k0 * (self.rate / N)
        if abs(resid) > 1e-3:
            y = y * np.exp(-2j * np.pi * resid / self.out_rate * np.arange(M))
        return y.astype(np.complex64)


# --------------------------------------------------------------------- FCCH
def fcch_search(x, rate, center, chans, nfft=4096, chunk_frames=512, tone_tol=8e3, q_thr=0.6):
    """Wideband STFT FCCH detector.  Returns {arfcn: [(t_s, tone_hz, q), ...]}."""
    df = rate / nfft
    w = np.hanning(nfft).astype(np.float32)
    n_frames = len(x) // nfft
    # per channel: bin index ranges for the channel (+-90 kHz) and tone window
    ch_idx = []
    for a, f, _ in chans:
        c = (f - center) / df + nfft / 2
        lo, hi = int(round(c - 90e3 / df)), int(round(c + 90e3 / df))
        t_lo = int(round(c + (FCCH_TONE - tone_tol) / df))
        t_hi = int(round(c + (FCCH_TONE + tone_tol) / df))
        if lo < 0.05 * nfft or hi > 0.95 * nfft:
            continue
        ch_idx.append((a, f, lo, hi, t_lo, t_hi))
    if not ch_idx:
        return {}
    hits = {a: [] for a, *_ in ch_idx}
    lo_a = np.array([c[2] for c in ch_idx])
    hi_a = np.array([c[3] for c in ch_idx])
    for f0 in range(0, n_frames, chunk_frames):
        m = min(chunk_frames, n_frames - f0)
        seg = x[f0 * nfft:(f0 + m) * nfft].reshape(m, nfft)
        S = np.abs(np.fft.fftshift(np.fft.fft(seg * w, axis=1), axes=1)) ** 2
        cs = np.concatenate([np.zeros((m, 1), S.dtype), np.cumsum(S, axis=1)], axis=1)
        tot = cs[:, hi_a + 1] - cs[:, lo_a] + 1e-20                         # (m, nch)
        for j, (a, f, lo, hi, t_lo, t_hi) in enumerate(ch_idx):
            sub = S[:, t_lo:t_hi + 1]
            k = np.argmax(sub, axis=1)
            kk = k + t_lo
            pk = (S[np.arange(m), np.clip(kk - 1, 0, nfft - 1)] + S[np.arange(m), kk]
                  + S[np.arange(m), np.clip(kk + 1, 0, nfft - 1)])
            q = pk / tot[:, j]
            for i in np.where(q > q_thr)[0]:
                hits[a].append(((f0 + i) * nfft / rate, (kk[i] - nfft / 2) * df + center - f, float(q[i])))
    out = {}
    for a, h in hits.items():
        if not h:
            continue
        # merge consecutive windows of the same burst; reject tones longer than a burst
        bursts, cur = [], [h[0]]
        for e in h[1:]:
            if e[0] - cur[-1][0] <= 1.5 * nfft / rate:
                cur.append(e)
            else:
                bursts.append(cur)
                cur = [e]
        bursts.append(cur)
        good = [b for b in bursts if (b[-1][0] - b[0][0]) < 1.2e-3]
        good = [max(b, key=lambda e: e[2]) for b in good]
        if len(good) < 2:
            continue
        # periodicity: FCCH spacing is 10 or 11 frames (46.2 / 50.8 ms)
        ts = np.array([g[0] for g in good])
        d = np.diff(ts) / FRAME_S
        ok = np.sum(np.abs(d - np.round(d)) < 0.15) >= max(1, 0.5 * len(d))
        if ok:
            out[a] = good
    return out


# ---------------------------------------------------------------------- SCH
# 64-bit extended training sequence of the SCH (3GPP TS 45.002 5.2.5)
SCH_TRAIN = np.array([1, 0, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0,
                      0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1,
                      0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1, 0, 1, 1], np.uint8)
SCH_TRAIN_POS = 42          # burst bits 0-2 tail, 3-41 data, 42-105 training, 106-144 data
SCH_POLY = 0b10101110101    # D^10+D^8+D^6+D^5+D^4+D^2+1


def _diff_encode(bits, prev=1):
    """GSM differential encoding: d_i = b_i xor b_{i-1}; a_i = 1 - 2 d_i."""
    b = np.concatenate([[prev], bits])
    return b[1:] ^ b[:-1]


# a_i of the training sequence (the first training bit's predecessor is the last data bit:
# unknown, so its d is dropped from the correlation template)
_TRAIN_A = 1.0 - 2.0 * _diff_encode(SCH_TRAIN)[1:].astype(np.float32)


def _soft_symbols(y, start, n_sym, phase):
    """Phase advance per symbol at OSR 4, normalised: ~+1 for a=+1, ~-1 for a=-1."""
    idx = start + phase + OSR * np.arange(n_sym + 1)
    idx = idx[idx < len(y)]
    s = y[idx]
    dphi = np.angle(s[1:] * np.conj(s[:-1]))
    return dphi / (np.pi / 2)


def _viterbi_r12k5(soft):
    """soft: 2*n values, +1 means bit 0.  Code G0=1+D3+D4, G1=1+D+D3+D4.  Returns n bits."""
    n = len(soft) // 2
    ns = 16
    INF = 1e9
    metric = np.full(ns, INF)
    metric[0] = 0.0
    hist = np.zeros((n, ns), np.uint8)
    prev_state = np.zeros((n, ns), np.uint8)
    # state = last 4 input bits (u(k-1) in bit0 ... u(k-4) in bit3)
    outs = np.zeros((ns, 2, 2), np.float32)
    for st in range(ns):
        u1, u2, u3, u4 = (st >> 0) & 1, (st >> 1) & 1, (st >> 2) & 1, (st >> 3) & 1
        for u in (0, 1):
            c0 = u ^ u3 ^ u4
            c1 = u ^ u1 ^ u3 ^ u4
            outs[st, u] = (1 - 2 * c0, 1 - 2 * c1)
    for k in range(n):
        r = soft[2 * k:2 * k + 2]
        new = np.full(ns, INF)
        for st in range(ns):
            if metric[st] >= INF:
                continue
            for u in (0, 1):
                nst = ((st << 1) | u) & 0xF
                m = metric[st] - float(np.dot(outs[st, u], r))
                if m < new[nst]:
                    new[nst] = m
                    hist[k, nst] = u
                    prev_state[k, nst] = st
        metric = new
    st = int(np.argmin(metric)) if metric[0] >= INF else 0     # tail bits force state 0
    bits = np.zeros(n, np.uint8)
    for k in range(n - 1, -1, -1):
        bits[k] = hist[k, st]
        st = prev_state[k, st]
    return bits


def _parity_ok(info25, parity10):
    """Fire-code check: (d(D)*D^10 + p(D)) mod g(D) == 1+D+...+D^9 (parity is inverted)."""
    reg = 0
    for b in list(info25) + list(parity10):
        reg = (reg << 1) | int(b)
        if reg & (1 << 10):
            reg ^= SCH_POLY
    return reg == 0x3FF


def _parse_sch(info):
    """25 info bits d(0..24) -> (bsic, t1, t2, t3p), as libosmocore gsm0503 (LSB-first packing)."""
    sb = [0, 0, 0, 0]
    for i, bit in enumerate(info):
        if bit:
            sb[i // 8] |= 1 << (i % 8)
    bsic = (sb[0] >> 2) & 0x3F
    t1 = ((sb[0] & 0x03) << 9) | (sb[1] << 1) | (sb[2] >> 7)
    t2 = (sb[2] >> 2) & 0x1F
    t3p = ((sb[2] & 0x03) << 1) | (sb[3] & 0x01)
    return bsic, t1, t2, t3p


def fn_from_sch(t1, t2, t3p):
    t3 = 10 * t3p + 1
    return 51 * ((t3 - t2) % 26) + t3 + 51 * 26 * t1


def decode_sch_burst(y, approx_start, search=40, polarity=1.0):
    """Find the SCH training sequence near approx_start (sample index at OSR 4),
    demodulate and decode.  Returns dict or None."""
    best = None
    tl = len(_TRAIN_A)
    for phase in range(OSR):
        s0 = approx_start - search * OSR
        if s0 < 0:
            s0 = phase
        soft = polarity * _soft_symbols(y, s0, (2 * search + 148), phase)
        if len(soft) < 2 * search + 148:
            return None
        # correlate a(training[1:]) against soft symbols
        c = np.correlate(soft, _TRAIN_A, mode="valid")
        k = int(np.argmax(c))
        if best is None or c[k] > best[0]:
            best = (float(c[k]), phase, s0, k)
    score, phase, s0, k = best
    if score < 0.5 * len(_TRAIN_A):
        return None
    # soft index k corresponds to burst bit SCH_TRAIN_POS + 1 (a of training bit 1)
    b0 = k - (SCH_TRAIN_POS + 1)                # soft index of burst bit 0's d
    soft = polarity * _soft_symbols(y, s0, (2 * search + 160), phase)
    if b0 < 1 or b0 + 148 > len(soft):
        return None
    a = soft[b0:b0 + 148]                       # a_i for burst bits 0..147 (d domain)
    # reconstruct b from d using the known training bits as anchor (b = cumulative xor of d)
    d_hard = (a < 0).astype(np.uint8)
    b = np.zeros(148, np.uint8)
    b[SCH_TRAIN_POS] = SCH_TRAIN[0]
    for i in range(SCH_TRAIN_POS + 1, 148):
        b[i] = b[i - 1] ^ d_hard[i]
    for i in range(SCH_TRAIN_POS - 1, -1, -1):
        b[i] = b[i + 1] ^ d_hard[i + 1]
    # soft values for b: magnitude from |a|, sign from hard b (good enough for Viterbi)
    mag = np.minimum(np.abs(a), np.roll(np.abs(a), -1)) + 1e-3
    sb = (1.0 - 2.0 * b) * mag
    coded = np.concatenate([sb[3:42], sb[106:145]])
    u = _viterbi_r12k5(coded)
    info, par = u[:25], u[25:35]
    ok = _parity_ok(info, par)
    return {"ok": ok, "score": score / len(_TRAIN_A), "info": info,
            "sample": s0 + phase + OSR * b0, "train_err": int(np.sum(b[42:106] != SCH_TRAIN))}


# --------------------------------------------------- MLSE (Laurent) demod
L_TAPS = 5


def _derotate(y, s0):
    """Remove the GMSK pi/2-per-symbol rotation (sample index absolute from s0)."""
    n = np.arange(s0, s0 + len(y))
    return y * np.exp(-1j * np.pi / 2 * n / OSR).astype(np.complex64)


def _ls_channel(zs, k, ct, L=L_TAPS):
    """Least-squares taps for zs[k+i] = sum_l h_l ct[i-l], i = L-1..len(ct)-1."""
    rows = np.array([ct[i - np.arange(L)] for i in range(L - 1, len(ct))], np.float32)
    obs = zs[k + L - 1:k + len(ct)]
    if len(obs) != len(rows):
        return None, np.inf
    h, *_ = np.linalg.lstsq(rows.astype(np.complex64), obs, rcond=None)
    res = float(np.mean(np.abs(obs - rows @ h) ** 2) / (np.mean(np.abs(obs) ** 2) + 1e-20))
    return h, res


def _mlse(zs, h, n_sym, m0):
    """Viterbi equaliser: symbols c_j (+-1), observation zs[m0+j] = sum_l h_l c_{j-l}."""
    L = len(h)
    ns = 1 << (L - 1)
    # state bits: bit l-1 = symbol c_{j-l} (0 -> +1, 1 -> -1)
    st = np.arange(ns)
    past = np.array([[1 - 2 * ((s >> (l - 1)) & 1) for l in range(1, L)] for s in st], np.float32)
    pred = {}
    for u in (0, 1):
        cu = 1 - 2 * u
        pred[u] = cu * h[0] + past @ h[1:]                     # (ns,)
    metric = np.zeros(ns)
    back = np.zeros((n_sym, ns), np.int16)
    for j in range(n_sym):
        m = m0 + j
        if m < 0 or m >= len(zs):
            back[j] = -1
            continue
        z = zs[m]
        cand0 = metric + np.abs(z - pred[0]) ** 2
        cand1 = metric + np.abs(z - pred[1]) ** 2
        # next state n has u = n & 1 and predecessors s = (n >> 1) | (b << (L-2)), b in {0,1}
        nx = np.arange(ns)
        u = nx & 1
        sa = nx >> 1
        sb = sa | (1 << (L - 2))
        ca = np.where(u == 0, cand0[sa], cand1[sa])
        cb = np.where(u == 0, cand0[sb], cand1[sb])
        new = np.minimum(ca, cb)
        bp = np.where(ca <= cb, sa, sb).astype(np.int16)
        metric = new
        back[j] = bp
    s = int(np.argmin(metric))
    bits = np.zeros(n_sym, np.uint8)
    for j in range(n_sym - 1, -1, -1):
        bits[j] = s & 1
        if back[j, 0] == -1:
            continue
        s = int(back[j, s])
    return bits


def decode_sch_mlse(y, approx_start, search=60):
    """SCH with channel estimation + MLSE.  Returns dict or None."""
    ct = (1.0 - 2.0 * SCH_TRAIN).astype(np.float32)
    s0 = max(0, approx_start - search * OSR)
    seg = y[s0:s0 + (2 * search + 160) * OSR]
    if len(seg) < (2 * search + 150) * OSR:
        return None
    z = _derotate(seg, s0)
    best = None
    for p in range(OSR):
        zs = z[p::OSR]
        c = np.abs(np.correlate(zs, ct, mode="valid"))
        k = int(np.argmax(c))
        for kk in range(max(0, k - 3), k + 2):
            h, res = _ls_channel(zs, kk, ct)
            if h is not None and (best is None or res < best[0]):
                best = (res, p, kk, h, zs)
    if best is None or best[0] > 0.6:
        return None
    res, p, k, h, zs = best
    # burst bit j (0..147) observed at zs[k + (j - 42) + 0]  (tap 0 = newest symbol)
    m0 = k - SCH_TRAIN_POS
    bits = _mlse(zs, h, 148, m0)
    coded = np.concatenate([bits[3:42], bits[106:145]]).astype(np.float32)
    u = _viterbi_r12k5(1.0 - 2.0 * coded)
    info, par = u[:25], u[25:35]
    return {"ok": _parity_ok(info, par), "score": 1 - res, "info": info,
            "sample": s0 + p + OSR * m0, "train_err": int(np.sum(bits[42:106] != SCH_TRAIN))}


def fcch_fine_cfo(y, t_s, win=400):
    """Precise tone frequency of one FCCH burst in a channelised signal.
    Returns (cfo_hz relative to the nominal +67.7 kHz, burst start sample) or None."""
    a = max(0, int(t_s * CH_RATE) - 600)
    seg = y[a:a + 2600]
    if len(seg) < win + 10:
        return None
    d = np.angle(seg[1:] * np.conj(seg[:-1]))
    k = np.ones(win) / win
    m = np.convolve(d, k, "valid")
    v = np.convolve(d ** 2, k, "valid") - m ** 2
    i = int(np.argmin(v))
    if v[i] > 0.05:                  # not a clean tone
        return None
    f = m[i] * CH_RATE / (2 * np.pi)
    if abs(f - FCCH_TONE) > 3e3:     # a neighbour's tone leaking into this channel
        return None
    return f - FCCH_TONE, a + i


def decode_sch_robust(y, approx_start, search=60):
    """MLSE SCH decode with a small residual-CFO search; falls back to the differential demod."""
    tried = []
    for df in (0.0, 150.0, -150.0, 300.0, -300.0):
        yy = y if df == 0 else y * np.exp(-2j * np.pi * df / CH_RATE * np.arange(len(y))).astype(np.complex64)
        d = decode_sch_mlse(yy, approx_start, search)
        if d and d["ok"]:
            return d
        tried.append(d)
    for pol in (1.0,):
        d = decode_sch_burst(y, approx_start, search, polarity=pol)
        if d and d["ok"]:
            return d
    return None


# --------------------------------------------------------------------- BCCH
# grgsm_decode (gr-gsm 1.0 / GNU Radio 3.10.12) references gnuradio.pdu.socket_pdu, which
# moved to gnuradio.network; this shim restores it and runs the stock script.  The script
# binds a fixed UDP port (4729) per instance, so the shim uses an ephemeral port to allow
# several decoders in parallel.
_SHIM = """
import sys, runpy
from gnuradio import pdu
try:
    from gnuradio import network
    _orig = getattr(pdu, "socket_pdu", None) or network.socket_pdu
    def _socket_pdu(kind, host, port, mtu=10000, *a):
        return _orig(kind, host, "0", mtu, *a)
    pdu.socket_pdu = _socket_pdu
except ImportError:
    pass
sys.argv = ["grgsm_decode"] + sys.argv[1:]
runpy.run_path("/usr/bin/grgsm_decode", run_name="__main__")
"""
GRGSM = "/usr/bin/grgsm_decode"
# System Information message types (RR, TS 44.018 table 10.4.1); everything else on the
# CCCH (paging, immediate assignment) is discarded - it is not cell identification.
SI_TYPES = {0x19: "SI1", 0x1a: "SI2", 0x02: "SI2bis", 0x03: "SI2ter", 0x07: "SI2quater",
            0x1b: "SI3", 0x1c: "SI4", 0x1d: "SI5", 0x1e: "SI6", 0x1f: "SI7", 0x18: "SI8",
            0x00: "SI13"}


def _lai(b):
    """5-byte Location Area Identification -> (mcc, mnc, lac)."""
    d = [b[0] & 0xF, b[0] >> 4, b[1] & 0xF, b[1] >> 4, b[2] & 0xF, b[2] >> 4]
    mcc = d[0] * 100 + d[1] * 10 + d[2]
    mnc = d[4] * 10 + d[5] if d[3] == 0xF else d[4] * 100 + d[5] * 10 + d[3]
    return mcc, mnc, (b[3] << 8) | b[4]


def parse_si(lines):
    """grgsm_decode -v output lines -> dict of cell identity fields (SI3/SI4 only)."""
    info = {"si_seen": []}
    for line in lines:
        m = re.match(r"\s*(\d+)\s+\d+:\s+((?:[0-9a-f]{2}\s*)+)$", line)
        if not m:
            continue
        b = bytes.fromhex(m.group(2).replace(" ", ""))
        if len(b) < 8 or b[1] != 0x06:
            continue
        mt = b[2]
        name = SI_TYPES.get(mt)
        if name is None or (mt == 0x00 and len(b) < 10):
            continue
        if name not in info["si_seen"]:
            info["si_seen"].append(name)
        if mt == 0x1b:                                   # SI3: CI, LAI
            info["ci"] = (b[3] << 8) | b[4]
            info["mcc"], info["mnc"], info["lac"] = _lai(b[5:10])
        elif mt == 0x1c and "mcc" not in info:           # SI4: LAI
            info["mcc"], info["mnc"], info["lac"] = _lai(b[3:8])
    return info


def _run_grgsm(path, fc, timeout=60):
    if not os.path.exists(GRGSM):
        return None
    try:
        return subprocess.Popen(["/usr/bin/python3", "-c", _SHIM, "-c", path, "-f", str(int(fc)),
                                 "-s", repr(CH_RATE), "-m", "BCCH", "-t", "0", "-v"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None


# ------------------------------------------------------------------- decode
def decode(rec, ctx):
    tmpdir = ctx.get("tmpdir", "/tmp")
    x = rec.iq()
    if rec.rate < 2 * CH_RATE or len(x) < rec.rate * 0.1:
        return []
    span = 0.45 * rec.rate
    chans = arfcn_list(rec.center_hz - span, rec.center_hz + span)
    if not chans:
        return []
    hits = fcch_search(x, rec.rate, rec.center_hz, chans)
    if not hits:
        return []
    ch = Channelizer(x, rec.rate)
    freq_of = {a: (f, band) for a, f, band in chans}
    cells = []
    for a, g in sorted(hits.items()):
        f, band = freq_of[a]
        cfo0 = float(np.median([e[1] for e in g])) - FCCH_TONE
        y0 = ch.get(f + cfo0 - rec.center_hz)
        fines = [fcch_fine_cfo(y0, e[0]) for e in g]
        fv = [c[0] for c in fines if c]
        if not fv:
            continue
        cfo = cfo0 + float(np.median(fv))
        y = ch.get(f + cfo - rec.center_hz)
        sch, tries = [], 0
        for c in fines:
            if not c or tries >= 10 or len(sch) >= 3:
                continue
            tries += 1
            d = decode_sch_robust(y, c[1] + int(FRAME_S * CH_RATE), search=30)
            if d and d["ok"]:
                sch.append(d)
        cells.append({"arfcn": a, "freq": f, "band": band, "cfo": cfo, "y": y, "sch": sch,
                      "n_fcch": len(g), "power_db": float(10 * np.log10(np.mean(np.abs(y) ** 2) + 1e-20))})
    # suppress cells that are a neighbour's leakage: same BSIC & FN on an adjacent ARFCN, weaker
    results = []
    procs = []
    for c in cells:
        if c["sch"]:
            bsic, t1, t2, t3p = _parse_sch(c["sch"][0]["info"])
            c["bsic"], c["fn"] = bsic, fn_from_sch(t1, t2, t3p)
    for c in cells:
        if "bsic" in c:
            for o in cells:
                if (o is not c and o.get("bsic") == c["bsic"] and abs(o["arfcn"] - c["arfcn"]) == 1
                        and o["power_db"] > c["power_db"] + 6):
                    c["leak"] = True
    for c in cells:
        if c.get("leak"):
            continue
        if "bsic" in c:
            path = os.path.join(tmpdir, f"gsm_arfcn{c['arfcn']}.cfile")
            c["y"].astype(np.complex64).tofile(path)
            procs.append((c, path, _run_grgsm(path, c["freq"])))
    for c, path, p in procs:
        si = {"si_seen": []}
        if p is not None:
            try:
                out, _ = p.communicate(timeout=90)
                si = parse_si(out.splitlines())
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            os.remove(path)
        except OSError:
            pass
        c["si"] = si
    for c in cells:
        if c.get("leak"):
            continue
        base = dict(arfcn=c["arfcn"], band=c["band"], cfo_hz=round(c["cfo"], 1),
                    power_db=round(c["power_db"], 1), fcch_bursts=c["n_fcch"])
        if "bsic" not in c:
            results.append(result(NAME, "detect", c["freq"], f"GSM:ARFCN{c['arfcn']}",
                                  f"GSM BCCH carrier ARFCN {c['arfcn']} ({c['freq']/1e6:.1f} MHz), FCCH only",
                                  crc_ok=None, **base))
            continue
        si = c.get("si", {})
        bsic = c["bsic"]
        f = dict(base, bsic=bsic, ncc=bsic >> 3, bcc=bsic & 7, fn=c["fn"], sch_ok=len(c["sch"]),
                 si_seen=si.get("si_seen", []))
        if "mcc" in si:
            op = NL_OPERATORS.get((si["mcc"], si["mnc"]), "")
            f.update(mcc=si["mcc"], mnc=si["mnc"], lac=si["lac"], operator=op)
            if "ci" in si:
                f["ci"] = si["ci"]
            cid = f"GSM:{si['mcc']}-{si['mnc']:02d}-{si['lac']}-{si.get('ci', '?')}"
            label = (f"GSM cell {si['mcc']}-{si['mnc']:02d} {op} LAC {si['lac']}"
                     + (f" CI {si['ci']}" if "ci" in si else "")
                     + f" BSIC {bsic >> 3}/{bsic & 7} ARFCN {c['arfcn']}")
        else:
            cid = f"GSM:ARFCN{c['arfcn']}:BSIC{bsic}"
            label = f"GSM cell ARFCN {c['arfcn']} ({c['freq']/1e6:.1f} MHz) BSIC {bsic >> 3}/{bsic & 7}"
        results.append(result(NAME, "crc", c["freq"], cid, label, crc_ok=True, **f))
    return results
