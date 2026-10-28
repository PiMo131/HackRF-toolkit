"""AIS decoder (161.975 / 162.025 MHz): GMSK 9600 Bd, NRZI, HDLC, CRC-16 X.25.

Each channel is mixed to baseband and resampled to 48 kHz (5 samples/symbol), FM
discriminated, high-passed against a moving average (removes HackRF ppm offset),
then hard-sliced at all 5 symbol phases.  Flag search, de-stuffing and CRC run on
bit strings (fast C string ops).  Decodes types 1/2/3/4/5/18/19/21/24.
"""
from fractions import Fraction

import numpy as np
from scipy import signal

from . import result

NAME = "ais"
CAPTURE = {"rate": 2e6, "seconds": 30, "center": 162.0e6}
CHANNELS = {"A": 161.975e6, "B": 162.025e6}
FS = 48000
SPS = 5
CHUNK_S = 10.0

_SIXBIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


def _crc16_x25(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def _channel(iq, rate, off):
    n = np.arange(len(iq), dtype=np.float64)
    x = iq * np.exp(-2j * np.pi * off / rate * n).astype(np.complex64)
    fr = Fraction(FS / rate).limit_denominator(2000)
    y = signal.resample_poly(x, fr.numerator, fr.denominator).astype(np.complex64)
    taps = signal.firwin(41, 7000, fs=FS)
    return signal.lfilter(taps, 1, y).astype(np.complex64)


def _bits_from_channel(y):
    d = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float32)
    # moving-average high-pass (64 symbols) removes carrier offset
    ma = np.convolve(d, np.ones(64 * SPS, np.float32) / (64 * SPS), "same")
    d = d - ma
    d = np.convolve(d, np.ones(SPS, np.float32) / SPS, "same")
    out = []
    for p in range(SPS):
        nrz = d[p::SPS] > 0
        b = (nrz[1:] == nrz[:-1]).astype(np.uint8)   # NRZI: no transition = 1
        out.append((p, b.tobytes().translate(_TR).decode()))
    return out


_TR = bytes.maketrans(b"\x00\x01", b"01")


def _frames(bits):
    """Yield (bit_index, payload_bytes) for HDLC frames with valid CRC."""
    flag = "01111110"
    i = bits.find(flag)
    while i >= 0:
        j = bits.find(flag, i + 8)
        if j < 0:
            break
        L = j - i - 8
        if L < 168 + 16 or L > 1100:
            i = j
            continue
        raw = bits[i + 8:j].replace("111110", "11111")
        if len(raw) % 8:
            # tolerate trailing stuffing/partial bits before the end flag
            raw = raw[: len(raw) - len(raw) % 8]
        by = bytes(int(raw[k:k + 8][::-1], 2) for k in range(0, len(raw), 8))
        if len(by) >= 23 and _crc16_x25(by) == 0xF0B8:
            yield i, by[:-2]
            i = bits.find(flag, j + 8)
        else:
            i = j


def _payload_bits(data):
    return "".join(f"{b:08b}" for b in data)


def _u(b, a, n):
    return int(b[a:a + n], 2) if len(b) >= a + n else None


def _s(b, a, n):
    v = _u(b, a, n)
    if v is None:
        return None
    return v - (1 << n) if v & (1 << (n - 1)) else v


def _txt(b, a, nchar):
    s = ""
    for k in range(nchar):
        v = _u(b, a + 6 * k, 6)
        if v is None:
            break
        s += _SIXBIT[v]
    return s.replace("@", "").strip()


def _latlon(b, lon_at, lat_at, lon_n=28, lat_n=27):
    lon, lat = _s(b, lon_at, lon_n), _s(b, lat_at, lat_n)
    if lon is None or lat is None:
        return None, None
    lon, lat = lon / 600000.0, lat / 600000.0
    if abs(lon) > 180 or abs(lat) > 90:
        return None, None
    return round(lat, 6), round(lon, 6)


def parse(data):
    b = _payload_bits(data)
    t = _u(b, 0, 6)
    f = {"type": t, "mmsi": _u(b, 8, 30)}
    if t in (1, 2, 3):
        f["nav_status"] = _u(b, 38, 4)
        f["sog_kn"] = (_u(b, 50, 10) or 0) / 10
        f["lat"], f["lon"] = _latlon(b, 61, 89)
        f["cog"] = (_u(b, 116, 12) or 0) / 10
        f["heading"] = _u(b, 128, 9)
    elif t == 4:
        f["lat"], f["lon"] = _latlon(b, 79, 107)
        f["base_station"] = True
    elif t == 5:
        f["imo"] = _u(b, 40, 30)
        f["callsign"] = _txt(b, 70, 7)
        f["name"] = _txt(b, 112, 20)
        f["ship_type"] = _u(b, 232, 8)
        f["destination"] = _txt(b, 302, 20)
    elif t in (18, 19):
        f["sog_kn"] = (_u(b, 46, 10) or 0) / 10
        f["lat"], f["lon"] = _latlon(b, 57, 85)
        f["cog"] = (_u(b, 112, 12) or 0) / 10
        if t == 19:
            f["name"] = _txt(b, 143, 20)
            f["ship_type"] = _u(b, 263, 8)
    elif t == 21:
        f["aid_type"] = _u(b, 38, 5)
        f["name"] = _txt(b, 43, 20)
        f["lat"], f["lon"] = _latlon(b, 164, 192)
    elif t == 24:
        part = _u(b, 38, 2)
        if part == 0:
            f["name"] = _txt(b, 40, 20)
        else:
            f["ship_type"] = _u(b, 40, 8)
            f["callsign"] = _txt(b, 90, 7)
    return {k: v for k, v in f.items() if v not in (None, "")}


def decode(rec, ctx=None):
    found = []   # (t, channel, fields)
    n_total = rec.n_samples()
    step = int(CHUNK_S * rec.rate)
    ovl = int(0.05 * rec.rate)
    for name, fch in CHANNELS.items():
        off = fch - rec.center_hz
        if abs(off) > 0.45 * rec.rate - 15e3:
            continue
        for s in range(0, n_total, step):
            y = _channel(rec.iq(s, step + ovl), rec.rate, off)
            t_lim = step / rec.rate          # frames starting in the overlap belong to the next chunk
            kept = []
            for p, bits in _bits_from_channel(y):
                for bi, data in _frames(bits):
                    tt = (bi * SPS + p) / FS
                    if s + step < n_total and tt >= t_lim:
                        continue
                    # the same frame is recovered at several symbol phases
                    if any(d == data and abs(t0 - tt) < 0.005 for t0, d in kept):
                        continue
                    kept.append((tt, data))
            for tt, data in kept:
                found.append((s / rec.rate + tt, name, fch, parse(data), data.hex()))
    # aggregate per MMSI (drop duplicate frames from chunk overlap)
    vessels = {}
    for t, ch, fch, f, hx in sorted(found, key=lambda x: x[0]):
        m = f.get("mmsi")
        v = vessels.setdefault(m, {"mmsi": m, "messages": 0, "types": {}, "channels": set(), "t0": t, "raw": []})
        v["messages"] += 1
        v["types"][f["type"]] = v["types"].get(f["type"], 0) + 1
        v["channels"].add(ch)
        v["freq"] = fch
        if len(v["raw"]) < 5:
            v["raw"].append(hx)
        for k, val in f.items():
            if k not in ("type",):
                v[k] = val
    out = []
    for m, v in vessels.items():
        v["channels"] = sorted(v["channels"])
        lbl = f"MMSI {m}"
        if v.get("name"):
            lbl += f" '{v['name']}'"
        if v.get("callsign"):
            lbl += f" {v['callsign']}"
        if v.get("lat") is not None:
            lbl += f" @ {v['lat']:.5f},{v['lon']:.5f}"
        if v.get("sog_kn") is not None:
            lbl += f" {v['sog_kn']} kn"
        if v.get("base_station"):
            lbl += " (base station)"
        lbl += f" ({v['messages']} msgs, types {sorted(v['types'])})"
        out.append(result(NAME, "crc", v.pop("freq"), f"MMSI:{m}", lbl, crc_ok=True, t_s=v.pop("t0"), **v))
    return out
