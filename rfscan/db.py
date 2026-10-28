"""SQLite store for a run: surveys, captures, classified signals, decoder results."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS surveys (id INTEGER PRIMARY KEY, t REAL, f_lo REAL, f_hi REAL,
    n_regions INT, spectrum_path TEXT, plan TEXT);
CREATE TABLE IF NOT EXISTS regions (survey_id INT, f_center REAL, f_lo REAL, f_hi REAL, bw REAL,
    snr REAL, peak REAL, occupancy REAL, continuous INT, band TEXT, spur INT);
CREATE TABLE IF NOT EXISTS captures (id INTEGER PRIMARY KEY, t REAL, kind TEXT, name TEXT, center REAL,
    rate REAL, seconds REAL, gain TEXT, decoders TEXT, n_signals INT, n_decodes INT,
    saved_bytes INT, wide_path TEXT, analysis_s REAL);
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, capture_id INT, t REAL, freq REAL, bw REAL,
    snr REAL, kind TEXT, label TEXT, conf REAL, hints TEXT, band TEXT, n_bursts INT, iq_path TEXT,
    features TEXT);
CREATE TABLE IF NOT EXISTS decodes (id INTEGER PRIMARY KEY, capture_id INT, t REAL, decoder TEXT,
    level TEXT, crc_ok INT, freq REAL, ident TEXT, label TEXT, fields TEXT, iq_path TEXT);
CREATE INDEX IF NOT EXISTS decodes_ident ON decodes(decoder, ident);
CREATE INDEX IF NOT EXISTS signals_freq ON signals(freq);
"""


def _json(o):
    return json.dumps(o, default=lambda x: x.tolist() if hasattr(x, "tolist") else str(x))


class DB:
    def __init__(self, path):
        self.path = path
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript(SCHEMA)
        self.c.commit()

    def add_survey(self, f_lo, f_hi, regions, spectrum_path, plan, spur_flags, band_names):
        cur = self.c.execute("INSERT INTO surveys(t,f_lo,f_hi,n_regions,spectrum_path,plan) VALUES (?,?,?,?,?,?)",
                             (time.time(), f_lo, f_hi, len(regions), spectrum_path, _json(plan)))
        sid = cur.lastrowid
        self.c.executemany(
            "INSERT INTO regions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, r.f_center, r.f_lo, r.f_hi, r.bw, r.snr_db, r.peak_db, r.occupancy, int(r.continuous),
              b, int(s)) for r, s, b in zip(regions, spur_flags, band_names)])
        self.c.commit()
        return sid

    def add_capture(self, t, kind, name, center, rate, seconds, gain, decoders):
        cur = self.c.execute(
            "INSERT INTO captures(t,kind,name,center,rate,seconds,gain,decoders) VALUES (?,?,?,?,?,?,?,?)",
            (t, kind, name, center, rate, seconds, _json(gain), ",".join(decoders)))
        self.c.commit()
        return cur.lastrowid

    def finish_capture(self, cid, result):
        t = result["t"]
        self.c.executemany(
            "INSERT INTO signals(capture_id,t,freq,bw,snr,kind,label,conf,hints,band,n_bursts,iq_path,features)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(cid, t, s["freq"], s["bw"], s["snr"], s["kind"], s["label"], s["conf"], _json(s["hints"]),
              s["band"], s.get("n_bursts", 0), s.get("iq_path"), _json(s.get("features", {})))
             for s in result["signals"]])
        self.c.executemany(
            "INSERT INTO decodes(capture_id,t,decoder,level,crc_ok,freq,ident,label,fields,iq_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(cid, t + (d.get("t_s") or 0), d["decoder"], d["level"],
              None if d["crc_ok"] is None else int(bool(d["crc_ok"])), d["freq_hz"], d["id"], d["label"],
              _json(d.get("fields", {})), d.get("iq_path")) for d in result["decodes"]])
        self.c.execute("UPDATE captures SET n_signals=?, n_decodes=?, saved_bytes=?, wide_path=?, analysis_s=?"
                       " WHERE id=?", (len(result["signals"]), len(result["decodes"]), result["saved_bytes"],
                                       result.get("wide_path"), result.get("analysis_s"), cid))
        self.c.commit()

    def known_ident(self, decoder, ident):
        return self.c.execute("SELECT 1 FROM decodes WHERE decoder=? AND ident=? LIMIT 1",
                              (decoder, ident)).fetchone() is not None

    def saved_bytes(self):
        return self.c.execute("SELECT COALESCE(SUM(saved_bytes),0) FROM captures").fetchone()[0]

    def emitters(self):
        """One row per decoded identity (latest label, counts, first/last seen)."""
        q = """SELECT decoder, ident, MIN(t), MAX(t), COUNT(*), MAX(level='crc'), MAX(level='sync'),
                      AVG(freq), (SELECT label FROM decodes d2 WHERE d2.decoder=d.decoder AND d2.ident=d.ident
                                  ORDER BY t DESC LIMIT 1),
                      (SELECT fields FROM decodes d2 WHERE d2.decoder=d.decoder AND d2.ident=d.ident
                                  ORDER BY t DESC LIMIT 1),
                      (SELECT iq_path FROM decodes d2 WHERE d2.decoder=d.decoder AND d2.ident=d.ident
                                  AND iq_path IS NOT NULL ORDER BY t DESC LIMIT 1)
               FROM decodes d GROUP BY decoder, ident ORDER BY decoder, MIN(t)"""
        return self.c.execute(q).fetchall()

    def signal_summary(self, bucket_hz=50e3):
        """Classified signals aggregated by frequency bucket."""
        q = """SELECT ROUND(freq/?)*? AS fb, COUNT(*), MAX(snr), AVG(bw), band,
                      (SELECT label FROM signals s2 WHERE ROUND(s2.freq/?)*?=ROUND(s.freq/?)*?
                       ORDER BY conf DESC, snr DESC LIMIT 1),
                      MAX(conf), SUM(n_bursts), MIN(t), MAX(t),
                      (SELECT iq_path FROM signals s2 WHERE ROUND(s2.freq/?)*?=ROUND(s.freq/?)*?
                       AND iq_path IS NOT NULL ORDER BY snr DESC LIMIT 1)
               FROM signals s GROUP BY fb ORDER BY fb"""
        b = bucket_hz
        return self.c.execute(q, (b, b, b, b, b, b, b, b, b, b)).fetchall()
