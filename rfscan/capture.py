"""One-shot capture helper: tune, auto-gain, record to SigMF.

  python -m rfscan.capture 433.92e6 2e6 10 out/ism433
"""
import sys
import time

from . import agc, recording
from .hackrf import HackRF


def capture(dev, center, rate, seconds, path=None, gain=None, description="", bursty=False, log=print):
    dev.set_rate(rate)
    dev.tune(center)
    if gain is None:
        lna, vga, amp, p, clip = agc.auto_gain(dev, bursty=bursty, log=None)
    else:
        lna, vga, amp = gain
        dev.set_gain(lna, vga, amp)
        p = clip = None
    t0 = time.time()
    iq, raw = dev.capture(int(rate * seconds))
    if log:
        log(f"  {center/1e6:.3f} MHz @ {rate/1e6:g} Msps {seconds}s gain=({lna},{vga},{amp}) "
            f"p99.9={p} clip={clip}")
    rec = None
    if path:
        rec = recording.write(path, center, rate, raw, "ci8", description,
                              extra={"rfscan:lna": lna, "rfscan:vga": vga, "rfscan:amp": amp})
    return rec, iq, raw, (lna, vga, amp)


if __name__ == "__main__":
    c, r, s, out = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    with HackRF() as d:
        capture(d, c, r, s, out)
