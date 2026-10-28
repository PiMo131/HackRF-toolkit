"""SigMF recordings: write/read IQ with metadata.

Raw dwell snapshots are stored as ci8 (HackRF native, 2 bytes/sample); channelised
bursts are stored as ci16 (more dynamic range after filtering/decimation).
"""
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field

import numpy as np

SIGMF_VERSION = "1.0.0"
_DTYPES = {"ci8": np.int8, "ci16_le": np.int16, "cf32_le": np.float32}


@dataclass
class Recording:
    path: str              # base path without extension
    center_hz: float
    rate: float
    datatype: str = "ci8"
    meta: dict = field(default_factory=dict)

    @property
    def data_path(self):
        return self.path + ".sigmf-data"

    @property
    def meta_path(self):
        return self.path + ".sigmf-meta"

    def n_samples(self):
        b = os.path.getsize(self.data_path)
        return b // (2 * np.dtype(_DTYPES[self.datatype]).itemsize)

    def iq(self, start=0, count=None):
        """complex64 samples scaled to roughly +-1.0."""
        dt_ = _DTYPES[self.datatype]
        if sys.platform == "emscripten":     # Pyodide: no mmap, read just the requested span
            n = -1 if count is None else 2 * count
            x = np.fromfile(self.data_path, dtype=dt_, count=n,
                            offset=2 * start * np.dtype(dt_).itemsize).astype(np.float32)
        else:
            raw = np.memmap(self.data_path, dtype=dt_, mode="r")
            a = 2 * start
            b = len(raw) if count is None else min(len(raw), a + 2 * count)
            x = np.asarray(raw[a:b], dtype=np.float32)
        if self.datatype == "ci8":
            x /= 128.0
        elif self.datatype == "ci16_le":
            x /= 32768.0
        return x.view(np.complex64)

    def annotations(self):
        return self.meta.get("annotations", [])

    @property
    def start_time(self):
        return self.meta.get("captures", [{}])[0].get("core:datetime")


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write(path, center_hz, rate, data, datatype=None, description="", extra=None,
          annotations=None, datetime=None):
    """data: int8 interleaved raw (ci8) or complex array (stored as ci16 normalised)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if datatype is None:
        datatype = "ci8" if data.dtype == np.int8 else "ci16_le"
    if datatype == "ci8":
        out = np.asarray(data, np.int8)
    elif datatype == "ci16_le":
        x = np.asarray(data, np.complex64)
        pk = float(np.max(np.abs(x.view(np.float32)))) if len(x) else 1.0
        scale = 30000.0 / pk if pk > 0 else 1.0
        out = np.round(x.view(np.float32) * scale).astype("<i2")
        extra = dict(extra or {}, **{"rfscan:ci16_scale": scale})
    else:
        out = np.asarray(data, np.complex64).view(np.float32)
    out.tofile(path + ".sigmf-data")
    meta = {
        "global": {
            "core:datatype": datatype,
            "core:sample_rate": float(rate),
            "core:version": SIGMF_VERSION,
            "core:hw": "HackRF One",
            "core:recorder": "rfscan",
            "core:description": description,
            **(extra or {}),
        },
        "captures": [{"core:sample_start": 0, "core:frequency": float(center_hz),
                      "core:datetime": datetime or _now_iso()}],
        "annotations": annotations or [],
    }
    with open(path + ".sigmf-meta", "w") as f:
        json.dump(meta, f, indent=1)
    return Recording(path, center_hz, rate, datatype, meta)


def load(path):
    base = path
    for ext in (".sigmf-meta", ".sigmf-data"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    with open(base + ".sigmf-meta") as f:
        meta = json.load(f)
    g = meta["global"]
    return Recording(base, float(meta["captures"][0]["core:frequency"]), float(g["core:sample_rate"]),
                     g["core:datatype"], meta)


def import_raw(src, center_hz, rate, fmt="cs8", dst=None):
    """Wrap a raw hackrf_transfer (cs8) / rtl_sdr (cu8) / cf32 file as a SigMF recording."""
    raw = np.fromfile(src, dtype={"cs8": np.int8, "cu8": np.uint8, "cf32": np.float32, "cs16": np.int16}[fmt])
    if fmt == "cu8":
        raw = (raw.astype(np.int16) - 127).astype(np.int8)
        fmt = "cs8"
    dst = dst or os.path.splitext(src)[0]
    if fmt == "cs8":
        return write(dst, center_hz, rate, raw, "ci8", f"imported {src}")
    if fmt == "cs16":
        iq = raw.astype(np.float32).view(np.complex64) / 32768
    else:
        iq = raw.view(np.complex64)
    return write(dst, center_hz, rate, iq, "ci16_le", f"imported {src}")
