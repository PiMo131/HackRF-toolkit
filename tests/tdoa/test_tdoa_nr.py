"""NR SSB TDOA regression.

1. Synthetic: three NR cells (case C SSBs, 20 ms period, 30 kHz SCS) with known fractional delays
   summed into one stream, written as a 20 Msps wideband SigMF -> rfscan.tdoa_nr.measure_nr must find
   all PCIs and recover the frame-start offsets (vs the strongest cell) to well within one 3.84 Msps
   sample (260 ns).
2. Solver: hyperbolic TDOA recovers a known position from 4 sites (and 2 capture groups).
3. Smoke test on a 0.2 s slice of a real capture (skipped if the capture is missing).

Plain script (`.venv/bin/python tests/tdoa/test_tdoa_nr.py`) or pytest.
"""
import os
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from rfscan import recording, tdoa, tdoa_nr  # noqa: E402
from rfscan.decoders import nr  # noqa: E402

FS0 = 7.68e6
NFFT = 256
CP = 18
TRUTH = (51.69, 4.44)


def _ssb(pci, issb, rng):
    nid1, nid2 = divmod(pci, 3)
    grid = np.zeros((4, 240), complex)
    k = np.arange(56, 183)
    grid[0, k] = nr.pss_seq(nid2)
    grid[2, k] = nr.sss_seq(nid1, nid2)
    for l in (1, 2, 3):
        for kk in range(240):
            if l == 2 and 48 <= kk < 192:
                continue
            grid[l, kk] = ((rng.integers(0, 2) * 2 - 1) + 1j * (rng.integers(0, 2) * 2 - 1)) / np.sqrt(2)
    r = nr._dmrs_seq(pci, issb)
    for i, (l, kk) in enumerate(nr._dmrs_res(pci)):
        grid[l, kk] = r[i]
    out = []
    for l in range(4):
        X = np.zeros(NFFT, complex)
        X[(np.arange(240) - 120) % NFFT] = grid[l]
        t = np.fft.ifft(X) * np.sqrt(NFFT)
        out.append(np.concatenate([t[-CP:], t]))
    return np.concatenate(out)


def make_cells(cells, dur=0.2, t_hf0=1.234e-3, rng=None):
    """cells: list of (pci, delay_s, amplitude, beams).  Frame (half-frame) starts at t_hf0 + k*20 ms;
    each cell's PSS useful part starts at t_hf0 + ssb_offset_s(issb) + delay."""
    rng = rng or np.random.default_rng(0)
    n = int(dur * FS0)
    x = np.zeros(n, complex)
    for pci, delay, amp, beams in cells:
        xc = np.zeros(n, complex)
        for t0 in np.arange(t_hf0, dur, 0.02):
            for b in beams:
                ts = t0 + nr.ssb_offset_s(b)                       # PSS useful-part start
                pos = int(round(ts * FS0)) - CP                    # CP start (integer, exact here)
                seg = _ssb(pci, b, rng) * amp
                if pos + len(seg) < n:
                    xc[pos:pos + len(seg)] += seg
        # fractional delay via a frequency-domain phase ramp
        f = np.fft.fftfreq(n, 1 / FS0)
        xc = np.fft.ifft(np.fft.fft(xc) * np.exp(-2j * np.pi * f * delay))
        x += xc
    x += (rng.normal(size=n) + 1j * rng.normal(size=n)) * 0.05
    return x


def to_wideband(x, fs=20e6, f_off=2.0e6, snr_db=10, rng=None):
    rng = rng or np.random.default_rng(1)
    n = len(x)
    X = np.fft.fft(x)
    N = int(round(n * fs / FS0))
    Y = np.zeros(N, complex)
    h = n // 2
    s = int(round(f_off / (fs / N)))
    Y[(np.arange(0, h) + s) % N] = X[:h]
    Y[(np.arange(-h, 0) + s) % N] = X[h:]
    y = np.fft.ifft(Y) * N / n
    p = np.mean(np.abs(y) ** 2)
    nv = p / 10 ** (snr_db / 10)
    y = y + (rng.normal(size=N) + 1j * rng.normal(size=N)) * np.sqrt(nv / 2)
    return (y / np.max(np.abs(y)) * 0.7).astype(np.complex64)


def test_synthetic_offsets():
    fc = 3600e6
    g, fref = nr.gscn_of(fc + 2.0e6)
    # PCI 517 (N_ID2 1) strongest, PCI 300 (N_ID2 0) +3.37 us, PCI 89 (N_ID2 2) -1.53 us
    cells = [(517, 0.40e-6, 1.0, (0, 1, 2, 3)), (300, 3.77e-6, 0.6, (0, 1)), (89, -1.13e-6, 0.45, (2, 3))]
    x = make_cells(cells)
    w = to_wideband(x, f_off=fref - fc)
    with tempfile.TemporaryDirectory() as d:
        rec = recording.write(os.path.join(d, "synth_nr_tdoa"), fc, 20e6, w, "ci16_le", "synthetic NR TDOA")
        cap = tdoa_nr.measure_nr(rec.meta_path, {"skip_s": 0.0})
    found = {c["pci"]: c for c in cap["cells"]}
    print("synthetic NR cells:")
    tdoa_nr.print_capture(cap)
    ref = cap["cells"][cap["ref"]]
    assert ref["pci"] == 517, ref["pci"]
    for pci, delay, _, beams in cells:
        assert pci in found, f"PCI {pci} not found ({sorted(found)})"
        c = found[pci]
        want = (delay - cells[0][1]) * 1e6
        err = c["offset_us"] - want
        print(f"  PCI {pci}: offset {c['offset_us']:.4f} us, expected {want:.4f} us, err {err*1e3:.1f} ns, "
              f"std {c['offset_std_us']*1e3:.1f} ns, SSB idx {c['issb']} (expected {list(beams)})")
        assert abs(err) < 0.26, err                      # within one 3.84 Msps sample
        assert set(c["issb"]) == set(beams), c["issb"]


def _tx(sites, truth, sigma, rng, groups=None):
    tx = []
    for j, (la, lo) in enumerate(sites):
        e = tdoa.to_enu(la, lo, 25.0, *truth)
        d = np.linalg.norm(e - [0, 0, 1.5])
        g = 0 if groups is None else groups[j]
        tx.append(dict(lat=la, lon=lo, h=25.0, t=d / tdoa.C_LIGHT + 1e-3 * (g + 1) + rng.normal() * sigma,
                       sigma=sigma, group=g))
    return tx


def test_solver():
    rng = np.random.default_rng(3)
    sites = [(51.6920, 4.4379), (51.7293, 4.4293), (51.6384, 4.4524), (51.6618, 4.3812)]
    sol = tdoa.solve(_tx(sites, TRUTH, 30e-9, rng), sigma_sync_s=30e-9)
    err = tdoa.haversine_m(sol["lat"], sol["lon"], *TRUTH)
    print(f"solver 4 sites: err {err:.1f} m, acc {sol['acc_m']:.1f} m")
    assert err < 60 and sol["acc_m"] < 150
    sites5 = sites + [(51.7451, 4.4488)]
    sol = tdoa.solve(_tx(sites5, TRUTH, 30e-9, rng, groups=[0, 0, 0, 1, 1]), sigma_sync_s=30e-9)
    err = tdoa.haversine_m(sol["lat"], sol["lon"], *TRUTH)
    print(f"solver 5 sites / 2 capture groups: err {err:.1f} m, acc {sol['acc_m']:.1f} m")
    assert err < 80
    # ENU round trip
    e = tdoa.to_enu(51.70, 4.47, 30.0, *TRUTH)
    la, lo, h = tdoa.from_enu(*e, *TRUTH)
    assert abs(la - 51.70) < 1e-7 and abs(lo - 4.47) < 1e-7 and abs(h - 30.0) < 1e-3


def test_real_slice():
    src = os.path.join(ROOT, "runs", "locate_20261030", "lte_b7_2660.sigmf-meta")
    if not os.path.exists(src):
        print("real capture missing, skipped")
        return
    r = recording.load(src)
    n = int(0.2 * r.rate)
    raw = np.memmap(r.data_path, dtype=np.int8, mode="r")[: 2 * n]
    with tempfile.TemporaryDirectory() as d:
        rec = recording.write(os.path.join(d, "slice"), r.center_hz, r.rate, np.array(raw), "ci8", "0.2 s slice")
        cap = tdoa_nr.measure_lte(rec.meta_path, {"skip_s": 0.0, "ppm_max": 1.0})
    tdoa_nr.print_capture(cap)
    pcis = {c["pci"] for c in cap["cells"]}
    assert 213 in pcis, pcis
    c = [c for c in cap["cells"] if c["pci"] == 213][0]
    # reference vs its own (median-per-frame) clock: the spread is the single-PSS timing noise
    assert abs(c["offset_us"]) < 0.05 and c["offset_std_us"] < 0.3, c


if __name__ == "__main__":
    test_solver()
    test_synthetic_offsets()
    test_real_slice()
    print("OK")
