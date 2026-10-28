"""Synthetic stereo FM broadcast with RDS, as a 2 Msps ci8 SigMF file, to test web/audio-worker.js.

Station at 100.300 MHz (file centre 100.000 MHz): L = 1 kHz tone, R = 2.5 kHz tone, 19 kHz pilot,
RDS PI 0x1234, PS "RFSCANWB", RT "HELLO FROM THE BROWSER RADIO", PTY 10 (Pop).
"""
import sys
import numpy as np
from scipy import signal

OUT = sys.argv[1] if len(sys.argv) > 1 else "tests/web/fm_rds_synth"
FS_MPX, FS_OUT, SEC = 250_000, 2_000_000, 8.0
PI, PTY, PS, RT = 0x1234, 10, "RFSCANWB", "HELLO FROM THE BROWSER RADIO\r"
OFFSET = {"A": 0x0FC, "B": 0x198, "C": 0x168, "Cp": 0x350, "D": 0x1B4}
SYNDROME = {"A": 383, "B": 14, "C": 303, "D": 663, "Cp": 748}


def crc10(data16):
    reg = data16 << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= 0x5B9 << (i - 10)
    return reg & 0x3FF


def syndrome(block26):  # same algorithm as the JS decoder / gr-rds
    reg = 0
    for i in range(26, 0, -1):
        reg = (reg << 1) | ((block26 >> (i - 1)) & 1)
        if reg & 1024:
            reg ^= 0x5B9
    for _ in range(10):
        reg <<= 1
        if reg & 1024:
            reg ^= 0x5B9
    return reg & 1023


def block(data16, off):
    b = (data16 << 10) | (crc10(data16) ^ OFFSET[off])
    assert syndrome(b) == SYNDROME[off], (off, syndrome(b))
    return b


def groups():
    out = []
    rt = RT.ljust(64)
    for k in range(400):
        a = k % 4
        B = (0 << 12) | (0 << 11) | (0 << 10) | (PTY << 5) | a
        D = (ord(PS[2 * a]) << 8) | ord(PS[2 * a + 1])
        out += [block(PI, "A"), block(B, "B"), block(0xCDCD, "C"), block(D, "D")]
        a = k % 16
        B = (2 << 12) | (0 << 11) | (0 << 10) | (PTY << 5) | a
        C = (ord(rt[4 * a]) << 8) | ord(rt[4 * a + 1]); D = (ord(rt[4 * a + 2]) << 8) | ord(rt[4 * a + 3])
        out += [block(PI, "A"), block(B, "B"), block(C, "C"), block(D, "D")]
    bits = []
    for b in out:
        bits += [(b >> i) & 1 for i in range(25, -1, -1)]
    return np.array(bits, np.int8)


n = int(SEC * FS_MPX)
t = np.arange(n) / FS_MPX
L = 0.4 * np.sin(2 * np.pi * 1000 * t)
R = 0.4 * np.sin(2 * np.pi * 2500 * t)
bits = groups()
e = np.zeros(len(bits), np.int8); prev = 0
for i, d in enumerate(bits):
    prev = d ^ prev; e[i] = prev
sym = np.where(np.repeat(e, 2) == 1, 1.0, -1.0)          # half-bits at 2375 Hz
sym[1::2] *= -1                                             # (+,-) for 1, (-,+) for 0
idx = np.minimum((t * 2375).astype(int), len(sym) - 1)
rds_bb = signal.lfilter(signal.firwin(255, 2400 / (FS_MPX / 2)), 1, sym[idx])
mpx = (0.5 * (L + R) + 0.09 * np.cos(2 * np.pi * 19000 * t) + 0.5 * (L - R) * np.cos(2 * np.pi * 38000 * t)
       + 0.04 * rds_bb * np.cos(2 * np.pi * 57000 * t))
mpx_up = signal.resample_poly(mpx, FS_OUT // FS_MPX, 1)
tt = np.arange(len(mpx_up)) / FS_OUT
phase = 2 * np.pi * 75_000 * np.cumsum(mpx_up) / FS_OUT
iq = 0.5 * np.exp(1j * (2 * np.pi * 300_000 * tt + phase))
rng = np.random.default_rng(0)
iq += 0.003 * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
raw = np.clip(np.round(np.stack([iq.real, iq.imag], 1).ravel() * 127), -127, 127).astype(np.int8)
sys.path.insert(0, ".")
from rfscan import recording
recording.write(OUT, 100.0e6, FS_OUT, raw, "ci8", "synthetic stereo FM + RDS at 100.300 MHz")
print(OUT, len(raw) // 2 / FS_OUT, "s", len(bits), "bits")
