"""Dwell auto-gain: iterate LNA/VGA/amp until the 8-bit ADC is well used but not clipping.

Judged on the 99.99th percentile of |I|,|Q| (target 30..100 of 127) and the clip
fraction (any |x| >= 126 above 1e-5 forces a backoff).  The probe is 100 ms by default
so intermittent strong bursts (Wi-Fi, DECT, sensors) are seen; bursty=True keeps
extra headroom.  Gain is distributed LNA-first (better noise figure), then VGA;
the RF amp is only enabled when LNA+VGA are near max and the signal is still weak.
"""
import numpy as np

TARGET_LO, TARGET_HI, TARGET = 30, 100, 60


def total(lna, vga, amp):
    return lna + vga + 11 * amp


def split(t):
    """Total gain in dB -> (lna, vga, amp), LNA first, amp only at the top."""
    amp = 0
    if t > 40 + 50:
        amp, t = 1, t - 11
    lna = int(min(40, max(0, t - 10)) // 8 * 8)
    vga = int(min(62, max(0, t - lna)) // 2 * 2)
    return lna, vga, amp


def measure(raw):
    a = np.abs(raw.astype(np.int16))
    p = float(np.percentile(a, 99.99))
    clip = float(np.mean(a >= 126))
    return p, clip


def auto_gain(dev, start=(24, 20, 0), probe_s=0.1, bursty=False, max_iter=7, log=None):
    """Adjust device gain in place.  Returns (lna, vga, amp, p99.99, clip_frac)."""
    n = int(dev.rate * probe_s)
    hi = TARGET_HI - (25 if bursty else 0)
    target = TARGET - (15 if bursty else 0)
    t = total(*start)
    g = split(t)
    p = clip = 0.0
    for _ in range(max_iter):
        dev.set_gain(*g)
        _, raw = dev.capture(n)
        p, clip = measure(raw)
        if log:
            log(f"    agc {g} p99.99={p:.0f} clip={clip:.1e}")
        if clip > 1e-5:
            step = -12.0
        elif p > hi:
            step = -max(4.0, 20 * np.log10(p / target))
        elif p < TARGET_LO:
            step = min(24.0, 20 * np.log10(target / max(p, 1)))
        else:
            break
        nt = float(np.clip(t + step, 0, 40 + 62 + 11))
        ng = split(nt)
        if ng == g:
            break
        g, t = ng, nt
    return (*g, p, clip)
