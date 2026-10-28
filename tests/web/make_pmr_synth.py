"""Synthetic PMR446 traffic for the channel-bank scanner test: NFM bursts on channels 3, 8 and 14.

2 Msps ci8 SigMF centred on 445.900 MHz (channels at +106..+294 kHz, clear of DC), 10 s:
  0.8-2.8 s ch3 (446.03125) 1 kHz tone, 3.3-5.3 s ch8 (446.09375) 700 Hz, 5.0-6.0 s ch14 (446.16875, stronger) 1.5 kHz,
  7.0-8.5 s ch3 again.
"""
import sys
import numpy as np

OUT = sys.argv[1] if len(sys.argv) > 1 else "tests/web/pmr_synth"
FS, SEC, FC = 2_000_000, 10.0, 445.9e6
CH = lambda n: 446.00625e6 + (n - 1) * 12.5e3
bursts = [(0.8, 2.8, CH(3), 1000, 0.25), (3.3, 5.3, CH(8), 700, 0.25), (5.0, 6.0, CH(14), 1500, 0.5), (7.0, 8.5, CH(3), 1000, 0.25)]
n = int(SEC * FS)
t = np.arange(n) / FS
rng = np.random.default_rng(0)
iq = 0.004 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
for t0, t1, f, tone, amp in bursts:
    a, b = int(t0 * FS), int(t1 * FS)
    tt = t[a:b]
    audio = np.sin(2 * np.pi * tone * tt)
    phase = 2 * np.pi * 2500 * np.cumsum(audio) / FS
    iq[a:b] += amp * np.exp(1j * (2 * np.pi * (f - FC) * tt + phase))
raw = np.clip(np.round(np.stack([iq.real, iq.imag], 1).ravel() * 127), -127, 127).astype(np.int8)
sys.path.insert(0, ".")
from rfscan import recording
recording.write(OUT, FC, FS, raw, "ci8", "synthetic PMR446 bursts ch3/ch8/ch14")
print(OUT)
