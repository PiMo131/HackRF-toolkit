"""Embed gr-lora_sdr generated packets (500 ksps, BW125, os=4) into a 4 Msps noisy stream."""
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording  # noqa: E402

FS = 4e6
rng = np.random.default_rng(1)


def first_packet(x, fs_in):
    on = np.abs(x) > 0.5
    idx = np.where(on)[0]
    start = idx[0]
    # first gap of >= 1000 zero samples after start
    off = np.where(~on[start:])[0]
    gaps = np.split(off, np.where(np.diff(off) != 1)[0] + 1)
    for g in gaps:
        if len(g) >= 1000:
            return x[start:start + g[0]]
    return x[start:]


def embed(packets, seconds=1.5, snr_db=10.0):
    n = int(FS * seconds)
    y = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64) * 0.02
    for pkt, t0, f_off, cfo in packets:
        z = signal.resample_poly(pkt, 8, 1).astype(np.complex64)   # 500k -> 4M
        a = 0.02 * np.sqrt(2) * 10 ** (snr_db / 20)
        k = np.arange(len(z))
        z = a * z * np.exp(2j * np.pi * (f_off + cfo) / FS * k)
        i0 = int(t0 * FS)
        y[i0:i0 + len(z)] += z[: n - i0]
    return y


if __name__ == "__main__":
    p7 = first_packet(np.fromfile("sf7.cf32", np.complex64), 500e3)
    p9 = first_packet(np.fromfile("sf9.cf32", np.complex64), 500e3)
    print("pkt lengths (ms):", len(p7) / 500, len(p9) / 500)
    y = embed([(p7, 0.10, 400e3, 1234.0), (p9, 0.50, -400e3, -2100.0)], seconds=1.5, snr_db=10)
    y /= np.max(np.abs(y.view(np.float32))) * 1.05
    raw = np.round(y.view(np.float32) * 127).astype(np.int8)
    recording.write("/home/jarvis/testscan/tests/lora/synth868", 868.5e6 - 400e3 + 0, FS, raw, "ci8",
                    "synthetic LoRa SF7 @+400k, SF9 @-400k")
    # low-SNR variant
    y = embed([(p7, 0.10, 400e3, 800.0), (p9, 0.50, -400e3, 1500.0)], seconds=1.5, snr_db=0)
    y /= np.max(np.abs(y.view(np.float32))) * 1.05
    raw = np.round(y.view(np.float32) * 127).astype(np.int8)
    recording.write("/home/jarvis/testscan/tests/lora/synth868_lowsnr", 868.1e6, FS, raw, "ci8", "low snr")
