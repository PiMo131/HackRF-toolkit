"""Per-capture analysis, run in worker processes (live) or directly (offline).

analyze(job) -> result dict:
  signals : classified carriers / burst clusters (freq, bw, snr, label, hints, iq_path)
  decodes : decoder results (see rfscan.decoders), de-duplicated per capture
  saved_bytes, wide_path, analysis_s

IQ retention policy ("save only relevant parts"):
  * burst clusters: up to N channelised bursts per frequency cluster (ci16 SigMF)
  * continuous carriers: a short channelised snippet when the signal is decoded or
    unidentified with decent SNR
  * the full wide capture only when job["keep_wide"] and a decoder produced a
    sync/crc-level result (useful to re-run cellular decoders later)
The temporary wide capture is deleted afterwards unless it is kept.
"""
import os
import shutil
import tempfile
import time
import traceback

import numpy as np

from . import bandplan, decoders, dsp, recording

MAX_SIGNALS = 30
MAX_BURSTS_SAVED = 12
SNIPPET_S = 0.2


def _fname(t, f, tag):
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t))
    safe = "".join(c if c.isalnum() else "-" for c in tag)[:40].strip("-")
    return f"{ts}_{f/1e6:.4f}MHz_{safe}"


def bursts_from_rec(rec, chunk_s=0.5):
    """Burst detection over a (possibly long) recording without loading it whole."""
    n = rec.n_samples()
    step = int(chunk_s * rec.rate)
    out = []
    for a in range(0, n, step):
        x = rec.iq(a, step)
        for b in dsp.detect_bursts(dsp.remove_dc(x), rec.rate, chunk_s=chunk_s):
            b.t0 += a / rec.rate
            b.t1 += a / rec.rate
            out.append(b)
    return dsp.merge_bursts(out)


def _save(path, center, rate, iq, desc, annotations=None, extra=None):
    r = recording.write(path, center, rate, iq, "ci16_le", desc, extra=extra, annotations=annotations)
    return r.data_path, os.path.getsize(r.data_path)


def _classify_carriers(rec, job):
    n = min(rec.n_samples(), int(rec.rate * 0.3))
    x = dsp.remove_dc(rec.iq(0, n))
    f, p, pm = dsp.psd(x, rec.rate, maxhold=True)
    cars, floor = dsp.find_carriers(f, p, rec.rate, pm)
    cars = sorted(cars, key=lambda c: -c["snr_db"])[:MAX_SIGNALS]
    sigs = []
    for c in cars:
        fc = rec.center_hz + c["f_off"]
        band = bandplan.best(fc)
        bw = max(c["bw"], 12e3)
        bb, br = dsp.extract(x, rec.rate, c["f_off"], bw * 1.1, os=1.25)
        feat = dsp.features(bb, br, bw)
        label, conf, hints = dsp.classify(feat, band, bw)
        sigs.append({"freq": fc, "bw": c["bw"], "snr": c["snr_db"], "kind": c["kind"], "label": label,
                     "conf": conf, "hints": hints, "band": band.service, "features": _slim(feat),
                     "_f_off": c["f_off"]})
    return sigs


def _classify_bursts(rec, job):
    bursts = bursts_from_rec(rec)
    sigs = []
    for fc_off, group in dsp.cluster_bursts_by_freq(bursts):
        fc = rec.center_hz + fc_off
        band = bandplan.best(fc)
        best = max(group, key=lambda b: b.snr_db)
        bw = max(12e3, float(np.median([b.bw for b in group])))
        pad = 2e-3
        a = max(0, int((best.t0 - pad) * rec.rate))
        x = rec.iq(a, int((best.dur + 2 * pad) * rec.rate))
        bb, br = dsp.extract(x, rec.rate, fc_off, bw * 1.3, os=1.5)
        feat = dsp.features(bb, br, bw)
        label, conf, hints = dsp.classify(feat, band, bw)
        durs = [b.dur for b in group]
        hints = hints + [f"{len(group)} bursts, median {np.median(durs)*1e3:.1f} ms"]
        if len(group) >= 3:
            gaps = np.diff(sorted(b.t0 for b in group))
            if len(gaps) and np.std(gaps) < 0.1 * np.mean(gaps):
                hints.append(f"periodic every {np.mean(gaps)*1e3:.0f} ms")
        sigs.append({"freq": fc, "bw": bw, "snr": max(b.snr_db for b in group), "kind": "bursty",
                     "label": label, "conf": conf, "hints": hints, "band": band.service,
                     "features": _slim(feat), "n_bursts": len(group), "_f_off": fc_off, "_bursts": group})
    return sigs


def _receiver_artifact(fc, bw):
    """HackRF-internal spurs (40 MHz reference harmonics) and below-DC capture edges."""
    if fc < 0.5e6:
        return "below receiver range (capture edge)"
    if bw < 40e3 and abs(fc - round(fc / 40e6) * 40e6) < 8e3:
        return "receiver spur (HackRF 40 MHz clock harmonic)"
    return None


def _mark_artifacts(sigs):
    for s in sigs:
        why = _receiver_artifact(s["freq"], s["bw"])
        if why:
            s["label"], s["conf"], s["kind"] = why, 0.99, "artifact"
    return sigs


def _slim(feat):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in feat.items()
            if k in ("papr_db", "env_cv", "duty", "obw_hz", "flatness", "fsk_levels", "ifreq_std_hz")}


def _occupied_bw(d):
    """Bandwidth a decoded emitter occupies (to fold fragments inside it)."""
    f = d.get("fields", {})
    if "bw_hz" in f:
        return float(f["bw_hz"])
    if d["decoder"] == "lte":
        mib = f.get("mib") or {}
        return 0.9e6 * mib.get("bw_mhz", 1.4) if mib.get("bw_mhz") else 1.08e6
    if d["decoder"] == "nr":
        return 240 * float(f.get("scs_khz", 30)) * 1e3
    return {"gsm": 200e3, "dab": 1.536e6}.get(d["decoder"], 0.0)


def _fold_into_carriers(sigs, decs):
    """Replace fragments inside a decoded wideband carrier by one consolidated signal."""
    out = list(sigs)
    for d in decs:
        bw = _occupied_bw(d)
        if bw < 1e6 or d["level"] not in ("crc", "sync"):
            continue
        fc = d.get("fields", {}).get("carrier_hz", d["freq_hz"])
        inside = [s for s in out if abs(s["freq"] - fc) <= bw / 2 and not s.get("carrier")]
        if not inside:
            continue
        best = max(inside, key=lambda s: s["snr"])
        out = [s for s in out if s not in inside]
        out.append({"freq": fc, "bw": bw / 0.9 if d["decoder"] == "lte" else bw, "snr": best["snr"],
                    "kind": "continuous", "label": f"{d['decoder']}: {d['label'][:90]}",
                    "conf": 1.0 if d["level"] == "crc" else 0.9, "hints": [f"{len(inside)} fragments folded"],
                    "band": best["band"], "features": best.get("features", {}), "_f_off": best.get("_f_off", 0.0),
                    "decoded": True, "carrier": True})
    return out


def _attach_decodes(sigs, decs):
    """Let decoder results override heuristic labels for overlapping signals."""
    for s in sigs:
        half = max(s["bw"] / 2, 30e3)
        hits = [d for d in decs if abs(d["freq_hz"] - s["freq"]) <= half and d["level"] in ("crc", "sync")]
        if hits:
            names = sorted({d["decoder"] for d in hits})
            s["label"] = f"{'/'.join(names)}: {hits[0]['label'][:80]}"
            s["conf"] = 1.0 if any(d["level"] == "crc" for d in hits) else 0.9
            s["decoded"] = True


def _dedupe(decs):
    seen = {}
    for d in decs:
        k = (d["decoder"], d["id"], d["label"])
        if k in seen:
            seen[k].setdefault("fields", {})["dup_count"] = seen[k]["fields"].get("dup_count", 1) + 1
        else:
            seen[k] = d
    return list(seen.values())


def analyze(job):
    t_start = time.time()
    rec = recording.load(job["path"])
    out_dir = job["out_dir"]
    iq_dir = os.path.join(out_dir, "iq")
    os.makedirs(iq_dir, exist_ok=True)
    t0 = job.get("t", time.time())
    result = {"t": t0, "signals": [], "decodes": [], "saved_bytes": 0, "errors": []}
    tmpdir = tempfile.mkdtemp(prefix="dec_", dir=job.get("tmp_root"))
    try:
        # 1. classify
        try:
            if job.get("bursts"):
                sigs = _classify_bursts(rec, job)
            else:
                sigs = _classify_carriers(rec, job)
        except Exception:
            result["errors"].append("classify: " + traceback.format_exc(limit=3))
            sigs = []
        # 2. decoders
        decs = []
        for name in job.get("decoders", []):
            mod = decoders.get(name)
            if mod is None:
                continue
            try:
                ctx = {"tmpdir": tmpdir, "target": job.get("target", {})}
                decs += mod.decode(rec, ctx) or []
            except Exception:
                result["errors"].append(f"{name}: " + traceback.format_exc(limit=3))
        decs = _dedupe(decs)
        sigs = _mark_artifacts(_fold_into_carriers(sigs, decs))
        _attach_decodes(sigs, decs)
        # 3. save relevant IQ
        save = job.get("save", True)
        n_saved = 0
        max_snips = job.get("max_snippets", 8 if not job.get("bursts") else 16)
        min_snr = job.get("min_save_snr", 12 if job.get("bursts") else 15)
        for s in sorted(sigs, key=lambda s: (not s.get("decoded"), -s["snr"])):
            if not save or n_saved >= max_snips:
                break
            interesting = s.get("decoded") or s["label"].startswith(("unidentified", "unknown")) \
                or s["kind"] == "bursty" or s["conf"] < 0.5
            if not interesting or s["snr"] < min_snr:
                continue
            if s.get("carrier") and job.get("keep_wide"):
                continue        # the whole wide capture is kept instead
            n_saved += 1
            base = os.path.join(iq_dir, _fname(t0, s["freq"], s["label"]))
            bw = max(s["bw"], 12e3) * 1.3
            if s.get("_bursts"):
                pieces, ann, pos = [], [], 0
                group = sorted(s["_bursts"], key=lambda b: -b.snr_db)[:MAX_BURSTS_SAVED]
                for b in sorted(group, key=lambda b: b.t0):
                    pad = 3e-3
                    a = max(0, int((b.t0 - pad) * rec.rate))
                    x = rec.iq(a, int((b.dur + 2 * pad) * rec.rate))
                    bb, br = dsp.extract(dsp.remove_dc(x), rec.rate, s["_f_off"], bw, os=2.0, min_rate=100e3)
                    ann.append({"core:sample_start": pos, "core:sample_count": len(bb),
                                "core:freq_lower_edge": s["freq"] - bw / 2, "core:freq_upper_edge": s["freq"] + bw / 2,
                                "core:label": s["label"][:60], "rfscan:t_offset_s": round(b.t0, 6),
                                "rfscan:snr_db": round(b.snr_db, 1)})
                    pieces += [bb, np.zeros(int(br * 0.01), np.complex64)]
                    pos += len(bb) + int(br * 0.01)
                if pieces:
                    p, nbytes = _save(base, s["freq"], br, np.concatenate(pieces),
                                      f"{s['label']} ({len(group)} of {s['n_bursts']} bursts)", ann,
                                      {"rfscan:source_capture": os.path.basename(rec.path)})
                    s["iq_path"] = os.path.relpath(p, out_dir)
                    result["saved_bytes"] += nbytes
            else:
                dur = SNIPPET_S if s["bw"] < 1e6 else 0.05
                x = rec.iq(0, int(rec.rate * dur))
                bb, br = dsp.extract(dsp.remove_dc(x), rec.rate, s["_f_off"], bw, os=1.5, min_rate=100e3)
                p, nbytes = _save(base, s["freq"], br, bb, s["label"], extra={
                    "rfscan:source_capture": os.path.basename(rec.path)})
                s["iq_path"] = os.path.relpath(p, out_dir)
                result["saved_bytes"] += nbytes
        for d in decs:
            near = [s for s in sigs if s.get("iq_path") and abs(s["freq"] - d["freq_hz"]) <= max(s["bw"], 60e3)]
            if near:
                d["iq_path"] = near[0]["iq_path"]
        # 4. keep the wide capture when a decoder locked onto something
        good = [d for d in decs if d["level"] in ("crc", "sync")]
        if job.get("keep_wide") and good and save:
            dst = os.path.join(iq_dir, _fname(t0, rec.center_hz, "wide-" + job.get("name", "")))
            for ext in (".sigmf-data", ".sigmf-meta"):
                shutil.copyfile(rec.path + ext, dst + ext)
            result["wide_path"] = os.path.relpath(dst + ".sigmf-data", out_dir)
            result["saved_bytes"] += os.path.getsize(dst + ".sigmf-data")
            for d in good:
                d.setdefault("iq_path", result["wide_path"])
        for s in sigs:
            s.pop("_f_off", None)
            s.pop("_bursts", None)
            s.pop("decoded", None)
            s.pop("carrier", None)
        result["signals"], result["decodes"] = sigs, decs
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if job.get("delete_source"):
            for ext in (".sigmf-data", ".sigmf-meta"):
                try:
                    os.remove(rec.path + ext)
                except OSError:
                    pass
    result["analysis_s"] = time.time() - t_start
    return result
