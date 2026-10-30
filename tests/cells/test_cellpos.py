"""Offline cell DB: convert a tiny synthetic cell_towers CSV, look cells up (exact CI, LAC fallback, unknown),
check the weighted estimate and the CLI. Plain script (`.venv/bin/python tests/cells/test_cellpos.py`) or pytest.
"""
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from rfscan import cellpos  # noqa: E402

CSV = """radio,mcc,net,area,cell,unit,lon,lat,range,samples,changeable,created,updated,averageSignal
GSM,204,8,3350,100,,4.40,51.70,1000,20,1,0,0,
GSM,204,8,3350,101,,4.50,51.70,3000,20,1,0,0,
GSM,204,8,3350,102,,4.45,51.66,0,3,1,0,0,
GSM,204,4,203,21751,,4.42,51.71,1500,9,1,0,0,
LTE,204,8,3350,999999,,9.0,50.0,1000,1,1,0,0,
GSM,206,1,10,5,,4.40,51.20,2000,5,1,0,0,
GSM,208,1,10,5,,2.0,48.0,2000,5,1,0,0,
"""
# OpenCelliD per-MCC files come without a header line
CSV_NOHEAD = "GSM,262,1,77,9,,7.0,51.0,500,4,1,0,0,\n"


def _setup(d):
    src = os.path.join(d, "towers.csv.gz")
    with gzip.open(src, "wt") as f:
        f.write(CSV)
    n = cellpos.convert(src, os.path.join(d, "cells"))
    src2 = os.path.join(d, "262.csv")
    with open(src2, "w") as f:
        f.write(CSV_NOHEAD)
    n.update(cellpos.convert(src2, os.path.join(d, "cells")))
    return n, os.path.join(d, "cells")


def test_convert_lookup_estimate():
    with tempfile.TemporaryDirectory() as d:
        n, cd = _setup(d)
        assert n == {204: 4, 206: 1, 262: 1}, n                      # 208 and the LTE row filtered out (default GSM)
        assert cellpos.convert(os.path.join(d, "towers.csv.gz"), os.path.join(d, "all"), radios=None)[204] == 5
        assert not os.path.exists(os.path.join(cd, "208.csv.gz"))
        with gzip.open(os.path.join(cd, "204.csv.gz"), "rt") as f:
            assert f.readline().strip() == ",".join(cellpos.OUT_HEADER)
        cellpos._cache.clear()
        cells = [{"mcc": 204, "mnc": 8, "lac": 3350, "ci": 100, "power_db": -30},
                 {"mcc": 204, "mnc": 8, "lac": 3350, "ci": 100, "power_db": -32},   # duplicate -> merged
                 {"mcc": 204, "mnc": 8, "lac": 3350, "ci": 555, "power_db": -50},   # unknown CI -> LAC centroid
                 {"mcc": 204, "mnc": 4, "lac": 999, "ci": 1, "power_db": -40},      # unknown LAC -> missing
                 {"mcc": 270, "mnc": 1, "lac": 1, "ci": 1}]                         # no file -> missing
        towers, missing = cellpos.lookup(cells, cd)
        by = {t["ci"]: t for t in towers}
        assert len(towers) == 2 and len(missing) == 2, (towers, missing)
        assert by[100]["how"] == "cell" and by[100]["power_db"] == -31 and by[100]["range"] == cellpos.MIN_RANGE
        assert by[555]["how"] == "lac" and abs(by[555]["lat"] - 51.68667) < 1e-4 and abs(by[555]["lon"] - 4.45) < 1e-6
        e = cellpos.estimate(towers)                                       # LAC centroid ignored when a CI is known
        assert e["src"] == "celldb" and e["n"] == 1 and e["lat"] == 51.7 and e["lon"] == 4.4 and "not used" in e["note"], e
        e = cellpos.estimate([by[555]])                                     # only a LAC: its centroid, spread as accuracy
        assert e["n"] == 1 and "LAC" in e["note"] and e["acc"] >= cellpos.MIN_ACC, e
        # two exact cells: weighted towards the stronger one (amplitude / range)
        t2, _ = cellpos.lookup([{"mcc": 204, "mnc": 8, "lac": 3350, "ci": 100, "power_db": -30},
                                {"mcc": 204, "mnc": 8, "lac": 3350, "ci": 101, "power_db": -40}], cd)
        e = cellpos.estimate(t2)
        assert e["n"] == 2 and 4.40 < e["lon"] < 4.42 and e["lat"] == 51.7, e   # weights 1/2000 vs 0.316/3000
        assert e["acc"] >= cellpos.MIN_ACC
        # one tower: accuracy is its range (floored)
        e1 = cellpos.estimate([by[100]])
        assert e1["lat"] == 51.7 and e1["acc"] == cellpos.MIN_ACC
        assert cellpos.estimate([]) is None


def test_cli_sqlite_and_json():
    with tempfile.TemporaryDirectory() as d:
        _, cd = _setup(d)
        db = os.path.join(d, "rfscan.sqlite")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE decodes (id INTEGER, capture_id INTEGER, t REAL, decoder TEXT, level TEXT, crc_ok INTEGER,"
                    " freq REAL, ident TEXT, label TEXT, fields TEXT, iq_path TEXT)")
        for ci, p in ((100, -30), (101, -45)):
            con.execute("INSERT INTO decodes VALUES (1,1,0,'gsm','CRC',1,9e8,'','',?,'')",
                        (json.dumps({"mcc": 204, "mnc": 8, "lac": 3350, "ci": ci, "power_db": p}),))
        con.execute("INSERT INTO decodes VALUES (1,1,0,'lte','CRC',1,9e8,'','','{}','')")
        con.commit()
        con.close()
        out = subprocess.run([sys.executable, "-m", "rfscan.cellpos", db, "--cells-dir", cd],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        j = json.loads(out)
        assert len(j) == 1 and j[0]["src"] == "celldb" and j[0]["n"] == 2 and 4.40 < j[0]["lon"] < 4.45
        js = os.path.join(d, "cells.json")
        with open(js, "w") as f:   # geolocate-API style input works too
            json.dump({"cellTowers": [{"mobileCountryCode": 204, "mobileNetworkCode": 4, "locationAreaCode": 203, "cellId": 21751}]}, f)
        j = json.loads(subprocess.run([sys.executable, "-m", "rfscan.cellpos", js, "--cells-dir", cd],
                                      cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        assert j[0]["lat"] == 51.71 and j[0]["lon"] == 4.42


if __name__ == "__main__":
    test_convert_lookup_estimate()
    test_cli_sqlite_and_json()
    print("ok")
