#!/usr/bin/env python3
"""POTATURA SEO (decisione 2026-06-24, eseguita 2026-08-26).

Per ogni cluster di keyword saturo (stessa lista di `keyword_caps` in
journal-sections.yml, match sul TOKEN nello slug come _saturated_keywords
di generate_journal) tiene UN articolo canonico e mette gli altri in
`noindex,follow` con `<link rel="canonical">` puntato al canonico.
Reversibile: nessuna cancellazione, solo meta tag.

Il canonico di ogni cluster è la pagina con più impression su Google
(snapshot Search Console passato con --gsc-data, formato di
pull_seo_data.py); a parità, l'articolo più recente.

Uso:
  python3 seo_prune.py --gsc-data seo_data.json           # dry-run
  python3 seo_prune.py --gsc-data seo_data.json --apply   # scrive i file
"""
import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
BASE_URL = "https://myvilla.la"

# Ordine di assegnazione: prima i token più specifici, "wildfire" per
# ultimo perché è il più generico ("wildfire-insurance" contiene anche
# "fire-insurance"). Un articolo appartiene a UN solo cluster.
CLUSTER_TOKENS = [
    "fair-plan", "ibhs", "state-farm", "non-renewal",
    "insurable-home", "fire-insurance", "wildfire",
]

NOINDEX_TAG = '<meta name="robots" content="noindex, follow">'
INDEX_RE = re.compile(r'<meta name="robots" content="[^"]*">')
CANONICAL_RE = re.compile(r'<link rel="canonical" href="[^"]*">')


def article_date(slug):
    j = BLOG_DIR / f"{slug}.json"
    if j.exists():
        try:
            return json.load(open(j)).get("_date") or ""
        except Exception:
            return ""
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gsc-data", required=True,
                    help="JSON di pull_seo_data.py (per le impression)")
    ap.add_argument("--apply", action="store_true",
                    help="scrive i file (default: dry-run)")
    args = ap.parse_args()

    gsc = json.load(open(args.gsc_data))
    impr = {}
    for row in gsc.get("gsc_pages", []):
        page = row["keys"][0].replace(BASE_URL, "")
        if page.startswith("/blog/"):
            slug = page[len("/blog/"):].removesuffix(".html")
            impr[slug] = impr.get(slug, 0) + int(row.get("impressions", 0))

    slugs = sorted(p.stem for p in BLOG_DIR.glob("*.html")
                   if p.stem not in ("index",)
                   and not p.stem.startswith("category"))

    clusters = {}
    for slug in slugs:
        for tok in CLUSTER_TOKENS:
            if tok in slug:
                clusters.setdefault(tok, []).append(slug)
                break

    total_noindex = 0
    plan = []  # (slug, canonical_slug)
    for tok in CLUSTER_TOKENS:
        members = clusters.get(tok) or []
        if len(members) < 2:
            continue
        members.sort(key=lambda s: (impr.get(s, 0), article_date(s)),
                     reverse=True)
        canonical = members[0]
        print(f"\n── cluster '{tok}': {len(members)} articoli — canonico: "
              f"{canonical} ({impr.get(canonical, 0)} impr)")
        for s in members[1:]:
            print(f"   noindex → {s} ({impr.get(s, 0)} impr)")
            plan.append((s, canonical))
            total_noindex += 1

    print(f"\nTotale: {total_noindex} articoli in noindex, "
          f"{len(slugs) - total_noindex} restano indicizzabili "
          f"(su {len(slugs)} articoli).")

    if not args.apply:
        print("\nDRY-RUN: nessun file toccato. Rilancia con --apply.")
        return 0

    changed = 0
    for slug, canonical in plan:
        f = BLOG_DIR / f"{slug}.html"
        html = f.read_text(encoding="utf-8")
        canonical_url = f"{BASE_URL}/blog/{canonical}.html"
        new = INDEX_RE.sub(NOINDEX_TAG, html, count=1)
        if new == html and NOINDEX_TAG not in html:
            # nessun meta robots esistente: inserisci dopo charset
            new = html.replace('<meta charset="UTF-8">',
                               '<meta charset="UTF-8">\n' + NOINDEX_TAG, 1)
        new = CANONICAL_RE.sub(
            f'<link rel="canonical" href="{canonical_url}">', new, count=1)
        if new != html:
            f.write_text(new, encoding="utf-8")
            changed += 1
    print(f"Scritti {changed} file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
