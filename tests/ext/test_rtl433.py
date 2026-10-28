import sys, time, numpy as np
sys.path.insert(0, "tests/ext")
import synth
from rfscan import recording
from rfscan.decoders import rtl433


def q8(x):
    v = x.view(np.float32)
    return np.clip(np.round(v * 100 / np.abs(v).max()), -127, 127).astype(np.int8)


fs = 2e6
env = synth.ppm_ook(synth.hexbits("5a80eaf37", 36), fs)
x = synth.embed(env, fs, 6.0, 230e3, 2.0, snr_db=15)
rec = recording.write("tests/ext/synth_nexus", 433.92e6, fs, q8(x), "ci8", "synthetic nexus")
t = time.time()
res = rtl433.decode(rec, {"tmpdir": "tests/ext/tmp"})
print("wide:", round(time.time() - t, 2), "s")
for r in res: print(" ", r["level"], r["freq_hz"], r["t_s"], r["label"])

# already-channelised ci16 burst at 1 Msps
from rfscan.decoders import _audio
y = _audio.channelize(x[int(1.9 * fs):int(2.4 * fs)], fs, 230e3, 1e6)
rec2 = recording.write("tests/ext/synth_nexus_burst", 433.92e6 + 230e3, 1e6, y, "ci16_le", "burst")
res = rtl433.decode(rec2, {"tmpdir": "tests/ext/tmp"})
print("burst:")
for r in res: print(" ", r["level"], r["freq_hz"], r["t_s"], r["label"])
