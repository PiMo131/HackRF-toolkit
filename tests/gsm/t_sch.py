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
    ch = gsm.Channelizer(x, r.rate)
    print(f"== {name}: fcch {len(h)} ({time.time()-t:.1f}s) out_rate={ch.out_rate:.2f}")
    for a, g in sorted(h.items()):
        f = [c[1] for c in chans if c[0] == a][0]
        tone = np.median([e[1] for e in g]); cfo = tone - gsm.FCCH_TONE
        y = ch.get(f + cfo - r.center_hz)
        res = []
        for (ts, _, q) in g[:8]:
            # SCH is one TDMA frame after FCCH (same timeslot); FCCH window start ~ burst start
            st = int((ts + gsm.FRAME_S) * gsm.CH_RATE)
            for pol in (1.0, -1.0):
                d = gsm.decode_sch_burst(y, st, search=60, polarity=pol)
                if d and d["ok"]:
                    res.append((pol, gsm._parse_sch(d["info"]), round(d["score"], 2), d["train_err"])); break
            else:
                res.append(("fail", d and round(d["score"],2), d and d["train_err"]))
        print(f"  ARFCN {a} {f/1e6:.1f} cfo={cfo:+.0f}", res[:5])
    print("  t=%.1fs" % (time.time() - t))
