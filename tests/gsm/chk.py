import sys, numpy as np
from rfscan import recording
for name in sys.argv[1:]:
    r = recording.load('testdata/'+name)
    raw = np.memmap(r.data_path, dtype=np.int8, mode='r')[:2_000_000].astype(np.float32)
    I, Q = raw[0::2], raw[1::2]
    def ac(v,k): v=v-v.mean(); return float(np.dot(v[:-k],v[k:])/np.dot(v,v))
    print(name, r.rate, 'I std', I.std(), 'Q std', Q.std(), 'acI1', round(ac(I,1),3), 'acI2', round(ac(I,2),3), 'IQcorr', round(float(np.corrcoef(I,Q)[0,1]),3), 'I->Q lag', round(float(np.corrcoef(I[1:],Q[:-1])[0,1]),3))
    print('  first bytes', raw[:24].astype(int).tolist())
