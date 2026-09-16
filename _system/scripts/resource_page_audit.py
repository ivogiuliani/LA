#!/usr/bin/env python3
"""
resource_page_audit.py — audit "broken link building" sulle pagine risorse
(.gov/.org/.edu) di ricostruzione LA/Malibu/Palisades e resilienza wildfire.

Cosa fa
  Per ogni pagina in _system/backlinks/resource_pages.yml:
    1. scarica la pagina (UA onesto, timeout 15);
    2. estrae i link esterni (max --max-links per pagina, default 80);
    3. li testa con validate_links.check_url (stesso validator del Journal;
       fallback interno se non importabile), in parallelo;
    4. scrive l'esito in _system/backlinks/resource_audit.json;
    5. per ogni pagina con >=1 link rotto genera una BOZZA email in
       _drafts/backlinks/resource_outreach/<id>.md che segnala i link rotti
       e offre in dono la risorsa (Westside Rebuild Tracker o checklist
       insurable-home), firmata "the office of Paolo Mezzalama".
  NON invia mai nulla. Le bozze restano in _drafts/ per la review umana.

Come si lancia
  python3 _system/scripts/resource_page_audit.py              # audit completo
  python3 _system/scripts/resource_page_audit.py --only ca-gov-lafires
  python3 _system/scripts/resource_page_audit.py --dry-run    # niente scritture
  python3 _system/scripts/resource_page_audit.py --max-links 40

Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
sys.path.insert(0, str(SCRIPT_DIR))
import backlinklib as bl  # noqa: E402

OUT_DIR = bl.DRAFTS_DIR / "backlinks" / "resource_outreach"
GIFTS = {
    "tracker": {
        "name": "Westside Rebuild Tracker",
        "url": "https://myvilla.la/research/",
        "what": ("a free, weekly-updated tracker of rebuild permits and completions across the "
                 "Palisades, Malibu and Altadena, built from public LADBS and County open data"),
    },
    "checklist": {
        "name": "Insurable Home in California checklist",
        "url": "https://myvilla.la/insurable-home-california.html",
        "what": ("a plain-language checklist of the 12 Safer from Wildfires measures, the 2026 WUI "
                 "Code and the IBHS Wildfire Prepared Home standard, with sources for each item"),
    },
}
SKIP_HOSTS = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
              "linkedin.com", "google.com", "apple.com", "play.google.com", "t.me",
              "nextdoor.com", "tiktok.com", "flickr.com", "vimeo.com")


def _check_url(url: str) -> dict:
    try:
        import validate_links  # type: ignore
        return validate_links.check_url(url)
    except Exception:  # noqa: BLE001
        st, body, _ = bl.fetch(url, timeout=12, max_bytes=20_000)
        ok = 200 <= st < 400 or st in (401, 403, 429)
        return {"url": url, "ok": ok, "status": st, "reason": "" if ok else str(st or "network"), "method": "GET"}


def audit_page(page: dict, max_links: int) -> dict:
    url = page["url"]
    res = {"id": page["id"], "url": url, "org": page.get("org"), "gift": page.get("gift", "tracker"),
           "http": 0, "external_links": 0, "tested": 0, "broken": [], "checked_at": bl.now_iso()}
    status, html, final = bl.fetch(url)
    res["http"] = status
    res["final_url"] = final
    if status == 0 or status >= 400:
        res["error"] = html[:120] if status == 0 else f"HTTP {status}"
        return res
    links = [l for l in bl.extract_external_links(html, bl.domain_of(final or url))
             if not any(h in bl.domain_of(l["href"]) for h in SKIP_HOSTS)]
    res["external_links"] = len(links)
    links = links[:max_links]
    res["tested"] = len(links)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        checks = list(ex.map(lambda l: _check_url(l["href"]), links))
    for l, c in zip(links, checks):
        if not c.get("ok"):
            # I 403/429 senza "tolerated" possono essere anti-bot: li segnaliamo
            # come "probabilmente rotti" solo se 404/410/dns; il resto è "da verificare".
            reason = str(c.get("reason") or "")
            hard = c.get("status") in (404, 410) or "network error" in reason or "soft-404" in reason
            res["broken"].append({"href": l["href"], "anchor": l["anchor"], "status": c.get("status"),
                                  "reason": reason, "confidence": "high" if hard else "check"})
    return res


def draft_email(res: dict, settings: dict) -> str:
    gift = GIFTS.get(res.get("gift") or "tracker", GIFTS["tracker"])
    sig = (settings.get("signatures") or {}).get("prospects") or "the office of Paolo Mezzalama · My Villa\ninfo@myvilla.la · myvilla.la"
    hard = [b for b in res["broken"] if b["confidence"] == "high"]
    show = (hard or res["broken"])[:3]
    lines = "\n".join(f"- {b['anchor'] or b['href']} → {b['href']} ({b['status'] or 'unreachable'})" for b in show)
    org = res.get("org") or "your team"
    n = len(res["broken"])
    subject = (f"{'One broken link' if n == 1 else str(n) + ' broken links'} on your {org} resource page "
               f"(and a free rebuild resource)")
    body = f"""Hello,

While reading your resource page ({res['url']}) we noticed that {len(res['broken'])} of the outbound links no longer resolve:

{lines}

We keep a list like this for our own readers, so we thought you would rather hear it than not.

If useful, we would be glad to offer {gift['what']}: {gift['name']} — {gift['url']}. It is free, has no sign-up, and we update it every week. If it fits the page, a link to it would help residents find it; if not, no worries at all.

Thank you for the work you do for the community.

{sig.strip()}
"""
    fm = (f"---\nkind: resource_outreach\npage_id: {res['id']}\npage_url: {res['url']}\n"
          f"org: \"{org}\"\ngift: {gift['name']}\nbroken_links: {len(res['broken'])}\n"
          f"high_confidence: {len(hard)}\nstatus: review\nto: \"\"   # contatto da leggere sulla pagina (form o casella generica)\n"
          f"created: {bl.today()}\nsender: office\n---\n\n")
    return fm + f"Subject: {subject}\n\n" + body


def run(*, only: Optional[str] = None, dry_run: bool = False, max_links: int = 80) -> dict:
    import yaml
    pages = (yaml.safe_load(bl.RESOURCE_PAGES_YML.read_text(encoding="utf-8")) or {}).get("pages", [])
    if only:
        pages = [p for p in pages if p.get("id") == only]
    settings = bl.load_settings()
    print(f"resource_page_audit: {len(pages)} pagine{' (dry-run)' if dry_run else ''}")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(lambda p: audit_page(p, max_links), pages):
            results.append(res)
            tag = f"{len(res['broken'])} rotti/{res['tested']}" if not res.get("error") else res["error"]
            print(f"  {res['id']:38} http={res['http']:<3} {tag}")
    drafts = 0
    if not dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    for res in results:
        if res["broken"]:
            path = OUT_DIR / f"{res['id']}.md"
            if not dry_run:
                path.write_text(draft_email(res, settings), encoding="utf-8")
                bl.ledger_append({"event": "resource_audit", "page_id": res["id"],
                                  "broken": len(res["broken"]), "draft": str(path.relative_to(bl.ROOT))})
            drafts += 1
    out = {"generated_at": bl.now_iso(), "pages": len(results),
           "pages_with_broken": sum(1 for r in results if r["broken"]),
           "drafts": drafts, "results": results}
    if not dry_run:
        bl.RESOURCE_AUDIT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"resource_page_audit: {out['pages_with_broken']} pagine con link rotti, {drafts} bozze in {OUT_DIR.relative_to(bl.ROOT)}/")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit link rotti su pagine risorse + bozze outreach.")
    ap.add_argument("--only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-links", type=int, default=80)
    a = ap.parse_args(argv)
    try:
        run(only=a.only, dry_run=a.dry_run, max_links=a.max_links)
    except Exception as e:  # noqa: BLE001
        print(f"resource_page_audit: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
