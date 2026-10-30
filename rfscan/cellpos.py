"""Offline cell-tower position lookup for "Where am I?".

Data: any `cell_towers` CSV (radio,mcc,net,area,cell,unit,lon,lat,range,samples,changeable,created,updated,averageSignal),
e.g. the Mozilla Location Service final full export (CC0/public domain, mirrored on archive.org) or an OpenCelliD
per-MCC file (needs a free token). `convert` filters it to a few MCCs and writes compact per-MCC files that both
this module and the browser (web/index.html, "cell DB (offline)") read:

    web/cells/<mcc>.csv.gz     header: radio,net,area,cell,lon,lat,range,samples

Usage:
    python -m rfscan.cellpos convert MLS-full-cell-export-final.csv.gz [--mcc 204,206,262] [--out web/cells]
    python -m rfscan.cellpos runs/<run>/rfscan.sqlite        # or a JSON list of {mcc,mnc,lac,ci,power_db}
        -> JSON list of {src:"celldb", lat, lon, acc, n, note}

Only GSM cells are matched: our LTE decoder has no SIB1 yet, so there is no ECI/TAC to look up.
"""
import csv
import gzip
import json
import math
import os
import sqlite3
import sys

CELLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "cells")
FULL_HEADER = ["radio", "mcc", "net", "area", "cell", "unit", "lon", "lat", "range", "samples",
               "changeable", "created", "updated", "averageSignal"]
OUT_HEADER = ["radio", "net", "area", "cell", "lon", "lat", "range", "samples"]
MIN_RANGE = 2000.0      # m; MLS 'range' is the spread of the observations, often 0 or tiny -> floor for a heard tower
MIN_ACC = 3000.0        # m; never claim better than this from cell IDs alone


# ---------------------------------------------------------------- convert
def convert(src, out_dir=CELLS_DIR, mccs=(204, 206, 262), radios=("GSM",)):
    """Stream a (gzipped) cell_towers CSV, keep rows of `mccs` (and `radios` if given), write <out>/<mcc>.csv.gz.
    Positions are rounded to 1e-5 deg (~1 m) to keep the files small. Returns {mcc: rows}."""
    mccs = {str(m) for m in mccs}
    radios = {r.upper() for r in radios} if radios else None
    os.makedirs(out_dir, exist_ok=True)
    outs, counts = {}, {}
    opener = gzip.open if str(src).endswith(".gz") else open
    with opener(src, "rt", newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.reader(f)
        first = next(rd, None)
        if first is None:
            return {}
        head = first if first and first[0].strip().lower() == "radio" else FULL_HEADER   # OpenCelliD files have no header
        ix = {k: head.index(k) for k in ("radio", "mcc", "net", "area", "cell", "lon", "lat", "range", "samples")}
        rows = rd if head is not FULL_HEADER else _chain([first], rd)
        for r in rows:
            if len(r) <= ix["samples"] or r[ix["mcc"]] not in mccs:
                continue
            radio = r[ix["radio"]].upper()
            if radios and radio not in radios:
                continue
            try:
                lon, lat = float(r[ix["lon"]]), float(r[ix["lat"]])
            except ValueError:
                continue
            m = r[ix["mcc"]]
            if m not in outs:
                outs[m] = gzip.open(os.path.join(out_dir, f"{m}.csv.gz"), "wt", newline="", compresslevel=9)
                outs[m].write(",".join(OUT_HEADER) + "\n")
                counts[m] = 0
            outs[m].write(f"{radio},{r[ix['net']]},{r[ix['area']]},{r[ix['cell']]},{lon:.5f},{lat:.5f},"
                          f"{r[ix['range']] or 0},{r[ix['samples']] or 0}\n")
            counts[m] += 1
    for o in outs.values():
        o.close()
    return {int(k): v for k, v in counts.items()}


def _chain(*its):
    for it in its:
        yield from it


# ---------------------------------------------------------------- lookup
_cache = {}


def load_mcc(mcc, cells_dir=CELLS_DIR):
    """{(radio, net, area, cell): (lat, lon, range, samples)} for one MCC, or None when the file is absent."""
    key = (cells_dir, int(mcc))
    if key in _cache:
        return _cache[key]
    path = os.path.join(cells_dir, f"{int(mcc)}.csv.gz")
    if not os.path.exists(path):
        _cache[key] = None
        return None
    db = {}
    with gzip.open(path, "rt", newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        for radio, net, area, cell, lon, lat, rng, smp in rd:
            db[(radio, int(net), int(area), int(cell))] = (float(lat), float(lon), float(rng or 0), int(smp or 0))
    _cache[key] = db
    return db


def haversine(a, b):
    R, toR = 6371e3, math.pi / 180
    dlat, dlon = (b[0] - a[0]) * toR, (b[1] - a[1]) * toR
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * toR) * math.cos(b[0] * toR) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def lac_area(db, net, lac, radio="GSM"):
    """Centroid + spread (m, 90th percentile distance to the centroid) of all cells of one LAC; None if unknown."""
    pts = [v for k, v in db.items() if k[0] == radio and k[1] == net and k[2] == lac]
    if not pts:
        return None
    c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    d = sorted(haversine(c, p) for p in pts)
    return {"lat": c[0], "lon": c[1], "spread": d[int(0.9 * (len(d) - 1))], "n": len(pts)}


def lookup(cells, cells_dir=CELLS_DIR):
    """cells: iterable of dicts with mcc,mnc,lac,ci[,power_db]. Duplicates are merged (mean power).
    Returns (towers, missing): towers = list of {mcc,mnc,lac,ci,power_db,lat,lon,range,how} where how is
    'cell' (exact CI found) or 'lac' (CI unknown, LAC centroid with its spread as range); missing = reasons."""
    merged = {}
    for c in cells:
        try:
            k = (int(c["mcc"]), int(c["mnc"]), int(c["lac"]), int(c["ci"]))
        except (KeyError, TypeError, ValueError):
            continue
        p = c.get("power_db")
        merged.setdefault(k, []).append(float(p) if p is not None else None)
    towers, missing = [], []
    for (mcc, mnc, lac, ci), ps in merged.items():
        ps = [p for p in ps if p is not None]
        pw = sum(ps) / len(ps) if ps else None
        db = load_mcc(mcc, cells_dir)
        tag = f"{mcc}-{mnc:02d} LAC {lac} CI {ci}"
        if db is None:
            missing.append(f"{tag}: no cell file {mcc}.csv.gz")
            continue
        hit = db.get(("GSM", mnc, lac, ci))      # UMTS/LTE cell IDs are a different (28-bit) space: no cross-match
        t = {"mcc": mcc, "mnc": mnc, "lac": lac, "ci": ci, "power_db": pw}
        if hit:
            towers.append({**t, "lat": hit[0], "lon": hit[1], "range": max(hit[2], MIN_RANGE), "samples": hit[3], "how": "cell"})
            continue
        la = lac_area(db, mnc, lac)
        if la:
            towers.append({**t, "lat": la["lat"], "lon": la["lon"], "range": max(la["spread"], MIN_RANGE),
                           "samples": la["n"], "how": "lac"})
        else:
            missing.append(f"{tag}: not in the database")
    return towers, missing


def estimate(towers):
    """Weighted mean of tower positions, weight = amplitude relative to the strongest (10^(dP/20)) / range.
    LAC centroids (CI unknown) are only used when no cell was found by CI: a LAC spans tens of km, so its centroid
    says much less than one known tower (validated near Willemstad NL: 14 km off with them mixed in, 0.4-0.6 km without).
    Accuracy: weighted RMS of (tower-to-estimate distance)^2 + (range/sqrt2)^2, floored at MIN_ACC. The receiver
    hears every tower, so it lies within about one range of each (RMS distance from the centre of a uniformly
    covered disc of radius R is R/sqrt2), and the towers' scatter around the estimate adds to that."""
    if not towers:
        return None
    exact = [t for t in towers if t["how"] == "cell"]
    use = exact or towers
    pmax = max((t["power_db"] for t in use if t["power_db"] is not None), default=0.0)
    ws = [10 ** (((t["power_db"] if t["power_db"] is not None else pmax - 10) - pmax) / 20) / t["range"] for t in use]
    W = sum(ws)
    lat = sum(w * t["lat"] for w, t in zip(ws, use)) / W
    lon = sum(w * t["lon"] for w, t in zip(ws, use)) / W
    var = sum(w * (haversine((lat, lon), (t["lat"], t["lon"])) ** 2 + t["range"] ** 2 / 2) for w, t in zip(ws, use)) / W
    acc = max(MIN_ACC, math.sqrt(var))
    nl = len(towers) - len(exact)
    if exact:
        note = f"offline cell DB: {len(exact)} cell{'s' if len(exact) != 1 else ''} by CI" + (f" ({nl} more with unknown CI, LAC only, not used)" if nl else "")
    else:
        note = f"offline cell DB: CI unknown, centroid of {nl} LAC{'s' if nl != 1 else ''}"
    return {"src": "celldb", "lat": round(lat, 5), "lon": round(lon, 5), "acc": round(acc), "n": len(use), "note": note}


# ---------------------------------------------------------------- inputs
def cells_from_sqlite(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = []
    for (fj,) in con.execute("SELECT fields FROM decodes WHERE decoder='gsm' AND crc_ok=1"):
        try:
            f = json.loads(fj or "{}")
        except ValueError:
            continue
        if f.get("mcc") is not None and f.get("lac") is not None and f.get("ci") is not None:
            out.append(f)
    con.close()
    return out


def cells_from_json(path):
    with open(path) as f:
        j = json.load(f)
    if isinstance(j, dict):
        j = j.get("cells") or j.get("cellTowers") or []
    out = []
    for c in j:   # accept our field names and the geolocate-API names
        if "fields" in c:
            c = c["fields"]
        out.append({"mcc": c.get("mcc", c.get("mobileCountryCode")), "mnc": c.get("mnc", c.get("mobileNetworkCode")),
                    "lac": c.get("lac", c.get("locationAreaCode")), "ci": c.get("ci", c.get("cellId")),
                    "power_db": c.get("power_db", c.get("signalStrength"))})
    return out


def main(argv=None):
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "convert":
        ap = argparse.ArgumentParser(prog="python -m rfscan.cellpos convert")
        ap.add_argument("src", help="cell_towers CSV(.gz): MLS full export or OpenCelliD per-MCC file")
        ap.add_argument("--mcc", default="204,206,262")
        ap.add_argument("--radio", default="GSM", help="comma list, e.g. GSM,LTE, or 'all' (default GSM: the only radio we "
                        "can match; all radios of MCC 262 would be ~43 MB)")
        ap.add_argument("--out", default=CELLS_DIR)
        a = ap.parse_args(argv[1:])
        n = convert(a.src, a.out, [int(x) for x in a.mcc.split(",")], None if a.radio == "all" else a.radio.split(","))
        for m, k in sorted(n.items()):
            p = os.path.join(a.out, f"{m}.csv.gz")
            print(f"{p}: {k} cells, {os.path.getsize(p) / 1e6:.1f} MB")
        return 0
    ap = argparse.ArgumentParser(prog="python -m rfscan.cellpos", description="offline GSM cell position estimate")
    ap.add_argument("input", help="rfscan.sqlite or a JSON list of cells {mcc,mnc,lac,ci,power_db}")
    ap.add_argument("--cells-dir", default=CELLS_DIR)
    ap.add_argument("-v", "--verbose", action="store_true", help="print the per-tower table to stderr")
    a = ap.parse_args(argv)
    cells = cells_from_sqlite(a.input) if a.input.endswith((".sqlite", ".db")) else cells_from_json(a.input)
    towers, missing = lookup(cells, a.cells_dir)
    if a.verbose:
        for t in towers:
            print(f"  {t['mcc']}-{t['mnc']:02d} LAC {t['lac']} CI {t['ci']}: {t['lat']:.5f},{t['lon']:.5f} "
                  f"r={t['range']:.0f} m P={t['power_db']} via {t['how']}", file=sys.stderr)
        for m in missing:
            print("  " + m, file=sys.stderr)
    e = estimate(towers)
    if e and missing:
        e["note"] += f"; {len(missing)} unknown"
    print(json.dumps([e] if e else []))
    if not e:
        print("no estimate: " + ("; ".join(missing) if missing else "no GSM cells with LAC/CI in the input"), file=sys.stderr)
    return 0 if e else 1


if __name__ == "__main__":
    sys.exit(main())
