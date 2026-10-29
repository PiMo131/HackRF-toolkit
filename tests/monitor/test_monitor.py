"""Monitor regression: heatmap + summary from a stored run, and a synthetic injected region
is reported as new while a region copied from the previous survey is not.

Runs on runs/20261029_104759 (falls back to the 2026-10-19 archive). Works as a plain
script (`.venv/bin/python tests/monitor/test_monitor.py`) and under pytest.
"""
import json
import os
import struct
import sys
import tempfile
import zlib

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from rfscan import monitor  # noqa: E402

RUNS = [os.path.join(ROOT, "runs", "20261029_104759"),
        os.path.join(ROOT, "iq_archive", "2026-10-19_antwerp", "scan2_odd_ones_1406"),
        os.path.join(ROOT, "iq_archive", "2026-10-19_antwerp", "scan1_full_1400")]
BASELINE = os.path.join(ROOT, "iq_archive", "2026-10-19_antwerp", "scan1_full_1400")


def _run_dir():
    for r in RUNS:
        if os.path.exists(os.path.join(r, "rfscan.sqlite")):
            return r
    raise SystemExit("no stored run with rfscan.sqlite found")


def _png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", head[16:24])


def test_outputs_and_injected_region():
    run = _run_dir()
    base = BASELINE if os.path.exists(os.path.join(BASELINE, "rfscan.sqlite")) and BASELINE != run else None
    surveys = monitor.load_run(run)
    assert surveys and surveys[-1]["path"], "run has no surveys with spectrum files"

    tr = monitor.Tracker(base)
    for s in surveys:
        tr.add_survey(s["id"], s["t"], s["regions"], monitor.load_spec(s["path"]))
    n_before = len(tr.new)

    # synthetic survey: copy of the last spectrum with a +30 dB, 5-bin bump where the baseline is quiet
    spec = monitor.load_spec(surveys[-1]["path"])
    f = spec["freqs"]
    exc = tr.base.excess
    quiet = np.where((f > 1.3e9) & (f < 1.5e9) & (exc < 3.0))[0]
    assert len(quiet) > 50
    k = quiet[len(quiet) // 2]
    f_inj = float(f[k])
    df = float(f[1] - f[0])
    spec["median"] = spec["median"].copy()
    spec["median"][k - 2:k + 3] += 30.0
    injected = dict(f_center=f_inj, f_lo=f_inj - 2.5 * df, f_hi=f_inj + 2.5 * df, bw=5 * df, snr=30.0,
                    occupancy=1.0, continuous=True, band="test")
    # a region seen in the previous survey must NOT come back as new
    prev = max(surveys[-1]["regions"], key=lambda r: r["snr"])
    new = tr.add_survey(999, surveys[-1]["t"] + 900, [injected, dict(prev)], spec)
    freqs = [round(n["f_center"]) for n in new]
    assert round(f_inj) in freqs, f"injected {f_inj/1e6:.3f} MHz not reported: {freqs}"
    assert round(prev["f_center"]) not in freqs, "previously seen region reported as new"
    assert len(tr.new) == n_before + 1

    with tempfile.TemporaryDirectory() as out:
        summ = tr.write(run, out_dir=out)
        w, h = _png_size(os.path.join(out, "heatmap.png"))
        assert w > monitor.HEAT_W and h > 10 * len(tr.rows)
        d = json.load(open(os.path.join(out, "summary.json")))
        assert d["n_surveys"] == len(surveys) + 1 and d["heat_rows"] == len(tr.rows)
        assert any(round(n["f_center"]) == round(f_inj) for n in d["new_signals"])
        assert any(b["band"].startswith("GNSS") for b in d["bands"])
        rows = np.load(os.path.join(out, "heatmap.npz"))["rows"]
        assert rows.shape == (len(tr.rows), monitor.HEAT_W)
        col = int((f_inj - f[0]) / (f[-1] - f[0]) * (monitor.HEAT_W - 1))
        assert rows[-1, max(0, col - 1):col + 2].max() > 25, "injected bump missing from heatmap row"
    # the HTML section renders without touching the run directory's monitor/ files
    html = monitor.html_section(run, len(surveys)) if os.path.exists(os.path.join(run, monitor.OUT_DIR)) else ""
    assert "Monitoring" in html or not html
    print(f"OK: {run} ({len(surveys)} surveys, baseline {'yes' if base else 'no'}), "
          f"{n_before} new signals in the run, injected {f_inj/1e6:.3f} MHz reported as new; "
          f"heatmap {w}x{h}, {summ['n_surveys']} rows")


def test_png_roundtrip():
    rgb = np.random.default_rng(0).integers(0, 255, (7, 11, 3), dtype=np.uint8)
    b = monitor.png_bytes(rgb)
    assert b[:8] == b"\x89PNG\r\n\x1a\n" and b[-12:] == struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
    i = b.index(b"IDAT")
    n = struct.unpack(">I", b[i - 4:i])[0]
    raw = zlib.decompress(b[i + 4:i + 4 + n])
    back = np.frombuffer(raw, np.uint8).reshape(7, 1 + 11 * 3)[:, 1:].reshape(7, 11, 3)
    assert np.array_equal(back, rgb)


if __name__ == "__main__":
    test_png_roundtrip()
    test_outputs_and_injected_region()
