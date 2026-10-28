import sys, time, numpy as np
from scipy import signal
sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording
SR = 1625e3 / 6 * 4   # 1.0833 Msps OSR4
def chan(x, rate, foff):
    n = np.arange(len(x))
    y = x * np.exp(-2j*np.pi*foff/rate*n).astype(np.complex64)
    return signal.resample_poly(y, 13, 240).astype(np.complex64)
r = recording.load("testdata/" + sys.argv[1]); x = r.iq()
for f in map(float, sys.argv[2:]):
    t=time.time(); y = chan(x, r.rate, f*1e6 - r.center_hz)
    d = y[1:]*np.conj(y[:-1])
    L = 568
    c = np.cumsum(np.r_[0, d]); m = (c[L:]-c[:-L])
    pw = np.cumsum(np.r_[0, np.abs(y[1:])*np.abs(y[:-1])]); pm = pw[L:]-pw[:-L]
    q = np.abs(m)/(pm+1e-12)
    ang = np.angle(m)
    hits = np.where((q > 0.8) & (np.abs(ang - np.pi/8) < 0.1))[0]
    # group
    starts = []
    for h in hits:
        if not starts or h - starts[-1][-1] > 100: starts.append([h])
        else: starts[-1].append(h)
    print(f"{f:.1f} MHz: {len(starts)} FCCH-like, t={time.time()-t:.2f}s", [(s[0], round(float(np.median(ang[s]))*SR/(2*np.pi)-67708,0)) for s in starts[:12]])
