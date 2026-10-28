import sys, time
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/b8_935"); x = r.iq()
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
ch = gsm.Channelizer(x, r.rate)
t=time.time()
for a, g in sorted(h.items()):
    f = [c[1] for c in chans if c[0] == a][0]
    cfo0 = np.median([e[1] for e in g]) - gsm.FCCH_TONE
    y0 = ch.get(f + cfo0 - r.center_hz)
    fines = [gsm.fcch_fine_cfo(y0, e[0]) for e in g]
    fv = [c[0] for c in fines if c]
    if not fv:
        print(a, "no clean FCCH"); continue
    cfo = cfo0 + float(np.median(fv))
    y = ch.get(f + cfo - r.center_hz)
    n1 = n2 = 0; ids=set()
    for fn_ in fines:
        if not fn_: continue
        st = fn_[1] + int(gsm.FRAME_S * gsm.CH_RATE)
        d = gsm.decode_sch_mlse(y, st, 30); n1 += bool(d and d["ok"])
        d2 = gsm.decode_sch_robust(y, st, 30); n2 += bool(d2 and d2["ok"])
        if d2 and d2["ok"]: ids.add(gsm._parse_sch(d2["info"])[0])
    print(f"{a:5d} {f/1e6:.1f} cfo={cfo:+.0f} fine_n={len(fv)}/{len(g)} spread={np.std(fv):.0f}Hz mlse={n1} robust={n2} bsic={ids}")
print("t", time.time()-t)
