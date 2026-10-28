import sys, time, numpy as np
sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording
SR = 1625e3/6*4
def fft_channelizer(x, rate):
    N = (len(x)//240)*240
    t=time.time(); X = np.fft.fft(x[:N]); 
    M = N*13//240
    def get(foff, bw=SR):
        k0 = int(round(foff/(rate/N)))
        half = M//2
        idx = (np.arange(-half, M-half) + k0) % N
        Y = X[idx]
        fr = np.arange(-half, M-half)*(rate/N)
        Y[np.abs(fr) > bw/2] = 0
        return np.fft.ifft(np.fft.ifftshift(Y)).astype(np.complex64)*(M/N)
    return get
r = recording.load("testdata/"+sys.argv[1]); x = r.iq()
t=time.time(); get = fft_channelizer(x, r.rate); print("fft", round(time.time()-t,2))
lo = r.center_hz - 0.45*r.rate; hi = r.center_hz + 0.45*r.rate
fs = np.arange(np.ceil(lo/200e3)*200e3, hi, 200e3)
t=time.time()
for f in fs:
    y = get(f - r.center_hz, 300e3)
    d = y[1:]*np.conj(y[:-1])
    L = 500
    c=np.cumsum(np.r_[0,d]); m=c[L:]-c[:-L]
    pw=np.cumsum(np.r_[0,np.abs(d)]); pm=pw[L:]-pw[:-L]; q=np.abs(m)/(pm+1e-12)
    fr = np.angle(m)*SR/2/np.pi
    ok = (q>0.85)&(np.abs(fr-67708)<25e3)
    # count separated bursts
    idx=np.where(ok)[0]; nb = 0 if len(idx)==0 else 1+int(np.sum(np.diff(idx)>2000))
    p = 10*np.log10(np.mean(np.abs(y)**2)+1e-20)
    if nb>0: print(f"{f/1e6:8.1f} pwr {p:6.1f} fcch_bursts {nb} medq {np.median(q):.2f} freq {np.median(fr[ok])/1e3:.1f}k")
print("scan", round(time.time()-t,2), len(fs), "ch")
