#!/usr/bin/env python3
"""Synthetic 2 Msps ci8 recording for the audio-worker data modes test:
  * NFM at centre+300 kHz: AX.25 UI frame (APRS position) as AFSK1200 (Bell 202), ±3 kHz deviation,
    plus a 100.0 Hz CTCSS tone at ±0.6 kHz, repeated 3 times
  * USB at centre+600 kHz: a single 1 kHz audio tone (i.e. a carrier at +601 kHz)
  * NFM at centre+900 kHz: DTMF digits "1234#" (±2.5 kHz)
Usage: make_aprs_synth.py out.ci8   (centre is nominally 144.5 MHz; see test_audio_modes.js)
"""
import sys
import numpy as np

RATE = 2_000_000
SECONDS = 3.0
APRS_INFO = b"!5112.34N/00424.56E>rfscan web test"


def ax25_ui(src, dst, path, info):
    def addr(call, last=False):
        c, _, ssid = call.partition("-")
        b = bytearray((c.ljust(6)).encode()[:6])
        b = bytearray(x << 1 for x in b)
        b.append(0x60 | ((int(ssid or 0) & 0xF) << 1) | (1 if last else 0))
        return bytes(b)
    calls = [dst, src] + path
    frame = b"".join(addr(c, i == len(calls) - 1) for i, c in enumerate(calls)) + b"\x03\xf0" + info
    crc = 0xFFFF
    for byte in frame:
        for i in range(8):
            bit = (byte >> i) & 1
            if (crc & 1) ^ bit:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    crc ^= 0xFFFF
    return frame + bytes([crc & 0xFF, crc >> 8])


def hdlc_bits(frame, flags=20):
    bits = []
    for _ in range(flags):
        bits += [0, 1, 1, 1, 1, 1, 1, 0]
    ones = 0
    for byte in frame:
        for i in range(8):
            b = (byte >> i) & 1
            bits.append(b)
            ones = ones + 1 if b else 0
            if ones == 5:
                bits.append(0); ones = 0
    for _ in range(flags):
        bits += [0, 1, 1, 1, 1, 1, 1, 0]
    # NRZI: 0 = transition, 1 = no transition
    out, cur = [], 1
    for b in bits:
        if b == 0:
            cur ^= 1
        out.append(cur)
    return np.array(out, dtype=np.int8)


def afsk(nrzi, fs, baud=1200.0):
    n = int(round(len(nrzi) * fs / baud))
    idx = np.minimum((np.arange(n) * baud / fs).astype(int), len(nrzi) - 1)
    f = np.where(nrzi[idx] == 1, 1200.0, 2200.0)
    return np.sin(2 * np.pi * np.cumsum(f) / fs)


def fm(mod, fs, dev_hz, f_off):
    ph = 2 * np.pi * np.cumsum(mod) * dev_hz / fs + 2 * np.pi * f_off * np.arange(len(mod)) / fs
    return np.exp(1j * ph)


def dtmf(digits, fs, on=0.08, off=0.05):
    lo = {"1": 697, "2": 697, "3": 697, "4": 770, "5": 770, "6": 770, "7": 852, "8": 852, "9": 852, "*": 941, "0": 941, "#": 941}
    hi = {"1": 1209, "2": 1336, "3": 1477, "4": 1209, "5": 1336, "6": 1477, "7": 1209, "8": 1336, "9": 1477, "*": 1209, "0": 1336, "#": 1477}
    out = []
    for d in digits:
        t = np.arange(int(on * fs)) / fs
        out.append(0.5 * (np.sin(2 * np.pi * lo[d] * t) + np.sin(2 * np.pi * hi[d] * t)))
        out.append(np.zeros(int(off * fs)))
    return np.concatenate(out)


def main(out):
    n = int(RATE * SECONDS)
    t = np.arange(n) / RATE
    # APRS burst, 3 repeats with gaps
    frame = ax25_ui("N0CALL-9", "APRS", ["WIDE1-1"], APRS_INFO)
    burst = afsk(hdlc_bits(frame), RATE)
    mod = np.zeros(n)
    pos = int(0.2 * RATE)
    while pos + len(burst) < n:
        mod[pos:pos + len(burst)] = burst
        pos += len(burst) + int(0.3 * RATE)
    ctcss = 0.2 * np.sin(2 * np.pi * 100.0 * t)        # 0.2 * 3 kHz = 0.6 kHz deviation
    iq = 0.3 * fm(mod + ctcss, RATE, 3000.0, 300e3)
    # USB: 1 kHz tone above a suppressed carrier at +600 kHz
    iq += 0.15 * np.exp(1j * 2 * np.pi * 601e3 * t)
    # DTMF on NFM at +900 kHz
    d = dtmf("1234#", RATE)
    mod2 = np.zeros(n); mod2[int(0.5 * RATE):int(0.5 * RATE) + len(d)] = d
    iq += 0.3 * fm(mod2, RATE, 2500.0, 900e3)
    iq += 0.01 * (np.random.randn(n) + 1j * np.random.randn(n))
    out8 = np.empty(2 * n, dtype=np.int8)
    out8[0::2] = np.clip(np.round(iq.real * 127), -127, 127)
    out8[1::2] = np.clip(np.round(iq.imag * 127), -127, 127)
    out8.tofile(out)
    print(f"wrote {out}: {SECONDS} s at {RATE} sps, APRS info {APRS_INFO.decode()}")


if __name__ == "__main__":
    main(sys.argv[1])
