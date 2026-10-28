import sys, time
from rfscan import recording
from rfscan.decoders import lte, nr
mod = {"lte": lte, "nr": nr}[sys.argv[1]]
for n in sys.argv[2:]:
    r = recording.load("testdata/" + n)
    t = time.time()
    res = mod.decode(r, {})
    print(f"== {n} ({r.n_samples()/r.rate:.2f}s) {len(res)} results, {time.time()-t:.1f}s", flush=True)
    for x in res:
        f = x["fields"]
        extra = f"z={f['sss_z']:.1f} rho={f['pss_rho']:.2f} cfo={f.get('cfo_hz', f.get('offset_from_gscn_hz', 0)):.0f} pwr={f['pss_power_dbfs']:.1f}"
        print("  ", x["level"], x["label"], extra, flush=True)
