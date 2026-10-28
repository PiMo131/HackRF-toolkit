import sys, numpy as np
from rfscan import recording
for name in sys.argv[1:]:
    r = recording.load('testdata/'+name)
    x = r.iq(0, 4_000_000)
    print(name, 'mean', x.mean(), 'rms', np.sqrt(np.mean(np.abs(x)**2)))
    x = x - x.mean()
    N=1000
    w=np.hanning(N)
    X = np.fft.fftshift(np.fft.fft(x[:len(x)//N*N].reshape(-1,N)*w,axis=1),axes=1)
    p = 10*np.log10(np.mean(np.abs(X)**2,axis=0)+1e-12)
    f = (np.arange(N)-N/2)*r.rate/N + r.center_hz
    q = p.reshape(-1,10).max(axis=1); fq=f.reshape(-1,10)[:,0]
    fl = np.percentile(q,10)
    print(' '.join(f"{ff/1e6:.1f}:{v-fl:.0f}" for ff,v in zip(fq,q)))
