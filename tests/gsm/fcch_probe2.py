import sys, numpy as np
from scipy import signal
sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording
SR = 1625e3/6*4
r = recording.load("testdata/" + sys.argv[1]); x = r.iq()
# polyphase approach: one pass decim to 2.5 Msps around each channel is slow; just do a few freqs
for f in map(float, sys.argv[2:]):
    n = np.arange(len(x)); y = x*np.exp(-2j*np.pi*(f*1e6-r.center_hz)/r.rate*n).astype(np.complex64)
    y = signal.resample_poly(y, 13, 240)
    d = y[1:]*np.conj(y[:-1]); L=500
    c=np.cumsum(np.r_[0,d]); m=c[L:]-c[:-L]
    pw=np.cumsum(np.r_[0,np.abs(d)]); pm=pw[L:]-pw[:-L]; q=np.abs(m)/(pm+1e-12)
    i=np.argsort(q)[::-1][:5]
    print(f, "q top", np.round(q[i],2), "freq kHz", np.round(np.angle(m[i])*SR/2/np.pi/1e3,1), "median q", round(float(np.median(q)),3))
