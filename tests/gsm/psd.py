import sys, numpy as np
from rfscan import recording
for name in sys.argv[1:]:
    r = recording.load('testdata/'+name)
    x = r.iq(0, 4_000_000)
    N=1000  # 20 kHz bins
    X = np.fft.fftshift(np.fft.fft(x[:len(x)//N*N].reshape(-1,N),axis=1),axes=1)
    p = 10*np.log10(np.mean(np.abs(X)**2,axis=0)+1e-12)
    f = (np.arange(N)-N/2)*r.rate/N + r.center_hz
    # aggregate 200k
    q = p.reshape(-1,10).max(axis=1); fq = f.reshape(-1,10).mean(axis=1)
    fl = np.median(q)
    print(name, 'floor', round(fl,1))
    print(' '.join(f"{ff/1e6:.1f}:{v-fl:.0f}" for ff,v in zip(fq,q) if v-fl>10))
