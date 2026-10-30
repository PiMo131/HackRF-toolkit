"""5G NR SSB search: PSS -> SSS -> PCI, SSB frequency / GSCN, PBCH-DMRS -> SSB index.

The SSB frequency is searched on a fine grid (NSA cells need not sit on the
GSCN sync raster), the capture is channelised to 3.84 Msps around each grid
point and the 3 PSS m-sequences are correlated.  Correlation is folded modulo
20 ms (default SSB periodicity) so all bursts in the capture contribute.  SSS
(336 hypotheses per N_ID2) accumulated over all PSS occurrences confirms the
cell.  PBCH DMRS (8 hypotheses) gives the SSB/beam index of each occurrence.
"""
import numpy as np
import scipy.fft as sfft

from . import result
from ._ofdm_util import Channelizer, fold_max, gold, peaks, pmap, sliding_energy, template_spec

NAME = "nr"
CAPTURE = {"rate": 20e6, "seconds": 0.1}

FS = 3.84e6
FS_WIDE = 7.68e6       # for PBCH DMRS (240 subcarriers)
DET_SECONDS = 0.06
PSS_THR = 0.08
SSS_Z = 6.0
USABLE = 0.75
PERIOD_S = 0.020


def _mseq(taps, init, n=127):
    x = list(init)
    for i in range(n - 7):
        x.append(sum(x[i + t] for t in taps) % 2)
    return np.array(x[:n])


_XP = _mseq([0, 4], [0, 1, 1, 0, 1, 1, 1])          # x(i+7) = x(i+4) + x(i)
_X0 = _mseq([0, 4], [1, 0, 0, 0, 0, 0, 0])
_X1 = _mseq([0, 1], [1, 0, 0, 0, 0, 0, 0])          # x(i+7) = x(i+1) + x(i)


def pss_seq(nid2):
    n = np.arange(127)
    return (1 - 2 * _XP[(n + 43 * nid2) % 127]).astype(np.float32)


def sss_seq(nid1, nid2):
    n = np.arange(127)
    m0 = 15 * (nid1 // 112) + 5 * nid2
    m1 = nid1 % 112
    return ((1 - 2 * _X0[(n + m0) % 127]) * (1 - 2 * _X1[(n + m1) % 127])).astype(np.float32)


_SSS_TAB = {}


def sss_table(nid2):
    if nid2 not in _SSS_TAB:
        _SSS_TAB[nid2] = np.array([sss_seq(n1, nid2) for n1 in range(336)], np.float32)
    return _SSS_TAB[nid2]


def _bins(nfft):
    """FFT bins for PSS/SSS subcarriers n=0..126 -> offset n-64 from SS_REF."""
    return (np.arange(127) - 64) % nfft


class _Num:
    def __init__(self, scs, fs=FS):
        self.scs = scs
        self.fs = fs
        self.nfft = int(round(fs / scs))
        self.cp = int(round(self.nfft * 144 / 2048))
        self.sym = self.nfft + self.cp
        self.period = int(round(PERIOD_S * fs))
        self.tmpl = []
        for r in range(3):
            X = np.zeros(self.nfft, np.complex64)
            X[_bins(self.nfft)] = pss_seq(r)
            self.tmpl.append(sfft.ifft(X).astype(np.complex64))
        self.tE = [float(np.sum(np.abs(t) ** 2)) for t in self.tmpl]
        self._ts = {}

    def tspec(self, m):
        if m not in self._ts:
            self._ts[m] = [template_spec(t, m) for t in self.tmpl]
        return self._ts[m]


# ---------------------------------------------------------------- raster helpers
def gscn_of(f_hz):
    """Nearest GSCN and its SS_REF (FR1)."""
    f = f_hz / 1e6
    if f >= 3000:
        n = int(round((f - 3000) / 1.44))
        return 7499 + n, (3000 + n * 1.44) * 1e6
    best = None
    for M in (1, 3, 5):
        n = int(round((f - M * 0.05) / 1.2))
        ref = n * 1.2 + M * 0.05
        g = 3 * n + (M - 3) // 2
        if best is None or abs(ref - f) < abs(best[1] / 1e6 - f):
            best = (g, ref * 1e6)
    return best


def nr_arfcn(f_hz):
    f = f_hz / 1e6
    if f >= 3000:
        return int(round(600000 + (f - 3000) / 0.015))
    return int(round(f / 0.005))


def nr_band(f_hz):
    f = f_hz / 1e6
    for name, lo, hi in (("n78", 3300, 3800), ("n77", 3300, 4200), ("n1", 2110, 2170), ("n3", 1805, 1880),
                         ("n7", 2620, 2690), ("n8", 925, 960), ("n20", 791, 821), ("n28", 758, 803),
                         ("n38", 2570, 2620), ("n75", 1432, 1517)):
        if lo <= f < hi:
            return name
    return "?"


# ---------------------------------------------------------------- search
def _search(ch, num, f_off):
    Y, _ = ch.spectrum(f_off, num.fs)
    m = len(Y)
    y = sfft.ifft(Y, workers=1)
    E = sliding_energy(y, num.nfft)
    out = []
    for r, T in enumerate(num.tspec(m)):
        c = sfft.ifft(Y * T, workers=1)
        rho = np.abs(c) ** 2 / (np.maximum(E, 1e-30) * num.tE[r])
        if len(rho) >= 2 * num.period:
            f, _ = fold_max(rho, num.period)
        else:
            f = rho
        med = float(np.median(f))
        for i in peaks(f, max(PSS_THR, 8 * med), num.nfft, 8):
            out.append((float(f[i]), r, i, f_off))
    return out


def _nms(dets, period, df_min=0.8e6):
    dets = sorted(dets, key=lambda d: -d[0])
    keep = []
    for d in dets:
        ok = True
        for k in keep:
            if d[1] == k[1] and abs(d[3] - k[3]) < df_min:
                dd = abs(d[2] - k[2]) % period
                if min(dd, period - dd) <= 4:
                    ok = False
                    break
        if ok:
            keep.append(d)
    return keep


def _occ(y, num, nid2, t0s):
    t = num.tmpl[nid2]
    occ = []
    for t0 in t0s:
        pos = t0
        while pos + num.nfft + 4 * num.sym < len(y):
            best, bi = -1, pos
            for d in range(-3, 4):
                p = pos + d
                if p < 0:
                    continue
                v = abs(np.vdot(t, y[p:p + num.nfft])) ** 2 / (np.sum(np.abs(y[p:p + num.nfft]) ** 2) + 1e-20)
                if v > best:
                    best, bi = v, p
            if best / num.tE[nid2] > 0.5 * PSS_THR:
                occ.append(bi)
            pos = bi + num.period
    return sorted(occ)


def _cfo(y, num, nid2, occ):
    t = num.tmpl[nid2]
    h = num.nfft // 2
    acc = 0j
    for p in occ:
        a1 = np.vdot(t[:h], y[p:p + h])
        a2 = np.vdot(t[h:], y[p + h:p + num.nfft])
        acc += a2 * np.conj(a1)
    return float(np.angle(acc) / (2 * np.pi * h / num.fs))


def _sss(y, num, nid2, occ):
    r = _sss_scores(y, num, nid2, occ)
    if r is None:
        return None
    tot, n = r
    i = int(np.argmax(tot))
    o = np.delete(tot, i)
    return float((tot[i] - o.mean()) / (o.std() + 1e-20)), i, n


def _sss_scores(y, num, nid2, occ):
    """Accumulated SSS correlation score for all 336 N_ID1 hypotheses (and the number of occurrences used)."""
    tab = sss_table(nid2)
    b = _bins(num.nfft)
    d = pss_seq(nid2)
    off = 2 * num.sym
    tot = np.zeros(336)
    n = 0
    for p in occ:
        s0 = p + off
        if s0 + num.nfft > len(y):
            continue
        P = sfft.fft(y[p:p + num.nfft])[b]
        S = sfft.fft(y[s0:s0 + num.nfft])[b]
        H = P * d
        eq = np.real(S * np.conj(H))
        tot += (tab @ eq) / (np.sqrt(np.sum(np.abs(H) ** 2) * np.sum(np.abs(S) ** 2)) + 1e-20)
        n += 1
    if n == 0:
        return None
    return tot, n


# ---------------------------------------------------------------- PBCH DMRS -> SSB index
def _dmrs_res(pci):
    """(l, k) of PBCH DMRS within the SSB (l=1..3, k=0..239), in mapping order."""
    v = pci % 4
    res = []
    for l in (1, 2, 3):
        for k in range(240):
            if k % 4 != v:
                continue
            if l == 2 and 48 <= k < 192:
                continue
            res.append((l, k))
    return res   # 144


def _dmrs_seq(pci, issb_bar):
    c = gold(2 ** 11 * (issb_bar + 1) * (pci // 4 + 1) + 2 ** 6 * (issb_bar + 1) + (pci % 4), 288)
    return ((1 - 2 * c[0::2].astype(np.float32)) + 1j * (1 - 2 * c[1::2].astype(np.float32))) / np.sqrt(2)


def _ssb_index(yw, numw, pci, p_wide):
    """Non-coherent DMRS detection (coherent over 3 DMRS REs) for the 8 i_SSB_bar hypotheses."""
    res = _dmrs_res(pci)
    grids = {}
    for l in (1, 2, 3):
        s = p_wide + l * numw.sym
        if s + numw.nfft > len(yw):
            return None, 0.0
        X = sfft.fft(yw[s:s + numw.nfft])
        k = np.arange(240) - 120
        grids[l] = X[k % numw.nfft]
    r = np.array([grids[l][k] for l, k in res])
    scores = []
    for i in range(8):
        z = r * np.conj(_dmrs_seq(pci, i))
        g = z[: (len(z) // 3) * 3].reshape(-1, 3).sum(axis=1)
        scores.append(float(np.sum(np.abs(g) ** 2)))
    scores = np.array(scores)
    i = int(np.argmax(scores))
    o = np.delete(scores, i)
    return i, float(scores[i] / (o.mean() + 1e-20))


# ---------------------------------------------------------------- SSB timing (for TDOA, see rfscan/tdoa_nr.py)
HALF_FRAME_S = 5e-3


def ssb_case(scs, fc_hz=None):
    """SSB pattern case (TS 38.213 4.1) for the SCS used in NL/CEPT bands."""
    if scs >= 30e3:
        return "C"          # n77/n78 (case B is only used in n5/n66)
    return "A"


def ssb_start_symbols(case="C", fc_hz=3.5e9):
    """First OFDM symbol (index within the half-frame) of every SSB candidate, TS 38.213 4.1.
    Cases A and C: {2, 8} + 14 n, n = 0, 1 at or below 3 GHz and n = 0..3 above (L_max 4 / 8).
    Case B: {4, 8, 16, 20} + 28 n, n = 0 / n = 0, 1."""
    big = fc_hz > 3e9
    if case == "B":
        return [b + 28 * n for n in range(2 if big else 1) for b in (4, 8, 16, 20)]
    return [b + 14 * n for n in range(4 if big else 2) for b in (2, 8)]


def symbol_useful_start_s(sym, scs=30e3):
    """Time from the half-frame start to the start of the useful part (after the CP) of OFDM symbol `sym`
    (index within the half-frame), normal CP.  TS 38.211 5.3.1: N_CP = 144k*2^-mu, plus 16k on the first
    symbol of every 0.5 ms (symbol 0 and 7*2^mu)."""
    mu = int(round(np.log2(scs / 15e3)))
    tc = 1.0 / (480e3 * 4096)
    kappa = 64
    n_u = 2048 * kappa * 2.0 ** -mu
    n_cp = 144 * kappa * 2.0 ** -mu
    per_half_ms = 7 * 2 ** mu
    t = 0.0
    for l in range(sym + 1):
        cp = n_cp + (16 * kappa if l % per_half_ms == 0 else 0)
        t += cp
        if l < sym:
            t += n_u
    return t * tc


def ssb_offset_s(issb, scs=30e3, case=None, fc_hz=3.5e9):
    """Time from the half-frame start to the useful part of the PSS of SSB candidate `issb`."""
    syms = ssb_start_symbols(case or ssb_case(scs), fc_hz)
    return symbol_useful_start_s(syms[issb], scs)


def _corr_mag2(y, tmpl, p):
    n = len(tmpl)
    if p < 0 or p + n > len(y):
        return 0.0
    return abs(np.vdot(tmpl, y[p:p + n])) ** 2


def _sss_time(num, pci):
    nid1, nid2 = divmod(pci, 3)
    X = np.zeros(num.nfft, np.complex64)
    X[_bins(num.nfft)] = sss_seq(nid1, nid2)
    return sfft.ifft(X).astype(np.complex64)


def fine_delay(Hs, ks, nfft, span=3.0, step=1 / 32):
    """Delay (samples, relative to the FFT window start) maximising the power-summed delay profile
    |sum_k H_k exp(j 2 pi k tau / nfft)|^2 of one or more channel estimates H (on subcarriers ks)."""
    tau = np.arange(-span, span + step / 2, step)
    E = np.exp(2j * np.pi * np.outer(ks, tau) / nfft)
    prof = np.zeros(len(tau))
    for H in Hs:
        prof += np.abs(np.asarray(H) @ E) ** 2
    i = int(np.argmax(prof))
    if 0 < i < len(tau) - 1:
        a, b, c = prof[i - 1], prof[i], prof[i + 1]
        den = a - 2 * b + c
        fr = 0.5 * (a - c) / den if den < 0 else 0.0
        return float(tau[i] + fr * step)
    return float(tau[i])


def refine_occurrence(y, num, p, pci, use_pss=True, search=2):
    """Sub-sample PSS/SSS timing of one SSB whose PSS useful part starts near integer sample p.

    Returns (t_parabola, t_freq, snr) in samples of y: the parabolic interpolation of the
    PSS(+SSS) correlation peak and the frequency-domain estimate (delay maximising the channel
    delay profile over the 127 PSS/SSS subcarriers, i.e. the phase slope fitted across the whole band).  use_pss=False uses the SSS only (to separate co-channel
    cells that share N_ID2 and hence the PSS)."""
    nid1, nid2 = divmod(pci, 3)
    off = 2 * num.sym
    ts = _sss_time(num, pci)
    tp = num.tmpl[nid2]

    def c(q):
        v = _corr_mag2(y, ts, q + off)
        if use_pss:
            v += _corr_mag2(y, tp, q)
        return v
    # integer peak within +-2
    vals = {q: c(q) for q in range(p - search, p + search + 1)}
    q0 = max(vals, key=vals.get)
    a, b, cc = c(q0 - 1), vals[q0], c(q0 + 1)
    den = a - 2 * b + cc
    frac = 0.5 * (a - cc) / den if den < 0 else 0.0
    t_par = q0 + float(np.clip(frac, -0.5, 0.5))
    # frequency-domain delay around q0 (window starting at q0): maximise |sum_k H_k e^{j 2 pi k tau / N}|
    # over a fine tau grid (ML delay of the dominant path over the full 3.8 MHz PSS/SSS bandwidth)
    bins = _bins(num.nfft)
    ks = np.arange(127) - 64
    Hs, sig, tot = [], 0.0, 0.0
    parts = [(off, sss_seq(nid1, nid2))]
    if use_pss:
        parts.append((0, pss_seq(nid2)))
    for o, seq in parts:
        s0 = q0 + o
        if s0 < 0 or s0 + num.nfft > len(y):
            continue
        Y = sfft.fft(y[s0:s0 + num.nfft])
        H = Y[bins] * seq
        Hs.append(H)
        sig += abs(np.sum(H)) ** 2 / len(H)
        tot += float(np.sum(np.abs(Y) ** 2))
    t_slope = q0 + fine_delay(Hs, ks, num.nfft) if Hs else t_par
    snr = sig / max(tot - sig, 1e-20) * num.nfft / 127
    return t_par, t_slope, float(snr)


def _period_ms(times, idx):
    """SSB periodicity (ms) from the spacing of occurrences of the same SSB index."""
    d = []
    by = {}
    for t, i in zip(times, idx):
        by.setdefault(i, []).append(t)
    for v in by.values():
        v = np.sort(v)
        d += list(np.diff(v))
    if not d:
        return None
    m = float(np.median(d)) * 1e3
    for p in (5, 10, 20, 40, 80, 160):
        if abs(m - p) < 1.0:
            return p
    return round(m, 2)


def ssb_timing(y, num, pci, occ, idx, fs=FS, scs=30e3, fc_hz=3.5e9, use_pss=True, method="slope", search=2):
    """Per-occurrence SSB arrival times and implied half-frame start times for one cell.

    y: channelised baseband (rate fs) centred on SS_REF; occ: integer PSS starts; idx: SSB index per
    occurrence (None where PBCH DMRS was inconclusive).  The SSB index fixes the SSB position within the
    half-frame (case C: symbols {2, 8} + 14 n); the half-frame bit is in the PBCH payload (MIB not
    decoded), so frame starts are only known modulo 5 ms (`frame_start_mod_s` is modulo 5 ms).
    Returns a dict with lists t_pss_s, t_hf_s (half-frame start = PSS time - SSB offset), issb, snr, plus
    a linear fit (drift of the receiver sample clock) and its residual std."""
    case = ssb_case(scs)
    nc = len(ssb_start_symbols(case, fc_hz))
    offs = [ssb_offset_s(i, scs, case, fc_hz) for i in range(nc)]
    t_pss, snrs, t_par = [], [], []
    for p in occ:
        tp, tsl, snr = refine_occurrence(y, num, p, pci, use_pss, search)
        t_pss.append((tsl if method == "slope" else tp) / fs)
        t_par.append(tp / fs)
        snrs.append(snr)
    t_pss = np.array(t_pss)
    idx = list(idx)
    known = [(t, i) for t, i in zip(t_pss, idx) if i is not None and i < nc]
    ambiguous = False
    if known:
        ref = np.array([t - offs[i] for t, i in known])
        # circular median modulo 5 ms
        ph = np.angle(np.mean(np.exp(2j * np.pi * ref / HALF_FRAME_S))) / (2 * np.pi) * HALF_FRAME_S
    else:
        ph = None
        ambiguous = True
    out_idx = []
    for k, (t, i) in enumerate(zip(t_pss, idx)):
        if i is None or i >= nc:
            if ph is None:
                i = 0     # unresolved: assume the first candidate (flagged)
            else:   # choose the candidate whose half-frame start matches the cell's other SSBs
                err = [abs(((t - offs[j] - ph + HALF_FRAME_S / 2) % HALF_FRAME_S) - HALF_FRAME_S / 2) for j in range(nc)]
                i = int(np.argmin(err))
        out_idx.append(int(i))
    t_hf = np.array([t - offs[i] for t, i in zip(t_pss, out_idx)])
    hf = np.round((t_hf - t_hf[0]) / HALF_FRAME_S).astype(int) if len(t_hf) else np.array([], int)
    res = dict(pci=int(pci), n=int(len(occ)), issb=out_idx, issb_from_dmrs=[i is not None for i in idx],
               t_pss_s=[float(v) for v in t_pss], t_pss_parabola_s=[float(v) for v in t_par],
               t_hf_s=[float(v) for v in t_hf], half_frame=[int(v) for v in hf], snr=[float(v) for v in snrs],
               index_ambiguous=ambiguous, period_ms=_period_ms(t_pss, out_idx), method=method)
    if len(t_hf) >= 3 and hf[-1] != hf[0]:
        A = np.vstack([np.ones(len(hf)), hf * HALF_FRAME_S]).T
        coef, *_ = np.linalg.lstsq(A, t_hf, rcond=None)
        r = t_hf - A @ coef
        res.update(fit_t0_s=float(coef[0]), drift_ppm=float((coef[1] - 1) * 1e6), resid_std_s=float(np.std(r)))
    elif len(t_hf):
        res.update(fit_t0_s=float(t_hf[0]), drift_ppm=0.0, resid_std_s=float(np.std(t_hf - t_hf[0] - hf * HALF_FRAME_S)))
    if len(t_hf):
        res["frame_start_mod_s"] = float(res["fit_t0_s"] % HALF_FRAME_S)
    return res


def cancel_cell(y, num, pci, occ, smooth=9):
    """Copy of y with the PSS and SSS (incl. CP) of cell `pci` at PSS starts `occ` subtracted.

    The channel is estimated per occurrence on the PSS and smoothed over `smooth` subcarriers (this
    suppresses the contribution of other, co-timed cells' PSS to the estimate), then the cell's PSS and
    SSS are re-synthesised and removed.  Synchronised cells transmit their SSBs at the same time, so
    weaker cells' SSS can only be detected after the strongest ones are cancelled."""
    y = np.array(y, np.complex64, copy=True)
    nid1, nid2 = divmod(pci, 3)
    b = _bins(num.nfft)
    d = pss_seq(nid2)
    sq = sss_seq(nid1, nid2)
    off = 2 * num.sym
    k = np.ones(smooth) / smooth
    for p in sorted(set(int(v) for v in occ)):
        if p - num.cp < 0 or p + off + num.nfft > len(y):
            continue
        H = sfft.fft(y[p:p + num.nfft])[b] * d
        Hp = np.convolve(np.pad(H, smooth // 2, mode="edge"), k, mode="valid")
        S = sfft.fft(y[p + off:p + off + num.nfft])[b] * sq
        Hs = np.convolve(np.pad(S, smooth // 2, mode="edge"), k, mode="valid")
        for o, seq, Hh in ((0, d, Hp), (off, sq, Hs)):
            X = np.zeros(num.nfft, np.complex64)
            X[b] = Hh * seq
            t = sfft.ifft(X)
            y[p + o - num.cp:p + o + num.nfft] -= np.concatenate([t[-num.cp:], t])
    return y


def _secondary_nid1(tot, primary, z_thr, max_n=3):
    """Further N_ID1 hypotheses with a significant SSS score (co-channel cells sharing N_ID2)."""
    out = []
    excl = [primary]
    for _ in range(max_n):
        o = np.delete(tot, excl)
        mu, sd = o.mean(), o.std() + 1e-20
        cand = [i for i in np.argsort(-tot) if i not in excl][0]
        z = (tot[cand] - mu) / sd
        if z < z_thr:
            break
        out.append((int(cand), float(z)))
        excl.append(int(cand))
    return out


# ---------------------------------------------------------------- entry
def decode(rec, ctx=None):
    ctx = ctx or {}
    fs = rec.rate
    if fs < FS:
        return []
    fc = rec.center_hz
    scs = float(ctx.get("scs", 30e3 if fc >= 3000e6 else 15e3))
    num = _Num(scs)
    n_total = rec.n_samples()
    x_det = rec.iq(0, min(n_total, int(float(ctx.get("det_seconds", DET_SECONDS)) * fs)))
    ch = Channelizer(x_det, fs, (FS,))
    half_bw = float(ctx.get("usable", USABLE)) * fs / 2 - 127 * scs / 2 - 0.1e6
    timing = bool(ctx.get("timing"))
    ppm = float(ctx.get("ppm_max", 10.0))

    def raster_cands(step):
        out = []
        f = fc - half_bw
        while f <= fc + half_bw:
            g, fg = gscn_of(f)
            if fc - half_bw <= fg <= fc + half_bw:
                span = ppm * 1e-6 * fg
                out += [fg - fc + d for d in np.arange(-span, span + 1, step)]
            f = fg + (1.44e6 if fc >= 3000e6 else 1.2e6) * 0.999
        return sorted(set(np.round(out, 1)))

    def run(cands):
        d = []
        for res in pmap(lambda o: _search(ch, num, o), cands, int(ctx.get("threads", 24))):
            d += res
        return d

    step = 10e3 if scs >= 30e3 else 7.5e3
    dets = run(raster_cands(step))
    full = ctx.get("nr_full_grid", fc >= 3000e6)   # NSA n77/n78 SSBs may sit off the sync raster
    if not dets and full and not ctx.get("nr_raster_only"):
        # NSA cells may place the SSB off the sync raster: fall back to a full grid
        dets = run(list(np.arange(-half_bw, half_bw + 1, step)))
    dets = _nms(dets, num.period)
    if not dets:
        return []
    x = rec.iq()
    chf = Channelizer(x, fs, (FS, FS_WIDE))
    numw = _Num(scs, FS_WIDE)
    # group detections of the same (root, freq) -> multiple beams
    groups = {}
    for rho, nid2, t0, foff in dets[:24]:
        key = None
        for k in groups:
            if k[0] == nid2 and abs(k[1] - foff) < 40e3:
                key = k
                break
        if key is None:
            key = (nid2, foff)
            groups[key] = []
        groups[key].append((rho, t0))
    found = {}
    cancel = []          # timing mode: cells found so far (f_ref, pci, occ) for SSS interference cancellation
    for (nid2, foff), lst in groups.items():
        y = chf.get(foff, FS)
        occ = _occ(y, num, nid2, [t for _, t in lst])
        if not occ:
            continue
        dcfo = _cfo(y, num, nid2, occ) if len(occ) else 0.0
        y = chf.get(foff + dcfo, FS)
        if timing:
            for f_c, pci_c, occ_c in cancel:
                if abs(f_c - (fc + foff + dcfo)) < 0.5e6:
                    y = cancel_cell(y, num, pci_c, occ_c)
        occ = _occ(y, num, nid2, sorted(set(occ)))
        s = _sss(y, num, nid2, occ)
        if s is None or s[0] < SSS_Z:
            continue
        z, nid1, n_used = s
        pci = 3 * nid1 + nid2
        f_ref = fc + foff + dcfo
        g, f_g = gscn_of(f_ref)
        key = (pci, int(round(f_ref / 50e3)))
        if key in found:
            continue
        # SSB index per occurrence (stretch)
        yw = chf.get(foff + dcfo, FS_WIDE)
        beams = {}
        occ_idx = []
        for p in occ:
            pw = int(round(p * FS_WIDE / FS))
            best, bp = -1, pw
            for dd in range(-2, 3):
                q = pw + dd
                if q < 0 or q + numw.nfft > len(yw):
                    continue
                v = abs(np.vdot(numw.tmpl[nid2], yw[q:q + numw.nfft]))
                if v > best:
                    best, bp = v, q
            i, ratio = _ssb_index(yw, numw, pci, bp)
            if i is not None and ratio > 1.8:
                beams.setdefault(i, 0)
                beams[i] += 1
            occ_idx.append(i if (i is not None and ratio > 1.8) else None)
        pwr = float(np.mean([np.mean(np.abs(y[p:p + num.nfft]) ** 2) for p in occ]))
        ppm_if_raster = (f_ref - f_g) / f_g * 1e6
        fields = dict(pci=pci, nid1=nid1, nid2=nid2, ss_ref_hz=float(f_ref), scs_khz=scs / 1e3,
                      gscn=g, gscn_ref_hz=float(f_g), offset_from_gscn_hz=float(f_ref - f_g),
                      ppm_if_on_raster=float(ppm_if_raster), nr_arfcn_ssb=nr_arfcn(f_ref), band=nr_band(f_ref),
                      sss_z=float(z), pss_rho=float(max(r for r, _ in lst)), occurrences=int(n_used),
                      ssb_indices=sorted(beams), pss_power_dbfs=float(10 * np.log10(pwr + 1e-20)))
        if timing:
            # opt-in (ctx["timing"]): sub-sample SSB arrival / half-frame start times for TDOA
            uo = {}
            for p_, i_ in zip(occ, occ_idx):
                uo.setdefault(p_, i_)
            uo = dict(sorted(uo.items()))
            fields["timing"] = ssb_timing(y, num, pci, list(uo), list(uo.values()), FS, scs, f_ref)
            cancel.append((f_ref, pci, list(uo)))
            tot, _ = _sss_scores(y, num, nid2, occ)
            sec = []
            for n1, z2 in _secondary_nid1(tot, nid1, SSS_Z):
                tm = ssb_timing(y, num, 3 * n1 + nid2, list(uo), list(uo.values()), FS, scs, f_ref, use_pss=False, search=8)
                tm["sss_z"] = z2
                sec.append(tm)
            fields["co_channel"] = sec
        bstr = f" SSB idx {sorted(beams)}" if beams else ""
        label = (f"5G NR {nr_band(f_ref)} SSB {f_ref/1e6:.3f} MHz (GSCN {g}{'' if abs(ppm_if_raster) < 12 else ' off-raster'}) "
                 f"PCI {pci} SCS {scs/1e3:g} kHz{bstr} (PSS+SSS)")
        found[key] = result(NAME, "sync", f_ref, f"NR:PCI{pci}@{f_ref/1e6:.2f}", label, crc_ok=None,
                            t_s=occ[0] / FS, **fields)
    return sorted(found.values(), key=lambda r: (r["freq_hz"], r["fields"]["pci"]))
