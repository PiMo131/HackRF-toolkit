"""Shared helpers for the LTE / NR cell-search decoders."""
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import numpy as np
import scipy.fft as sfft


def pmap(fn, items, workers):
    """map() over a thread pool; a plain map where threads are unavailable (Pyodide in the browser)."""
    if sys.platform == "emscripten" or workers <= 1:
        return list(map(fn, items))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def gold(c_init, n):
    """3GPP length-31 Gold sequence c(0..n-1) (36.211 7.2 / 38.211 5.2.1)."""
    nc = 1600
    tot = n + nc
    x1 = np.zeros(tot + 31, np.uint8)
    x2 = np.zeros(tot + 31, np.uint8)
    x1[0] = 1
    for i in range(31):
        x2[i] = (c_init >> i) & 1
    for i in range(tot):
        x1[i + 31] = x1[i + 3] ^ x1[i]
        x2[i + 31] = x2[i + 3] ^ x2[i + 2] ^ x2[i + 1] ^ x2[i]
    return x1[nc:nc + n] ^ x2[nc:nc + n]


class Channelizer:
    """Exact FFT-slice channelizer: one big FFT, then any (offset, rate) via a small IFFT.

    The slice is a brick-wall filter; the output is circular (fine for correlation work).
    """

    def __init__(self, x, fs, out_rates=(1.92e6,), dc_notch_hz=3e3):
        den = 1
        for fo in out_rates:
            fr = Fraction(int(round(fo)), int(round(fs)))
            den = np.lcm(den, fr.denominator)
        n = (len(x) // den) * den
        self.fs = float(fs)
        self.n = n
        self.df = self.fs / n
        self.X = sfft.fft(np.asarray(x[:n], np.complex64), workers=-1)
        if dc_notch_hz:
            # HackRF LO leakage / DC offset: a huge tone at 0 Hz wrecks normalised correlation
            k = int(np.ceil(dc_notch_hz / self.df))
            self.X[:k + 1] = 0
            self.X[-k:] = 0

    def spectrum(self, f_off, fo):
        """FFT-ordered spectrum (length M) of the band centred at f_off, sampled at fo."""
        m = int(round(self.n * fo / self.fs))
        s = int(round(f_off / self.df))
        k = np.concatenate([np.arange(0, m // 2), np.arange(-(m // 2), 0)])
        return self.X[(s + k) % self.n], f_off - s * self.df

    def get(self, f_off, fo):
        Y, resid = self.spectrum(f_off, fo)
        y = sfft.ifft(Y, workers=1) * (len(Y) / self.n)
        if abs(resid) > 0:
            t = np.arange(len(y)) / fo
            y = y * np.exp(-2j * np.pi * resid * t).astype(np.complex64)
        return y.astype(np.complex64)


def sliding_energy(y, L):
    e = np.abs(y).astype(np.float64) ** 2
    cs = np.concatenate([[0.0], np.cumsum(np.concatenate([e, e[:L - 1]]))])
    return cs[L:L + len(y)] - cs[:len(y)]


def norm_xcorr_spec(Y, T_conj, energy, t_energy):
    """|corr|^2 / (E_y * E_t) using spectra: Y (signal), T_conj = conj(FFT(template padded))."""
    c = sfft.ifft(Y * T_conj, workers=1)
    return (np.abs(c) ** 2) / (np.maximum(energy, 1e-20) * t_energy)


def template_spec(t, m):
    tt = np.zeros(m, np.complex64)
    tt[:len(t)] = t
    return np.conj(sfft.fft(tt))


def fold_max(rho, period):
    n = (len(rho) // period) * period
    if n == 0:
        return rho, 1
    f = rho[:n].reshape(-1, period).mean(axis=0)
    return f, n // period


def peaks(f, thr, min_sep, max_n=3):
    """Indices of up to max_n local maxima above thr separated by >= min_sep (circular)."""
    out = []
    g = f.copy()
    for _ in range(max_n):
        i = int(np.argmax(g))
        if g[i] < thr:
            break
        out.append(i)
        L = len(g)
        idx = (np.arange(i - min_sep, i + min_sep + 1)) % L
        g[idx] = 0
    return out
