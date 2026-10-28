import sys, numpy as np
from rfscan import recording
for name in sys.argv[1:]:
    r = recording.load('testdata/'+name)
    x = r.iq(0, 4_000_000)
    N=100  # 200 kHz bins
    X = np.fft.fftshift(np.fft.fft(x[:len(x)//N*N].reshape(-1,N),axis=1),axes=1)
    p = 10*np.log10(np.mean(np.abs(X)**2,axis=0)+1e-12)
    f = (np.arange(N)-N/2)*r.rate/N + r.center_hz
    fl = np.percentile(p,5)
    print(name, 'p5', round(fl,1))
    print(' '.join(f"{ff/1e6:.1f}:{v-fl:.0f}" for ff,v in zip(f,p)))
