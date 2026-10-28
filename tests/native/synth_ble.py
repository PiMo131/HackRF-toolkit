"""Synthetic BLE ADV_IND packets (GFSK BT0.5 h0.5) injected on ch38 into noise; decode."""
import time

import numpy as np
from scipy import signal

from rfscan import recording
from rfscan.decoders import ble


def adv_pdu(mac, name, cid):
    adva = bytes.fromhex(mac.replace(":", ""))[::-1]
    ad = bytes([2, 0x01, 0x06]) + bytes([len(name) + 1, 0x09]) + name.encode()
    ad += bytes([5, 0xFF, cid & 0xFF, cid >> 8, 0x12, 0x34])
    payload = adva + ad
    return bytes([0x40 | 0x00, len(payload)]) + payload      # ADV_IND, TxAdd random


def air_bits(pdu, ch):
    crc = ble.crc24(pdu)
    body = pdu + bytes([crc & 0xFF, (crc >> 8) & 0xFF, crc >> 16])
    bits = np.unpackbits(np.frombuffer(body, np.uint8)[:, None], axis=1)[:, ::-1].ravel()
    bits = bits ^ ble._WHITEN[ch][: len(bits)]
    return np.concatenate([ble._PRE_BITS, ble._AA_BITS, bits])


def gfsk(bits, fs=4e6, bt=0.5):
    sps = int(fs / 1e6)
    x = np.repeat(bits * 2.0 - 1.0, sps)
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    g = np.exp(-(2 * np.pi ** 2 * bt ** 2 / np.log(2)) * t ** 2)
    g /= g.sum()
    f = np.convolve(x, g, "same")
    return np.exp(1j * np.cumsum(f) * np.pi * 0.5 / sps)


def main(snr_db, rate=4e6, center=2426e6):
    rng = np.random.default_rng(3)
    n = int(rate * 1.0)
    x = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.05).astype(np.complex64)
    sigma_ch = 0.05 * np.sqrt(2) * np.sqrt(2e6 / rate)
    amp = sigma_ch * 10 ** (snr_db / 20)
    devs = [("C0:11:22:33:44:55", "rfscan-a", 0x004C), ("D1:AA:BB:CC:DD:EE", "beacon-b", 0x0006)]
    sent = 0
    for k in range(60):
        mac, name, cid = devs[k % 2]
        bb = gfsk(air_bits(adv_pdu(mac, name, cid), 38))
        if rate != 4e6:
            bb = signal.resample_poly(bb, int(rate / 1e6), 4)
        off = 2426e6 - center + rng.uniform(-60e3, 60e3)
        i = int((0.01 + k * 0.016) * rate)
        kk = np.arange(len(bb))
        x[i:i + len(bb)] += (amp * bb * np.exp(2j * np.pi * off / rate * kk + 1j * rng.uniform(0, 6.28))).astype(np.complex64)
        sent += 1
    rec = recording.write("tests/native/synth_ble", center, rate, x, "ci16_le", "synthetic BLE")
    t0 = time.time()
    res = ble.decode(rec, {})
    got = sum(r["fields"]["packets"] for r in res)
    print(f"SNR {snr_db} dB rate {rate/1e6:g}M: {len(res)} devs, {got}/{sent} pkts, {time.time()-t0:.2f}s for 1 s IQ")
    for r in res:
        print("   ", r["label"])


if __name__ == "__main__":
    for s in (20, 12, 9):
        main(s)
    main(15, rate=20e6, center=2430e6)
