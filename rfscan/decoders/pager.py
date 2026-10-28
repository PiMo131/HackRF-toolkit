"""Paging decoder: FLEX (P2000 Dutch emergency paging, 169.650 MHz) and POCSAG.

Channels: 169.650 MHz is always demodulated when inside the recording span;
other narrowband (<=25 kHz) bursty clusters in VHF/UHF are tried too.
Each channel -> FM discriminator -> 22050 Hz s16le -> multimon-ng (both polarities).
multimon-ng validates FLEX/POCSAG codewords with BCH(31,21) + parity, so hits are level="crc".
"""
import re
import subprocess

import numpy as np
from scipy import signal

from . import _audio, result

NAME = "pager"
CAPTURE = {"rate": 2e6, "seconds": 60, "center": 169.65e6 + 300e3}  # offset tune: keep P2000 off the DC spike

P2000_HZ = 169.650e6
AUDIO_RATE = 22050
CH_RATE = 44100
DEMODS = ["FLEX", "POCSAG512", "POCSAG1200", "POCSAG2400"]
MULTIMON = "multimon-ng"

_FLEX_RE = re.compile(r"^FLEX[|:]")
_POCSAG_RE = re.compile(r"^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d)\s*(?:(Alpha|Numeric|Skyper):\s*(.*))?$")


def _channel_audio(rec, f_off):
    """FM demod one 25 kHz channel to int16 audio at 22050 Hz."""
    x, r1 = _audio.mix_decimate(rec, f_off, min_rate=4 * CH_RATE)
    if abs(f_off) <= 15e3:   # HackRF DC offset falls inside the channel: remove it
        x = x - np.mean(x)
    y = _audio.channelize(x, r1, 0.0, CH_RATE, bw=20e3)
    d = _audio.fm_demod(y)
    d = d - np.median(d)                    # residual carrier offset
    b, a_ = signal.butter(4, 6000 / (CH_RATE / 2))
    d = signal.lfilter(b, a_, d)[::2]       # 44100 -> 22050
    # scale: +-4.8 kHz deviation -> ~ +-12000
    dev_rad = 2 * np.pi * 4800 / CH_RATE
    return np.clip(d / dev_rad * 12000, -32767, 32767).astype(np.int16)


def _multimon(audio, timeout=120):
    cmd = [MULTIMON, "-t", "raw", "-q", "-e", "-u"]
    for d in DEMODS:
        cmd += ["-a", d]
    cmd += ["-"]
    try:
        p = subprocess.run(cmd, input=audio.tobytes(), capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "replace").splitlines()
    except subprocess.TimeoutExpired as e:
        return (e.stdout or b"").decode("utf-8", "replace").splitlines()


def _parse(line, f_abs):
    line = line.strip()
    if _FLEX_RE.match(line):
        # FLEX|2026-10-19 12:00:00|1600/2/K/A|07.120|001234567 001234568|ALN|P 1 BDH-01 ...
        parts = line.split("|")
        if len(parts) >= 7:
            mode, frame, caps, typ, msg = parts[2], parts[3], parts[4], parts[5], "|".join(parts[6:])
        else:  # older "FLEX: ..." layout
            mode, frame, caps, typ, msg = "", "", "", "", line.split(":", 1)[1]
        capcodes = caps.split()
        ident = f"FLEX:{capcodes[0]}" if capcodes else "FLEX:?"
        p2000 = abs(f_abs - P2000_HZ) < 5e3
        return result(NAME, "crc", f_abs, ident,
                      f"{'P2000 ' if p2000 else ''}FLEX {caps} {typ}: {msg.strip()}"[:200], crc_ok=True,
                      protocol="FLEX", mode=mode, frame=frame, capcodes=capcodes, msg_type=typ,
                      message=msg.strip(), p2000=p2000, raw=line)
    m = _POCSAG_RE.match(line)
    if m:
        proto, addr, func, kind, msg = m.groups()
        text = (msg or "").strip()
        # BCH error correction lets noise or the edge of the neighbouring P2000 FLEX
        # channel through as "valid" codewords; real numeric pages rarely contain the
        # spare/error symbols U [ ].
        junk = sum(c in "U[]" for c in text) / max(1, len(text))
        plausible = abs(f_abs - P2000_HZ) > 30e3 and not (kind == "Numeric" and junk > 0.08)
        return result(NAME, "crc" if plausible else "detect", f_abs, f"POCSAG:{addr}",
                      f"{'' if plausible else '(implausible) '}{proto} addr={addr} func={func} {kind or ''}: {text}"[:200],
                      crc_ok=plausible, protocol=proto, address=int(addr), function=int(func),
                      msg_type=kind, message=(msg or "").strip(), raw=line)
    return None


def _channels(rec, ctx):
    lo, hi = rec.center_hz - 0.45 * rec.rate, rec.center_hz + 0.45 * rec.rate
    chans = []
    if lo <= P2000_HZ <= hi:
        chans.append(P2000_HZ - rec.center_hz)
    if rec.rate > 100e3 and ctx.get("scan_clusters", True):
        P, f, blk = _audio.spectrogram(rec, nfft=2048, block_s=0.02, max_s=ctx.get("cluster_scan_s", 60))
        for c in _audio.active_clusters(P, f, thr_db=8.0, min_blocks=3, merge_hz=15e3):
            if c["bw"] <= 30e3 and all(abs(c["f_off"] - x) > 12.5e3 for x in chans):
                chans.append(c["f_off"])
    if not chans and rec.rate <= 100e3:
        chans.append(0.0)  # already channelised recording
    return chans[: ctx.get("max_channels", 6)]


def decode(rec, ctx):
    results = []
    for f_off in _channels(rec, ctx):
        f_abs = rec.center_hz + f_off
        audio = _channel_audio(rec, f_off)
        seen = set()
        for pol in (1, -1):
            for line in _multimon((audio * pol).astype(np.int16)):
                r = _parse(line, f_abs)
                if r and r["fields"]["raw"] not in seen:
                    seen.add(r["fields"]["raw"])
                    r["fields"]["inverted"] = pol < 0
                    results.append(r)
    return results
