"""Mode S / ADS-B 1090 MHz decoder (native numpy demod + CRC-24, pyModeS v3 field decode).

Pipeline: channel at 1090 MHz -> resample to 8 Msps (4 samples/chip) -> magnitude ->
vectorised preamble search -> PPM bit slicing (56/112 bit) -> CRC-24 (with 1-bit
error correction for DF17/18) -> per-aircraft aggregation.  DF0/4/5/16/20/21 use
address-parity: accepted only when the recovered address belongs to an aircraft
already confirmed by a DF11/17/18 message.
"""
from fractions import Fraction

import numpy as np
from scipy import signal

from . import result

NAME = "adsb"
CAPTURE = {"rate": 2.4e6, "seconds": 10, "center": 1090e6}
FREQ = 1090e6
REF_POS = (52.1, 5.1)   # receiver reference (Netherlands) for single-message CPR decode
FS = 8e6                # working rate: 4 samples per 0.5 us chip
CHUNK = 8_000_000       # samples per processing chunk at FS

_POLY = 0xFFF409


def _crc_table():
    t = np.zeros(256, np.uint32)
    for i in range(256):
        c = i << 16
        for _ in range(8):
            c = ((c << 1) ^ _POLY) if c & 0x800000 else (c << 1)
        t[i] = c & 0xFFFFFF
    return t


_TABLE = _crc_table()


def crc24(msgs):
    """msgs: (N, nbytes) uint8 -> (N,) remainder over the whole message (0 = valid for DF11/17/18)."""
    crc = np.zeros(len(msgs), np.uint32)
    for k in range(msgs.shape[1]):
        idx = ((crc >> 16) ^ msgs[:, k]) & 0xFF
        crc = ((crc << 8) & 0xFFFFFF) ^ _TABLE[idx]
    return crc


def _syndromes(nbits):
    """remainder produced by a single flipped bit at each position -> bit index."""
    nbytes = nbits // 8
    m = np.zeros((nbits, nbytes), np.uint8)
    for j in range(nbits):
        m[j, j // 8] = 0x80 >> (j % 8)
    syn = crc24(m)
    return {int(s): j for j, s in enumerate(syn) if j >= 5}   # never flip DF bits


_SYN112 = _syndromes(112)


def _to_4msps(iq, rate, center):
    off = FREQ - center
    if abs(off) > 1e3:
        n = np.arange(len(iq), dtype=np.float64)
        iq = iq * np.exp(-2j * np.pi * off / rate * n).astype(np.complex64)
    if abs(rate - FS) < 1:
        return iq
    fr = Fraction(FS / rate).limit_denominator(64)
    return signal.resample_poly(iq, fr.numerator, fr.denominator).astype(np.complex64)


def _win(m, n, o, w):
    """sum of m[i+o : i+o+w] for i in 0..n-1 (via cumulative sum)."""
    return _CS[o + w:o + w + n] - _CS[o:o + n]


_CS = None
_AP = []   # (indices, msg rows, remainders) of address/parity candidates, per decode() call


def _demod(mag, t0):
    """Find preambles in magnitude array at FS; return list of (t_s, bytes, kind, rem)."""
    global _CS
    c_ = int(FS * 0.5e-6)            # samples per 0.5 us chip
    m = mag
    n = len(m) - (16 + 224) * c_ - 4 * c_
    if n <= 0:
        return []
    _CS = np.concatenate(([0.0], np.cumsum(m, dtype=np.float64)))
    hi = (_win(m, n, 0, c_) + _win(m, n, 2 * c_, c_) + _win(m, n, 7 * c_, c_) + _win(m, n, 9 * c_, c_)) / (4 * c_)
    lo = (_win(m, n, c_, c_) + _win(m, n, 3 * c_, 4 * c_) + _win(m, n, 10 * c_, 6 * c_)) / (11 * c_)
    noise = np.median(m) + 1e-9
    cand = np.where((hi > 2.0 * lo) & (hi > 2.2 * noise))[0]
    if len(cand) == 0:
        return []
    # keep the best-scoring sample within each 0.5 us cluster
    score = hi[cand] / (lo[cand] + 1e-9)
    grp = np.r_[0, np.cumsum(np.diff(cand) > c_)]
    best = {}
    for g, cidx, sc in zip(grp, cand, score):
        if g not in best or sc > best[g][1]:
            best[g] = (cidx, sc)
    cand = np.array([v[0] for v in best.values()])
    out = []
    ap_store = _AP
    k = np.arange(112) * 2 * c_
    data0 = 16 * c_
    span = max(1, c_ // 2)
    for d in range(-span, span + 1):
        c = cand + d
        c = c[(c >= 0) & (c < n)]
        base = c[:, None] + data0 + k[None, :]
        a = _CS[base + c_] - _CS[base]
        b = _CS[base + 2 * c_] - _CS[base + c_]
        bits = (a > b).astype(np.uint8)
        by = np.packbits(bits, axis=1)
        df = by[:, 0] >> 3
        rem_l = crc24(by)
        rem_s = crc24(by[:, :7])
        long_ = df >= 16
        # CRC-checked formats: DF17/18 (with 1-bit repair) and DF11
        for i in np.where(long_ & ((df == 17) | (df == 18)))[0]:
            r = int(rem_l[i])
            msg = by[i].copy()
            if r != 0:
                j = _SYN112.get(r)
                if j is None:
                    continue
                msg[j // 8] ^= 0x80 >> (j % 8)
            out.append((c[i], msg.tobytes(), "crc", None))
        for i in np.where((df == 11) & ((rem_s & 0xFFFF80) == 0))[0]:
            out.append((c[i], by[i, :7].tobytes(), "crc", None))
        # address/parity formats: keep vectorised, resolved later against confirmed ICAOs
        ap_l = long_ & ((df == 20) | (df == 21) | (df == 16))
        ap_s = (df == 0) | (df == 4) | (df == 5)
        for sel, rem, nb in ((ap_l, rem_l, 14), (ap_s, rem_s, 7)):
            ii = np.where(sel)[0]
            if len(ii):
                ap_store.append((c[ii], by[ii, :nb], rem[ii]))
    seen, res = set(), []
    for idx, msg, kind, r in sorted(out, key=lambda x: (x[0], x[2] != "crc")):
        key = idx // (8 * c_)
        if key in seen or (key - 1) in seen:
            continue
        if kind == "crc":
            seen.add(key)
        res.append(((t0 + idx) / FS, msg, kind, r))
    return res


def _decode_fields(hexmsg):
    try:
        import pyModeS
        return dict(pyModeS.decode(hexmsg, reference=REF_POS))
    except Exception:
        return {}


def decode(rec, ctx=None):
    if abs(rec.center_hz - FREQ) > rec.rate * 0.45:
        return []
    msgs = []
    _AP.clear()
    ap_t0 = []
    n_total = rec.n_samples()
    step = int(CHUNK * rec.rate / FS)
    ovl = int(200e-6 * rec.rate)
    for s in range(0, n_total, step):
        iq = rec.iq(s, step + ovl)
        iq = iq - iq.mean()
        x = _to_4msps(iq, rec.rate, rec.center_hz)
        mag = np.abs(x).astype(np.float32)
        t0 = int(round(s * FS / rec.rate))
        before = len(_AP)
        msgs.extend(_demod(mag, t0))
        ap_t0.extend([t0] * (len(_AP) - before))
    # dedupe overlap
    seen, uniq = set(), []
    for t, m, kind, r in msgs:
        key = (round(t * 1e5), m)
        if key not in seen:
            seen.add(key)
            uniq.append((t, m, kind, r))
    confirmed = {}
    for t, m, kind, r in uniq:
        if kind == "crc":
            icao = m[1:4].hex().upper()
            confirmed.setdefault(icao, []).append((t, m))
    if confirmed and _AP:
        known = np.array([int(k, 16) for k in confirmed], np.uint32)
        for (idx, rows, rem), t0 in zip(_AP, ap_t0):
            for i in np.where(np.isin(rem, known))[0]:
                confirmed[f"{int(rem[i]):06X}"].append(((t0 + idx[i]) / FS, rows[i].tobytes()))
        # one frame can be sliced at several timing offsets: dedupe per aircraft
        for icao, lst in confirmed.items():
            lst.sort()
            ded = []
            for t, m in lst:
                if ded and ded[-1][1] == m and t - ded[-1][0] < 5e-6:
                    continue
                ded.append((t, m))
            confirmed[icao] = ded
    _AP.clear()
    out = []
    for icao, lst in confirmed.items():
        lst.sort()
        info = {"icao": icao, "messages": len(lst), "dfs": {}}
        positions = []
        for t, m in lst:
            df = m[0] >> 3
            info["dfs"][int(df)] = info["dfs"].get(int(df), 0) + 1
            if df not in (17, 18, 4, 20, 5, 21, 11):
                continue
            f = _decode_fields(m.hex().upper())
            for k in ("callsign", "altitude", "groundspeed", "track", "vertical_rate",
                      "squawk", "category", "airspeed", "heading", "wake_vortex"):
                v = f.get(k)
                if v is not None:
                    info[k] = v
            if f.get("latitude") is not None:
                positions.append((round(t, 3), f["latitude"], f["longitude"]))
        if positions:
            info["lat"], info["lon"] = positions[-1][1], positions[-1][2]
            info["positions"] = positions[-20:]
        # a lone DF11 hit is weak evidence; require >=2 CRC messages
        if sum(1 for _ in lst) < 2:
            continue
        lbl = f"ICAO {icao}"
        if "callsign" in info:
            lbl += f" {info['callsign'].strip('_ ')}"
        if "altitude" in info:
            lbl += f" {info['altitude']} ft"
        if "lat" in info:
            lbl += f" @ {info['lat']:.4f},{info['lon']:.4f}"
        if "groundspeed" in info:
            lbl += f" {info['groundspeed']} kt"
        lbl += f" ({len(lst)} msgs)"
        out.append(result(NAME, "crc", FREQ, f"ICAO:{icao}", lbl, crc_ok=True,
                          t_s=float(lst[0][0]), **info))
    return out
