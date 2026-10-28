import sys, time, numpy as np
sys.path.insert(0, "tests/ext")
import synth
from rfscan import recording
from rfscan.decoders import dab
from test_rtl433 import q8

def dab_like(seconds, fs=2.048e6, rng=np.random.default_rng(3)):
    frames = []
    for _ in range(int(seconds / 0.096) + 1):
        fr = [np.zeros(dab.TNULL, np.complex64)]
        for _s in range(76):
            X = np.zeros(2048, np.complex64)
            k = np.r_[1:769, 2048 - 768:2048]
            X[k] = np.exp(1j * np.pi / 2 * rng.integers(0, 4, len(k)))
            t = np.fft.ifft(X) * np.sqrt(2048 / len(k))
            fr.append(np.r_[t[-504:], t].astype(np.complex64))
        frames.append(np.concatenate(fr))
    return np.concatenate(frames)

# 20 Msps wide recording centred 212 MHz with an ensemble on 11C? -> place on 10B (211.648)
fs = 20e6
y = dab_like(0.5)
from scipy import signal
from fractions import Fraction
fr = Fraction(fs / 2.048e6).limit_denominator(1000)
yw = signal.resample_poly(y, fr.numerator, fr.denominator).astype(np.complex64)
yw /= np.sqrt(np.mean(np.abs(yw) ** 2))
x = synth.embed(yw, fs, len(yw) / fs + 0.05, 211.648e6 - 212e6, 0.02, snr_db=0)
rec = recording.write("tests/ext/synth_dab", 212e6, fs, q8(x), "ci8", "synthetic dab")
t = time.time()
res = dab.decode(rec, {"tmpdir": "tests/ext/tmp"})
print(round(time.time() - t, 1), "s")
for r in res:
    f = r["fields"]
    print(" ", r["level"], r["id"], round(f["cp_rho"], 3), round(f["cp_ratio"], 1), f["null_frames"], f["frames"], f.get("welle_synced"))
