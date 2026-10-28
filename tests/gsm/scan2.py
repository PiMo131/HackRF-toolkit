import sys, time, numpy as np
sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording
SR = 1625e3/6*4
def fft_channelizer(x, rate):
    N = (len(x)//240)*240
    X = np.fft.fft(x[:N]); M = N*13//240
    def get(foff, bw=SR):
        k0 = int(round(foff/(rate/N))); half = M//2
        idx = (np.arange(-half, M-half) + k0) % N
        Y = X[idx].copy(); fr = np.arange(-half, M-half)*(rate/N)
        Y[np.abs(fr) > bw/2] = 0
        return np.fft.ifft(np.fft.ifftshift(Y)).astype(np.complex64)*(M/N)
    return get
def fcch(y, W=512, hop=128):
    n = (len(y)-W)//hop
    idx = np.arange(W)[None,:] + hop*np.arange(n)[:,None]
    S = np.abs(np.fft.fft(y[idx], axis=1))**2
    f = np.fft.fftfreq(W, 1/SR)
    sel = (f > 40e3) & (f < 95e3)
    tot = S.sum(1)+1e-20
    pk = S[:, sel].max(1); kk = np.argmax(S[:, sel],1)
    return pk/tot, f[sel][kk], hop
r = recording.load("testdata/"+sys.argv[1]); x = r.iq()
get = fft_channelizer(x, r.rate)
lo = r.center_hz - 0.45*r.rate; hi = r.center_hz + 0.45*r.rate
fs = np.arange(np.ceil(lo/200e3)*200e3, hi, 200e3)
t=time.time()
for f in fs:
    y = get(f - r.center_hz, 300e3)
    q, fr, hop = fcch(y)
    hit = q > 0.25
    idx = np.where(hit)[0]
    groups=[]
    for i in idx:
        if groups and i-groups[-1][-1] <= 3: groups[-1].append(i)
        else: groups.append([i])
    # FCCH burst ~ 0.546 ms -> ~ (592-512)/128+1 ≈ 1..3 windows; continuous tones give long groups
    bursts=[g for g in groups if len(g)<=6]
    longg=[g for g in groups if len(g)>6]
    if bursts or longg:
        ts=[g[0]*hop/SR*1e3 for g in bursts]
        print(f"{f/1e6:8.1f} bursts {len(bursts):3d} long {len(longg)} maxq {q.max():.2f} medq {np.median(q):.3f} t(ms) {np.round(ts[:6],1)} f {np.round([np.median(fr[g])/1e3 for g in bursts[:4]],1)}")
print("scan", round(time.time()-t,2))
