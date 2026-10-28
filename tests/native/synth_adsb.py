"""Inject known Mode S frames into real 1090 noise and check the decoder recovers them."""
import os, sys, time
import numpy as np
from rfscan import recording
from rfscan.decoders import adsb

MSGS = ["8D4840D6202CC371C32CE0576098",   # KLM1023 ident
        "8D40621D58C382D690C8AC2863A7",   # airborne pos (even)
        "8D40621D58C386435CC412692AD6",   # airborne pos (odd)
        "8D485020994409940838175B284F"]   # velocity

def ppm(hexmsg, fs, amp, f_off, rng):
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(hexmsg), np.uint8))
    chips = [1,0,1,0,0,0,0,1,0,1,0,0,0,0,0,0]   # 8us preamble in 0.5us chips
    for b in bits: chips += [1,0] if b else [0,1]
    t_chip = 0.5e-6
    n = int(len(chips) * t_chip * fs) + 2
    t = np.arange(n) / fs
    env = np.array(chips)[np.minimum((t / t_chip).astype(int), len(chips)-1)].astype(np.float32)
    ph = rng.uniform(0, 2*np.pi)
    return (amp * env * np.exp(1j*(2*np.pi*f_off*t + ph))).astype(np.complex64)

def main(snr_db=12, out="tests/native/synth_adsb"):
    rng = np.random.default_rng(1)
    base = recording.load("testdata/adsb_1090")
    fs = base.rate
    x = base.iq(0, int(fs*5)).copy()
    x -= x.mean()
    sigma = np.sqrt(np.mean(np.abs(x)**2))
    amp = sigma * 10**(snr_db/20)
    pos = []
    for k in range(200):
        m = MSGS[k % 4]
        s = ppm(m, fs, amp, rng.uniform(-50e3, 50e3), rng)
        i = int(rng.uniform(0.001, 4.99) * fs)
        x[i:i+len(s)] += s
        pos.append(i)
    rec = recording.write(out, base.center_hz, fs, x, "ci16_le", "synthetic adsb")
    t0 = time.time()
    res = adsb.decode(rec, {})
    dt = time.time() - t0
    tot = sum(r["fields"]["messages"] for r in res)
    print(f"SNR {snr_db} dB: {len(res)} aircraft, {tot}/200 msgs recovered, {dt:.2f}s for 5 s IQ")
    for r in res: print("  ", r["label"])

if __name__ == "__main__":
    for s in (20, 12, 9, 7):
        main(float(s))
