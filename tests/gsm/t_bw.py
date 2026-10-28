import sys
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/b8_935"); x = r.iq()
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
ch = gsm.Channelizer(x, r.rate)
for bw in (270e3, 200e3, 160e3, 130e3):
    tot = {}
    for a, g in sorted(h.items()):
        f = [c[1] for c in chans if c[0] == a][0]
        cfo = np.median([e[1] for e in g]) - gsm.FCCH_TONE
        y = ch.get(f + cfo - r.center_hz, bw=bw)
        n = 0
        for (ts, _, q) in g:
            st = int((ts + gsm.FRAME_S) * gsm.CH_RATE)
            d = gsm.decode_sch_mlse(y, st, search=60)
            n += bool(d and d["ok"])
        tot[a] = f"{n}/{len(g)}"
    print(int(bw/1e3), {k: v for k, v in tot.items() if not v.startswith("0/")}, "cells ok:", sum(not v.startswith("0/") for v in tot.values()))
