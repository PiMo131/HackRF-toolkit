"""Build GMSK AIS frames from known NMEA payloads, inject at 161.975/162.025 into noise, decode."""
import sys
import time

import numpy as np
from scipy import signal

from rfscan import recording
from rfscan.decoders import ais

PAYLOADS = [
    ("13u?etPv2;0n:dDPwUM1U1Cb069D", "A"),   # type 1, MMSI 265547250
    ("15RTgt0PAso;90TKcjM8h6g208CQ", "B"),   # type 1, MMSI 371798000
    ("55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp888888888880", "A"),  # type 5
]


def armor_to_bits(p):
    bits = ""
    for c in p:
        v = ord(c) - 48
        if v > 40:
            v -= 8
        bits += f"{v:06b}"
    return bits[: len(bits) - len(bits) % 8]


def frame_bits(payload):
    pb = armor_to_bits(payload)
    data = bytes(int(pb[k:k + 8], 2) for k in range(0, len(pb), 8))
    fcs = ais._crc16_x25(data) ^ 0xFFFF
    allb = data + bytes([fcs & 0xFF, fcs >> 8])
    # on-air: each byte LSB first
    s = "".join(f"{b:08b}"[::-1] for b in allb)
    # bit stuffing
    out, ones = "", 0
    for ch in s:
        out += ch
        ones = ones + 1 if ch == "1" else 0
        if ones == 5:
            out += "0"
            ones = 0
    return "01" * 12 + "01111110" + out + "01111110" + "0" * 8


def gmsk(bits, fs=48000, sps=5, bt=0.4):
    # NRZI: 0 -> transition
    lvl, nrz = 1, []
    for b in bits:
        if b == "0":
            lvl = -lvl
        nrz.append(lvl)
    x = np.repeat(np.array(nrz, float), sps)
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    g = np.exp(-(2 * np.pi ** 2 * bt ** 2 / np.log(2)) * t ** 2)
    g /= g.sum()
    f = np.convolve(x, g, "same")
    ph = np.cumsum(f) * np.pi * 0.5 / sps   # h = 0.5
    return np.exp(1j * ph)


def main(snr_db):
    rng = np.random.default_rng(2)
    rate = 2e6
    n = int(rate * 6)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.05
    x = noise.copy()
    sigma_ch = 0.05 * np.sqrt(2) * np.sqrt(25e3 / rate)   # noise rms within 25 kHz channel
    amp = sigma_ch * 10 ** (snr_db / 20)
    t_ins = 0.2
    sent = 0
    while t_ins < 5.7:
        for p, ch in PAYLOADS:
            bb = gmsk(frame_bits(p))
            up = signal.resample_poly(bb, 125, 3)                 # 48k -> 2M
            off = ais.CHANNELS[ch] - 162.0e6 + rng.uniform(-1500, 1500)   # ppm error
            k = np.arange(len(up))
            sig = amp * up * np.exp(2j * np.pi * off / rate * k + 1j * rng.uniform(0, 6.28))
            i = int(t_ins * rate)
            x[i:i + len(sig)] += sig.astype(np.complex64)
            t_ins += 0.09
            sent += 1
    rec = recording.write("tests/native/synth_ais", 162.0e6, rate, x, "ci16_le", "synthetic AIS")
    t0 = time.time()
    res = ais.decode(rec, {})
    msgs = sum(r["fields"]["messages"] for r in res)
    print(f"SNR {snr_db} dB (in 25k): {len(res)} vessels, {msgs}/{sent} frames, {time.time()-t0:.2f}s for 6 s IQ")
    for r in res:
        print("   ", r["label"])


if __name__ == "__main__":
    for s in (20, 12, 9, 6):
        main(s)
