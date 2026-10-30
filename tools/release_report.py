#!/usr/bin/env python3
"""Voortgangsrapport passieve RF-lokalisatie als PDF, volgens het Huisstijlhandboek Profile Equipment (v1.0, sept 2026).

    python3 tools/release_report.py [--version v0.0.3] [--out dist] [--html-only]

Leest docs/localisation_status.json (meetgegevens, bronnen, doel), vult versie/commit/datum uit git aan,
schrijft dist/<naam>.html en zet die met headless Chrome om naar dist/<naam>.pdf. Draait in de release-workflow
(.github/workflows/release.yml) bij elke tag v*; de PDF wordt aan de GitHub-release gehangen.

Huisstijlregels die hier zijn toegepast (handboek hoofdstuk 6, 7 en 8):
  kleuren   Profile goud #C2A202 (accenten, kop 2), Profile donker #474220 (kop 1, tabelkoppen), bijna zwart #262626
            (lopende tekst), grijs alleen in de voettekst; geen donkere vlakken, schaduwen of effecten
  letter    Aptos, anders Arial (op Linux/CI: Liberation Sans, metrisch gelijk aan Arial); één lettertype per document
  koppen    kop 1 vet 16 pt in Profile donker, kop 2 vet 13 pt in Profile goud, lopende tekst 10,5 pt, regelafstand 1,15
  pagina    logo in kleur rechtsboven, max 4,5 cm breed, vrije ruimte een kwart van de logohoogte; voettekst documentnaam
            links en 'Pagina X van Y' rechts in 8 pt grijs; tabellen met kopregel in merkkleur en witte tekst, rijen
            afwisselend wit en lichtgrijs, dunne grijze randen
Logo: docs/report/logo.svg = 'Logo/Profile Equipment - kleur.svg' uit de huisstijlmap (niet aanpassen, niet hertekenen).
"""
import argparse
import base64
import datetime as dt
import html
import json
import math
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATUS = ROOT / "docs" / "localisation_status.json"
LOGO = ROOT / "docs" / "report" / "logo.svg"
GOLD, DARK, TEXT, GREY, LIGHT, LINE = "#C2A202", "#474220", "#262626", "#808080", "#F2F2F2", "#D9D9D9"
STATUS_NL = {"built": "Gebouwd", "partial": "Deels", "planned": "Gepland", "idea": "Idee", "done": "Gereed"}


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


def logo_data_uri():
    if not LOGO.exists():
        sys.exit(f"logo ontbreekt: {LOGO} (kopieer 'Logo/Profile Equipment - kleur.svg' uit de huisstijlmap)")
    return "data:image/svg+xml;base64," + base64.b64encode(LOGO.read_bytes()).decode()


def bar_chart(methods, target_m):
    """Horizontale staven op logschaal: gemeten fout per methode (Profile donker, beste in goud), doel als gouden lijn."""
    rows = sorted((m for m in methods if m.get("best_error_m")), key=lambda m: m["best_error_m"])
    if not rows:
        return ""
    lo, hi = math.log10(50), math.log10(400e3)
    w, rh, left = 560, 22, 180
    def x(v): return left + (math.log10(v) - lo) / (hi - lo) * (w - left - 60)
    h = 30 + rh * len(rows) + 26
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="Aptos, Arial, Liberation Sans, sans-serif">']
    for tick in (100, 1000, 10000, 100000):
        out.append(f'<line x1="{x(tick):.1f}" y1="20" x2="{x(tick):.1f}" y2="{h-24}" stroke="{LINE}" stroke-width="1"/>'
                   f'<text x="{x(tick):.1f}" y="{h-8}" font-size="10" fill="{GREY}" text-anchor="middle">{km(tick)}</text>')
    tx = x(target_m)
    out.append(f'<line x1="{tx:.1f}" y1="14" x2="{tx:.1f}" y2="{h-24}" stroke="{GOLD}" stroke-width="2" stroke-dasharray="4 3"/>'
               f'<text x="{tx+4:.1f}" y="12" font-size="10" fill="{GOLD}" font-weight="bold">doel {km(target_m)}</text>')
    for i, m in enumerate(rows):
        y = 26 + i * rh
        bw = x(m["best_error_m"]) - left
        out.append(f'<text x="{left-8}" y="{y+13}" font-size="11" fill="{TEXT}" text-anchor="end">{esc(m["name"])}</text>'
                   f'<rect x="{left}" y="{y+3}" width="{bw:.1f}" height="{rh-7}" fill="{GOLD if i == 0 else DARK}"/>'
                   f'<text x="{left+bw+6:.1f}" y="{y+13}" font-size="11" fill="{TEXT}">{km(m["best_error_m"])}</text>')
    out.append("</svg>")
    return "".join(out)


def render(d, version, commit, date):
    p, t, truth = d["project"], d["target"], d["truth"]
    measured = [m for m in d["methods"] if m.get("best_error_m")]
    best = min(measured, key=lambda m: m["best_error_m"]) if measured else None
    n_built = sum(1 for m in d["methods"] if m["status"] == "built")
    docname = f"Voortgangsrapport passieve RF-lokalisatie {version}"
    tiles = [
        (km(best["best_error_m"]) if best else "–", f"beste gemeten fout ({esc(best['name'])}, {esc(best['date'])})" if best else "nog geen meting"),
        (km(t["acc_m"]), "doel met de huidige hardware"),
        (str(len(d["rf_sources"])), "RF-bronnen in gebruik of gepland"),
        (f"{n_built} van {len(d['methods'])}", "methoden gebouwd"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="num">{esc(a)}</div><div class="lbl">{b}</div></div>' for a, b in tiles)
    miles = "".join(f'<tr><td class="c">{STATUS_NL[m["status"]]}</td><td>{esc(m["label"])}</td><td class="r">{km(m["acc_m"])}</td></tr>' for m in t["milestones"])
    meth_rows = "".join(
        f'<tr><td><b>{esc(m["name"])}</b><br><span class="sub">{esc(m["source"])}</span></td><td>{esc(m["how"])}</td>'
        f'<td class="c">{STATUS_NL[m["status"]]}</td><td class="c">{esc(m["estimate"] or "–")}</td><td class="c"><b>{km(m["best_error_m"])}</b></td>'
        f'<td class="c">{"±" + km(m["stated_acc_m"]) if m["stated_acc_m"] else "–"}</td><td>{esc(m["note"])}</td></tr>' for m in d["methods"])
    src_rows = "".join(f'<tr><td><b>{esc(s["name"])}</b></td><td>{esc(s["freq"])}</td><td>{esc(s["yields"])}</td><td>{esc(s["use"])}</td></tr>' for s in d["rf_sources"])
    css = f"""
    @page {{ size: A4; margin: 15mm 20mm 20mm 20mm;
      @bottom-left {{ content: "{docname}"; font: 8pt Aptos, Arial, "Liberation Sans", sans-serif; color: {GREY}; }}
      @bottom-right {{ content: "Pagina " counter(page) " van " counter(pages); font: 8pt Aptos, Arial, "Liberation Sans", sans-serif; color: {GREY}; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font: 10.5pt/1.15 Aptos, Arial, "Liberation Sans", Helvetica, sans-serif; color: {TEXT}; background: #fff; }}
    table.page {{ border-collapse: collapse; margin: 0; }} table.page > thead > tr > td, table.page > tbody > tr > td {{ padding: 0; border: 0; background: none; }}
    table.page > thead {{ display: table-header-group; }}          /* de thead herhaalt op elke afgedrukte pagina: zo staat het logo overal rechtsboven */
    .head {{ text-align: right; padding-bottom: 5mm; border-bottom: 1px solid {LINE}; }} .logo {{ width: 42mm; height: auto; }}   /* < 4,5 cm; vrije ruimte = kwart logohoogte */
    h1 {{ font-size: 16pt; font-weight: bold; color: {DARK}; margin: 7mm 0 2mm; break-after: avoid; }}
    h2 {{ font-size: 13pt; font-weight: bold; color: {GOLD}; margin: 5mm 0 1.5mm; break-after: avoid; }}
    p {{ margin: 0 0 2.5mm; }}
    .title {{ font-size: 26pt; font-weight: bold; color: {DARK}; line-height: 1.1; margin: 8mm 0 1mm; }}
    .subtitle {{ font-size: 16pt; color: {GOLD}; margin: 0 0 3mm; }}
    .descr {{ font-size: 11pt; color: {TEXT}; }} .meta {{ font-size: 9pt; color: {GREY}; margin-bottom: 6mm; }}
    .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4mm; margin: 5mm 0 2mm; }}
    .tile {{ background: {LIGHT}; padding: 3.5mm 4mm; border-left: 3px solid {GOLD}; }}
    .tile .num {{ font-size: 18pt; font-weight: bold; color: {DARK}; line-height: 1.1; }} .tile .lbl {{ font-size: 8.5pt; color: {TEXT}; margin-top: 1mm; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 9pt; margin: 1.5mm 0 3mm; }}
    th {{ background: {DARK}; color: #fff; text-align: left; font-weight: bold; padding: 1.6mm 2mm; border: 1px solid {DARK}; }}
    td {{ padding: 1.6mm 2mm; border: 1px solid {LINE}; vertical-align: top; }} tr:nth-child(odd) td {{ background: {LIGHT}; }}
    td.c {{ white-space: nowrap; }} td.r {{ text-align: right; white-space: nowrap; }} .sub {{ color: {GREY}; font-size: 8pt; }}
    tr {{ break-inside: avoid; }} thead {{ display: table-header-group; }}
    table.methods col.m {{ width: 15%; }} table.methods col.h {{ width: 29%; }} table.methods col.s {{ width: 8%; }} table.methods col.e {{ width: 13%; }} table.methods col.f {{ width: 7%; }} table.methods col.a {{ width: 8%; }}
    table.links td:first-child {{ width: 32%; }} table.miles td:first-child {{ width: 16%; }}
    .chart {{ width: 100%; height: auto; margin: 1mm 0 2mm; }}
    .panel {{ background: {LIGHT}; border-left: 3px solid {GOLD}; padding: 3.5mm 4mm; margin: 2mm 0 4mm; break-inside: avoid; }} .panel h2 {{ margin-top: 0; }} .panel p {{ margin: 0; }}
    a {{ color: {DARK}; text-decoration: none; }}
    .pb {{ break-before: page; }}
    """
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><title>{esc(docname)}</title><style>{css}</style></head><body>
<table class="page"><thead><tr><td><div class="head"><img class="logo" alt="Profile Equipment" src="{logo_data_uri()}"></div></td></tr></thead><tbody><tr><td>
<div class="title">Waar ben ik, zonder GPS?</div>
<div class="subtitle">Passieve RF-lokalisatie: voortgangsrapport</div>
<p class="descr">{esc(p["name"])} · {esc(p["theme"])}</p>
<p class="meta">Versie {esc(version)} · {esc(date)} · commit {esc(commit)} · {esc(p["owner"])}</p>
<p>{esc(p["summary"])}</p>
<div class="tiles">{tiles_html}</div>

<h1>1. Wat we doen en waar het staat</h1>
<p>Alle code, documentatie en meetnotities staan openbaar op GitHub. De browserversie draait zonder installatie op GitHub Pages en praat via WebUSB met de HackRF.</p>
<table class="links"><thead><tr><th>Wat</th><th>Waar</th></tr></thead>
<tr><td>Repository</td><td><a href="{esc(p["repo"])}">{esc(p["repo"])}</a></td></tr>
<tr><td>Browser-app</td><td><a href="{esc(p["pages"])}">{esc(p["pages"])}</a></td></tr>
<tr><td>Documentatie en notities</td><td><a href="{esc(p["docs"])}">{esc(p["docs"])}</a></td></tr>
<tr><td>Hardware</td><td>{esc(p["hardware"])}</td></tr>
<tr><td>Referentiepositie</td><td>{esc(truth["place"])} ({truth["lat"]:.2f} N, {truth["lon"]:.2f} E)</td></tr></table>

<h1>2. Het best haalbare resultaat waar we naartoe werken</h1>
<div class="panel"><h2>{esc(t["headline"])}</h2><p>{esc(t["description"])}</p></div>
<h2>Mijlpalen</h2>
<table class="miles"><thead><tr><th>Status</th><th>Mijlpaal</th><th>Nauwkeurigheid</th></tr></thead>{miles}</table>

<h1>3. Huidige status: beste gehaalde positienauwkeurigheid</h1>
<p>Gemeten fout van elke methode ten opzichte van de referentiepositie, op logschaal. De door de methoden zelf opgegeven onzekerheid (±) is bewust conservatief en meestal veel groter dan de werkelijke fout.</p>
{bar_chart(d["methods"], t["acc_m"])}
<table class="methods"><colgroup><col class="m"><col class="h"><col class="s"><col class="e"><col class="f"><col class="a"><col></colgroup>
<thead><tr><th>Methode</th><th>Werkwijze</th><th>Status</th><th>Schatting</th><th>Fout</th><th>±</th><th>Opmerking</th></tr></thead>{meth_rows}</table>

<h1>4. RF-bronnen die we gebruiken</h1>
<table><thead><tr><th>Bron</th><th>Frequentie</th><th>Wat het oplevert</th><th>Gebruik voor positie</th></tr></thead>{src_rows}</table>
<p class="meta">Gegenereerd door tools/release_report.py uit docs/localisation_status.json. Intern werkdocument van Profile Equipment B.V.</p>
</td></tr></tbody></table>
</body></html>"""


def to_pdf(html_path, pdf_path):
    chrome = next((c for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome") if shutil.which(c)), None)
    if not chrome:
        sys.exit("geen Chrome/Chromium gevonden voor de PDF-conversie (gebruik --html-only)")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--run-all-compositor-stages-before-draw",
           "--virtual-time-budget=5000", "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
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
