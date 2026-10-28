import sys, time, tempfile
sys.path.insert(0, "/home/jarvis/testscan")
from rfscan import recording
from rfscan.decoders import gsm
for name in sys.argv[1:]:
    r = recording.load("testdata/" + name)
    t = time.time()
    with tempfile.TemporaryDirectory() as td:
        res = gsm.decode(r, {"tmpdir": td})
    print(f"== {name}: {len(res)} results in {time.time()-t:.1f}s")
    for x in res:
        print(f"  [{x['level']}] {x['label']}  si={x['fields'].get('si_seen')} sch_ok={x['fields'].get('sch_ok')} pwr={x['fields']['power_db']}")
