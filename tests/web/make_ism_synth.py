"""Synthetic ISM traffic for the live rtl_433 test (tests/web/test_rtl433_live.js).

The 2026-10-19 reference captures (testdata/ism433, ism868) hold no rtl_433-decodable packets (native
rtl_433 finds none either), and the wM-Bus gas meter seen live on 2026-10-29 (ELS id 36325352, mode C,
868.94 MHz) was decoded from a watch capture whose wide IQ was not kept. So this script rebuilds that
meter's telegram bit-exact from the logged payload, plus a Nexus-style OOK sensor, as 2 Msps ci8 SigMF:
  tests/web/ism433_synth: centre 434.4 MHz, Nexus PPM-OOK bursts on 433.92 MHz at 1.5 s, 3.90 s (across the
                          4 s chunk border, tests mergeWindow) and 6.5 s (weak, 8 dB)
  tests/web/ism868_synth: centre 868.6 MHz, wM-Bus mode C format A frames (100 kbps FSK +-50 kHz) on 868.95 MHz
                          at 1.0 s, 3.997 s (across the border) and 6.5 s (weak, 12 dB)
Usage: python3 tests/web/make_ism_synth.py [outdir]
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ext"))
sys.path.insert(0, ".")
import synth  # noqa: E402
from rfscan import recording  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "tests/web"
FS, SEC = 2_000_000, 8.0


def crc16_mbus(data):
    """EN 13757-4 CRC: poly 0x3D65, init 0, complemented, MSB first (rtl_433 m_bus_crc_valid)."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x3D65) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF


def wmbus_mode_c_frame(c, manuf, ident_bcd, version, dev_type, payload):
    """Preamble + mode C format A sync + block 1 + 16-byte blocks, each with CRC. Returns bits (MSB first)."""
    block1 = bytes([9 + len(payload), c]) + manuf + ident_bcd + bytes([version, dev_type])
    frame = block1 + crc16_mbus(block1).to_bytes(2, "big")
    for i in range(0, len(payload), 16):
        blk = payload[i:i + 16]
        frame += blk + crc16_mbus(blk).to_bytes(2, "big")
    raw = bytes([0x55] * 6 + [0x54, 0x3D, 0x54, 0xCD]) + frame + bytes([0x55])
    return [(b >> (7 - k)) & 1 for b in raw for k in range(8)]


def to_ci8(x):
    return np.clip(np.round(np.stack([x.real, x.imag], 1).ravel() * 127), -127, 127).astype(np.int8)


NOISE_RMS = 0.05    # complex rms of the noise floor, -26 dBFS (a HackRF at LNA 40 / VGA 46 on a quiet 868 MHz channel)


def place(iq, burst, fs, f_off, at_s, snr_db):
    """Add a unit-amplitude burst snr_db above the noise floor of the full 2 MHz (3 dB more in a 1 MHz channel, 9 dB in 250 kHz)."""
    i0 = int(at_s * fs)
    k = np.arange(len(burst)) + i0
    iq[i0:i0 + len(burst)] += (burst * NOISE_RMS * 10 ** (snr_db / 20) * np.exp(2j * np.pi * f_off / fs * k)).astype(np.complex64)


rng = np.random.default_rng(3)
n = int(SEC * FS)
noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * (NOISE_RMS / np.sqrt(2))

# ---- 433.92 MHz: Nexus-TH style PPM OOK (36 bits, 3 repeats ~0.3 s)
iq = noise.copy()
env = synth.ppm_ook(synth.hexbits("5a80eaf37", 36), FS, repeats=3).astype(np.complex64)
for at, snr in [(1.5, 20), (3.90, 20), (6.5, 8)]:
    place(iq, env, FS, 433.92e6 - 434.4e6, at, snr)
recording.write(os.path.join(OUT, "ism433_synth"), 434.4e6, FS, to_ci8(iq), "ci8", "synthetic Nexus OOK on 433.92 MHz")

# ---- 868.95 MHz: wM-Bus mode C, the ELS gas meter logged on 2026-10-29 (id 36325352)
iq = noise.copy()
payload = bytes.fromhex("7ad03401290e00000f5235164f836d55aba2098020265fd5fb0370cb6be5f67b89211c36e4")
bits = wmbus_mode_c_frame(0x44, bytes([0x93, 0x15]), bytes([0x52, 0x53, 0x32, 0x36]), 0x50, 0x03, payload)
burst = synth.fsk_iq(bits, 100e3, FS, 50e3, one_is_low=False)
for at, snr in [(1.0, 20), (3.997, 20), (6.5, 12)]:
    place(iq, burst, FS, 868.95e6 - 868.6e6, at, snr)
recording.write(os.path.join(OUT, "ism868_synth"), 868.6e6, FS, to_ci8(iq), "ci8", "synthetic wM-Bus mode C (ELS 36325352) on 868.95 MHz")
print(f"{len(bits)} bit wM-Bus frame ({len(bits) / 100e3 * 1e3:.1f} ms), {len(env) / FS * 1e3:.0f} ms OOK burst -> {OUT}/ism433_synth, {OUT}/ism868_synth")
