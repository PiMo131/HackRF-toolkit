"""Minimal ctypes binding to libhackrf (no -dev package needed).

Gives in-process control of frequency / gain / sample rate so the dwell
stage can retune and re-gain quickly without spawning hackrf_transfer.
"""
import ctypes as C
import os
import threading
import time

import numpy as np

# HackRF Pro needs libhackrf >= 2026.01 (built into third_party/prefix by setup.sh);
# fall back to the system library for HackRF One.
PREFIX = os.environ.get("RFSCAN_HACKRF_PREFIX",
                        os.path.join(os.path.dirname(__file__), "..", "third_party", "prefix"))
_local = os.path.join(PREFIX, "lib", "libhackrf.so.0")
_lib = C.CDLL(_local if os.path.exists(_local) else "libhackrf.so.0")


def tool(name):
    """Path + env for a hackrf_* command-line tool matching the loaded library."""
    p = os.path.join(PREFIX, "bin", name)
    if os.path.exists(p):
        env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(PREFIX, "lib"))
        return p, env
    return name, None


class _Transfer(C.Structure):
    _fields_ = [
        ("device", C.c_void_p),
        ("buffer", C.POINTER(C.c_uint8)),
        ("buffer_length", C.c_int),
        ("valid_length", C.c_int),
        ("rx_ctx", C.c_void_p),
        ("tx_ctx", C.c_void_p),
    ]


_CB = C.CFUNCTYPE(C.c_int, C.POINTER(_Transfer))

_lib.hackrf_open.argtypes = [C.POINTER(C.c_void_p)]
_lib.hackrf_close.argtypes = [C.c_void_p]
_lib.hackrf_set_freq.argtypes = [C.c_void_p, C.c_uint64]
_lib.hackrf_set_sample_rate.argtypes = [C.c_void_p, C.c_double]
_lib.hackrf_set_baseband_filter_bandwidth.argtypes = [C.c_void_p, C.c_uint32]
_lib.hackrf_compute_baseband_filter_bw.argtypes = [C.c_uint32]
_lib.hackrf_compute_baseband_filter_bw.restype = C.c_uint32
_lib.hackrf_set_lna_gain.argtypes = [C.c_void_p, C.c_uint32]
_lib.hackrf_set_vga_gain.argtypes = [C.c_void_p, C.c_uint32]
_lib.hackrf_set_amp_enable.argtypes = [C.c_void_p, C.c_uint8]
_lib.hackrf_set_antenna_enable.argtypes = [C.c_void_p, C.c_uint8]
_lib.hackrf_start_rx.argtypes = [C.c_void_p, _CB, C.c_void_p]
_lib.hackrf_stop_rx.argtypes = [C.c_void_p]
_lib.hackrf_error_name.restype = C.c_char_p


class HackRFError(RuntimeError):
    pass


STALL_S = 2.0    # no samples for this long while capturing = stream is dead


def _chk(rc, what):
    if rc != 0:
        raise HackRFError(f"{what}: {_lib.hackrf_error_name(rc).decode()} ({rc})")


class HackRF:
    """One open device. Use as a context manager; close before hackrf_sweep runs."""

    def __init__(self):
        _chk(_lib.hackrf_init(), "hackrf_init")
        self._dev = C.c_void_p()
        _chk(_lib.hackrf_open(C.byref(self._dev)), "hackrf_open")
        self.freq = None
        self.rate = None
        self.lna = self.vga = self.amp = None
        self._lock = threading.Lock()
        self._want = 0
        self._chunks = []
        self._got = 0
        self._skip = 0
        self._rx_total = 0
        self._done = threading.Event()
        self._cb = _CB(self._on_rx)  # keep reference alive
        self._streaming = False
        _chk(_lib.hackrf_set_antenna_enable(self._dev, 0), "antenna_enable")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        if self._dev:
            if self._streaming:
                _lib.hackrf_stop_rx(self._dev)
                self._streaming = False
            _lib.hackrf_close(self._dev)
            _lib.hackrf_exit()
            self._dev = None

    # -- configuration -----------------------------------------------------
    # With libhackrf 2024.02 on this board, samples are corrupted (narrow low-pass
    # hump, I/Q imbalance) if the frequency or sample rate is changed while
    # streaming, or if streaming restarts without a full re-configuration.  So:
    # freq/rate changes stop the stream; the next capture() re-applies everything
    # and starts it again.  Gain changes are safe while streaming (used by AGC).
    def tune(self, freq_hz):
        f = int(freq_hz)
        if f != self.freq:
            self._stop_stream()
            self.freq = f

    def set_rate(self, rate_hz, bw_hz=None):
        r = float(rate_hz)
        if r != self.rate or bw_hz != getattr(self, "_bw_req", None):
            self._stop_stream()
            self._bw_req = bw_hz
            self.rate = r

    def set_gain(self, lna, vga, amp):
        self.lna = int(max(0, min(40, lna)) // 8 * 8)
        self.vga = int(max(0, min(62, vga)) // 2 * 2)
        self.amp = 1 if amp else 0
        if self._streaming:
            self._apply_gain()

    def _apply_gain(self):
        if self.lna is None:
            return
        _chk(_lib.hackrf_set_lna_gain(self._dev, self.lna), "set_lna")
        _chk(_lib.hackrf_set_vga_gain(self._dev, self.vga), "set_vga")
        _chk(_lib.hackrf_set_amp_enable(self._dev, self.amp), "set_amp")

    def _configure(self):
        r = self.rate
        _chk(_lib.hackrf_set_sample_rate(self._dev, r), "set_sample_rate")
        bw = _lib.hackrf_compute_baseband_filter_bw(int(getattr(self, "_bw_req", None) or 0.75 * r))
        _chk(_lib.hackrf_set_baseband_filter_bandwidth(self._dev, bw), "set_bb_bw")
        _chk(_lib.hackrf_set_freq(self._dev, self.freq), "set_freq")
        self._apply_gain()

    # -- streaming ---------------------------------------------------------
    def _on_rx(self, t):
        tr = t.contents
        n = tr.valid_length
        self._rx_total += n
        with self._lock:
            if self._got >= self._want:
                return 0
            if self._skip > 0:
                self._skip -= n
                return 0
            buf = np.ctypeslib.as_array(tr.buffer, shape=(n,)).copy()
            self._chunks.append(buf)
            self._got += n
            if self._got >= self._want:
                self._done.set()
        return 0

    def _start_stream(self):
        if not self._streaming:
            if self.rate is None or self.freq is None:
                raise HackRFError("set_rate() and tune() before capture()")
            self._configure()
            _chk(_lib.hackrf_start_rx(self._dev, self._cb, None), "start_rx")
            self._streaming = True

    def _stop_stream(self):
        if self._streaming:
            with self._lock:
                self._want = 0
            _lib.hackrf_stop_rx(self._dev)
            self._streaming = False

    def capture(self, n_samples, settle_s=0.004, timeout=None):
        """Return complex64 IQ (scaled to +-1.0) plus the raw interleaved int8 buffer."""
        nbytes = 2 * int(n_samples)
        self._start_stream()
        with self._lock:
            self._chunks = []
            self._got = 0
            # drop in-flight USB buffers (old gain) plus settling time
            self._skip = int(2 * self.rate * settle_s) + 2 * 262144
            self._done.clear()
            self._want = nbytes
        tmo = timeout or (n_samples / self.rate + 2.0)
        t_end = time.time() + tmo
        last, t_last = -1, time.time()
        # watch progress so a USB drop-out fails in seconds instead of after the full timeout
        while not self._done.wait(0.25):
            got = self._rx_total
            if got != last:
                last, t_last = got, time.time()
            elif time.time() - t_last > STALL_S:
                raise HackRFError(f"capture timeout: USB stream stalled after {self._got}/{nbytes} bytes "
                                  f"(device dropped off USB? check power/cable)")
            if time.time() > t_end:
                raise HackRFError(f"capture timeout: {self._got}/{nbytes} bytes in {tmo:.1f}s")
        raw = np.concatenate(self._chunks)[:nbytes].view(np.int8)
        self._chunks = []
        iq = raw.astype(np.float32).view(np.complex64) / 128.0
        return iq, raw


if __name__ == "__main__":
    with HackRF() as h:
        h.set_rate(2e6)
        h.set_gain(24, 20, 0)
        for f in (100e6, 433.92e6, 950e6):
            h.tune(f)
            t0 = time.time()
            iq, raw = h.capture(200_000)
            print(f"{f/1e6:8.2f} MHz  n={len(iq)}  rms={np.sqrt(np.mean(np.abs(iq)**2)):.4f} "
                  f"peak={np.abs(raw).max()}  {1e3*(time.time()-t0):.0f} ms")
