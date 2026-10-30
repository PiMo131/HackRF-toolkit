#!/usr/bin/env python3
"""Voortgangsrapport passieve RF-lokalisatie als PDF, in de huisstijl van Profile Equipment.

    python3 tools/release_report.py [--version v0.0.3] [--out dist] [--html-only]

Leest docs/localisation_status.json (meetgegevens, bronnen, doel), vult versie/commit/datum uit git aan,
schrijft dist/<naam>.html en zet die met headless Chrome om naar dist/<naam>.pdf. Draait in de release-workflow
(.github/workflows/release.yml) bij elke tag v*; de PDF wordt aan de GitHub-release gehangen.
Huisstijl (van profile-equipment.com): Inter voor tekst, Space Grotesk voor koppen, inkt #17150E, goud #C2A202,
donkergoud #8A7302, grijsgroen #7C7558, zand #E7E2D1, papier #FAF9F5. Een logo wordt meegenomen als
docs/report/logo.svg (of .png) bestaat; anders een tekstmerk.
"""
import argparse
import base64
import datetime as dt
import html
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATUS = ROOT / "docs" / "localisation_status.json"
INK, GOLD, GOLD_D, MUTED, OLIVE, SAND, PAPER, CREAM = "#17150E", "#C2A202", "#8A7302", "#7C7558", "#45412A", "#E7E2D1", "#FAF9F5", "#F5F1E6"
STATUS_NL = {"built": ("Gebouwd", GOLD_D), "partial": ("Deels", MUTED), "planned": ("Gepland", OLIVE), "idea": ("Idee", OLIVE), "done": ("Gereed", GOLD_D)}


def git(*args, default=""):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return default


def esc(s):
    return html.escape("" if s is None else str(s))


def km(m):
    if m is None:
        return "–"
    return f"{m/1000:.2f} km" if m < 10000 else f"{m/1000:.0f} km"


def logo_tag():
    for name in ("logo.svg", "logo.png"):
        p = ROOT / "docs" / "report" / name
        if p.exists():
            mime = "image/svg+xml" if name.endswith("svg") else "image/png"
            return f'<img class="logo" alt="Profile Equipment" src="data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}">'
    return '<div class="wordmark">Profile <span>Equipment</span></div>'


def bar_chart(methods, target_m):
    """Horizontale staafjes op logschaal: gemeten fout per methode, doel als gouden lijn."""
    rows = [m for m in methods if m.get("best_error_m")]
    rows.sort(key=lambda m: m["best_error_m"])
    if not rows:
        return ""
    import math
    lo, hi = math.log10(50), math.log10(400e3)
    w, rh, left = 520, 22, 190
    def x(v): return left + (math.log10(v) - lo) / (hi - lo) * (w - left - 20)
    h = 30 + rh * len(rows) + 28
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="Inter, system-ui, sans-serif">']
    for tick in (100, 1000, 10000, 100000):
        out.append(f'<line x1="{x(tick):.1f}" y1="20" x2="{x(tick):.1f}" y2="{h-26}" stroke="{SAND}" stroke-width="1"/>'
                   f'<text x="{x(tick):.1f}" y="{h-10}" font-size="10" fill="{MUTED}" text-anchor="middle">{km(tick)}</text>')
    tx = x(target_m)
    out.append(f'<line x1="{tx:.1f}" y1="14" x2="{tx:.1f}" y2="{h-26}" stroke="{GOLD}" stroke-width="2" stroke-dasharray="4 3"/>'
               f'<text x="{tx+4:.1f}" y="12" font-size="10" fill="{GOLD_D}" font-weight="600">doel {km(target_m)}</text>')
    for i, m in enumerate(rows):
        y = 26 + i * rh
        bw = x(m["best_error_m"]) - left
        out.append(f'<text x="{left-8}" y="{y+13}" font-size="11" fill="{INK}" text-anchor="end">{esc(m["name"])}</text>'
                   f'<rect x="{left}" y="{y+3}" width="{bw:.1f}" height="{rh-7}" fill="{INK if i else GOLD}" rx="2"/>'
                   f'<text x="{left+bw+6:.1f}" y="{y+13}" font-size="11" fill="{OLIVE}">{km(m["best_error_m"])}</text>')
    out.append("</svg>")
    return "".join(out)


def render(d, version, commit, date):
    p, t, truth = d["project"], d["target"], d["truth"]
    measured = [m for m in d["methods"] if m.get("best_error_m")]
    best = min(measured, key=lambda m: m["best_error_m"]) if measured else None
    n_built = sum(1 for m in d["methods"] if m["status"] == "built")
    tiles = [
        (km(best["best_error_m"]) if best else "–", f"beste gemeten fout ({esc(best['name'])}, {esc(best['date'])})" if best else "nog geen meting"),
        (km(t["acc_m"]), "doel met huidige hardware (TDOA op DAB en 5G)"),
        (str(len(d["rf_sources"])), "RF-bronnen in gebruik of gepland"),
        (f"{n_built}/{len(d['methods'])}", "methoden gebouwd / in totaal"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="num">{esc(a)}</div><div class="lbl">{b}</div></div>' for a, b in tiles)
    miles = "".join(f'<li><span class="pill" style="background:{STATUS_NL[m["status"]][1]}">{STATUS_NL[m["status"]][0]}</span>'
                    f'<span>{esc(m["label"])}</span><span class="acc">{km(m["acc_m"])}</span></li>' for m in t["milestones"])
    meth_rows = "".join(
        f'<tr><td><b>{esc(m["name"])}</b><br><span class="sub">{esc(m["source"])}</span></td><td>{esc(m["how"])}</td>'
        f'<td class="c"><span class="pill" style="background:{STATUS_NL[m["status"]][1]}">{STATUS_NL[m["status"]][0]}</span></td>'
        f'<td class="c">{esc(m["estimate"] or "–")}</td><td class="c"><b>{km(m["best_error_m"])}</b></td><td class="c">{"±" + km(m["stated_acc_m"]) if m["stated_acc_m"] else "–"}</td>'
        f'<td class="sub">{esc(m["note"])}</td></tr>' for m in d["methods"])
    src_rows = "".join(f'<tr><td><b>{esc(s["name"])}</b></td><td>{esc(s["freq"])}</td><td>{esc(s["yields"])}</td><td>{esc(s["use"])}</td></tr>' for s in d["rf_sources"])
    css = f"""
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@700&display=swap');
    @page {{ size: A4; margin: 16mm 16mm 18mm 16mm; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font: 10.5pt/1.5 Inter, system-ui, sans-serif; color: {INK}; background: #fff; }}
    h1, h2, h3, .num, .wordmark {{ font-family: 'Space Grotesk', Inter, system-ui, sans-serif; font-weight: 700; letter-spacing: -0.02em; margin: 0; }}
    h1 {{ font-size: 30pt; line-height: 1.1; margin: 6mm 0 3mm; }}
    h2 {{ font-size: 16pt; margin: 9mm 0 3mm; padding-top: 3mm; border-top: 1px solid {SAND}; break-after: avoid; }}
    .eyebrow {{ font-size: 8.5pt; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; color: {GOLD_D}; }}
    header {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 4mm; border-bottom: 2px solid {INK}; }}
    .logo {{ height: 11mm; }} .wordmark {{ font-size: 15pt; }} .wordmark span {{ color: {GOLD}; }}
    .meta {{ text-align: right; font-size: 8.5pt; color: {MUTED}; line-height: 1.4; }}
    .lead {{ font-size: 11.5pt; color: {OLIVE}; max-width: 160mm; }}
    .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4mm; margin: 6mm 0; }}
    .tile {{ background: {PAPER}; border: 1px solid {SAND}; border-radius: 3mm; padding: 4mm; }}
    .tile .num {{ font-size: 20pt; color: {GOLD}; line-height: 1.1; }} .tile .lbl {{ font-size: 8.5pt; color: {MUTED}; margin-top: 1.5mm; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 8.8pt; margin-top: 2mm; }}
    th {{ text-align: left; font-size: 8pt; letter-spacing: 0.06em; text-transform: uppercase; color: {MUTED}; border-bottom: 1px solid {INK}; padding: 1.5mm 2mm; }}
    td {{ padding: 2mm; border-bottom: 1px solid {SAND}; vertical-align: top; }} td.c {{ white-space: nowrap; }}
    .sub {{ color: {MUTED}; font-size: 8pt; }} tr {{ break-inside: avoid; }}
    .pill {{ display: inline-block; color: #fff; font-size: 7.5pt; font-weight: 600; padding: 0.3mm 2mm; border-radius: 99px; }}
    ul.miles {{ list-style: none; padding: 0; margin: 2mm 0; }} ul.miles li {{ display: grid; grid-template-columns: 18mm 1fr 20mm; gap: 3mm; align-items: center; padding: 1.6mm 0; border-bottom: 1px solid {SAND}; }}
    ul.miles .acc {{ text-align: right; font-weight: 600; color: {OLIVE}; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8mm; break-inside: avoid; }}
    .chart {{ width: 100%; height: auto; margin-top: 2mm; }}
    .box {{ background: {INK}; color: #fff; border-radius: 3mm; padding: 5mm 6mm; margin-top: 4mm; }}
    .box .eyebrow {{ color: {GOLD}; }} .box h3 {{ font-size: 12.5pt; margin: 1mm 0 2mm; }} .box p {{ margin: 0; color: rgba(255,255,255,.8); font-size: 8.8pt; line-height: 1.45; }}
    .lead {{ font-size: 11pt; }} .tiles {{ margin: 5mm 0; }}
    table.methods col.m {{ width: 15%; }} table.methods col.h {{ width: 30%; }} table.methods col.s {{ width: 8%; }} table.methods col.e {{ width: 13%; }} table.methods col.f {{ width: 7%; }} table.methods col.a {{ width: 8%; }}
    section.sources {{ break-before: page; }} section.sources h2 {{ border-top: 0; margin-top: 0; }}
    a {{ color: {GOLD_D}; text-decoration: none; }}
    footer {{ margin-top: 10mm; padding-top: 3mm; border-top: 1px solid {SAND}; font-size: 8pt; color: {MUTED}; display: flex; justify-content: space-between; }}
    """
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><title>Passieve RF-lokalisatie {esc(version)}</title><style>{css}</style></head><body>
<header>{logo_tag()}<div class="meta">Voortgangsrapport · {esc(version)}<br>{esc(date)} · commit {esc(commit)}<br>{esc(p["owner"])}</div></header>
<div class="eyebrow" style="margin-top:7mm">{esc(p["name"])} · {esc(p["theme"])}</div>
<h1>Waar ben ik, zonder GPS?</h1>
<p class="lead">{esc(p["summary"])}</p>
<div class="tiles">{tiles_html}</div>

<h2>Wat we doen en waar het staat</h2>
<div class="two"><div>
<p>Alle code, documentatie en meetnotities staan openbaar op GitHub; de browserversie draait zonder installatie op GitHub Pages en praat via WebUSB met de HackRF.</p>
<table><tr><th>Wat</th><th>Waar</th></tr>
<tr><td>Repository</td><td><a href="{esc(p["repo"])}">{esc(p["repo"])}</a></td></tr>
<tr><td>Browser-app</td><td><a href="{esc(p["pages"])}">{esc(p["pages"])}</a></td></tr>
<tr><td>Documentatie en notities</td><td><a href="{esc(p["docs"])}">{esc(p["docs"])}</a></td></tr>
<tr><td>Hardware</td><td>{esc(p["hardware"])}</td></tr>
<tr><td>Referentiepositie</td><td>{esc(truth["place"])} ({truth["lat"]:.2f} N, {truth["lon"]:.2f} E)</td></tr></table>
</div><div class="box"><div class="eyebrow">Doel</div><h3>{esc(t["headline"])}</h3><p>{esc(t["description"])}</p></div></div>

<h2>Mijlpalen naar het best haalbare resultaat</h2>
<ul class="miles">{miles}</ul>

<h2>Huidige status: beste gehaalde positienauwkeurigheid</h2>
<p>Gemeten fout van elke methode ten opzichte van de referentiepositie (logschaal). De opgegeven onzekerheid (±) van de methoden is bewust conservatief en meestal veel groter dan de werkelijke fout.</p>
{bar_chart(d["methods"], t["acc_m"])}
<table class="methods"><colgroup><col class="m"><col class="h"><col class="s"><col class="e"><col class="f"><col class="a"><col></colgroup><tr><th>Methode</th><th>Werkwijze</th><th>Status</th><th>Schatting</th><th>Fout</th><th>Opgegeven ±</th><th>Opmerking</th></tr>{meth_rows}</table>

<section class="sources"><h2>RF-bronnen die we gebruiken</h2>
<table><tr><th>Bron</th><th>Frequentie</th><th>Wat het oplevert</th><th>Gebruik voor positie</th></tr>{src_rows}</table></section>

<footer><span>{esc(p["owner"])} · intern werkdocument · gegenereerd door tools/release_report.py uit docs/localisation_status.json</span><span>{esc(version)} · {esc(date)}</span></footer>
</body></html>"""


def to_pdf(html_path, pdf_path):
    chrome = next((c for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome") if shutil.which(c)), None)
    if not chrome:
        sys.exit("geen Chrome/Chromium gevonden voor de PDF-conversie (gebruik --html-only)")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--run-all-compositor-stages-before-draw",
           "--virtual-time-budget=8000", "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", default=None, help="versielabel (standaard: git describe --tags)")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--html-only", action="store_true")
    a = ap.parse_args()
    d = json.loads(STATUS.read_text(encoding="utf-8"))
    version = a.version or git("describe", "--tags", "--always", default="dev")
    commit = git("rev-parse", "--short", "HEAD", default="?")
    date = dt.date.today().isoformat()
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    base = f"rfscan-lokalisatie-rapport-{version}"              # geen with_suffix: "v0.0.3" heeft zelf een punt
    html_path = out / (base + ".html"); html_path.write_text(render(d, version, commit, date), encoding="utf-8")
    print(html_path)
    if not a.html_only:
        pdf_path = out / (base + ".pdf"); to_pdf(html_path, pdf_path); print(pdf_path)


if __name__ == "__main__":
    main()
