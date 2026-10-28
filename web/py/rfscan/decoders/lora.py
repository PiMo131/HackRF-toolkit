"""LoRa (CSS) detection + full decode to CRC.

1. Native detection (numpy): coarse spectrogram -> energy bursts in time/frequency ->
   per burst, mix to baseband, decimate to 1 sample/chip for BW in {125k,250k,500k},
   dechirp test for SF7..12.  A run of >=5 identical dechirped bins = preamble
   (level "detect").  Up/down-chirp bins give timing + CFO; the two symbols after
   the preamble give the sync word (0x34 LoRaWAN public, 0x12 private) -> "sync".
2. Full decode: the burst is re-channelised to 4*BW and fed to gr-lora_sdr
   (built in third_party/prefix) in a headless flowgraph run with the system
   python3.  Explicit header + payload CRC -> level "crc".  LoRaWAN MHDR/DevAddr/
   FCnt/FPort are parsed (payload stays encrypted).
"""
import json
import os
import subprocess
import tempfile
from fractions import Fraction

import numpy as np
from scipy import ndimage, signal

from . import result

NAME = "lora"
CAPTURE = {"rate": 4e6, "seconds": 30, "center": 868.5e6}

BWS = (125e3, 250e3, 500e3)
SFS = range(7, 13)
OS_GR = 4                      # oversampling handed to gr-lora_sdr
_HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = os.environ.get("RFSCAN_GR_PREFIX",
                        os.path.normpath(os.path.join(_HERE, "..", "..", "third_party", "prefix")))

CHANNELS = {  # EU868 + EU433 LoRaWAN channels (MHz)
    868.1: "EU868 ch0", 868.3: "EU868 ch1", 868.5: "EU868 ch2",
    867.1: "EU868 ch3", 867.3: "EU868 ch4", 867.5: "EU868 ch5", 867.7: "EU868 ch6",
    867.9: "EU868 ch7", 868.8: "EU868 FSK/G2", 869.525: "EU868 RX2/G3 (downlink)",
    433.175: "EU433 ch0", 433.375: "EU433 ch1", 433.575: "EU433 ch2",
}
MTYPES = ["JoinRequest", "JoinAccept", "UnconfirmedUp", "UnconfirmedDown",
          "ConfirmedUp", "ConfirmedDown", "RejoinRequest", "Proprietary"]


def channel_name(f_hz):
    f = f_hz / 1e6
    best = min(CHANNELS, key=lambda c: abs(c - f))
    return CHANNELS[best] if abs(best - f) < 0.02 else None


# --------------------------------------------------------------------------
# energy bursts
# --------------------------------------------------------------------------
def _spectrogram(rec, nfft, navg, chunk_frames=4096):
    """Averaged |STFT|^2 over the whole recording, chunked. rows: time, cols: fftshifted freq."""
    n = rec.n_samples()
    hop = nfft * navg
    rows = []
    win = np.hanning(nfft).astype(np.float32)
    step = chunk_frames * nfft
    for s in range(0, n - nfft, step):
        x = rec.iq(s, min(step, n - s))
        m = (len(x) // hop) * hop
        if m == 0:
            break
        f = np.fft.fft(x[:m].reshape(-1, nfft) * win, axis=1)
        p = (f.real ** 2 + f.imag ** 2).reshape(-1, navg, nfft).mean(1)
        rows.append(np.fft.fftshift(p, axes=1).astype(np.float32))
    return np.vstack(rows) if rows else np.zeros((0, nfft), np.float32), hop


def find_bursts(rec, thr_db=6.0, min_bw=80e3, min_dur=0.008, max_bursts=400):
    fs = rec.rate
    nfft = int(2 ** np.round(np.log2(fs / 15.625e3)))
    navg = max(1, int(round(0.0005 * fs / nfft)))
    S, hop = _spectrogram(rec, nfft, navg)
    if len(S) < 4:
        return []
    sample = S[:: max(1, len(S) // 4000)]
    floor = np.median(sample, axis=0) + 1e-20
    mask = S > floor * 10 ** (thr_db / 10)
    mask[:, nfft // 2 - 1:nfft // 2 + 2] = False      # HackRF DC spike
    mask = ndimage.binary_closing(mask, structure=np.ones((5, 3)))
    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    lab, nlab = ndimage.label(mask)
    df = fs / nfft
    dt = hop / fs
    out = []
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        t0, t1 = sl[0].start * dt, sl[0].stop * dt
        b0, b1 = sl[1].start, sl[1].stop
        bw = (b1 - b0) * df
        if bw < min_bw or (t1 - t0) < min_dur or bw > 700e3:
            continue
        f_lo = (b0 - nfft / 2) * df
        f_hi = (b1 - nfft / 2) * df
        sub = S[sl]
        snr = 10 * np.log10(np.mean(sub) / np.mean(floor[b0:b1]))
        out.append(dict(t0=t0, t1=t1, f_off=(f_lo + f_hi) / 2, bw=bw, snr=float(snr)))
    out.sort(key=lambda b: -b["snr"])
    return out[:max_bursts]


# --------------------------------------------------------------------------
# chirp math
# --------------------------------------------------------------------------
_CHIRPS = {}


def upchirp(sf):
    if sf not in _CHIRPS:
        N = 2 ** sf
        n = np.arange(N)
        _CHIRPS[sf] = np.exp(2j * np.pi * (n * n / (2 * N) - 0.5 * n)).astype(np.complex64)
    return _CHIRPS[sf]


def _resample(x, fs_in, fs_out):
    fr = Fraction(fs_out / fs_in).limit_denominator(2000)
    return signal.resample_poly(x, fr.numerator, fr.denominator).astype(np.complex64)


def _baseband(rec, t0, t1, f_off, fs_out):
    s0 = max(0, int(t0 * rec.rate))
    s1 = min(rec.n_samples(), int(t1 * rec.rate))
    x = rec.iq(s0, s1 - s0)
    n = np.arange(len(x))
    x = x * np.exp(-2j * np.pi * f_off / rec.rate * (n + s0)).astype(np.complex64)
    return _resample(x, rec.rate, fs_out), s0


def _dechirp(x, sf, off, conj_ref=True):
    """Windows of N samples starting at off; returns (peak bin, peak fraction) arrays."""
    N = 2 ** sf
    m = (len(x) - off) // N
    if m <= 0:
        return np.zeros(0, int), np.zeros(0)
    w = x[off:off + m * N].reshape(m, N)
    ref = np.conj(upchirp(sf)) if conj_ref else upchirp(sf)
    F = np.fft.fft(w * ref, axis=1)
    P = F.real ** 2 + F.imag ** 2
    k = P.argmax(1)
    frac = P.max(1) / (P.sum(1) + 1e-20)
    return k, frac


def _runs(k, frac, min_len=5, min_frac=0.25, N=None):
    """Longest run of windows with ~equal peak bin and good concentration."""
    best = (0, 0)  # (len, start)
    i = 0
    while i < len(k):
        if frac[i] < min_frac:
            i += 1
            continue
        j = i
        while j + 1 < len(k) and frac[j + 1] >= min_frac and \
                min((k[j + 1] - k[i]) % N, (k[i] - k[j + 1]) % N) <= 1:
            j += 1
        if j - i + 1 > best[0]:
            best = (j - i + 1, i)
        i = j + 1
    return best if best[0] >= min_len else None


def _sync_check(x, sf, bw):
    """Try to find a preamble for this SF. Returns dict or None."""
    N = 2 ** sf
    k, frac = _dechirp(x, sf, 0)
    r = _runs(k, frac, N=N)
    if r is None:
        return None
    run_len, run_start = r
    ku = int(np.median(k[run_start:run_start + run_len]))
    conc = float(np.median(frac[run_start:run_start + run_len]))
    info = dict(sf=sf, bw=bw, preamble_syms=run_len, concentration=conc, sync_word=None)
    # look for downchirps right after the run (unaligned grid)
    a = run_start + run_len
    seg = x[a * N:(a + 6) * N]
    kd_all, fd = _dechirp(seg, sf, 0, conj_ref=False)
    if len(fd) == 0 or fd.max() < 0.25:
        return info
    kd = int(kd_all[fd.argmax()])
    best = None
    for tau_c in ((kd - ku) / 2.0, (kd - ku) / 2.0 + N / 2):
        tau = int(round(tau_c)) % N
        cfo = (ku + tau) % N                     # ku = cfo - tau
        if cfo > N / 2:
            cfo -= N
        # aligned grid: symbols start at tau (mod N) relative to the unaligned grid
        k2, f2 = _dechirp(x, sf, tau)
        r2 = _runs(k2, f2, N=N)
        if r2 is None:
            continue
        end = r2[1] + r2[0]
        if end + 2 > len(k2):
            continue
        v = [int((k2[end + i] - k2[end - 1]) % N) for i in range(2)]
        ok = all(min(vi % 8, 8 - vi % 8) <= 1 and 0 < vi < 16 * 8 for vi in v)
        score = (ok, sum(f2[end:end + 2]))
        if best is None or score > best[0]:
            sw = ((int(round(v[0] / 8)) & 0xF) << 4) | (int(round(v[1] / 8)) & 0xF)
            best = (score, dict(tau=tau, cfo_bins=cfo, sync_vals=v, sync_word=sw if ok else None,
                                preamble_end=end))
    if best:
        info.update(best[1])
        info["cfo_hz"] = info["cfo_bins"] * bw / N
    return info


def detect(rec, max_bursts=400):
    found = []
    for b in find_bursts(rec, max_bursts=max_bursts):
        hits = []
        for bw in BWS:
            if not (0.5 * bw <= b["bw"] <= 2.2 * bw):
                continue
            # preamble+sync for the largest SF is ~ 14.25 * 4096/bw; take start of burst
            t_need = 16 * 2 ** 12 / bw
            x, s0 = _baseband(rec, max(0, b["t0"] - 0.002), min(b["t1"], b["t0"] + t_need), b["f_off"], bw)
            for sf in SFS:
                if len(x) < 8 * 2 ** sf:
                    break
                info = _sync_check(x, sf, bw)
                if info:
                    info["_s0"] = s0
                    hits.append(info)
        if not hits:
            continue
        # prefer sync-confirmed, then longest preamble, then concentration
        h = max(hits, key=lambda i: (i["sync_word"] is not None, i["concentration"] > 0.5,
                                     i["preamble_syms"], i["concentration"]))
        f_c = rec.center_hz + b["f_off"] + h.get("cfo_hz", 0.0)
        found.append(dict(h, t0=b["t0"], t1=b["t1"], f_off=b["f_off"] + h.get("cfo_hz", 0.0),
                          freq_hz=f_c, snr_db=b["snr"]))
    return found


# --------------------------------------------------------------------------
# gr-lora_sdr full decode
# --------------------------------------------------------------------------
_RUNNER = r'''
import sys, json, numpy as np
import pmt
from gnuradio import gr, blocks
from gnuradio import lora_sdr
a = json.loads(sys.argv[1])
tb = gr.top_block()
src = blocks.file_source(gr.sizeof_gr_complex, a["file"], False)
fs, sf, bw = a["fs"], a["sf"], a["bw"]
fsync = lora_sdr.frame_sync(int(a["center"]), int(bw), sf, False, [a["sync_word"]], int(fs / bw), 8)
fft = lora_sdr.fft_demod(False, True)
gray = lora_sdr.gray_mapping(False)
dei = lora_sdr.deinterleaver(False)
ham = lora_sdr.hamming_dec(False)
hdr = lora_sdr.header_decoder(False, 1, 255, True, 2, False)
dew = lora_sdr.dewhitening()
crc = lora_sdr.crc_verif(0, False)
sink = blocks.vector_sink_b()
tb.msg_connect(hdr, "frame_info", fsync, "frame_info")
tb.connect(src, fsync, fft, gray, dei, ham, hdr, dew, crc, sink)
tb.run()
data = bytes(sink.data())
tags = sorted(sink.tags(), key=lambda t: t.offset)
out = []
for t in tags:
    if pmt.symbol_to_string(t.key) != "frame_info":
        continue
    d = pmt.to_python(t.value)
    n = int(d.get("pay_len", 0))
    pay = data[t.offset:t.offset + n]
    out.append(dict(offset=int(t.offset), pay_len=n, cr=int(d.get("cr", -1)), has_crc=int(d.get("crc", 0)),
                    crc_valid=d.get("crc_valid"), ldro=int(d.get("ldro_mode", -1)), err=int(d.get("err", 0)),
                    payload=pay.hex()))
print("@@JSON@@" + json.dumps(out))
'''


def gr_available():
    return os.path.isdir(os.path.join(PREFIX, "lib", "python3.13", "site-packages", "gnuradio", "lora_sdr")) \
        or any(os.path.isdir(os.path.join(PREFIX, "lib", d, "site-packages", "gnuradio", "lora_sdr"))
               for d in os.listdir(os.path.join(PREFIX, "lib")) if d.startswith("python")) \
        if os.path.isdir(os.path.join(PREFIX, "lib")) else False


def _gr_env():
    env = dict(os.environ)
    sp = [os.path.join(PREFIX, "lib", d, "site-packages") for d in os.listdir(os.path.join(PREFIX, "lib"))
          if d.startswith("python")]
    env["PYTHONPATH"] = ":".join(sp + [env.get("PYTHONPATH", "")])
    libs = [os.path.join(PREFIX, "lib", "x86_64-linux-gnu"), os.path.join(PREFIX, "lib")]
    env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")])
    return env


def gr_decode(rec, det, tmpdir, timeout=60):
    """Run gr-lora_sdr on one detected burst. Returns list of frame dicts (may be empty)."""
    bw, sf = det["bw"], det["sf"]
    fs = OS_GR * bw
    sym = 2 ** sf / bw
    t0 = max(0.0, det["t0"] - 4 * sym)
    t1 = det["t1"] + 4 * sym
    x, _ = _baseband(rec, t0, t1, det["f_off"], fs)
    rng = np.random.default_rng(0)
    sig = np.sqrt(np.mean(np.abs(x[: len(x) // 8 + 1]) ** 2)) + 1e-9
    pad = (rng.normal(size=int(fs * 8 * sym)) + 1j * rng.normal(size=int(fs * 8 * sym))) * sig * 0.05
    x = np.concatenate([pad, x, pad, pad]).astype(np.complex64)
    x /= np.max(np.abs(x)) + 1e-9
    fn = os.path.join(tmpdir, f"lora_{int(det['freq_hz'])}_{det['t0']:.3f}_sf{sf}.cf32")
    x.tofile(fn)
    sw = det["sync_word"] if det.get("sync_word") is not None else 0x34
    arg = json.dumps(dict(file=fn, fs=fs, sf=sf, bw=bw, center=det["freq_hz"], sync_word=sw))
    try:
        p = subprocess.run(["/usr/bin/python3", "-c", _RUNNER, arg], capture_output=True, text=True,
                           timeout=timeout, env=_gr_env())
    except subprocess.TimeoutExpired:
        return []
    finally:
        try:
            os.remove(fn)
        except OSError:
            pass
    for line in p.stdout.splitlines():
        if line.startswith("@@JSON@@"):
            return json.loads(line[8:])
    return []


def parse_lorawan(payload):
    if len(payload) < 1:
        return {}
    mhdr = payload[0]
    mtype = MTYPES[mhdr >> 5]
    d = {"mtype": mtype, "major": mhdr & 3}
    if mtype in ("UnconfirmedUp", "UnconfirmedDown", "ConfirmedUp", "ConfirmedDown") and len(payload) >= 12:
        d["dev_addr"] = payload[1:5][::-1].hex().upper()
        fctrl = payload[5]
        d["fctrl"] = fctrl
        d["fcnt"] = int.from_bytes(payload[6:8], "little")
        fopts_len = fctrl & 0x0F
        i = 8 + fopts_len
        if len(payload) - 4 > i:
            d["fport"] = payload[i]
        d["mic"] = payload[-4:].hex()
        d["netid_type"] = payload[4] >> 5
    elif mtype == "JoinRequest" and len(payload) >= 23:
        d["join_eui"] = payload[1:9][::-1].hex().upper()
        d["dev_eui"] = payload[9:17][::-1].hex().upper()
        d["dev_nonce"] = int.from_bytes(payload[17:19], "little")
    # plausibility: LoRaWAN major must be 0 and length >= 12/23
    d["plausible"] = (mhdr & 3) == 0 and (len(payload) >= 12)
    return d


def decode(rec, ctx):
    tmpdir = ctx.get("tmpdir") or tempfile.mkdtemp(prefix="rfscan_lora_")
    out = []
    use_gr = ctx.get("lora_gr", True) and gr_available()
    for det in detect(rec, max_bursts=ctx.get("lora_max_bursts", 400)):
        ch = channel_name(det["freq_hz"])
        sw = det.get("sync_word")
        base = dict(sf=det["sf"], bw=det["bw"], duration_s=round(det["t1"] - det["t0"], 4),
                    sync_word=None if sw is None else f"0x{sw:02X}", channel=ch,
                    snr_db=round(det["snr_db"], 1), preamble_syms=det["preamble_syms"],
                    cfo_hz=round(det.get("cfo_hz", 0.0), 1))
        swname = {0x34: "LoRaWAN public", 0x12: "private"}.get(sw, "unknown")
        frames = gr_decode(rec, det, tmpdir) if (use_gr and det.get("sync_word") is not None) else []
        if frames:
            for f in frames:
                pay = bytes.fromhex(f["payload"])
                lw = parse_lorawan(pay) if sw == 0x34 else {}
                crc_ok = bool(f.get("crc_valid")) if f.get("has_crc") else None
                level = "crc" if crc_ok else "sync"
                ident = f"LoRaWAN:{lw['dev_addr']}" if lw.get("dev_addr") else \
                    f"LoRa:{det['freq_hz']/1e6:.3f}:SF{det['sf']}"
                lbl = f"LoRa SF{det['sf']}/BW{det['bw']/1e3:.0f}k {ch or ''} {len(pay)}B " \
                      f"CRC {'OK' if crc_ok else ('BAD' if crc_ok is False else 'n/a')}"
                if lw.get("dev_addr"):
                    lbl += f" {lw['mtype']} DevAddr {lw['dev_addr']} FCnt {lw['fcnt']}"
                elif lw.get("dev_eui"):
                    lbl += f" JoinRequest DevEUI {lw['dev_eui']}"
                out.append(result(NAME, level, det["freq_hz"], ident, lbl, crc_ok=crc_ok, t_s=det["t0"],
                                  payload=f["payload"], cr=f["cr"], pay_len=f["pay_len"],
                                  lorawan=lw, **base))
        else:
            # no frame out of gr-lora_sdr (no sync word, header checksum failed, or gr unavailable)
            level = "sync" if sw is not None else "detect"
            lbl = f"LoRa CSS SF{det['sf']}/BW{det['bw']/1e3:.0f}k {ch or ''} sync {base['sync_word'] or '?'} ({swname})"
            out.append(result(NAME, level, det["freq_hz"], f"LoRa:{det['freq_hz']/1e6:.3f}:SF{det['sf']}",
                              lbl, crc_ok=None, t_s=det["t0"], **base))
    return out
