import sys
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/b8_935"); x = r.iq()
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
ch = gsm.Channelizer(x, r.rate)
for a in (984, 44, 1019):
    g = h[a]; f = [c[1] for c in chans if c[0] == a][0]
    cfo = np.median([e[1] for e in g]) - gsm.FCCH_TONE
    y = ch.get(f + cfo - r.center_hz)
    for (ts, _, q) in g[:10]:
        st = int((ts + gsm.FRAME_S) * gsm.CH_RATE)
        d1 = gsm.decode_sch_burst(y, st, search=60); d2 = gsm.decode_sch_mlse(y, st, search=60)
        f1 = d1 and (d1["ok"], d1["train_err"], d1["sample"]); f2 = d2 and (d2["ok"], d2["train_err"], round(d2["score"],2), d2["sample"])
        print(a, round(ts*1e3,1), "diff:", f1, " mlse:", f2)
