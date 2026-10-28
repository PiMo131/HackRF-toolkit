import sys, time
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
for name in sys.argv[1:]:
    r = recording.load("testdata/" + name); x = r.iq()
    t = time.time()
    chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
    h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
    print(f"== {name}: {len(chans)} chans, {time.time()-t:.1f}s")
    for a, g in sorted(h.items()):
        f = [c[1] for c in chans if c[0] == a][0]
        print(f"  ARFCN {a:4d} {f/1e6:8.1f} n={len(g):2d} tone_off={np.median([e[1] for e in g])/1e3:+.1f}k q={np.median([e[2] for e in g]):.2f} t0={g[0][0]*1e3:.1f}ms")
