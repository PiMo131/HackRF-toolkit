"""Synthetic NR SSB burst (SCS 30 kHz, case C, 4 beams, 20 ms period) for testing rfscan.decoders.nr."""
import sys
import time

import numpy as np

from rfscan import recording
from rfscan.decoders import nr

FS0 = 7.68e6
NFFT = 256
CP = 18


def ssb_symbols(pci, issb, rng):
    nid1, nid2 = divmod(pci, 3)
    grid = np.zeros((4, 240), complex)
    k = np.arange(56, 183)
    grid[0, k] = nr.pss_seq(nid2)
    grid[2, k] = nr.sss_seq(nid1, nid2)
    # PBCH data (random QPSK) on symbols 1,3 and edges of 2
    for l in (1, 2, 3):
        for kk in range(240):
            if l == 2 and 48 <= kk < 192:
                continue
            grid[l, kk] = ((rng.integers(0, 2) * 2 - 1) + 1j * (rng.integers(0, 2) * 2 - 1)) / np.sqrt(2)
    r = nr._dmrs_seq(pci, issb)
    for i, (l, kk) in enumerate(nr._dmrs_res(pci)):
        grid[l, kk] = r[i]
    out = []
    for l in range(4):
        X = np.zeros(NFFT, complex)
        X[(np.arange(240) - 120) % NFFT] = grid[l]
        t = np.fft.ifft(X) * np.sqrt(NFFT)
        out.append(np.concatenate([t[-CP:], t]))
    return np.concatenate(out)


def make(pci=517, dur=0.1, beams=(0, 1, 2, 3), rng=None):
    rng = rng or np.random.default_rng(0)
    n = int(dur * FS0)
    x = np.zeros(n, complex)
    sym = NFFT + CP
    slot = int(0.5e-3 * FS0)
    starts = [2, 8, 16, 22]   # case C symbol starts (2 slots)
    for t0 in np.arange(0, n, int(0.02 * FS0)):
        for b in beams:
            s = starts[b]
            pos = t0 + (s // 14) * slot + (s % 14) * sym + 2
            seg = ssb_symbols(pci, b, rng) * (0.5 + 0.5 * b / 3)
            if pos + len(seg) < n:
                x[pos:pos + len(seg)] += seg
    # background: other OFDM-ish noise-like data in the carrier
    x += (rng.normal(size=n) + 1j * rng.normal(size=n)) * 0.05
    return x


def to_wideband(x, fs=20e6, f_off=2.1e6, cfo=25e3, snr_db=5, rng=None):
    rng = rng or np.random.default_rng(1)
    n = len(x)
    X = np.fft.fft(x)
    N = int(round(n * fs / FS0))
    Y = np.zeros(N, complex)
    h = n // 2
    s = int(round(f_off / (fs / N)))
    Y[(np.arange(0, h) + s) % N] = X[:h]
    Y[(np.arange(-h, 0) + s) % N] = X[h:]
    y = np.fft.ifft(Y) * N / n
    y *= np.exp(2j * np.pi * cfo * np.arange(N) / fs)
    p = np.mean(np.abs(y[np.abs(y) > 0]) ** 2)
    nv = p / 10 ** (snr_db / 10) * fs / FS0
    y = y + (rng.normal(size=N) + 1j * rng.normal(size=N)) * np.sqrt(nv / 2)
    return (y / np.max(np.abs(y)) * 0.7).astype(np.complex64)


if __name__ == "__main__":
    snr = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    fc = 3600e6
    # SSB on GSCN raster: choose GSCN near fc + 2.1 MHz
    g, fref = nr.gscn_of(fc + 2.1e6)
    w = to_wideband(make(), f_off=fref - fc, cfo=25e3, snr_db=snr)
    rec = recording.write("tests/lte/synth_nr", fc, 20e6, w, "ci16_le", "synthetic NR")
    t = time.time()
    res = nr.decode(rec, {})
    print("expected PCI 517, GSCN", g, "SS_REF", fref, "+25 kHz CFO")
    for r in res:
        f = r["fields"]
        print(r["level"], r["label"], "z=%.1f off=%.0f" % (f["sss_z"], f["offset_from_gscn_hz"]))
    print("time %.1fs" % (time.time() - t))
