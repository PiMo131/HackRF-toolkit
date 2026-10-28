"""Decoder registry.

Every decoder module in this package exposes:

    NAME: str                       registry key (matches bandplan decoder names)
    def decode(rec, ctx) -> list[dict]

`rec` is an rfscan.recording.Recording (rec.iq() -> complex64, rec.center_hz, rec.rate).
`ctx` is a dict with at least {"tmpdir": str} and optionally {"band": Band, "target": {...}}.
Decoders must work purely offline on a recording (live mode just calls them on fresh files),
must not open the HackRF, and must never raise for "nothing found" (return []).

Each result dict:
    decoder   : NAME
    level     : "crc"    payload passed a CRC/parity/checksum
                "sync"   synchronisation sequence confirmed (e.g. LTE PSS+SSS), no CRC
                "detect" modulation/feature evidence only
    crc_ok    : True / False / None
    freq_hz   : absolute frequency of the emitter
    t_s       : offset in seconds from recording start (optional)
    id        : stable emitter identity used to aggregate (e.g. "ICAO:4840D6", "LTE:PCI123@1815.0")
    label     : one-line human summary
    fields    : dict with decoder-specific details
Optional decoder-level capture hint (used by the scheduler):
    CAPTURE = {"rate": 2e6, "seconds": 10, "center": None}
"""
import importlib
import pkgutil

_REGISTRY = {}


def registry():
    if not _REGISTRY:
        for m in pkgutil.iter_modules(__path__):
            if m.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{__name__}.{m.name}")
            except Exception as e:  # a broken optional decoder must not kill the scanner
                print(f"[decoders] {m.name} unavailable: {e}")
                continue
            if hasattr(mod, "NAME") and hasattr(mod, "decode"):
                _REGISTRY[mod.NAME] = mod
    return _REGISTRY


def get(name):
    return registry().get(name)


def result(decoder, level, freq_hz, id, label, crc_ok=None, t_s=None, **fields):
    return {"decoder": decoder, "level": level, "crc_ok": crc_ok, "freq_hz": float(freq_hz),
            "t_s": t_s, "id": id, "label": label, "fields": fields}
