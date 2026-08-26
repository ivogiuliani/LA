#!/usr/bin/env python3
"""Interlink pillar → journal (SEO, 2026-08-26).

In ogni pillar commerciale inserisce/aggiorna una sezione "From the
Journal" con i 4 articoli recenti più pertinenti. Al primo run inserisce
la sezione (con i marker) subito prima del footer; ai run successivi
riscrive solo il contenuto tra i marker. Esclude gli articoli in noindex
(potatura): i link interni devono puntare alle pagine indicizzabili.

Uso: python3 update_pillar_journal.py
"""
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
FOOTER_ANCHOR = '<footer class="footer">'

PILLARS = [
    {
        "file": "malibu-custom-home-builder.html",
        "key": "MALIBU",
        "match": lambda s, sec: "malibu" in s,
    },
    {
        "file": "italian-villa-california-builder.html",
        "key": "ITALIAN",
        "match": lambda s, sec: any(t in s for t in (
            "italian", "mediterranean", "tuscan", "palazzo", "villa", "como")),
    },
    {
        "file": "beverly-hills-custom-home.html",
        "key": "BEVERLY",
        "match": lambda s, sec: any(t in s for t in (
            "beverly-hills", "bel-air", "hidden-hills", "holmby", "aman",
            "spec-mansion")) or sec == "market",
    },
    {
        "file": "icf-concrete-home-builder-los-angeles.html",
        "key": "ICF",
        "match": lambda s, sec: any(t in s for t in (
            "concrete", "icf", "fire-resistant", "rebuild", "construction",
            "structure")) or sec in ("materials", "concrete_arch",
                                     "concrete_architecture", "fire_code"),
    },
]


def load_articles():
    """Articoli indicizzabili, più recenti prima: (slug, title, date, sec)."""
    out = []
    for j in sorted(BLOG_DIR.glob("*.json")):
        if j.stem == "index" or j.stem.startswith("category"):
            continue
        html = j.with_suffix(".html")
        if not html.exists():
            continue
        try:
            content = html.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r'name="robots"\s+content="noindex', content):
            continue  # potatura: mai linkare le pagine deindicizzate
        try:
            d = json.load(open(j))
        except Exception:
            continue
        out.append({
            "slug": j.stem,
            "title": d.get("title") or j.stem.replace("-", " ").title(),
            "date": d.get("_date") or "",
            "sec": d.get("_section_id") or "",
        })
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


def esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def section_html(key, items):
    cards = "\n".join(
        '      <a href="https://myvilla.la/blog/{slug}.html" '
        'style="display:block;padding:18px 0;border-top:1px solid '
        'var(--line, rgba(44,44,44,0.12));text-decoration:none">'
        '<span style="display:block;font-family:var(--sans, Montserrat, '
        "sans-serif);font-size:10px;letter-spacing:0.14em;"
        'text-transform:uppercase;color:var(--terracotta, #C2714F);'
        'margin-bottom:6px">{date}</span>'
        '<span style="display:block;font-family:var(--serif, Georgia, '
        "serif);font-size:21px;line-height:1.25;"
        'color:var(--charcoal, #2C2C2C)">{title}</span></a>'.format(
            slug=it["slug"], date=esc(it["date"]), title=esc(it["title"]))
        for it in items)
    return (
        f"<!-- JOURNAL:{key}:START -->\n"
        '<section style="background:var(--cream, #FAF8F5);'
        'padding:64px 24px">\n'
        '  <div style="max-width:840px;margin:0 auto">\n'
        '    <div style="font-family:var(--sans, Montserrat, sans-serif);'
        "font-size:11px;letter-spacing:0.22em;text-transform:uppercase;"
        'color:var(--terracotta, #C2714F);margin-bottom:6px">'
        "From the Journal</div>\n"
        '    <div style="font-family:var(--serif, Georgia, serif);'
        'font-size:30px;color:var(--charcoal, #2C2C2C);margin-bottom:18px">'
        "Recent notes on this subject</div>\n"
        f"{cards}\n"
        '    <a href="https://myvilla.la/blog/" '
        'style="display:inline-block;margin-top:22px;font-family:var(--sans, '
        "Montserrat, sans-serif);font-size:12px;letter-spacing:0.12em;"
        'text-transform:uppercase;color:var(--espresso, #3E2F2B)">'
        "All journal entries &rarr;</a>\n"
        "  </div>\n"
        "</section>\n"
        f"<!-- JOURNAL:{key}:END -->"
    )


def main():
    articles = load_articles()
    for p in PILLARS:
        f = ROOT_DIR / p["file"]
        if not f.exists():
            print(f"[pillar-journal] WARN: {p['file']} non trovato")
            continue
        picks = [a for a in articles if p["match"](a["slug"], a["sec"])][:4]
        if not picks:
            print(f"[pillar-journal] {p['file']}: nessun articolo pertinente")
            continue
        html = f.read_text(encoding="utf-8")
        block = section_html(p["key"], picks)
        start = f"<!-- JOURNAL:{p['key']}:START -->"
        end = f"<!-- JOURNAL:{p['key']}:END -->"
        if start in html:
            new = re.sub(re.escape(start) + r".*?" + re.escape(end),
                         block, html, count=1, flags=re.S)
            action = "aggiornata"
        elif FOOTER_ANCHOR in html:
            new = html.replace(FOOTER_ANCHOR,
                               block + "\n\n" + FOOTER_ANCHOR, 1)
            action = "inserita"
        else:
            print(f"[pillar-journal] WARN: nessun anchor in {p['file']}")
            continue
        if new != html:
            f.write_text(new, encoding="utf-8")
        print(f"[pillar-journal] {p['file']}: sezione {action} "
              f"({len(picks)} articoli)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
