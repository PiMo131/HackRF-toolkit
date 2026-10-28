import sys, subprocess, time
sys.path.insert(0, "/home/jarvis/testscan")
import numpy as np
from rfscan import recording
from rfscan.decoders import gsm
r = recording.load("testdata/" + sys.argv[1]); x = r.iq()
a = int(sys.argv[2])
chans = gsm.arfcn_list(r.center_hz - 0.45 * r.rate, r.center_hz + 0.45 * r.rate)
f = [c[1] for c in chans if c[0] == a][0]
ch = gsm.Channelizer(x, r.rate)
y = ch.get(f + float(sys.argv[3]) - r.center_hz)
path = "tests/gsm/tmp/a%d.cfile" % a
y.astype(np.complex64).tofile(path)
t = time.time()
p = subprocess.run(["grgsm_decode", "-c", path, "-f", str(f), "-s", str(gsm.CH_RATE), "-m", "BCCH", "-t", "0", "-v"],
                   capture_output=True, text=True, timeout=120)
print("rc", p.returncode, "%.1fs" % (time.time() - t))
print("\n".join(l for l in p.stdout.splitlines() if "xtrx" not in l and "CPU" not in l)[:3000])
print("ERR", p.stderr[-1500:])
