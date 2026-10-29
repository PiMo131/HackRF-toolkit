#!/usr/bin/env python3
"""Assemble the GitHub Pages site: the browser app (web/) plus the project docs rendered to HTML.

    python3 web/build_site.py _site        # needs: pip install markdown

Output: _site/ = copy of web/ + docs/index.html (README) + docs/<name>.html for every docs/*.md +
docs/screenshots/. Links between .md files are rewritten to .html; image paths that point at
docs/screenshots/ keep working because the docs live under the same prefix.
"""
import pathlib
import re
import shutil
import sys

import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
STYLE = """<style>
:root{color-scheme:dark}body{margin:0;background:#0b0e13;color:#d7dce5;font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px}a{color:#6fb3ff}h1,h2,h3{color:#fff;line-height:1.25}
code,pre{font-family:ui-monospace,monospace;font-size:13px}pre{background:#11151c;border:1px solid #2a303b;border-radius:6px;padding:12px;overflow:auto}
code{background:#11151c;padding:1px 4px;border-radius:3px}table{border-collapse:collapse;margin:12px 0;font-size:14px}
th,td{border:1px solid #2a303b;padding:6px 8px;vertical-align:top;text-align:left}th{background:#11151c}img{max-width:100%;border-radius:6px;border:1px solid #2a303b}
nav{font-size:13px;color:#8a93a3;margin-bottom:16px}nav a{margin-right:12px}blockquote{border-left:3px solid #2a303b;margin:0;padding:0 12px;color:#8a93a3}
</style>"""


def render(md_path, title, nav, depth):
    text = md_path.read_text(encoding="utf-8")
    text = re.sub(r"\]\(((?:[\w./-]+/)?[\w.-]+)\.md(#[\w-]*)?\)", r"](\1.html\2)", text)       # foo.md -> foo.html
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc"])
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title>{STYLE}</head><body><main><nav>{nav}</nav>{body}</main></body></html>")


def main(out):
    out = pathlib.Path(out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "web", out, ignore=shutil.ignore_patterns("*.cs8", "*.raw", "*.sigmf-data", "__pycache__", "build_site.py"))
    docs_out = out / "docs"
    docs_out.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "docs" / "screenshots", docs_out / "screenshots", dirs_exist_ok=True)
    pages = sorted((ROOT / "docs").glob("*.md"))
    nav = "<a href=\"../\">app</a><a href=\"index.html\">README</a>" + "".join(f"<a href=\"{p.stem}.html\">{p.stem}</a>" for p in pages)
    readme = (ROOT / "README.md").read_text(encoding="utf-8").replace("](docs/", "](")          # README lives one level up
    (docs_out / "README.tmp.md").write_text(readme, encoding="utf-8")
    (docs_out / "index.html").write_text(render(docs_out / "README.tmp.md", "HackRF toolkit", nav, 1), encoding="utf-8")
    (docs_out / "README.tmp.md").unlink()
    for p in pages:
        (docs_out / f"{p.stem}.html").write_text(render(p, p.stem, nav, 1), encoding="utf-8")
    print(f"{out}: app + {len(pages) + 1} doc pages")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_site")
