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
- Social (2026-09-22): icone Instagram + LinkedIn subito dopo il blocco Contact del footer (marker MV-SOCIAL).
- CSS minimo in <style id="mv-contact-css"> (una volta per pagina).

Idempotente (riscrive tra marker). Uso:
  python3 inject_contact_links.py            # tutte le pagine
  python3 inject_contact_links.py --dry-run  # solo report
Target: *.html a root (esclusa index.html), research/*.html, blog/*.html, blog/category/*.html.
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
SOC_S, SOC_E = "<!-- MV-SOCIAL:START -->", "<!-- MV-SOCIAL:END -->"
# Profili social (2026-09-22): icone Instagram + LinkedIn nel footer di ogni pagina
SOCIAL_IG = "https://www.instagram.com/myvilla.la/"
SOCIAL_LI = "https://www.linkedin.com/company/myvilla-la/"
SVG_IG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 2.2c3.2 0 3.6 0 4.8.1 1.2.1 1.8.2 2.2.4.6.2 1 .5 1.4.9.4.4.7.8.9 1.4.2.4.4 1.1.4 2.2.1 1.3.1 1.6.1 4.8s0 3.6-.1 4.8c-.1 1.2-.2 1.8-.4 2.2-.2.6-.5 1-.9 1.4-.4.4-.8.7-1.4.9-.4.2-1.1.4-2.2.4-1.3.1-1.6.1-4.8.1s-3.6 0-4.8-.1c-1.2-.1-1.8-.2-2.2-.4-.6-.2-1-.5-1.4-.9-.4-.4-.7-.8-.9-1.4-.2-.4-.4-1.1-.4-2.2C2.2 15.6 2.2 15.2 2.2 12s0-3.6.1-4.8c.1-1.2.2-1.8.4-2.2.2-.6.5-1 .9-1.4.4-.4.8-.7 1.4-.9.4-.2 1.1-.4 2.2-.4C8.4 2.2 8.8 2.2 12 2.2M12 0C8.7 0 8.3 0 7.1.1 5.8.1 4.9.3 4.1.6c-.8.3-1.5.7-2.1 1.4C1.3 2.6.9 3.3.6 4.1.3 4.9.1 5.8.1 7.1 0 8.3 0 8.7 0 12s0 3.7.1 4.9c.1 1.3.3 2.2.6 2.9.3.8.7 1.5 1.4 2.1.6.7 1.3 1.1 2.1 1.4.8.3 1.6.5 2.9.6 1.2.1 1.6.1 4.9.1s3.7 0 4.9-.1c1.3-.1 2.2-.3 2.9-.6.8-.3 1.5-.7 2.1-1.4.7-.6 1.1-1.3 1.4-2.1.3-.8.5-1.6.6-2.9.1-1.2.1-1.6.1-4.9s0-3.7-.1-4.9c-.1-1.3-.3-2.2-.6-2.9-.3-.8-.7-1.5-1.4-2.1C21.4 1.3 20.7.9 19.9.6 19.1.3 18.2.1 16.9.1 15.7 0 15.3 0 12 0zm0 5.8a6.2 6.2 0 1 0 0 12.4 6.2 6.2 0 0 0 0-12.4zM12 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm6.4-11.8a1.4 1.4 0 1 0 0 2.9 1.4 1.4 0 0 0 0-2.9z"/></svg>'
SVG_LI = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20.4 20.4h-3.6v-5.6c0-1.3 0-3-1.8-3s-2.1 1.4-2.1 2.9v5.7H9.3V9h3.4v1.6c.5-.9 1.6-1.8 3.4-1.8 3.6 0 4.3 2.4 4.3 5.5v6.1zM5.3 7.4a2.1 2.1 0 1 1 0-4.2 2.1 2.1 0 0 1 0 4.2zM7.1 20.4H3.5V9h3.6v11.4zM22.2 0H1.8C.8 0 0 .8 0 1.7v20.5c0 1 .8 1.8 1.8 1.8h20.4c1 0 1.8-.8 1.8-1.8V1.7C24 .8 23.2 0 22.2 0z"/></svg>'
SOCIAL_HTML = (f'<span class="mv-social" aria-label="Social profiles">'
               f'<a href="{SOCIAL_IG}" target="_blank" rel="noopener" aria-label="My Villa on Instagram" title="Instagram" data-ev="social_click" data-cta-id="footer_instagram">{SVG_IG}</a>'
               f'<a href="{SOCIAL_LI}" target="_blank" rel="noopener" aria-label="My Villa on LinkedIn" title="LinkedIn" data-ev="social_click" data-cta-id="footer_linkedin">{SVG_LI}</a>'
               f'</span>')
CSS = ('<style id="mv-contact-css">'
       '.nav-contact{font-size:11px;letter-spacing:.15em;text-transform:uppercase;font-weight:500;'
       'color:var(--warm-sand,#C4A265);opacity:.85;text-decoration:none;transition:opacity .3s;white-space:nowrap}'
       '.nav-contact:hover{opacity:1}'
       '.nav-contact--solo{margin-left:auto;margin-right:18px}'
       '.nav-links .nav-contact{margin-right:4px}'
       '@media (max-width:760px){.nav-contact--solo{display:none}}'
       '.mv-social{display:inline-flex;align-items:center;gap:12px;margin-left:12px;vertical-align:middle}'
       '.mv-social a{display:inline-flex;color:var(--warm-sand,#C4A265);opacity:.85;transition:opacity .3s}'
       '.mv-social a:hover{opacity:1}'
       '.mv-social svg{width:16px;height:16px;fill:currentColor;display:block}'
       '.footer-links .mv-social{margin-left:4px}'
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
            fpos = html.find("<footer")
            m = re.search(r'<a href="[^"]*privacy\.html[^"]*">[^<]*</a>', html[fpos:]) if fpos != -1 else None
            if m:
                end = fpos + m.end()
                html = html[:end] + block + html[end:]
                rep["footer"] = "inserted"
        else:
            rep["footer"] = "updated" if changed else "unchanged"

    # ── social (subito dopo il blocco Contact del footer) ──
    rep["social"] = "skip"
    sblock = _wrap(SOC_S, SOC_E, SOCIAL_HTML)
    html, changed = _replace_block(html, SOC_S, SOC_E, sblock)
    if changed is None:
        k = html.find(FOOT_E)
        if k != -1:
            k += len(FOOT_E)
            html = html[:k] + sblock + html[k:]
            rep["social"] = "inserted"
    else:
        rep["social"] = "updated" if changed else "unchanged"

    # ── css ──
    if rep["nav"] != "skip" or rep["social"] != "skip":
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
    for pat in ("*.html", "research/*.html", "blog/*.html", "blog/category/*.html"):
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
