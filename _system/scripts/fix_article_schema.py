#!/usr/bin/env python3
"""
fix_article_schema.py — normalizza schema.org / meta SEO di TUTTI gli
articoli pubblicati in blog/*.html (idempotente, riesegubile ogni giorno).

Cosa corregge, per ogni articolo (blog/<slug>.html, escluso index.html):
  1. Article.publisher.logo.url  → https://myvilla.la/img/myvilla-logo.png
     (il vecchio /assets/img/myvilla-logo.png era un 404)
  2. Article.dateModified         → se manca, = datePublished
  3. Article.author               → {"@type":"Organization",
                                     "name":"My Villa Editorial Team",
                                     "url":"https://myvilla.la/team.html"}
  4. <link rel="canonical">       → https://myvilla.la/blog/<slug>.html
     ECCETTO gli articoli "potati" da seo_prune.py (robots noindex +
     canonical verso il pillar del cluster): quelli restano com'erano.
  5. <meta name="robots">         → index,follow,max-image-preview:large,max-snippet:-1
     (gli articoli potati restano noindex,follow — non si tocca).

Ogni sostituzione nel blocco JSON-LD è chirurgica (regex sul testo, non
ri-serializzazione) per non riscrivere l'intero blocco; dopo le modifiche
il blocco viene ri-parsato con json.loads: se non è più valido, il file
NON viene scritto e l'errore finisce nel report.

Uso:
  python3 _system/scripts/fix_article_schema.py            # applica
  python3 _system/scripts/fix_article_schema.py --dry-run  # solo report
  python3 _system/scripts/fix_article_schema.py --json     # report JSON
  python3 _system/scripts/fix_article_schema.py blog/x.html  # singolo file

Exit code: sempre 0 (la pipeline non deve fermarsi); errori nel report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
SITE = "https://myvilla.la"

LOGO_URL = f"{SITE}/img/myvilla-logo.png"
AUTHOR = {"@type": "Organization", "name": "My Villa Editorial Team",
          "url": f"{SITE}/team.html"}
AUTHOR_JSON = ('{ "@type": "Organization", "name": "My Villa Editorial Team", '
               f'"url": "{SITE}/team.html" }}')
ROBOTS_INDEX = "index,follow,max-image-preview:large,max-snippet:-1"

LDJSON_RE = re.compile(
    r'(<script type="application/ld\+json">)(.*?)(</script>)', re.DOTALL)
ROBOTS_RE = re.compile(r'<meta name="robots" content="([^"]*)"\s*/?>')
CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]*)"\s*/?>')
LOGO_RE = re.compile(r'("logo"\s*:\s*\{[^{}]*?"url"\s*:\s*")([^"]+)(")')
AUTHOR_RE = re.compile(r'"author"\s*:\s*\{[^{}]*\}')
DATE_PUB_RE = re.compile(r'^(\s*)"datePublished"\s*:\s*"([^"]+)"\s*,?\s*$',
                         re.MULTILINE)


def _is_article_block(text: str) -> bool:
    try:
        data = json.loads(text)
    except Exception:
        return False
    if isinstance(data, dict):
        if data.get("@type") == "Article":
            return True
        for node in data.get("@graph", []) or []:
            if isinstance(node, dict) and node.get("@type") == "Article":
                return True
    return False


def _fix_ldjson(block: str, changes: List[str]) -> Optional[str]:
    """Ritorna il blocco corretto (o None se non è un Article / errore)."""
    if not _is_article_block(block):
        return None
    new = block

    # 1. publisher.logo.url
    def _logo(m):
        if m.group(2) != LOGO_URL:
            changes.append("logo")
            return m.group(1) + LOGO_URL + m.group(3)
        return m.group(0)
    new = LOGO_RE.sub(_logo, new, count=1)

    # 3. author
    m = AUTHOR_RE.search(new)
    if m:
        try:
            cur = json.loads("{" + m.group(0) + "}")["author"]
        except Exception:
            cur = None
        if cur != AUTHOR:
            new = new[:m.start()] + '"author": ' + AUTHOR_JSON + new[m.end():]
            changes.append("author")
    else:
        # nessun author: lo inseriamo dopo datePublished
        mp = DATE_PUB_RE.search(new)
        if mp:
            indent = mp.group(1)
            ins = f'{indent}"author": {AUTHOR_JSON},\n'
            new = new[:mp.end() + 1] + ins + new[mp.end() + 1:]
            changes.append("author_added")

    # 2. dateModified
    if '"dateModified"' not in new:
        mp = DATE_PUB_RE.search(new)
        if mp:
            indent, date = mp.group(1), mp.group(2)
            ins = f'{indent}"dateModified": "{date}",\n'
            new = new[:mp.end() + 1] + ins + new[mp.end() + 1:]
            changes.append("dateModified")

    # validazione finale
    try:
        json.loads(new)
    except Exception as e:  # noqa: BLE001
        changes.append(f"ERROR_invalid_json:{e}")
        return None
    return new


def fix_file(path: Path, dry_run: bool = False) -> Dict:
    slug = path.stem
    rec: Dict = {"slug": slug, "changes": [], "pruned": False, "error": None}
    try:
        html = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"read: {e}"
        return rec
    orig = html
    changes: List[str] = []

    # robots + canonical (rispetta la potatura SEO)
    rm = ROBOTS_RE.search(html)
    robots_now = rm.group(1) if rm else ""
    pruned = "noindex" in robots_now.lower()
    rec["pruned"] = pruned
    if not pruned:
        if rm and robots_now != ROBOTS_INDEX:
            html = html[:rm.start()] + \
                f'<meta name="robots" content="{ROBOTS_INDEX}">' + html[rm.end():]
            changes.append("robots")
        elif not rm:
            # inserisci dopo <meta name="author"> se c'è, altrimenti dopo <title>
            anchor = re.search(r'<meta name="author"[^>]*>\n?', html) or \
                re.search(r'</title>\n?', html)
            if anchor:
                html = html[:anchor.end()] + \
                    f'<meta name="robots" content="{ROBOTS_INDEX}">\n' + html[anchor.end():]
                changes.append("robots_added")
        expected = f"{SITE}/blog/{slug}.html"
        cm = CANONICAL_RE.search(html)
        if cm and cm.group(1) != expected:
            html = html[:cm.start()] + f'<link rel="canonical" href="{expected}">' + html[cm.end():]
            changes.append(f"canonical({cm.group(1)})")
        elif not cm:
            anchor = re.search(r'<meta name="robots"[^>]*>\n?', html)
            if anchor:
                html = html[:anchor.end()] + \
                    f'<link rel="canonical" href="{expected}">\n' + html[anchor.end():]
                changes.append("canonical_added")

    # JSON-LD Article
    found_article = False

    def _sub(m):
        nonlocal found_article
        inner_changes: List[str] = []
        fixed = _fix_ldjson(m.group(2), inner_changes)
        if fixed is None:
            if inner_changes:
                changes.extend(inner_changes)
            return m.group(0)
        found_article = True
        changes.extend(inner_changes)
        return m.group(1) + fixed + m.group(3)

    html = LDJSON_RE.sub(_sub, html)
    if not found_article:
        changes.append("WARN_no_article_schema")

    rec["changes"] = changes
    if html != orig and not dry_run:
        errs = [c for c in changes if c.startswith("ERROR")]
        if errs:
            rec["error"] = "; ".join(errs)
        else:
            path.write_text(html, encoding="utf-8")
            rec["written"] = True
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="file o directory (default blog/)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="report JSON su stdout")
    args = ap.parse_args()

    files: List[Path] = []
    targets = [Path(p) for p in args.paths] or [BLOG_DIR]
    for t in targets:
        if t.is_dir():
            files.extend(sorted(p for p in t.glob("*.html") if p.name != "index.html"))
        elif t.suffix == ".html" and t.name != "index.html":
            files.append(t)

    report = {"total": len(files), "modified": 0, "pruned": 0, "errors": 0,
              "by_change": {}, "files": []}
    for f in files:
        rec = fix_file(f, dry_run=args.dry_run)
        real = [c for c in rec["changes"] if not c.startswith("WARN")
                and not c.startswith("ERROR")]
        if real:
            report["modified"] += 1
        if rec["pruned"]:
            report["pruned"] += 1
        if rec["error"] or any(c.startswith("ERROR") for c in rec["changes"]):
            report["errors"] += 1
        for c in rec["changes"]:
            key = c.split("(")[0].split(":")[0]
            report["by_change"][key] = report["by_change"].get(key, 0) + 1
        if rec["changes"] or rec["error"]:
            report["files"].append(rec)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        mode = "DRY-RUN" if args.dry_run else "APPLY"
        print(f"[fix_article_schema] {mode}: {report['total']} articoli, "
              f"{report['modified']} da modificare/modificati, "
              f"{report['pruned']} potati (robots/canonical intatti), "
              f"{report['errors']} errori")
        for k, v in sorted(report["by_change"].items()):
            print(f"   {k:>26}: {v}")
        for rec in report["files"]:
            if rec["error"]:
                print(f"   !! {rec['slug']}: {rec['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
