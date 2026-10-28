"""LTE cell search: PSS -> SSS -> PCI, then PBCH -> MIB (CRC16 verified).

Works on a wideband recording (e.g. 20 Msps) that may hold several carriers.
Every 100 kHz raster point inside the usable bandwidth is channelised to
1.92 Msps (FFT-slice), and the three PSS roots are correlated for a set of
CFO hypotheses covering the HackRF crystal error.  Correlation is folded
modulo 5 ms so the whole capture contributes.  Detections are confirmed by SSS
(168 x 2 hypotheses, accumulated over all half-frames) and, where possible,
by decoding the MIB with its CRC/antenna mask.
"""
import numpy as np
import scipy.fft as sfft

from . import result
from ._ofdm_util import Channelizer, fold_max, gold, peaks, pmap, sliding_energy, template_spec

NAME = "lte"
CAPTURE = {"rate": 20e6, "seconds": 0.3}

FS = 1.92e6
NFFT = 128
HALF = 9600            # 5 ms at 1.92 Msps
DET_SECONDS = 0.1
PSS_THR = 0.05         # folded normalised correlation
SSS_Z = 6.0
USABLE = 0.75          # fraction of the capture bandwidth that is usable (HackRF BB filter)

# NL / CEPT downlink bands: (band, f_dl_low MHz, f_dl_high MHz, N_offs_DL, duplex)
BANDS = [
    (1, 2110.0, 2170.0, 0, "FDD"),
    (3, 1805.0, 1880.0, 1200, "FDD"),
    (7, 2620.0, 2690.0, 2750, "FDD"),
    (8, 925.0, 960.0, 3450, "FDD"),
    (20, 791.0, 821.0, 6150, "FDD"),
    (28, 758.0, 803.0, 9210, "FDD"),
    (32, 1452.0, 1496.0, 9920, "SDL"),
    (38, 2570.0, 2620.0, 37750, "TDD"),
    (69, 2570.0, 2620.0, 9660, "SDL"),
    (75, 1432.0, 1517.0, 69466, "SDL"),
]
N_RB_MIB = [6, 15, 25, 50, 75, 100]
PHICH_RES = ["1/6", "1/2", "1", "2"]


def earfcn(f_hz):
    f = f_hz / 1e6
    out = []
    for b, lo, hi, noff, dup in BANDS:
        if lo <= f < hi:
            out.append({"band": b, "earfcn": int(round(noff + (f - lo) / 0.1)), "duplex": dup})
    return out


# ---------------------------------------------------------------- sequences
def pss_freq(nid2):
    u = (25, 29, 34)[nid2]
    n = np.arange(62)
    d = np.where(n < 31, np.exp(-1j * np.pi * u * n * (n + 1) / 63),
                 np.exp(-1j * np.pi * u * (n + 1) * (n + 2) / 63))
    return d.astype(np.complex64)


def sc_bins(nfft=NFFT):
    """FFT bins for the 62 PSS/SSS subcarriers (-31..-1, +1..+31)."""
    k = np.concatenate([np.arange(-31, 0), np.arange(1, 32)])
    return k % nfft


def pss_time(nid2):
    X = np.zeros(NFFT, np.complex64)
    X[sc_bins()] = pss_freq(nid2)
    return sfft.ifft(X).astype(np.complex64)


def _mseq(taps, n=31):
    x = [0, 0, 0, 0, 1]
    for i in range(n - 5):
        x.append(sum(x[i + t] for t in taps) % 2)
    return 1 - 2 * np.array(x[:n])


_S = _mseq([0, 2])        # x(i+5) = x(i+2) + x(i)
_C = _mseq([0, 3])        # x(i+5) = x(i+3) + x(i)
_Z = _mseq([0, 1, 2, 4])  # x(i+5) = x(i+4)+x(i+2)+x(i+1)+x(i)


def sss_seq(nid1, nid2, sf5):
    qp = nid1 // 30
    q = (nid1 + qp * (qp + 1) // 2) // 30
    mp = nid1 + q * (q + 1) // 2
    m0 = mp % 31
    m1 = (m0 + mp // 31 + 1) % 31
    n = np.arange(31)
    s0, s1 = _S[(n + m0) % 31], _S[(n + m1) % 31]
    c0, c1 = _C[(n + nid2) % 31], _C[(n + nid2 + 3) % 31]
    z0, z1 = _Z[(n + (m0 % 8)) % 31], _Z[(n + (m1 % 8)) % 31]
    d = np.empty(62)
    if not sf5:
        d[0::2] = s0 * c0
        d[1::2] = s1 * c1 * z0
    else:
        d[0::2] = s1 * c0
        d[1::2] = s0 * c1 * z1
    return d


_SSS_TAB = {}


def sss_table(nid2):
    """(336, 62): rows 0..167 = subframe-0 form, 168..335 = subframe-5 form."""
    if nid2 not in _SSS_TAB:
        _SSS_TAB[nid2] = np.array([sss_seq(n1, nid2, sf) for sf in (0, 1) for n1 in range(168)],
                                  np.float32)
    return _SSS_TAB[nid2]


_PSS_T = [pss_time(i) for i in range(3)]
_PSS_E = [float(np.sum(np.abs(t) ** 2)) for t in _PSS_T]
_TSPEC = {}


def _tspecs(m):
    if m not in _TSPEC:
        _TSPEC[m] = [template_spec(t, m) for t in _PSS_T]
    return _TSPEC[m]


# ---------------------------------------------------------------- PSS search
def _pss_search(ch, f_off):
    Y, _ = ch.spectrum(f_off, FS)
    m = len(Y)
    y = sfft.ifft(Y, workers=1)
    E = sliding_energy(y, NFFT)
    out = []
    for r, T in enumerate(_tspecs(m)):
        c = sfft.ifft(Y * T, workers=1)
        rho = np.abs(c) ** 2 / (np.maximum(E, 1e-30) * _PSS_E[r])
        f, nper = fold_max(rho, HALF)
        med = float(np.median(f))
        for i in peaks(f, max(PSS_THR, 6 * med), 8, 3):
            out.append((float(f[i]), r, i, f_off, med))
    return out


def _nms(dets, df_min=1.0e6, dt=4):
    dets = sorted(dets, key=lambda d: -d[0])
    keep = []
    for d in dets:
        ok = True
        for k in keep:
            if d[1] == k[1] and abs(d[3] - k[3]) < df_min:
                dd = abs(d[2] - k[2]) % HALF
                if min(dd, HALF - dd) <= dt:
                    ok = False
                    break
        if ok:
            keep.append(d)
    return keep


# ---------------------------------------------------------------- per-cell processing
def _occurrences(y, nid2, t0):
    """Precise PSS starts near t0 + k*HALF (tracks sample-clock drift)."""
    t = _PSS_T[nid2]
    occ = []
    pos = t0
    while pos + NFFT + 3 < len(y):
        best, bi = -1, pos
        for d in range(-3, 4):
            p = pos + d
            if p < 600 or p + NFFT >= len(y):
                continue
            v = abs(np.vdot(t, y[p:p + NFFT]))
            if v > best:
                best, bi = v, p
        if pos >= 600:
            occ.append(bi)
        pos = bi + HALF
    return occ


def _cfo_fine(y, nid2, occ):
    t = _PSS_T[nid2]
    acc = 0j
    for p in occ:
        a1 = np.vdot(t[:64], y[p:p + 64])
        a2 = np.vdot(t[64:], y[p + 64:p + 128])
        acc += a2 * np.conj(a1)
    return float(np.angle(acc) / (2 * np.pi * 64 / FS))


SSS_MODES = {"FDD-normal": 137, "FDD-extended": 160, "TDD-normal": 412, "TDD-extended": 480}


def _sss(y, nid2, occ):
    tab = sss_table(nid2)
    bins = sc_bins()
    dpss = pss_freq(nid2)
    t_first = occ[0]
    best = None
    for mode, off in SSS_MODES.items():
        tot = np.zeros(336)
        n_used = 0
        for p in occ:
            s0 = p - off
            if s0 < 0:
                continue
            P = sfft.fft(y[p:p + NFFT])[bins]
            S = sfft.fft(y[s0:s0 + NFFT])[bins]
            H = P * np.conj(dpss)
            eq = np.real(S * np.conj(H))
            sc = tab @ eq
            idx = int(round((p - t_first) / HALF)) % 2
            if idx:
                sc = np.concatenate([sc[168:], sc[:168]])
            tot += sc / (np.sqrt(np.sum(np.abs(H) ** 2) * np.sum(np.abs(S) ** 2)) + 1e-20)
            n_used += 1
        if n_used == 0:
            continue
        i = int(np.argmax(tot))
        others = np.delete(tot, i)
        z = (tot[i] - others.mean()) / (others.std() + 1e-20)
        if best is None or z > best[0]:
            # i < 168: first occurrence is subframe 0
            best = (float(z), mode, i % 168, i >= 168, n_used)
    return best


# ---------------------------------------------------------------- MIB / PBCH
_PERM = [1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31,
         0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30]


def _ratematch_map(D=40, E=1920):
    R = -(-D // 32)
    nd = R * 32 - D
    w = []
    for s in range(3):
        y = [None] * nd + [s * D + k for k in range(D)]
        for k in range(R * 32):
            w.append(y[(k % R) * 32 + _PERM[k // R]])
    w = [v for v in w if v is not None]
    return np.array([w[j % len(w)] for j in range(E)])


_RM = _ratematch_map()
_G = [0o133, 0o171, 0o165]


def _conv_out_table():
    """out[state, bit] -> 3 output bits; state = last 6 inputs (bit0 = newest)."""
    out = np.zeros((64, 2, 3), np.int8)
    for st in range(64):
        for b in range(2):
            reg = (st << 1) | b   # bit0 = c_k, bit j = c_{k-j}
            for i, g in enumerate(_G):
                taps = [(g >> (6 - j)) & 1 for j in range(7)]
                out[st, b, i] = sum(taps[j] * ((reg >> j) & 1) for j in range(7)) % 2
    return out


_COUT = _conv_out_table()
_SIGN = (1 - 2 * _COUT).astype(np.float32)   # (64,2,3)


def conv_encode_tb(bits):
    st = 0
    for b in bits[-6:]:
        st = ((st << 1) | int(b)) & 63
    out = []
    for b in bits:
        out.append(_COUT[st, int(b)])
        st = ((st << 1) | int(b)) & 63
    return np.array(out).T.reshape(-1)   # stream-major d0..., d1..., d2...


_NS = np.arange(64)
_PA = _NS >> 1
_PB = _PA | 32
_BB = _NS & 1


def viterbi_tb(llr, K=40, reps=3):
    """llr: (3, K) soft values, positive => bit 0.  Tail-biting via wrap-around."""
    L = np.tile(llr, (1, reps))
    n = L.shape[1]
    metric = np.zeros(64)
    prev = np.zeros((n, 64), np.int16)
    for k in range(n):
        bm = _SIGN @ L[:, k]                       # (64,2): from state s with input b
        ca = metric[_PA] + bm[_PA, _BB]
        cb = metric[_PB] + bm[_PB, _BB]
        take_b = cb > ca
        new = np.where(take_b, cb, ca)
        prev[k] = np.where(take_b, _PB, _PA)
        metric = new - new.max()
    st = int(np.argmax(metric))
    bits = np.zeros(n, np.uint8)
    for k in range(n - 1, -1, -1):
        bits[k] = st & 1
        st = prev[k, st]
    return bits[K:2 * K] if reps >= 3 else bits[:K]


def crc16(bits):
    reg = 0
    for b in bits:
        top = ((reg >> 15) & 1) ^ int(b)
        reg = (reg << 1) & 0xFFFF
        if top:
            reg ^= 0x1021
    return np.array([(reg >> (15 - i)) & 1 for i in range(16)], np.uint8)


_MASK = {1: np.zeros(16, np.uint8), 2: np.ones(16, np.uint8), 4: np.tile([0, 1], 8).astype(np.uint8)}


def _crs(ns, l, pci, port):
    """CRS (k' positions in the central 72, complex values) for slot ns, symbol l."""
    c = gold(1024 * (7 * (ns + 1) + l + 1) * (2 * pci + 1) + 2 * pci + 1, 4 * 110)
    r = ((1 - 2 * c[0::2].astype(np.float32)) + 1j * (1 - 2 * c[1::2].astype(np.float32))) / np.sqrt(2)
    if port in (0, 1):
        v = (0 if l == 0 else 3) if port == 0 else (3 if l == 0 else 0)
    else:
        v = 3 * (ns % 2) if port == 2 else 3 + 3 * (ns % 2)
    j = np.arange(12)
    k = 6 * j + (v + pci % 6) % 6
    return k, r[104 + j]


def _grid72(y, start, backoff=2):
    """FFT of the symbol whose useful part starts at `start`; returns 72 central subcarriers."""
    s = start - backoff
    if s < 0 or s + NFFT > len(y):
        return None
    X = sfft.fft(y[s:s + NFFT])
    q = np.concatenate([np.arange(-36, 0), np.arange(1, 37)])
    ph = np.exp(-2j * np.pi * q * backoff / NFFT)   # undo backoff ramp (keeps CRS phase consistent)
    return X[q % NFFT] * ph


def _sym_start(slot0, ns, l):
    base = slot0 + ns * 960
    return base + (10 if l == 0 else 147 + (l - 1) * 137)


def _chan_est(y, slot0, pci):
    """Per-port channel on the 72 central subcarriers for PBCH symbols (slot 1, l=0..3)."""
    kk = np.arange(72)

    def est(ns, l, port):
        g = _grid72(y, _sym_start(slot0, ns, l))
        if g is None:
            return None
        k, r = _crs(ns, l, pci, port)
        h = g[k] * np.conj(r)
        return np.interp(kk, k, h.real) + 1j * np.interp(kk, k, h.imag)

    H = {}
    for p in (0, 1):
        a = est(1, 0, p)
        b = est(1, 4, p)
        if a is None or b is None:
            return None
        H[p] = [a + (b - a) * l / 4.0 for l in range(4)]
    for p in (2, 3):
        a = est(1, 1, p)
        b = est(0, 1, p)
        if a is None:
            return None
        h = a if b is None else 0.5 * (a + b)
        H[p] = [h] * 4
    return H


def _pbch_re(pci):
    vs = pci % 3
    res = []
    for l in range(4):
        for k in range(72):
            if l < 2 and k % 3 == vs:
                continue
            res.append((l, k))
    return res   # 240


def _pbch_llr(y, slot0, pci, nports):
    H = _chan_est(y, slot0, pci)
    if H is None:
        return None
    grids = [_grid72(y, _sym_start(slot0, 1, l)) for l in range(4)]
    if any(g is None for g in grids):
        return None
    res = _pbch_re(pci)
    r = np.array([grids[l][k] for l, k in res])
    h = {p: np.array([H[p][l][k] for l, k in res]) for p in range(4)}
    x = np.zeros(240, np.complex64)
    if nports == 1:
        x = r * np.conj(h[0])
    else:
        for i in range(0, 240, 2):
            if nports == 2:
                pa, pb = 0, 1
            else:
                pa, pb = (0, 2) if (i % 4) == 0 else (1, 3)
            h0 = 0.5 * (h[pa][i] + h[pa][i + 1])
            h1 = 0.5 * (h[pb][i] + h[pb][i + 1])
            r0, r1 = r[i], r[i + 1]
            x[i] = np.conj(h0) * r0 + h1 * np.conj(r1)
            x[i + 1] = np.conj(h0) * r1 - h1 * np.conj(r0)
    llr = np.empty(480, np.float32)
    llr[0::2] = x.real
    llr[1::2] = x.imag
    return llr / (np.std(llr) + 1e-20)


_SCR = {}


def _scramble(pci):
    if pci not in _SCR:
        _SCR[pci] = (1 - 2 * gold(pci, 1920).astype(np.float32))
    return _SCR[pci]


def _try_mib(llr480_list, pci, nports):
    """llr480_list: list of (frame_index, llr) for consecutive frames. Returns MIB dict or None."""
    scr = _scramble(pci)
    for fi, llr in llr480_list:
        for seg in range(4):
            soft = llr * scr[480 * seg:480 * seg + 480]
            acc = np.zeros(120, np.float32)
            np.add.at(acc, _RM[480 * seg:480 * seg + 480], soft)
            bits = viterbi_tb(acc.reshape(3, 40))
            a, p = bits[:24], bits[24:]
            if np.array_equal(crc16(a) ^ _MASK[nports], p):
                return _parse_mib(a, seg, fi)
    return None


def _parse_mib(a, seg, fi):
    v = lambda s, n: int("".join(str(int(b)) for b in a[s:s + n]), 2)
    bwi = v(0, 3)
    return {"n_rb": N_RB_MIB[bwi] if bwi < 6 else None,
            "bw_mhz": {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}.get(N_RB_MIB[bwi] if bwi < 6 else 0),
            "phich_duration": "extended" if a[3] else "normal",
            "phich_resource": PHICH_RES[v(4, 2)],
            "sfn": (v(6, 8) << 2) | seg, "frame_index": fi,
            "mib_hex": f"{v(0, 24):06x}"}


def _decode_mib(y, occ, sf0_first, mode, pci):
    if mode != "FDD-normal" and mode != "TDD-normal":
        return None
    # subframe-0 PSS occurrences
    t0 = occ[0]
    sf0 = [p for p in occ if (int(round((p - t0) / HALF)) % 2 == 0) == sf0_first]
    frames = []
    for p in sf0:
        if mode == "FDD-normal":
            slot0 = p - 832
        else:
            # PSS in subframe 1 (DwPTS sym 2); SSS in slot 1 sym 6 of subframe 0, 412 samples earlier
            slot0 = (p - 412) - 832 - 960
        if slot0 < 0 or slot0 + 1920 > len(y):
            continue
        frames.append(slot0)
    for nports in (1, 2, 4):
        lst = []
        for fi, s0 in enumerate(frames[:12]):
            llr = _pbch_llr(y, s0, pci, nports)
            if llr is not None:
                lst.append((fi, llr))
        mib = _try_mib(lst[:6], pci, nports)
        if mib:
            mib["tx_ports"] = nports
            # count how many frames decode (consistency)
            ok = 0
            for fi, llr in lst:
                if _try_mib([(fi, llr)], pci, nports):
                    ok += 1
            mib["frames_crc_ok"] = ok
            mib["frames_tried"] = len(lst)
            return mib
    return None


# ---------------------------------------------------------------- main entry
def decode(rec, ctx=None):
    ctx = ctx or {}
    fs = rec.rate
    if fs < FS:
        return []
    n_total = rec.n_samples()
    x_det = rec.iq(0, min(n_total, int(DET_SECONDS * fs)))
    ch = Channelizer(x_det, fs, (FS,))
    fc = rec.center_hz
    half_bw = USABLE * fs / 2 - 0.5e6
    lo = np.ceil((fc - half_bw) / 100e3) * 100e3
    hi = np.floor((fc + half_bw) / 100e3) * 100e3
    raster = np.arange(lo, hi + 1, 100e3)
    ppm_max = float(ctx.get("ppm_max", 10.0))
    ppm_hint = ctx.get("ppm")
    if ppm_hint is not None:
        cfos = [float(ppm_hint) * fc * 1e-6 + d for d in (-2.5e3, 0, 2.5e3)]
    else:
        span = ppm_max * 1e-6 * fc
        nh = int(np.ceil(span / 5e3))
        cfos = [5e3 * i for i in range(-nh, nh + 1)]
    jobs = [(f - fc + c, f, c) for f in raster for c in cfos]
    dets = []
    for (off, f, c), res in zip(jobs, pmap(lambda j: _pss_search(ch, j[0]), jobs, int(ctx.get("threads", 24)))):
        for rho, r, i, foff, med in res:
            dets.append((rho, r, i, foff, f, c, med))
    dets = _nms(dets)
    if not dets:
        return []
    # refine & confirm on the full capture
    x = rec.iq()
    chf = Channelizer(x, fs, (FS,))
    found = {}
    for rho, nid2, t0, foff, f_raster, cfo, med in dets[:12]:
        y = chf.get(foff, FS)
        occ = _occurrences(y, nid2, t0)
        if len(occ) < 2:
            continue
        dcfo = _cfo_fine(y, nid2, occ)
        y = chf.get(foff + dcfo, FS)
        occ = _occurrences(y, nid2, occ[0])
        sss = _sss(y, nid2, occ)
        if sss is None or sss[0] < SSS_Z:
            continue
        z, mode, nid1, sf5_first, n_used = sss
        pci = 3 * nid1 + nid2
        f_meas = fc + foff + dcfo
        key = (pci, int(round(f_raster / 1e5)))
        if key in found:
            continue
        pwr = float(np.mean([np.mean(np.abs(y[p:p + NFFT]) ** 2) for p in occ]))
        mib = None
        try:
            mib = _decode_mib(y, occ, not sf5_first, mode, pci)
        except Exception as e:  # never let MIB decoding kill cell detection
            mib = None
        ppm = (f_meas - f_raster) / f_raster * 1e6
        bands = earfcn(f_raster)
        b = ",".join(f"B{e['band']}/{e['earfcn']}" for e in bands) or "?"
        duplex = mode.split("-")[0]
        cp = mode.split("-")[1]
        fields = dict(pci=pci, nid1=nid1, nid2=nid2, carrier_hz=float(f_raster), measured_hz=float(f_meas),
                      cfo_hz=float(f_meas - f_raster), ppm=float(ppm), earfcn=bands, duplex=duplex, cp=cp,
                      pss_rho=float(rho), sss_z=float(z), half_frames=int(n_used),
                      pss_power_dbfs=float(10 * np.log10(pwr + 1e-20)))
        if mib:
            fields["mib"] = mib
            label = (f"LTE {f_raster/1e6:.1f} MHz {b} PCI {pci} {duplex} {mib['bw_mhz']} MHz BW, "
                     f"{mib['tx_ports']} ports, SFN {mib['sfn']} (MIB CRC ok)")
            found[key] = result(NAME, "crc", f_raster, f"LTE:PCI{pci}@{f_raster/1e6:.1f}", label,
                                crc_ok=True, t_s=occ[0] / FS, **fields)
        else:
            label = f"LTE {f_raster/1e6:.1f} MHz {b} PCI {pci} {duplex}/{cp} CP (PSS+SSS)"
            found[key] = result(NAME, "sync", f_raster, f"LTE:PCI{pci}@{f_raster/1e6:.1f}", label,
                                crc_ok=None, t_s=occ[0] / FS, **fields)
    return sorted(found.values(), key=lambda r: (r["freq_hz"], r["fields"]["pci"]))
