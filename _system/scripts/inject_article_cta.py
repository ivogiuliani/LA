#!/usr/bin/env python3
"""
inject_article_cta.py — CTA di conversione negli articoli del Journal
(blog/*.html). Idempotente: si può rilanciare ogni giorno dopo la publish.

Per ogni articolo (blog/<slug>.html, escluso index.html):
  (a) MID-CTA — un blocco tra i marker <!-- MV-MIDCTA:START/END --> inserito
      dopo il 3° paragrafo del body (dopo il 2° se l'articolo è corto, o
      subito prima del blocco "Our Perspective" se cortissimo). Una riga in
      voce My Villa con link a
        https://myvilla.la/private-briefing.html?src=journal_mid&slug=<slug>
      classe .mv-midcta, CSS inline minimale (palette del Journal),
      attributi data-ev="journal_cta_click" data-cta="mid".
      Se il blocco esiste già viene SOSTITUITO (così un cambio di testo
      si propaga a tutto l'archivio con un solo run).
  (b) CTA finale "Request a Briefing" (sezione .journal-cta) → punta a
        https://myvilla.la/private-briefing.html?src=journal_end&slug=<slug>
      con data-ev/data-cta="end". "Explore My Villa" resta la home.
  (c) Un piccolo <script> tra <!-- MV-CTAJS:START/END --> prima di </body>
      che manda a GA4 (gtag) l'evento data-ev al click, con cta + slug.

Le stesse funzioni sono importate da generate_journal.py e build_v2.py,
così i NUOVI articoli nascono già con le CTA (inject_mid_cta / end_cta_href
/ CTA_JS_BLOCK).

Uso:
  python3 _system/scripts/inject_article_cta.py            # applica a blog/
  python3 _system/scripts/inject_article_cta.py --dry-run  # solo report
  python3 _system/scripts/inject_article_cta.py --json
  python3 _system/scripts/inject_article_cta.py blog/x.html

Exit code: sempre 0 (la pipeline non deve fermarsi); errori nel report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"

LANDING = "https://myvilla.la/private-briefing.html"
MID_START, MID_END = "<!-- MV-MIDCTA:START -->", "<!-- MV-MIDCTA:END -->"
JS_START, JS_END = "<!-- MV-CTAJS:START -->", "<!-- MV-CTAJS:END -->"

MID_TEXT_LEAD = "Planning a home in Malibu or Beverly Hills?"
MID_TEXT_LINK = "Request a private briefing with a My Villa partner."


def mid_cta_href(slug: str) -> str:
    return f"{LANDING}?src=journal_mid&amp;slug={slug}"


def end_cta_href(slug: str) -> str:
    return f"{LANDING}?src=journal_end&amp;slug={slug}"


def mid_cta_html(slug: str) -> str:
    """Blocco MID-CTA completo di marker (CSS inline, nessuna dipendenza)."""
    return (
        f"{MID_START}\n"
        '<aside class="mv-midcta" aria-label="Request a private briefing" '
        'style="margin:40px 0;padding:22px 26px;border-left:3px solid #C2714F;'
        "background:#FAF6F0;font-family:'Montserrat',-apple-system,sans-serif;"
        'font-size:14px;line-height:1.65;color:#3E2F2B;">\n'
        '  <span style="display:block;font-size:11px;font-weight:600;'
        'letter-spacing:.22em;text-transform:uppercase;color:#C2714F;'
        'margin-bottom:8px;">From My Villa</span>\n'
        f'  {MID_TEXT_LEAD} <a href="{mid_cta_href(slug)}" '
        'data-ev="journal_cta_click" data-cta="mid" '
        'style="color:#C2714F;font-weight:600;text-decoration:underline;'
        f'text-underline-offset:3px;">{MID_TEXT_LINK}</a>\n'
        "</aside>\n"
        f"{MID_END}"
    )


CTA_JS_BLOCK = (
    f"{JS_START}\n"
    "<script>\n"
    "(function(){document.addEventListener('click',function(e){"
    "var a=e.target&&e.target.closest?e.target.closest('[data-ev]'):null;"
    "if(!a||typeof window.gtag!=='function')return;"
    "var slug=(location.pathname.split('/').pop()||'').replace(/\\.html$/,'');"
    "window.gtag('event',a.getAttribute('data-ev'),{cta:a.getAttribute('data-cta')||'',"
    "slug:slug,transport_type:'beacon'});},true);})();\n"
    "</script>\n"
    f"{JS_END}"
)

# paragrafi "veri" del body: <p> senza classe pullquote/source-citation
P_RE = re.compile(r"<p\b([^>]*)>.*?</p>", re.DOTALL)
SKIP_CLASSES = ("pullquote", "source-citation", "perspective", "mv-")
END_CTA_RE = re.compile(
    r'<a href="[^"]*" class="cta-btn cta-btn-secondary"[^>]*>Request a Briefing</a>')
# strip: il blocco E gli spazi/newline attorno, così la re-inserzione è
# deterministica (idempotenza byte-per-byte, niente righe vuote accumulate)
MID_BLOCK_RE = re.compile(r"\s*" + re.escape(MID_START) + r".*?" + re.escape(MID_END) + r"\s*",
                          re.DOTALL)
JS_BLOCK_RE = re.compile(re.escape(JS_START) + r".*?" + re.escape(JS_END) + r"\n?",
                         re.DOTALL)


def _strip_mid(body: str) -> str:
    return MID_BLOCK_RE.sub("", body)


def inject_mid_cta(body_html: str, slug: str) -> str:
    """Inserisce (o sostituisce) il MID-CTA dentro il body HTML dell'articolo.

    `body_html` è il contenuto dell'articolo SENZA il blocco Our Perspective
    (come nel sidecar JSON) oppure l'intero <article> (uso batch): in
    entrambi i casi si conta solo fino a un eventuale <div class="perspective">.
    """
    body = _strip_mid(body_html)
    block = mid_cta_html(slug)
    limit = len(body)
    m_persp = re.search(r'<(div|aside) class="perspective"', body)
    if m_persp:
        limit = m_persp.start()
    paras = []
    for m in P_RE.finditer(body):
        if m.start() >= limit:
            break
        attrs = m.group(1)
        if any(c in attrs for c in SKIP_CLASSES):
            continue
        paras.append(m)
    if len(paras) >= 5:
        at = paras[2].end()
    elif len(paras) >= 3:
        at = paras[1].end()
    elif m_persp:
        at = m_persp.start()
        return body[:at].rstrip() + "\n\n" + block + "\n\n    " + body[at:]
    elif paras:
        at = paras[-1].end()
    else:
        return body.rstrip() + "\n\n" + block + "\n"
    return body[:at] + "\n\n" + block + "\n\n" + body[at:].lstrip()


def process_html(html: str, slug: str, changes: List[str]) -> str:
    if '<article class="article-content">' not in html:
        changes.append("SKIP_no_article_content")
        return html
    # (a) mid CTA — lavora sull'intero <article> … </article>
    m = re.search(r'<article class="article-content">.*?</article>', html, re.DOTALL)
    if m:
        new_art = inject_mid_cta(m.group(0), slug)
        if new_art != m.group(0):
            changes.append("mid_updated" if MID_START in m.group(0) else "mid_added")
            html = html[:m.start()] + new_art + html[m.end():]
    # (b) end CTA
    target = (f'<a href="{end_cta_href(slug)}" class="cta-btn cta-btn-secondary" '
              'data-ev="journal_cta_click" data-cta="end">Request a Briefing</a>')
    em = END_CTA_RE.search(html)
    if em and em.group(0) != target:
        html = html[:em.start()] + target + html[em.end():]
        changes.append("end_cta")
    elif not em:
        changes.append("WARN_no_end_cta")
    # (c) JS
    if JS_START in html:
        cur = JS_BLOCK_RE.search(html)
        if cur and cur.group(0).strip() != CTA_JS_BLOCK.strip():
            html = html[:cur.start()] + CTA_JS_BLOCK + "\n" + html[cur.end():]
            changes.append("js_updated")
    elif "</body>" in html:
        html = html.replace("</body>", CTA_JS_BLOCK + "\n</body>", 1)
        changes.append("js_added")
    return html


def process_file(path: Path, dry_run: bool = False) -> Dict:
    rec: Dict = {"slug": path.stem, "changes": [], "error": None}
    try:
        html = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"read: {e}"
        return rec
    new = process_html(html, path.stem, rec["changes"])
    if new != html:
        # sanity: un solo mid block, marker bilanciati
        if new.count(MID_START) != 1 or new.count(MID_END) != 1:
            rec["error"] = "marker MV-MIDCTA non bilanciati, file NON scritto"
            return rec
        if not dry_run:
            path.write_text(new, encoding="utf-8")
            rec["written"] = True
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="CTA di conversione negli articoli del Journal")
    ap.add_argument("paths", nargs="*", help="file o directory (default blog/)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files: List[Path] = []
    for t in ([Path(p) for p in args.paths] or [BLOG_DIR]):
        if t.is_dir():
            files.extend(sorted(p for p in t.glob("*.html") if p.name != "index.html"))
        elif t.suffix == ".html" and t.name != "index.html":
            files.append(t)

    report = {"total": len(files), "modified": 0, "errors": 0, "by_change": {}, "files": []}
    for f in files:
        rec = process_file(f, dry_run=args.dry_run)
        real = [c for c in rec["changes"] if not c.startswith(("WARN", "SKIP"))]
        if real:
            report["modified"] += 1
        if rec["error"]:
            report["errors"] += 1
        for c in rec["changes"]:
            report["by_change"][c] = report["by_change"].get(c, 0) + 1
        if rec["changes"] or rec["error"]:
            report["files"].append(rec)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        mode = "DRY-RUN" if args.dry_run else "APPLY"
        print(f"[inject_article_cta] {mode}: {report['total']} articoli, "
              f"{report['modified']} modificati, {report['errors']} errori")
        for k, v in sorted(report["by_change"].items()):
            print(f"   {k:>24}: {v}")
        for rec in report["files"]:
            if rec["error"]:
                print(f"   !! {rec['slug']}: {rec['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
