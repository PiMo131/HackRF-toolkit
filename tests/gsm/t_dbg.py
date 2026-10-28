import sys
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/b8_935"); x = r.iq()
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
h = gsm.fcch_search(x, r.rate, r.center_hz, chans)
ch = gsm.Channelizer(x, r.rate)
for a in (8, 10, 1017, 5):
    g = h[a]; f = [c[1] for c in chans if c[0] == a][0]
    cfo = np.median([e[1] for e in g]) - gsm.FCCH_TONE
    y = ch.get(f + cfo - r.center_hz)
    print(a, "fcch t(ms):", [round(e[0]*1e3,1) for e in g[:6]], "pow dB", round(10*np.log10(np.mean(abs(y)**2)),1))
    for (ts,_,q) in g[:3]:
        st = int((ts + gsm.FRAME_S) * gsm.CH_RATE)
        for off in range(-8, 9):   # scan timeslots around
            s2 = st + int(off*156.25*4)
            best=-9
            for ph in range(4):
                soft = gsm._soft_symbols(y, max(0,s2-60*4), 270, ph)
                c = np.correlate(soft, gsm._TRAIN_A, mode="valid")
                best=max(best, c.max()/len(gsm._TRAIN_A))
            print(f"   ts {ts*1e3:.1f} slot{off:+d} best={best:.2f}", end="" if off<8 else "\n")
        print()
