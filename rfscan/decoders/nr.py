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
    i = int(np.argmax(tot))
    o = np.delete(tot, i)
    return float((tot[i] - o.mean()) / (o.std() + 1e-20)), i, n


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
    x_det = rec.iq(0, min(n_total, int(DET_SECONDS * fs)))
    ch = Channelizer(x_det, fs, (FS,))
    half_bw = USABLE * fs / 2 - 127 * scs / 2 - 0.1e6
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
    for (nid2, foff), lst in groups.items():
        y = chf.get(foff, FS)
        occ = _occ(y, num, nid2, [t for _, t in lst])
        if not occ:
            continue
        dcfo = _cfo(y, num, nid2, occ) if len(occ) else 0.0
        y = chf.get(foff + dcfo, FS)
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
        pwr = float(np.mean([np.mean(np.abs(y[p:p + num.nfft]) ** 2) for p in occ]))
        ppm_if_raster = (f_ref - f_g) / f_g * 1e6
        fields = dict(pci=pci, nid1=nid1, nid2=nid2, ss_ref_hz=float(f_ref), scs_khz=scs / 1e3,
                      gscn=g, gscn_ref_hz=float(f_g), offset_from_gscn_hz=float(f_ref - f_g),
                      ppm_if_on_raster=float(ppm_if_raster), nr_arfcn_ssb=nr_arfcn(f_ref), band=nr_band(f_ref),
                      sss_z=float(z), pss_rho=float(max(r for r, _ in lst)), occurrences=int(n_used),
                      ssb_indices=sorted(beams), pss_power_dbfs=float(10 * np.log10(pwr + 1e-20)))
        bstr = f" SSB idx {sorted(beams)}" if beams else ""
        label = (f"5G NR {nr_band(f_ref)} SSB {f_ref/1e6:.3f} MHz (GSCN {g}{'' if abs(ppm_if_raster) < 12 else ' off-raster'}) "
                 f"PCI {pci} SCS {scs/1e3:g} kHz{bstr} (PSS+SSS)")
        found[key] = result(NAME, "sync", f_ref, f"NR:PCI{pci}@{f_ref/1e6:.2f}", label, crc_ok=None,
                            t_s=occ[0] / FS, **fields)
    return sorted(found.values(), key=lambda r: (r["freq_hz"], r["fields"]["pci"]))
