import sys, time, numpy as np
sys.path.insert(0, "tests/ext")
import synth
from rfscan import recording
from rfscan.decoders import pager
from test_rtl433 import q8

fs = 2e6
bits = synth.pocsag_bits(1234567, 3, "TEST P2000 BRAND Amsterdam")
for name, center, foff in [("pocsag_dc", 169.65e6, 0.0), ("pocsag_off", 169.95e6, -300e3)]:
    iq = synth.fsk_iq(bits, 1200, fs, 4500)
    x = synth.embed(iq, fs, 4.0, foff, 0.5, snr_db=12)
    x = x + 0.01  # simulate a HackRF DC offset
    rec = recording.write(f"tests/ext/{name}", center, fs, q8(x), "ci8", name)
    t = time.time()
    res = pager.decode(rec, {"tmpdir": "tests/ext/tmp"})
    print(name, round(time.time() - t, 2), "s")
    for r in res: print("  ", r["level"], r["freq_hz"], r["label"], "inv" if r["fields"]["inverted"] else "")
