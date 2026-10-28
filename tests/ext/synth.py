"""Synthetic test signals for the rtl433 / pager / dab decoders."""
import numpy as np


def ppm_ook(bits, fs, short=500e-6, zero_gap=1000e-6, one_gap=2000e-6, sync_gap=4000e-6, repeats=12):
    """Nexus-style PPM OOK envelope."""
    def on(t): return np.ones(int(t * fs), np.float32)
    def off(t): return np.zeros(int(t * fs), np.float32)
    row = []
    for b in bits:
        row += [on(short), off(one_gap if b else zero_gap)]
    row += [on(short), off(sync_gap)]
    one = np.concatenate(row)
    return np.concatenate([off(sync_gap)] + [one] * repeats)


def hexbits(h, n):
    v = int(h, 16)
    return [(v >> (len(h) * 4 - 1 - i)) & 1 for i in range(n)]


def embed(env_or_iq, fs, total_s, f_off, at_s, snr_db=20, rng=None, fm_dev=None):
    """Place a burst into complex noise (unit noise power) at offset f_off."""
    rng = rng or np.random.default_rng(1)
    n = int(total_s * fs)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) / np.sqrt(2)
    s = np.asarray(env_or_iq)
    a = 10 ** (snr_db / 20)
    i0 = int(at_s * fs)
    k = np.arange(len(s))
    if np.iscomplexobj(s):
        burst = s * a
    else:
        burst = s.astype(np.complex64) * a
    burst = burst * np.exp(2j * np.pi * f_off / fs * (k + i0))
    noise[i0:i0 + len(s)] += burst.astype(np.complex64)
    return (noise * 0.05).astype(np.complex64)


# ---- POCSAG ---------------------------------------------------------------
_G = 0b11101101001  # x^10+x^9+x^8+x^6+x^5+x^3+1


def _bch(data21):
    cw = data21 << 10
    for i in range(30, 9, -1):
        if cw & (1 << i):
            cw ^= _G << (i - 10)
    word = (data21 << 10) | cw
    return (word << 1) | (bin(word).count("1") & 1)


def pocsag_bits(address, func, text):
    SYNC, IDLE = 0x7CD215D8, 0x7A89C197
    msg = []
    for ch in text:
        c = ord(ch)
        msg += [(c >> i) & 1 for i in range(7)]  # 7-bit LSB first
    while len(msg) % 20:
        msg.append(0)
    words = [_bch((1 << 20) | int("".join(map(str, msg[i:i + 20])), 2)) for i in range(0, len(msg), 20)]
    addr_cw = _bch(((address >> 3) << 2) | func)
    frame = address & 7
    cws = [IDLE] * (2 * frame) + [addr_cw] + words
    while len(cws) % 16:
        cws.append(IDLE)
    cws += [IDLE] * 16
    bits = [1, 0] * 288
    for b in range(0, len(cws), 16):
        for w in [SYNC] + cws[b:b + 16]:
            bits += [(w >> (31 - i)) & 1 for i in range(32)]
    return bits


def fsk_iq(bits, baud, fs, dev, one_is_low=True):
    sps = fs / baud
    n = int(len(bits) * sps)
    idx = (np.arange(n) / sps).astype(int)
    sym = np.array(bits)[idx] * 2 - 1
    if one_is_low:
        sym = -sym
    ph = np.cumsum(2 * np.pi * dev * sym / fs)
    return np.exp(1j * ph).astype(np.complex64)
