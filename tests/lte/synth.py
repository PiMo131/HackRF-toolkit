"""Synthetic LTE FDD (6 RB) generator for self-testing rfscan.decoders.lte."""
import numpy as np

from rfscan import recording
from rfscan.decoders import lte
from rfscan.decoders._ofdm_util import gold

Q = np.concatenate([np.arange(-36, 0), np.arange(1, 37)])   # k' -> subcarrier


def ofdm_frame(grids_by_port, hs):
    """grids_by_port[p][ns][l] (72 complex) -> time samples at 1.92 Msps, summed over ports with gain h."""
    out = []
    for ns in range(20):
        for l in range(7):
            X = np.zeros(128, complex)
            for p, g in enumerate(grids_by_port):
                X[Q % 128] += hs[p] * g[ns][l]
            t = np.fft.ifft(X) * np.sqrt(128)
            cp = 10 if l == 0 else 9
            out.append(np.concatenate([t[-cp:], t]))
    return np.concatenate(out)


def make(pci=123, nports=2, sfn0=100, n_frames=4, mib_bits=None, rng=None):
    rng = rng or np.random.default_rng(0)
    nid1, nid2 = divmod(pci, 3)
    sig = []
    for fr in range(n_frames):
        sfn = sfn0 + fr
        grids = [[[None] * 7 for _ in range(20)] for _ in range(nports)]
        for p in range(nports):
            for ns in range(20):
                for l in range(7):
                    grids[p][ns][l] = ((rng.integers(0, 2, 72) * 2 - 1) + 1j * (rng.integers(0, 2, 72) * 2 - 1)) / np.sqrt(2) / np.sqrt(nports)
        # CRS: all ports' positions zeroed on every port first, then insert own
        for ns in range(20):
            for l in (0, 1, 4):
                for p in range(4):
                    if l == 1 and p < 2 or l in (0, 4) and p >= 2:
                        continue
                    if p >= 4:
                        continue
                    k, r = lte._crs(ns, l, pci, p)
                    for q in range(nports):
                        grids[q][ns][l][k] = 0
                    if p < nports:
                        grids[p][ns][l][k] = r
        # PSS / SSS (port 0 only, others zero)
        for ns, sf5 in ((0, 0), (10, 1)):
            for q in range(nports):
                grids[q][ns][6][:] = 0
                grids[q][ns][5][:] = 0
            kk = np.concatenate([np.arange(5, 36), np.arange(36, 67)])
            grids[0][ns][6][kk] = lte.pss_freq(nid2)
            grids[0][ns][5][kk] = lte.sss_seq(nid1, nid2, sf5)
        # PBCH
        if mib_bits is None:
            a = np.zeros(24, np.uint8)
            bw = 2  # n25
            a[0:3] = [(bw >> 2) & 1, (bw >> 1) & 1, bw & 1]
            a[4:6] = [0, 1]
            s8 = sfn >> 2
            a[6:14] = [(s8 >> (7 - i)) & 1 for i in range(8)]
        else:
            a = mib_bits
        cw = np.concatenate([a, lte.crc16(a) ^ lte._MASK[nports]])
        e = lte.conv_encode_tb(cw)[lte._RM]
        e = e ^ gold(pci, 1920)
        seg = sfn % 4
        b = e[480 * seg:480 * seg + 480]
        d = ((1 - 2 * b[0::2]) + 1j * (1 - 2 * b[1::2])) / np.sqrt(2)
        res = lte._pbch_re(pci)
        for i, (l, k) in enumerate(res):
            for q in range(nports):
                grids[q][1][l][k] = 0
        if nports == 1:
            for i, (l, k) in enumerate(res):
                grids[0][1][l][k] = d[i]
        else:
            for i in range(0, 240, 2):
                x0, x1 = d[i], d[i + 1]
                (l0, k0), (l1, k1) = res[i], res[i + 1]
                grids[0][1][l0][k0] = x0 / np.sqrt(2)
                grids[1][1][l0][k0] = -np.conj(x1) / np.sqrt(2)
                grids[0][1][l1][k1] = x1 / np.sqrt(2)
                grids[1][1][l1][k1] = np.conj(x0) / np.sqrt(2)
        hs = [1.0, 0.7 * np.exp(1j * 1.1), 0.5j, 0.4][:nports]
        sig.append(ofdm_frame(grids, hs))
    return np.concatenate(sig)


def to_wideband(x192, fs=20e6, f_off=3.3e6, cfo=12e3, snr_db=10, rng=None):
    rng = rng or np.random.default_rng(1)
    n = len(x192)
    X = np.fft.fft(x192)
    N = int(round(n * fs / 1.92e6))
    Y = np.zeros(N, complex)
    half = n // 2
    s = int(round(f_off / (fs / N)))
    Y[(np.arange(0, half) + s) % N] = X[:half]
    Y[(np.arange(-half, 0) + s) % N] = X[half:]
    y = np.fft.ifft(Y) * N / n
    y *= np.exp(2j * np.pi * cfo * np.arange(N) / fs)
    p = np.mean(np.abs(y) ** 2)
    nv = p / 10 ** (snr_db / 10) * fs / 1.92e6     # SNR defined in the 1.92 MHz band
    y = y + (rng.normal(size=N) + 1j * rng.normal(size=N)) * np.sqrt(nv / 2)
    return (y / np.max(np.abs(y)) * 0.7).astype(np.complex64)


if __name__ == "__main__":
    import sys, time
    nports = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    snr = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    x = make(pci=301, nports=nports, n_frames=12)
    w = to_wideband(x, snr_db=snr)
    rec = recording.write("tests/lte/synth", 1815e6, 20e6, w, "ci16_le", "synthetic LTE")
    t = time.time()
    for r in lte.decode(rec, {}):
        print(r["level"], r["label"], {k: r["fields"][k] for k in ("cfo_hz", "sss_z", "pss_rho")})
    print("time", time.time() - t)
