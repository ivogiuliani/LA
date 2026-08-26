#!/usr/bin/env python3
"""Interlink journal → pillar (SEO, 2026-08-26).

In ogni articolo del journal inserisce (o aggiorna) un blocco "From the
Studio" che punta alla pillar commerciale più pertinente. Idempotente:
il blocco vive tra i marker PILLAR-XLINK e viene riscritto a ogni run —
lo script gira nella pipeline di publish, quindi anche i nuovi articoli
lo ricevono automaticamente.

Uso: python3 crosslink_pillars.py            (tutti gli articoli)
"""
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
BASE = "https://myvilla.la"

PILLARS = {
    "malibu": {
        "url": f"{BASE}/malibu-custom-home-builder.html",
        "title": "Luxury Home Builder in Malibu",
        "blurb": "fire-resilient Italian villas on the coast — our approach "
                 "and process.",
    },
    "italian": {
        "url": f"{BASE}/italian-villa-california-builder.html",
        "title": "Italian Villa Builder in California",
        "blurb": "Mediterranean villas engineered to European standards.",
    },
    "beverly": {
        "url": f"{BASE}/beverly-hills-custom-home.html",
        "title": "Custom Home Builder in Beverly Hills &amp; Bel Air",
        "blurb": "reinforced-concrete luxury homes for LA&#x27;s estate "
                 "districts.",
    },
    "icf": {
        "url": f"{BASE}/icf-concrete-home-builder-los-angeles.html",
        "title": "Concrete Home Builder in Los Angeles",
        "blurb": "reinforced concrete &amp; ICF custom villas: approach, "
                 "materials, process.",
    },
}

START = "<!-- PILLAR-XLINK:START -->"
END = "<!-- PILLAR-XLINK:END -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
ANCHOR = '<a href="https://myvilla.la/blog/" class="back-link">'


def pick_pillar(slug, section):
    s = slug.lower()
    if "malibu" in s:
        return "malibu"
    if any(t in s for t in ("italian", "mediterranean", "tuscan", "palazzo",
                            "villa-cresta", "como")):
        return "italian"
    if any(t in s for t in ("beverly-hills", "bel-air", "hidden-hills",
                            "holmby", "aman", "spec-mansion")):
        return "beverly"
    if section == "market":
        return "beverly"
    return "icf"


def block_html(p):
    return (
        f"{START}\n"
        '<div class="sources-section pillar-crosslink" '
        'style="margin-top:28px">\n'
        '  <div class="sources-title">From the Studio</div>\n'
        '  <ul class="sources-list">\n'
        f'    <li><strong><a href="{p["url"]}">{p["title"]}</a></strong> '
        f'&mdash; {p["blurb"]}</li>\n'
        "  </ul>\n"
        "</div>\n"
        f"{END}"
    )


def main():
    added = updated = skipped = 0
    for f in sorted(BLOG_DIR.glob("*.html")):
        if f.stem == "index" or f.stem.startswith("category"):
            continue
        html = f.read_text(encoding="utf-8")
        if ANCHOR not in html:
            skipped += 1
            continue
        section = ""
        j = f.with_suffix(".json")
        if j.exists():
            try:
                section = json.load(open(j)).get("_section_id") or ""
            except Exception:
                pass
        blk = block_html(PILLARS[pick_pillar(f.stem, section)])
        if START in html:
            new = BLOCK_RE.sub(blk, html, count=1)
            if new != html:
                updated += 1
        else:
            new = html.replace(ANCHOR, blk + "\n\n    " + ANCHOR, 1)
            added += 1
        if new != html:
            f.write_text(new, encoding="utf-8")
    print(f"[crosslink_pillars] blocchi aggiunti: {added}, "
          f"aggiornati: {updated}, saltati (senza anchor): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
