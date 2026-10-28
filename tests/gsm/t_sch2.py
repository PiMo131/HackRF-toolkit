import sys, time
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/b8_935"); x = r.iq()
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
ch = gsm.Channelizer(x, r.rate)
for a in (1019, 984, 44, 42):
    g = h[a]; f = [c[1] for c in chans if c[0] == a][0]
    cfo = np.median([e[1] for e in g]) - gsm.FCCH_TONE
    y = ch.get(f + cfo - r.center_hz)
    for (ts, _, q) in g[:12]:
        st = int((ts + gsm.FRAME_S) * gsm.CH_RATE)
        d = gsm.decode_sch_burst(y, st, search=60)
        if d and d["ok"]:
            bsic,t1,t2,t3p = gsm._parse_sch(d["info"]); fn = gsm.fn_from_sch(t1,t2,t3p)
            print(a, f"t={ts*1e3:7.1f}ms bsic={bsic:2d} (ncc {bsic>>3} bcc {bsic&7}) t1={t1} t2={t2} t3p={t3p} FN={fn}  FN-t/frame={fn - ts/gsm.FRAME_S:.1f}")
