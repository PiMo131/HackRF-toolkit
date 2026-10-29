"""Self-contained HTML report for a run directory (inline SVG spectrum, no deps)."""
import glob
import html
import json
import os
import time

import numpy as np

from . import bandplan, monitor
from .db import DB

CAT_COLORS = {"cellular": "#e4572e", "cellular_ul": "#f3a712", "broadcast": "#6a4c93", "ism": "#29a19c",
              "aero": "#1982c4", "maritime": "#1982c4", "paging": "#ff595e", "tetra": "#8ac926",
              "wifi": "#ffca3a", "dect": "#b5838d", "gnss": "#577590", "satellite": "#577590"}


def _svg_spectrum(npz, regions, width=1400, height=320):
    d = np.load(npz)
    f, mean, mx, fl = d["freqs"], d["median"], d["max"], d["floor"]
    ok = ~np.isnan(mean)
    f, mean, mx, fl = f[ok], mean[ok], mx[ok], fl[ok]
    if len(f) == 0:
        return ""
    # decimate to ~2 points per pixel with max-preserving bins
    nb = min(len(f), width * 2)
    edges = np.linspace(0, len(f), nb + 1).astype(int)
    fx = np.array([f[a] for a in edges[:-1]])
    mm = np.array([np.max(mean[a:b]) for a, b in zip(edges[:-1], edges[1:])])
    xx = np.array([np.max(mx[a:b]) for a, b in zip(edges[:-1], edges[1:])])
    ff = np.array([np.min(fl[a:b]) for a, b in zip(edges[:-1], edges[1:])])
    lo, hi = float(np.nanpercentile(ff, 1)) - 5, float(np.nanmax(xx)) + 3
    f0, f1 = fx[0], fx[-1]
    X = lambda v: 50 + (v - f0) / (f1 - f0) * (width - 60)
    Y = lambda v: 10 + (hi - v) / (hi - lo) * (height - 40)

    def path(v):
        return "M" + " L".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(fx, v))
    parts = [f'<svg viewBox="0 0 {width} {height}" class="spec" xmlns="http://www.w3.org/2000/svg">']
    for r in regions:
        cat = bandplan.best(r[1]).category
        c = CAT_COLORS.get(cat, "#999")
        x0, x1 = X(r[2]), max(X(r[3]), X(r[2]) + 1.5)
        parts.append(f'<rect x="{x0:.1f}" y="10" width="{x1-x0:.1f}" height="{height-40}" fill="{c}" '
                     f'opacity="0.18"><title>{r[1]/1e6:.3f} MHz, {r[4]/1e3:.0f} kHz, SNR {r[5]:.1f} dB — '
                     f'{html.escape(r[9] or "")}</title></rect>')
    parts.append(f'<path d="{path(xx)}" class="mx"/><path d="{path(mm)}" class="mean"/><path d="{path(ff)}" class="fl"/>')
    for g in np.arange(np.ceil(lo / 10) * 10, hi, 10):
        parts.append(f'<text x="5" y="{Y(g)+4:.0f}" class="ax">{g:.0f}</text>')
    step = 250e6 if (f1 - f0) > 2e9 else 50e6 if (f1 - f0) > 300e6 else 10e6
    for t in np.arange(np.ceil(f0 / step) * step, f1, step):
        parts.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="10" y2="{height-30}" class="grid"/>'
                     f'<text x="{X(t):.1f}" y="{height-12}" class="ax" text-anchor="middle">{t/1e6:.0f}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def _link(p):
    if not p:
        return ""
    return f'<a href="{html.escape(p)}" title="{html.escape(p)}">IQ</a>'


def write(run_dir, refresh=None):
    db = DB(os.path.join(run_dir, "rfscan.sqlite"))
    c = db.c
    surveys = c.execute("SELECT id,t,f_lo,f_hi,n_regions,spectrum_path FROM surveys ORDER BY id DESC").fetchall()
    caps = c.execute("SELECT COUNT(*), MIN(t), MAX(t), COALESCE(SUM(saved_bytes),0) FROM captures").fetchone()
    emit = db.emitters()
    sigs = db.signal_summary()
    spec = ""
    if surveys:
        sid, _, _, _, _, sp = surveys[0]
        regs = c.execute("SELECT survey_id,f_center,f_lo,f_hi,bw,snr,peak,occupancy,continuous,band,spur "
                         "FROM regions WHERE survey_id=? AND spur=0", (sid,)).fetchall()
        if sp and os.path.exists(os.path.join(run_dir, sp)):
            spec = _svg_spectrum(os.path.join(run_dir, sp), regs)
    else:
        regs = []

    rows_e = []
    for dec, ident, t0, t1, n, crc, sync, f, label, fields, iq in emit:
        lvl = "crc" if crc else "sync" if sync else "detect"
        rows_e.append(f"<tr class='{lvl}'><td>{dec}</td><td><span class='lvl {lvl}'>{lvl.upper()}</span></td>"
                      f"<td class='num'>{f/1e6:.4f}</td><td>{html.escape(ident)}</td>"
                      f"<td>{html.escape(label)}</td><td class='num'>{n}</td><td>{_ts(t0)}</td><td>{_ts(t1)}</td>"
                      f"<td>{_link(iq)}</td></tr>")
    rows_s = []
    for fb, n, snr, bw, band, label, conf, nb, t0, t1, iq in sigs:
        if snr is None or snr < 10:
            continue
        rows_s.append(f"<tr><td class='num'>{fb/1e6:.3f}</td><td class='num'>{(bw or 0)/1e3:.0f}</td>"
                      f"<td class='num'>{snr:.1f}</td><td>{html.escape(label or '')}</td><td class='num'>{conf:.2f}</td>"
                      f"<td>{html.escape(band or '')}</td><td class='num'>{n}</td><td class='num'>{nb or ''}</td>"
                      f"<td>{_ts(t1)}</td><td>{_link(iq)}</td></tr>")
    # odd ones out: undecoded signals outside the well-understood services
    common = {"cellular", "cellular_ul", "wifi", "broadcast", "rail"}
    svc_cat = {b.service: b.category for b in bandplan.BANDS}
    rows_o = []
    for fb, n, snr, bw, band, label, conf, nb, t0, t1, iq in sorted(sigs, key=lambda r: -(r[2] or 0)):
        cat = svc_cat.get(band or "", "unknown")
        if snr is None or snr < 12 or (conf or 0) >= 0.9 or cat in common or fb < 0.5e6 \
                or (label or "").startswith(("receiver spur", "below receiver")) \
                or ((bw or 0) < 40e3 and abs(fb - round(fb / 40e6) * 40e6) < 60e3):
            continue
        rows_o.append(f"<tr><td class='num'>{fb/1e6:.3f}</td><td class='num'>{(bw or 0)/1e3:.0f}</td>"
                      f"<td class='num'>{snr:.1f}</td><td>{html.escape(label or '')}</td>"
                      f"<td>{html.escape(band or '')}</td><td class='num'>{n}</td><td class='num'>{nb or ''}</td>"
                      f"<td>{_ts(t0)}</td><td>{_ts(t1)}</td><td>{_link(iq)}</td></tr>")
    rows_r = []
    for r in sorted(regs, key=lambda r: -r[5])[:200]:
        rows_r.append(f"<tr><td class='num'>{r[1]/1e6:.3f}</td><td class='num'>{r[4]/1e3:.0f}</td>"
                      f"<td class='num'>{r[5]:.1f}</td><td class='num'>{r[7]:.2f}</td>"
                      f"<td>{'cont' if r[8] else 'intermittent'}</td><td>{html.escape(r[9] or '')}</td></tr>")
    n_crc = sum(1 for e in emit if e[5])
    mon = monitor.html_section(run_dir, len(surveys)) if surveys else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RF Environment Scan</title>{f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""}<style>
:root{{--bg:#fafaf8;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e3e0;--card:#fff;--acc:#2a6fdb}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#151517;--fg:#e8e8ea;--mut:#9a9aa0;--line:#2c2c30;--card:#1d1d20;--acc:#6ea0ff}}}}
:root[data-theme="dark"]{{--bg:#151517;--fg:#e8e8ea;--mut:#9a9aa0;--line:#2c2c30;--card:#1d1d20;--acc:#6ea0ff}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}}
main{{max-width:1500px;margin:auto}} h1{{font-size:22px;margin:4px 0}} h2{{font-size:17px;margin:28px 0 8px}}
.meta{{color:var(--mut)}} .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}}
.card b{{display:block;font-size:22px}} .wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:var(--card);color:var(--mut);font-weight:600}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.lvl{{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid currentColor}} .lvl.crc{{color:#2e9e4f}} .lvl.sync{{color:#c98a00}} .lvl.detect{{color:var(--mut)}}
svg.spec{{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
svg .mean{{fill:none;stroke:var(--acc);stroke-width:1}} svg .mx{{fill:none;stroke:#e4572e;stroke-width:.6;opacity:.6}}
svg .fl{{fill:none;stroke:var(--mut);stroke-width:.8;stroke-dasharray:3 3}} svg .ax{{fill:var(--mut);font-size:11px}} svg .grid{{stroke:var(--line)}}
a{{color:var(--acc)}} .legend span{{margin-right:14px}}
h3{{font-size:15px;margin:20px 0 6px}} svg.heat{{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
svg.spark rect{{fill:var(--acc)}}
</style></head><body><main>
<h1>RF Environment Scan</h1>
<div class="meta">{html.escape(os.path.abspath(run_dir))} &middot; {_ts(caps[1])} → {_ts(caps[2])}{' &middot; <b>live</b>, updated ' + time.strftime('%H:%M:%S') if refresh else ''}</div>
<div class="cards">
<div class="card"><b>{len(rows_o)}</b>odd ones out</div><div class="card"><b>{len(emit)}</b>decoded emitters</div><div class="card"><b>{n_crc}</b>CRC-verified</div>
<div class="card"><b>{len(rows_s)}</b>signal channels</div><div class="card"><b>{caps[0]}</b>dwell captures</div>
<div class="card"><b>{len(surveys)}</b>surveys</div><div class="card"><b>{caps[3]/1e6:.0f} MB</b>IQ saved</div></div>
<h2>Spectrum (latest survey)</h2>
<div class="legend meta"><span style="color:var(--acc)">— mean</span><span style="color:#e4572e">— max-hold</span><span>- - noise floor</span><span>shaded = detected energy regions (hover)</span></div>
{spec or '<p class="meta">no survey</p>'}
{mon}
<h2>Odd ones out</h2><p class="meta">Undecoded signals outside cellular, Wi-Fi/BLE and broadcast bands, strongest first. These have IQ saved for closer analysis.</p>
<div class="wrap"><table><tr><th>MHz</th><th>BW kHz</th><th>SNR</th><th>classification</th><th>band plan</th><th>seen</th><th>bursts</th><th>first</th><th>last</th><th></th></tr>
{''.join(rows_o) or '<tr><td colspan=10 class=meta>none yet</td></tr>'}</table></div>
<h2>Decoded emitters</h2><div class="wrap"><table><tr><th>decoder</th><th>level</th><th>MHz</th><th>identity</th><th>details</th><th>msgs</th><th>first</th><th>last</th><th></th></tr>
{''.join(rows_e) or '<tr><td colspan=9 class=meta>none yet</td></tr>'}</table></div>
<h2>Classified signals</h2><div class="wrap"><table><tr><th>MHz</th><th>BW kHz</th><th>SNR</th><th>classification</th><th>conf</th><th>band plan</th><th>seen</th><th>bursts</th><th>last</th><th></th></tr>
{''.join(rows_s)}</table></div>
<h2>Survey energy regions (top 200 by SNR)</h2><div class="wrap"><table><tr><th>MHz</th><th>BW kHz</th><th>SNR</th><th>occupancy</th><th>type</th><th>band plan</th></tr>
{''.join(rows_r)}</table></div>
</main></body></html>"""
    path = os.path.join(run_dir, "report.html")
    with open(path, "w") as fh:
        fh.write(doc)
    return path
