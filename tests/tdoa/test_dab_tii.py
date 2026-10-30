"""DAB TII + TDOA positioning tests (rfscan.tdoa_dab).

Run:  .venv/bin/python tests/tdoa/test_dab_tii.py        (or with pytest if installed)

1. test_solver:        exact synthetic delays -> Gauss-Newton TDOA recovers the position.
2. test_synthetic_sfn: a synthetic mode-I SFN (5 transmitters, own TII each, fractional
                       delays from geometry, noise) -> TII decoding + delays + fix < 150 m.
3. test_real_smoke:    first 3 s of runs/locate_20261030/dab_207 (9C): Rotterdam TII 19-05
                       must be found (skipped when the capture is absent).
"""
import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from rfscan import tdoa_dab as T  # noqa: E402

C = T.C
FS = T.FS
# synthetic geometry (not real sites): receiver and 5 transmitters around it
RX = (51.60, 4.70)
SITES = [("A", 51.75, 4.55, 4, 10), ("B", 51.45, 4.60, 4, 5), ("C", 51.62, 5.05, 3, 12),
         ("D", 51.85, 4.95, 4, 4), ("E", 51.40, 4.95, 4, 3)]


def _dist(a, b):
    """WGS84 3-D distance between two points on the ellipsoid (m)."""
    return float(np.linalg.norm(T._ecef(*a) - T._ecef(*b)))


def test_solver():
    rx_clock = 123e-6
    tx = [dict(lat=la, lon=lo, group="g", tau_s=_dist((la, lo), RX) / C + rx_clock, sigma_s=20e-9)
          for _, la, lo, *_ in SITES]
    sol = T.solve_tdoa(tx)
    err = _dist((sol["lat"], sol["lon"]), RX)
    assert err < 1.0, err
    # two ensembles with different clock offsets, 3 + 2 transmitters
    for i, t in enumerate(tx):
        t["group"] = "a" if i < 3 else "b"
        t["tau_s"] += 0 if i < 3 else 37e-6
    sol = T.solve_tdoa(tx)
    assert _dist((sol["lat"], sol["lon"]), RX) < 1.0
    print(f"test_solver ok (err {err:.3f} m)")


def _symbol(X, cp):
    t = np.fft.ifft(X) * math.sqrt(T.TU)
    return np.r_[t[-cp:], t]


def synth_sfn(n_frames=24, snr_db=0.0, seed=1, sites=SITES, rx=RX, amps=None):
    """Baseband 2.048 Msps mode-I SFN as seen at rx. TII on alternate frames.
    Returns (y, true window-free delays in samples per site)."""
    rng = np.random.default_rng(seed)
    P = T.prs_spectrum()
    k = np.r_[1:769, -768:0] % T.TU
    data = []
    for _ in range(n_frames):
        syms = []
        for _ in range(75):
            X = np.zeros(T.TU, complex)
            X[k] = np.exp(1j * np.pi / 2 * (rng.integers(0, 4, len(k)) + 0.5))
            syms.append(X)
        data.append(syms)
    d = np.array([_dist((la, lo), rx) / C * FS for _, la, lo, *_ in sites])
    d_rel = d - d.min() + 20.0                     # samples, fractional
    amps = amps if amps is not None else [1.0, 0.7, 0.5, 0.6, 0.45]
    total = None
    for (name, la, lo, main, sub), dl, a in zip(sites, d_rel, amps):
        ks, k1 = T.tii_carriers(main, sub)
        out = []
        for f in range(n_frames):
            Xn = np.zeros(T.TU, complex)
            if f % 2 == 1:
                Xn[ks % T.TU] = P[k1 % T.TU]
            out.append(_symbol(Xn, T.TNULL - T.TU))
            out.append(_symbol(P, T.TG))
            out += [_symbol(X, T.TG) for X in data[f]]
        x = np.concatenate(out)
        n = len(x)
        F = np.fft.fft(x)
        fr = np.fft.fftfreq(n)
        x = np.fft.ifft(F * np.exp(-2j * np.pi * fr * dl)) * a      # fractional delay
        total = x if total is None else total + x
    p = np.mean(np.abs(total) ** 2)
    noise = (rng.standard_normal(len(total)) + 1j * rng.standard_normal(len(total))) * math.sqrt(
        p / 10 ** (snr_db / 10) / 2) * math.sqrt(1536 / 2048)
    y = (total + noise).astype(np.complex64)
    y = y * np.exp(2j * np.pi * 350.0 / FS * np.arange(len(y)))       # CFO 350 Hz
    return y.astype(np.complex64), d_rel


def test_synthetic_sfn():
    y, d_rel = synth_sfn()
    r = T.process_y(y, "SYN", 0.0)
    assert r is not None, "no sync"
    found = {(t["main"], t["sub"]): t for t in r["tii"]}
    codes = [(m, s) for *_, m, s in SITES]
    for c in codes:
        assert c in found, f"TII {c} not detected; found {list(found)}"
    # delays relative to the first site
    ref = found[codes[0]]["cands"][0]["tau"]
    for (name, *_ , m, s), dl in zip(SITES, d_rel):
        got = found[(m, s)]["cands"][0]["tau"] - ref
        want = dl - d_rel[0]
        assert abs(got - want) < 0.3, (name, got, want)
    rows = [dict(lat=la, lon=lo, site=n, group="syn", cands=found[(m, s)]["cands"])
            for n, la, lo, m, s in SITES]
    sol, combo = T.solve_with_ambiguity(rows)
    err = _dist((sol["lat"], sol["lon"]), RX)
    assert err < 150, err
    print(f"test_synthetic_sfn ok: {len(found)} TII, fix error {err:.0f} m, acc {sol['acc']:.0f} m")


def test_cli_synthetic():
    """End to end through the CLI: synthetic SFN at 20 Msps as SigMF + a synthetic database."""
    import json
    import tempfile
    from scipy import signal
    from rfscan import recording
    y, _ = synth_sfn(n_frames=24, seed=2)
    fs, f_off = 20e6, 227.360e6 - 224e6                # block 12C in a 224 MHz capture
    x = signal.resample_poly(y, 625, 64).astype(np.complex64)
    x *= np.exp(2j * np.pi * f_off / fs * np.arange(len(x))).astype(np.complex64)
    x /= np.max(np.abs(x.view(np.float32)))
    with tempfile.TemporaryDirectory() as td:
        recording.write(os.path.join(td, "syn"), 224e6, fs, (x * 0.9).astype(np.complex64), "ci16_le")
        db = {"ensembles": [{"block": "12C", "eid": "0000", "label": "SYN", "transmitters": [
            {"site": n, "lat": la, "lon": lo, "tii_main": m, "tii_sub": s_} for n, la, lo, m, s_ in SITES]}]}
        with open(os.path.join(td, "db.json"), "w") as f:
            json.dump(db, f)
        est = T.main([os.path.join(td, "syn.sigmf-meta"), "--blocks", "12C", "--db",
                      os.path.join(td, "db.json"), "--json-only"])
    assert est and est[0]["src"] == "dabtii" and est[0]["n"] == 5, est
    assert set(est[0]) == {"src", "lat", "lon", "acc", "n", "note"}
    err = _dist((est[0]["lat"], est[0]["lon"]), RX)
    assert err < 200, err
    print(f"test_cli_synthetic ok: fix error {err:.0f} m, acc {est[0]['acc']} m")


def test_real_smoke():
    cap = os.path.join(ROOT, "runs", "locate_20261030", "dab_207.sigmf-meta")
    if not os.path.exists(cap):
        print("test_real_smoke skipped (capture absent)")
        return
    from rfscan import recording
    rec = recording.load(cap)
    y = T.channelise(rec, 206.352, max_s=3.0)
    r = T.process_y(y, "9C", 206.352)
    assert r is not None
    codes = {(t["main"], t["sub"]) for t in r["tii"]}
    assert (19, 5) in codes, codes                 # Rotterdam, strongest 9C transmitter there
    print(f"test_real_smoke ok: 9C TII {sorted(codes)}")


if __name__ == "__main__":
    test_solver()
    test_synthetic_sfn()
    test_cli_synthetic()
    test_real_smoke()
