"""Bluetooth LE advertising decoder (channels 37/38/39), GFSK 1 Msym/s, CRC-24.

For every advertising channel inside the recording span: mix to baseband, resample
to 4 Msps, FM discriminate, slice against a moving average, search the advertising
access address 0x8E89BED6 (<=2 bit errors) at all 4 sample phases, re-estimate the
carrier offset from the known preamble+AA symbols, then de-whiten the PDU and
check CRC-24 (init 0x555555).  One result per advertiser address.
"""
from fractions import Fraction

import numpy as np
from scipy import signal

from . import result

NAME = "ble"
CAPTURE = {"rate": 4e6, "seconds": 5, "center": 2426e6}
ADV_CHANNELS = {37: 2402e6, 38: 2426e6, 39: 2480e6}
FS = 4e6
SPS = 4
AA = 0x8E89BED6
CHUNK = 8_000_000      # samples at FS

PDU_TYPES = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ",
             4: "SCAN_RSP", 5: "CONNECT_IND", 6: "ADV_SCAN_IND", 7: "ADV_EXT_IND"}
COMPANIES = {0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
             0x0087: "Garmin", 0x0157: "Huami/Xiaomi", 0x038F: "Xiaomi", 0x0059: "Nordic",
             0x000D: "Texas Instruments", 0x02E5: "Espressif", 0x0499: "Ruuvi", 0x0171: "Amazon",
             0x01DA: "Logitech", 0x0310: "SGL Italia", 0x00D2: "Dialog/Renesas", 0x0822: "Tile",
             0x067C: "Tile", 0x0002: "Intel", 0x000F: "Broadcom", 0x001D: "Qualcomm",
             0x0131: "Cypress", 0x0BDA: "Realtek", 0x05A7: "Sonos", 0x0301: "Bose",
             0x012D: "Sony", 0x00C4: "LG", 0x0046: "MediaTek", 0xFFFF: "test/unassigned"}


def _whitening(ch, n=8 * 260):
    """Spec Vol 6 Part B 3.2: pos0=1, pos1..6 = channel index (MSB at pos1)."""
    r = [1] + [(ch >> (5 - k)) & 1 for k in range(6)]
    out = np.zeros(n, np.uint8)
    for i in range(n):
        o = r[6]
        out[i] = o
        r = [o, r[0], r[1], r[2], r[3] ^ o, r[4], r[5]]
    return out


_WHITEN = {ch: _whitening(ch) for ch in ADV_CHANNELS}


def _crc_lut():
    lut = np.zeros(256, np.uint32)
    for k in range(256):
        s = k
        for _ in range(8):
            s = (s >> 1) ^ 0xDA6000 if s & 1 else s >> 1
        lut[k] = s
    return lut


_LUT = _crc_lut()


def crc24(data, init=0xAAAAAA):     # reflected form of 0x555555 / poly 0x00065B
    s = init
    for b in data:
        s = (s >> 8) ^ int(_LUT[(int(b) ^ s) & 0xFF])
    return s


def _aa_bits():
    return np.array([(AA >> i) & 1 for i in range(32)], np.uint8)


_AA_BITS = _aa_bits()
_PRE_BITS = np.array([0, 1, 0, 1, 0, 1, 0, 1], np.uint8)   # preamble for AA with LSB 0
_KNOWN = np.concatenate([_PRE_BITS, _AA_BITS]) * 2.0 - 1.0  # +-1


def _to_fs(iq, rate, off):
    n = np.arange(len(iq), dtype=np.float64)
    x = iq * np.exp(-2j * np.pi * off / rate * n).astype(np.complex64)
    fr = Fraction(FS / rate).limit_denominator(200)
    y = signal.resample_poly(x, fr.numerator, fr.denominator) if fr != 1 else x
    taps = signal.firwin(33, 800e3, fs=FS)
    return signal.lfilter(taps, 1, y).astype(np.complex64)


def _bytes_lsb(bits):
    nb = len(bits) // 8
    return np.packbits(bits[: nb * 8].reshape(nb, 8)[:, ::-1], axis=1).ravel()


def _mac(b):
    return ":".join(f"{x:02X}" for x in b[::-1])


def _parse_ad(ad):
    out = {}
    i = 0
    while i < len(ad):
        L = ad[i]
        if L == 0 or i + 1 + L > len(ad):
            break
        t, v = ad[i + 1], bytes(ad[i + 2:i + 1 + L])
        if t in (0x08, 0x09):
            out["name"] = v.decode("utf-8", "replace")
        elif t == 0xFF and len(v) >= 2:
            cid = v[0] | (v[1] << 8)
            out["company_id"] = f"0x{cid:04X}"
            out["company"] = COMPANIES.get(cid, "unknown")
            out["mfg_data"] = v[2:].hex()
        elif t in (0x02, 0x03):
            out.setdefault("uuid16", []).extend(f"{v[k] | v[k+1] << 8:04X}" for k in range(0, len(v) - 1, 2))
        elif t == 0x16 and len(v) >= 2:
            out.setdefault("service_data", []).append(f"{v[0] | v[1] << 8:04X}:{v[2:].hex()}")
        elif t == 0x0A and len(v) == 1:
            out["tx_power_dbm"] = int(np.int8(v[0]))
        elif t == 0x01 and v:
            out["flags"] = v[0]
        i += 1 + L
    return out


def _parse_pdu(pdu):
    h0, ln = pdu[0], pdu[1]
    t = h0 & 0x0F
    f = {"pdu_type": PDU_TYPES.get(t, f"type{t}"), "tx_random": bool(h0 & 0x40)}
    p = pdu[2:2 + ln]
    adva = None
    if t in (0, 2, 4, 6) and ln >= 6:
        adva = p[:6]
        f.update(_parse_ad(p[6:]))
    elif t == 1 and ln >= 12:
        adva = p[:6]
        f["target"] = _mac(p[6:12])
    elif t == 3 and ln >= 12:
        f["scanner"] = _mac(p[:6])
        adva = p[6:12]
    elif t == 5 and ln >= 12:
        f["initiator"] = _mac(p[:6])
        adva = p[6:12]
    elif t == 7 and ln >= 2:
        hl = p[0] & 0x3F
        if hl >= 1 and p[1] & 0x01 and ln >= 8:
            adva = p[2:8]
    return (_mac(adva) if adva is not None else None), f


def _scan(y, ch, t0):
    d = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float32)
    ma = np.convolve(d, np.ones(32 * SPS, np.float32) / (32 * SPS), "same")
    hp = d - ma
    pw = np.abs(y[1:]) ** 2
    noise_pw = float(np.median(pw)) + 1e-12
    pkts = []
    for p in range(SPS):
        sym = hp[p::SPS]
        bits = (sym > 0).astype(np.uint8)
        n = len(bits) - 32
        if n <= 0:
            continue
        w = np.zeros(n, np.uint64)
        for k in range(32):
            w |= bits[k:k + n].astype(np.uint64) << np.uint64(k)
        err = np.bitwise_count(w ^ np.uint64(AA))
        for i in np.where(err <= 2)[0]:
            s0 = i - 8                       # preamble start (symbol index)
            if s0 < 0:
                continue
            raw = d[p::SPS]
            known = raw[s0:s0 + 40]
            if len(known) < 40:
                continue
            # least squares: raw = dc + a*b  over known +-1 symbols
            A = np.vstack([np.ones(40), _KNOWN]).T
            dc, a = np.linalg.lstsq(A, known, rcond=None)[0]
            if a <= 0:
                continue
            body = raw[i + 32:i + 32 + 8 * (2 + 255 + 3)]
            b = ((body - dc) > 0).astype(np.uint8)
            if len(b) < 16:
                continue
            wh = _WHITEN[ch]
            hdr = _bytes_lsb(b[:16] ^ wh[:16])
            ln = int(hdr[1])
            nbits = 8 * (2 + ln + 3)
            if ln < 6 or ln > 255 or len(b) < nbits:
                continue
            by = _bytes_lsb(b[:nbits] ^ wh[:nbits])
            pdu, rx_crc = by[:2 + ln], by[2 + ln:]
            if crc24(pdu) != (int(rx_crc[0]) | int(rx_crc[1]) << 8 | int(rx_crc[2]) << 16):
                continue
            k_samp = (i * SPS + p)
            seg = pw[k_samp:k_samp + nbits * SPS]
            snr = 10 * np.log10(float(np.mean(seg)) / noise_pw) if len(seg) else None
            pkts.append(((t0 + k_samp) / FS, bytes(pdu), snr))
    # same packet found at neighbouring phases
    pkts.sort()
    out = []
    for t, pdu, snr in pkts:
        if out and out[-1][1] == pdu and t - out[-1][0] < 5e-6:
            continue
        out.append((t, pdu, snr))
    return out


def decode(rec, ctx=None):
    devs = {}
    n_total = rec.n_samples()
    for ch, f in ADV_CHANNELS.items():
        off = f - rec.center_hz
        if abs(off) > 0.5 * rec.rate * 0.75 - 1.1e6 and abs(off) > 1e3:   # HackRF BB filter ~0.75*rate
            continue
        step = int(CHUNK * rec.rate / FS)
        ovl = int(2.2e-3 * rec.rate)
        for s in range(0, n_total, step):
            y = _to_fs(rec.iq(s, step + ovl), rec.rate, off)
            t0 = int(round(s * FS / rec.rate))
            for t, pdu, snr in _scan(y, ch, t0):
                if s + step < n_total and t * rec.rate >= s + step:
                    continue
                mac, fld = _parse_pdu(pdu)
                if mac is None:
                    continue
                d = devs.setdefault(mac, {"mac": mac, "packets": 0, "pdu_types": {}, "channels": set(),
                                          "t0": t, "snr_db": []})
                d["packets"] += 1
                d["pdu_types"][fld["pdu_type"]] = d["pdu_types"].get(fld["pdu_type"], 0) + 1
                d["channels"].add(ch)
                if snr is not None:
                    d["snr_db"].append(snr)
                d["freq"] = f
                for k, v in fld.items():
                    if k != "pdu_type":
                        d[k] = v
                d.setdefault("example_pdu", pdu.hex())
    out = []
    for mac, d in devs.items():
        d["channels"] = sorted(d["channels"])
        d["snr_db"] = round(float(np.median(d["snr_db"])), 1) if d["snr_db"] else None
        lbl = f"BLE {mac}{' (random)' if d.get('tx_random') else ''}"
        if d.get("name"):
            lbl += f" '{d['name']}'"
        if d.get("company"):
            lbl += f" {d['company']}"
        lbl += f" {'/'.join(sorted(d['pdu_types']))} x{d['packets']}"
        out.append(result(NAME, "crc", d.pop("freq"), f"BLE:{mac}", lbl, crc_ok=True, t_s=d.pop("t0"), **d))
    return out
