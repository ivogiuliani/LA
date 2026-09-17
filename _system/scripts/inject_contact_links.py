#!/usr/bin/env python3
"""
inject_contact_links.py — voce "Contact" nel menu e nel footer di TUTTE le pagine
tranne la home (che la riceve da inject_conversion_layer.py con i marker CONV:*).

Decisione 2026-09-17: la sezione Contact (form + email) è nella home (#contact);
ogni altra pagina deve poterci arrivare dal menu principale e dal footer.

- Menu: <a class="nav-contact" href="https://myvilla.la/#contact">Contact</a>
  inserito subito prima del bottone .nav-cta (marker MV-NAVCONTACT).
- Footer: link "Contact" dentro .footer-links, oppure " · Contact" dopo il link
  Privacy nelle pagine con footer a una riga (marker MV-FOOTCONTACT).
- CSS minimo in <style id="mv-contact-css"> (una volta per pagina).

Idempotente (riscrive tra marker). Uso:
  python3 inject_contact_links.py            # tutte le pagine
  python3 inject_contact_links.py --dry-run  # solo report
Target: *.html a root (esclusa index.html), research/*.html, blog/*.html.
Gira nel post-publish di publish_all_drafts.py (le pagine rigenerate lo ricevono).
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONTACT_URL = "https://myvilla.la/#contact"
NAV_S, NAV_E = "<!-- MV-NAVCONTACT:START -->", "<!-- MV-NAVCONTACT:END -->"
FOOT_S, FOOT_E = "<!-- MV-FOOTCONTACT:START -->", "<!-- MV-FOOTCONTACT:END -->"
CSS = ('<style id="mv-contact-css">'
       '.nav-contact{font-size:11px;letter-spacing:.15em;text-transform:uppercase;font-weight:500;'
       'color:var(--warm-sand,#C4A265);opacity:.85;text-decoration:none;transition:opacity .3s;white-space:nowrap}'
       '.nav-contact:hover{opacity:1}'
       '.nav-contact--solo{margin-left:auto;margin-right:18px}'
       '.nav-links .nav-contact{margin-right:4px}'
       '@media (max-width:760px){.nav-contact--solo{display:none}}'
       '</style>')


def _wrap(s: str, e: str, inner: str) -> str:
    return f"{s}{inner}{e}"


def _replace_block(html: str, s: str, e: str, block: str) -> tuple[str, bool]:
    i, j = html.find(s), html.find(e)
    if i != -1 and j != -1 and j > i:
        j2 = j + len(e)
        if html[i:j2] == block:
            return html, False
        return html[:i] + block + html[j2:], True
    return html, None  # type: ignore[return-value]


def patch(path: Path, dry: bool) -> dict:
    html = path.read_text(encoding="utf-8")
    orig = html
    rep = {"nav": "skip", "footer": "skip", "css": "skip"}

    # ── nav ──
    solo = '<div class="nav-links">' not in html
    nav_link = (f'<a href="{CONTACT_URL}" class="nav-contact{" nav-contact--solo" if solo else ""}" '
                f'data-ev="cta_click" data-cta-id="nav_contact">Contact</a>')
    block = _wrap(NAV_S, NAV_E, nav_link)
    html, changed = _replace_block(html, NAV_S, NAV_E, block)
    if changed is None:
        m = re.search(r'[ \t]*<a [^>]*class="nav-cta"[^>]*>', html)
        if m:
            indent = re.match(r"[ \t]*", m.group(0)).group(0)
            html = html[:m.start()] + f"{indent}{block}\n" + html[m.start():]
            rep["nav"] = "inserted"
    else:
        rep["nav"] = "updated" if changed else "unchanged"

    # ── footer ──
    foot_link = f'<a href="https://myvilla.la/research/westside-rebuild-tracker.html">Research</a> <a href="{CONTACT_URL}">Contact</a>'
    if '<div class="footer-links">' in html:
        block = _wrap(FOOT_S, FOOT_E, f"\n    {foot_link}")
        html, changed = _replace_block(html, FOOT_S, FOOT_E, block)
        if changed is None:
            m = re.search(r'<div class="footer-links">', html)
            j = html.find("</div>", m.end())
            html = html[:j].rstrip() + block + "\n  " + html[j:]
            rep["footer"] = "inserted"
        else:
            rep["footer"] = "updated" if changed else "unchanged"
    else:
        block = _wrap(FOOT_S, FOOT_E, " &middot; " + foot_link.replace("</a> <a", "</a> &middot; <a"))
        html, changed = _replace_block(html, FOOT_S, FOOT_E, block)
        if changed is None:
            m = re.search(r'<a href="[^"]*privacy\.html[^"]*">[^<]*</a>', html)
            if m and "<footer" in html and m.start() > html.find("<footer"):
                html = html[:m.end()] + block + html[m.end():]
                rep["footer"] = "inserted"
        else:
            rep["footer"] = "updated" if changed else "unchanged"

    # ── css ──
    if rep["nav"] != "skip":
        if 'id="mv-contact-css"' in html:
            html2 = re.sub(r'<style id="mv-contact-css">.*?</style>', CSS, html, count=1, flags=re.S)
            rep["css"] = "unchanged" if html2 == html else "updated"; html = html2
        elif "</head>" in html:
            html = html.replace("</head>", CSS + "\n</head>", 1); rep["css"] = "inserted"

    if html != orig and not dry:
        path.write_text(html, encoding="utf-8")
    rep["changed"] = html != orig
    return rep


def targets() -> list[Path]:
    out = []
    for pat in ("*.html", "research/*.html", "blog/*.html"):
        for f in sorted(glob.glob(str(ROOT / pat))):
            p = Path(f)
            if p.name == "index.html" and p.parent == ROOT:
                continue  # la home ha i marker CONV:* dell'injector principale
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    stats = {"files": 0, "changed": 0, "nav": 0, "footer": 0, "nav_skip": []}
    for p in targets():
        try:
            r = patch(p, a.dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {p.relative_to(ROOT)}: {exc}"); continue
        stats["files"] += 1
        stats["changed"] += int(r["changed"])
        stats["nav"] += int(r["nav"] in ("inserted", "updated", "unchanged"))
        stats["footer"] += int(r["footer"] in ("inserted", "updated", "unchanged"))
        if r["nav"] == "skip":
            stats["nav_skip"].append(str(p.relative_to(ROOT)))
    print(f"[contact-links] file {stats['files']} · modificati {stats['changed']} · menu {stats['nav']} · footer {stats['footer']}"
          + (" · DRY-RUN" if a.dry_run else ""))
    if stats["nav_skip"]:
        print("  senza .nav-cta (menu non toccato):", ", ".join(stats["nav_skip"][:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
