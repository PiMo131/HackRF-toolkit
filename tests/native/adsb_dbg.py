import numpy as np
from scipy import signal
from rfscan import recording
from rfscan.decoders import adsb
r=recording.load('testdata/adsb_1090')
iq=r.iq(0,24_000_000); iq=iq-iq.mean()
x=signal.resample_poly(iq,10,3).astype(np.complex64)   # 8 Msps, 4 samples/chip
m=np.abs(x); FS=8e6
n=len(m)-1000
def S(o,w=4): # sum of chip window starting at offset o (in samples) for all i
    return sum(m[o+k:n+o+k] for k in range(w))
hi=(S(0)+S(8)+S(28)+S(36))/16
lo=(S(4)+S(16,8)+S(44,8))/20
noise=np.median(m)
for thr in (1.8,2.2,3):
    cand=np.where((hi>thr*lo)&(hi>2.5*noise))[0]
    print(thr,len(cand))
cand=np.where((hi>1.8*lo)&(hi>2.5*noise))[0]
k=np.arange(112)*8
ok=0; good=[]
for d in range(-3,4):
    c=cand+d; c=c[(c>0)&(c+64+112*8+8<len(m))]
    base=c[:,None]+64+k[None,:]
    a=sum(m[base+j] for j in range(4)); b=sum(m[base+4+j] for j in range(4))
    by=np.packbits((a>b).astype(np.uint8),axis=1)
    rl=adsb.crc24(by); rs=adsb.crc24(by[:,:7])
    df=by[:,0]>>3
    g=np.where(((rl==0)&((df==17)|(df==18))) | ((rs==0)&(df==11)))[0]
    good+= [ (c[i], by[i].tobytes().hex()) for i in g]
    fix=sum(1 for i in range(len(c)) if df[i] in (17,18) and int(rl[i]) in adsb._SYN112)
    print(d, len(g), 'fixable', fix)
print(sorted(set(h[:8] for _,h in good)))
