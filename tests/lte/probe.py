import sys
import numpy as np, scipy.fft as F
from rfscan import recording
from rfscan.decoders import lte
from rfscan.decoders._ofdm_util import Channelizer, sliding_energy
name, f0, f1 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
r = recording.load('testdata/' + name); x = r.iq(0, 2000000)
ch = Channelizer(x, r.rate, (1.92e6,))
best = []
for f in np.arange(f0, f1, 100e3):
    for c in np.arange(-25e3, 25001, 2.5e3):
        Y, _ = ch.spectrum(f - r.center_hz + c, 1.92e6); y = F.ifft(Y); E = sliding_energy(y, 128)
        for k, T in enumerate(lte._tspecs(len(Y))):
            rho = np.abs(F.ifft(Y * T)) ** 2 / (E * lte._PSS_E[k])
            fo, _ = lte.fold_max(rho, 9600)
            best.append((fo.max(), np.median(fo), rho.max(), f / 1e6, c, k, int(fo.argmax())))
best.sort(reverse=True)
for b in best[:12]: print(['%.4g' % v for v in b])
