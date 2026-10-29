"""rfscan command line.

  rfscan scan     [--range 1:6000] [--duration 3600] ...   live autonomous scan
  rfscan survey   [--range 1:6000] [--seconds 30]          one wideband survey, print regions
  rfscan analyze  PATH [PATH...] [--out DIR]               offline: classify + decode stored IQ
  rfscan report   RUN_DIR                                  (re)build report.html
  rfscan monitor  RUN_DIR [--baseline RUN_DIR]             heatmap, band occupancy, new-signal list
  rfscan capture  FREQ_MHZ RATE_MSPS SECONDS OUT           single capture with auto-gain
  rfscan decoders                                          list decoders and their status
"""
import argparse
import glob
import os
import sys
import time


def _range(s):
    a, b = s.split(":")
    return float(a), float(b)


def cmd_scan(a):
    from .scheduler import Scanner
    out = a.out or os.path.join("runs", time.strftime("%Y%m%d_%H%M%S"))
    lo, hi = _range(a.range)
    Scanner(out, lo, hi, survey_s=a.survey_seconds, resurvey_s=a.resurvey, duration=a.duration,
            watch=not a.no_watch, watch_names=a.watch.split(",") if a.watch else None,
            storage_gb=a.storage_gb, workers=a.workers, bin_w=a.bin_khz * 1e3,
            keep_wide=not a.no_keep_wide and a.focus != "odd", focus=a.focus, baseline=a.baseline).run()


def cmd_survey(a):
    from . import bandplan, sweep
    from .scheduler import is_spur
    lo, hi = _range(a.range)
    regions, spec, plan = sweep.survey(lo, hi, a.bin_khz * 1e3, a.seconds)
    real = [r for r in regions if not is_spur(r)]
    print(f"{len(real)} regions ({len(regions)-len(real)} spur-like hidden)")
    for r in sorted(real, key=lambda r: r.f_center):
        print(f"{r.f_center/1e6:10.3f} MHz  bw {r.bw/1e3:7.0f} kHz  snr {r.snr_db:5.1f} dB  occ {r.occupancy:4.2f}  "
              f"{'cont ' if r.continuous else 'inter'}  {bandplan.best(r.f_center).service}")


def _collect_inputs(paths, fmt, rate, freq):
    from . import recording
    recs = []
    for p in paths:
        files = sorted(glob.glob(os.path.join(p, "**", "*.sigmf-meta"), recursive=True)) if os.path.isdir(p) else [p]
        for f in files:
            if f.endswith((".sigmf-meta", ".sigmf-data")):
                recs.append(recording.load(f))
            else:
                if not (rate and freq):
                    sys.exit(f"{f}: raw file needs --rate and --freq (and --format)")
                recs.append(recording.import_raw(f, freq * 1e6, rate * 1e6, fmt))
    # de-duplicate meta/data pairs
    seen, out = set(), []
    for r in recs:
        if r.path not in seen:
            seen.add(r.path)
            out.append(r)
    return out


def cmd_analyze(a):
    from concurrent.futures import ProcessPoolExecutor
    from . import bandplan, decoders, pipeline, report
    from .db import DB
    recs = _collect_inputs(a.paths, a.format, a.rate, a.freq)
    out = a.out or os.path.join("runs", time.strftime("analyze_%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    db = DB(os.path.join(out, "rfscan.sqlite"))
    avail = decoders.registry()
    jobs = []
    for r in recs:
        span = 0.4 * r.rate
        decs = a.decoders.split(",") if a.decoders else \
            [d for d in bandplan.decoders_for(r.center_hz - span, r.center_hz + span) if d in avail]
        bursts = a.mode == "bursts" or (a.mode == "auto" and r.rate <= 8e6)
        t = time.mktime(time.strptime(r.start_time[:19], "%Y-%m-%dT%H:%M:%S")) if r.start_time else time.time()
        cid = db.add_capture(t, "file", os.path.basename(r.path), r.center_hz, r.rate,
                             r.n_samples() / r.rate, [], decs)
        jobs.append((cid, r, {"path": r.path, "out_dir": out, "decoders": decs, "bursts": bursts, "t": t,
                              "name": os.path.basename(r.path), "save": not a.no_save, "keep_wide": False}))
    print(f"analyzing {len(jobs)} recording(s) -> {out}")
    with ProcessPoolExecutor(max_workers=a.workers or max(2, (os.cpu_count() or 4) // 3)) as ex:
        futs = {ex.submit(pipeline.analyze, j): (cid, r) for cid, r, j in jobs}
        for f in futs:
            cid, r = futs[f]
            res = f.result()
            db.finish_capture(cid, res)
            print(f"\n== {os.path.basename(r.path)}  {r.center_hz/1e6:.3f} MHz @ {r.rate/1e6:g} Msps "
                  f"({res['analysis_s']:.1f}s)")
            for d in res["decodes"]:
                print(f"   {d['level'].upper():6s} {d['decoder']:7s} {d['freq_hz']/1e6:10.4f}  {d['label'][:110]}")
            for s in sorted(res["signals"], key=lambda s: -s["snr"])[:a.top]:
                print(f"   signal {s['freq']/1e6:10.4f} MHz {s['bw']/1e3:7.0f} kHz {s['snr']:5.1f} dB  {s['label'][:70]}"
                      f"{'  ['+s['iq_path']+']' if s.get('iq_path') else ''}")
            for e in res.get("errors", []):
                print("   [warn]", e.strip().splitlines()[-1])
    print("\nreport:", report.write(out))


def cmd_report(a):
    from . import report
    print(report.write(a.run_dir))


def cmd_monitor(a):
    """Offline monitoring of a stored run: new-signal list, heatmap PNG, JSON summary."""
    from . import monitor
    summ, _ = monitor.analyze(a.run_dir, a.baseline, f_tol=a.f_tol_khz * 1e3, db_tol=a.db_tol, min_snr=a.min_snr)
    out = os.path.join(a.run_dir, monitor.OUT_DIR)
    news = [n for n in summ["new_signals"] if n["persisted"] >= min(a.min_persist, n["later_surveys"])]
    once = sum(1 for n in news if n["later_surveys"] and not n["persisted"])
    base = f"baseline run {a.baseline} ({summ['n_baseline_surveys']} surveys) + earlier surveys" if a.baseline \
        else "baseline = earlier surveys of this run"
    print(f"{summ['n_surveys']} surveys, {base}; tolerance {a.f_tol_khz:g} kHz / {a.db_tol:g} dB, min SNR {a.min_snr:g} dB")
    print(f"{len(news)} new signals ({once} seen in one survey only, listed last):")
    for n in news:
        pers = f"{n['persisted']}/{n['later_surveys']}" if n["later_surveys"] else "  -"
        bdb = "   -" if n["baseline_db"] is None else f"{n['baseline_db']:4.1f}"
        print(f"{n['freq_mhz']:10.3f} MHz  bw {n['bw_khz']:6.0f} kHz  snr {n['snr']:5.1f} dB  base {bdb} dB"
              f"  occ {n['occupancy']:4.2f}  {'cont ' if n['continuous'] else 'inter'}  first {n['t_str'][11:16]}  "
              f"persisted {pers:>5s}  {n['band'][:45]}")
    if a.log_events:
        monitor.log_events(a.run_dir, news)
        print(f"{len(news)} new_signal events appended to {os.path.join(a.run_dir, 'events.jsonl')}")
    print(f"heatmap: {os.path.join(out, 'heatmap.png')}\nsummary: {os.path.join(out, 'summary.json')}")


def cmd_dashboard(a):
    """Serve a live, auto-refreshing report for a (running) scan on localhost."""
    import functools
    import http.server
    import threading
    from . import report
    run = a.run_dir
    if run is None:     # newest run directory
        runs = sorted(glob.glob(os.path.join("runs", "*", "rfscan.sqlite")), key=os.path.getmtime)
        if not runs:
            sys.exit("no runs/*/rfscan.sqlite found")
        run = os.path.dirname(runs[-1])
    lock = threading.Lock()
    last = [0.0]

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html", "/report.html"):
                with lock:
                    if time.time() - last[0] > 3:
                        try:
                            report.write(run, refresh=a.refresh)
                        except Exception as e:   # db busy mid-write: serve the previous page
                            print("report:", e)
                        last[0] = time.time()
                self.path = "/report.html"
            return super().do_GET()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer((a.host, a.port), functools.partial(H, directory=run))
    print(f"dashboard for {run}: http://{a.host}:{a.port}/  (Ctrl-C to stop)")
    srv.serve_forever()


def cmd_capture(a):
    from .capture import capture
    from .hackrf import HackRF
    with HackRF() as d:
        capture(d, a.freq * 1e6, a.rate * 1e6, a.seconds, a.out, bursty=a.bursty)
    print(a.out + ".sigmf-meta")


def cmd_decoders(a):
    from . import decoders
    reg = decoders.registry()
    for n, m in sorted(reg.items()):
        doc = (m.__doc__ or "").strip().splitlines()
        print(f"{n:8s} {getattr(m, 'CAPTURE', '')!s:45.45s} {doc[0] if doc else ''}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="rfscan", description="Autonomous HackRF RF environment scanner")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="live autonomous scan")
    s.add_argument("--range", default="1:6000", help="MHz lo:hi (default 1:6000)")
    s.add_argument("--duration", type=float, default=None, help="seconds (default: until Ctrl-C)")
    s.add_argument("--survey-seconds", type=float, default=30)
    s.add_argument("--resurvey", type=float, default=900, help="seconds between surveys")
    s.add_argument("--bin-khz", type=float, default=100)
    s.add_argument("--no-watch", action="store_true", help="skip fixed watch bands (ISM/ADS-B/AIS/pager/BLE)")
    s.add_argument("--watch", help="comma list of watch bands: ism433,srd868,vhf169,adsb,ais,ble38,pmr446")
    s.add_argument("--storage-gb", type=float, default=10)
    s.add_argument("--no-keep-wide", action="store_true", help="never keep full 20 MHz captures")
    s.add_argument("--focus", choices=["all", "odd"], default="all",
                   help="odd: skip cellular/Wi-Fi/BLE/broadcast/ADS-B, spend time on unexplained signals")
    s.add_argument("--workers", type=int)
    s.add_argument("--out")
    s.add_argument("--baseline", help="run dir whose surveys count as 'seen before' for new-signal alerts")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("survey", help="single wideband survey")
    s.add_argument("--range", default="1:6000")
    s.add_argument("--seconds", type=float, default=30)
    s.add_argument("--bin-khz", type=float, default=100)
    s.set_defaults(fn=cmd_survey)
    s = sub.add_parser("analyze", help="offline analysis of stored IQ (SigMF or raw)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out")
    s.add_argument("--decoders", help="comma list (default: from band plan)")
    s.add_argument("--mode", choices=["auto", "bursts", "carriers"], default="auto")
    s.add_argument("--format", default="cs8", choices=["cs8", "cu8", "cs16", "cf32"], help="raw file format")
    s.add_argument("--rate", type=float, help="Msps for raw files")
    s.add_argument("--freq", type=float, help="centre MHz for raw files")
    s.add_argument("--no-save", action="store_true")
    s.add_argument("--top", type=int, default=8)
    s.add_argument("--workers", type=int)
    s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("report")
    s.add_argument("run_dir")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("monitor", help="heatmap + band occupancy + new signals of a stored run")
    s.add_argument("run_dir")
    s.add_argument("--baseline", help="run dir whose surveys count as 'seen before' (default: earlier surveys only)")
    s.add_argument("--f-tol-khz", type=float, default=150, help="frequency tolerance (default 150 kHz)")
    s.add_argument("--db-tol", type=float, default=6, help="must exceed the baseline by this much (default 6 dB)")
    s.add_argument("--min-snr", type=float, default=10, help="ignore weaker regions (default 10 dB)")
    s.add_argument("--min-persist", type=int, default=0,
                   help="only list signals seen again in at least N later surveys (default 0: all)")
    s.add_argument("--log-events", action="store_true", help="also append new_signal events to events.jsonl")
    s.set_defaults(fn=cmd_monitor)
    s = sub.add_parser("dashboard", help="live auto-refreshing report in the browser")
    s.add_argument("run_dir", nargs="?", help="default: newest run")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--refresh", type=int, default=10, help="browser refresh seconds")
    s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("capture")
    s.add_argument("freq", type=float)
    s.add_argument("rate", type=float)
    s.add_argument("seconds", type=float)
    s.add_argument("out")
    s.add_argument("--bursty", action="store_true")
    s.set_defaults(fn=cmd_capture)
    s = sub.add_parser("decoders")
    s.set_defaults(fn=cmd_decoders)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
