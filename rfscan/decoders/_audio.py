"""Shared DSP helpers for the external-tool decoders (rtl433, pager, dab).

Activity detection uses a per-bin *time* baseline (median over the recording),
so the HackRF passband shape and DC/LO spurs cancel out; only energy that
changes over time (bursts) or sits clearly above the smoothed spectrum stands out.
"""
import numpy as np
from scipy import signal


def spectrogram(rec, nfft=512, block_s=0.005, max_s=None):
    """Power (dB) per [time-block, bin], averaged over nfft-frames within each block.
    Returns P[db], f_offsets (Hz, fftshifted), block length in samples."""
    fs = rec.rate
    n = rec.n_samples()
    if max_s:
        n = min(n, int(max_s * fs))
    blk = max(nfft, int(fs * block_s))
    nb = n // blk
    frames = max(1, blk // nfft)   # cover every sample: short bursts must not fall between frames
    win = np.hanning(nfft).astype(np.float32)
    P = np.empty((nb, nfft), np.float32)
    chunk = max(1, int(4e6 // blk))  # ~4 M samples per read
    for c0 in range(0, nb, chunk):
        c1 = min(nb, c0 + chunk)
        x = rec.iq(c0 * blk, (c1 - c0) * blk).reshape(c1 - c0, blk)
        x = x[:, : frames * nfft].reshape(c1 - c0, frames, nfft)
        X = np.fft.fft(x * win, axis=2)
        P[c0:c1] = np.mean(np.abs(X) ** 2, axis=1)
    P = np.fft.fftshift(10 * np.log10(P + 1e-20), axes=1)
    f = (np.arange(nfft) - nfft / 2) * fs / nfft
    return P, f, blk


def active_clusters(P, f, thr_db=8.0, min_blocks=1, merge_hz=60e3, dc_guard_hz=None, edge_frac=0.06):
    """Find frequency clusters with bursty energy.

    Returns list of dicts {f_off, bw, blocks(bool[nb]), peak_db} sorted by activity.
    """
    nb, nfft = P.shape
    df = f[1] - f[0]
    base = np.median(P, axis=0)
    ex = P - base[None, :]
    act = ex > thr_db
    # ignore the DC spike and filter roll-off at the edges
    guard = np.zeros(nfft, bool)
    dc_guard_hz = dc_guard_hz or 2 * df
    guard |= np.abs(f) <= dc_guard_hz
    e = int(edge_frac * nfft)
    guard[:e] = guard[nfft - e:] = True
    act[:, guard] = False
    # require >=2 adjacent active bins in a block (single-bin noise spikes are common)
    a2 = act & (np.roll(act, 1, axis=1) | np.roll(act, -1, axis=1))
    cnt = a2.sum(axis=0)
    cand = np.where(cnt >= min_blocks)[0]
    if len(cand) == 0:
        return []
    groups, cur = [], [cand[0]]
    for i in cand[1:]:
        if (i - cur[-1]) * df <= merge_hz:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    out = []
    for g in groups:
        lo, hi = g[0], g[-1]
        blocks = a2[:, lo:hi + 1].any(axis=1)
        w = cnt[lo:hi + 1].astype(float)
        fc = float(np.sum(f[lo:hi + 1] * w) / np.sum(w))
        out.append({"f_off": fc, "bw": float((hi - lo + 1) * df), "blocks": blocks,
                    "n_blocks": int(blocks.sum()), "peak_db": float(ex[:, lo:hi + 1].max())})
    out.sort(key=lambda c: -c["n_blocks"])
    return out


def segments(blocks, blk, pad_blocks=4, max_gap_blocks=10):
    """bool per block -> list of (start_sample, end_sample) with padding, merged across small gaps."""
    idx = np.where(blocks)[0]
    if len(idx) == 0:
        return []
    segs = []
    s = e = idx[0]
    for i in idx[1:]:
        if i - e <= max_gap_blocks:
            e = i
        else:
            segs.append((s, e))
            s = e = i
    segs.append((s, e))
    n = len(blocks)
    return [(max(0, a - pad_blocks) * blk, min(n, b + 1 + pad_blocks) * blk) for a, b in segs]


def channelize(x, fs, f_off, out_rate, bw=None):
    """Mix f_off to DC, low-pass to bw (default 0.8*out_rate), rational resample to out_rate."""
    n = np.arange(len(x), dtype=np.float64)
    y = (x * np.exp(-2j * np.pi * f_off / fs * n).astype(np.complex64)).astype(np.complex64)
    from fractions import Fraction
    fr = Fraction(out_rate / fs).limit_denominator(1000)
    up, down = fr.numerator, fr.denominator
    bw = bw or 0.8 * out_rate
    # anti-alias filter designed at the upsampled rate
    cutoff = min(bw / 2, 0.45 * out_rate) / (fs * up / 2)
    taps = signal.firwin(int(24 * max(up, down)) | 1, cutoff) * up
    return signal.resample_poly(y, up, down, window=taps).astype(np.complex64)


def fm_demod(x):
    """Quadrature discriminator -> instantaneous frequency in rad/sample."""
    return np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)


def resample_real(a, fs_in, fs_out):
    from fractions import Fraction
    fr = Fraction(fs_out / fs_in).limit_denominator(1000)
    return signal.resample_poly(a, fr.numerator, fr.denominator).astype(np.float32)


def mix_decimate(rec, f_off, min_rate, start=0, count=None, chunk_s=1.0):
    """Stream a recording: mix f_off to DC, stateful FIR low-pass, integer decimate.

    Output rate = rec.rate / D with D the largest integer keeping rate >= min_rate.
    Phase-continuous and transient-free across chunks (unlike per-chunk resampling).
    Returns (y, out_rate).
    """
    fs = rec.rate
    D = max(1, int(fs // min_rate))
    n_total = rec.n_samples() - start if count is None else min(count, rec.n_samples() - start)
    taps = signal.firwin(8 * D + 1, 0.8 / D).astype(np.float32) if D > 1 else None
    zi = np.zeros(len(taps) - 1, np.complex64) if D > 1 else None
    chunk = max(D, int(fs * chunk_s) // D * D)
    out = []
    ph = 0  # decimation phase carried across chunks
    for a in range(0, n_total, chunk):
        x = rec.iq(start + a, min(chunk, n_total - a))
        k = np.arange(start + a, start + a + len(x), dtype=np.float64)
        y = (x * np.exp(-2j * np.pi * f_off / fs * k)).astype(np.complex64)
        if D > 1:
            y, zi = signal.lfilter(taps, 1.0, y, zi=zi)
            y = y[ph::D]
            ph = (ph - len(x)) % D
        out.append(y.astype(np.complex64))
    return (np.concatenate(out) if out else np.zeros(0, np.complex64)), fs / D
