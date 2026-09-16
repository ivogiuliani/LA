#!/usr/bin/env python3
"""
mention_monitor.py — cerca ogni settimana le menzioni di My Villa sul web
(Brave Search API + Google CSE), le classifica linked/unlinked e trasforma
le menzioni senza link in prospect (status=needs_contact).

Cosa fa
  1. Query (Brave se BRAVE_API_KEY, Google CSE se GOOGLE_CSE_API_KEY+ENGINE_ID;
     se mancano entrambe: skip con messaggio, exit 0):
       "myvilla.la"  ·  "My Villa" Mezzalama  ·  "My Villa" Malibu concrete
       "My Villa" "Los Angeles" Italian villa concrete  ·  "My Villa" "IT'S Architecture"
  2. Esclude i nostri domini e gli omonimi (villa.edu, villaforyou, myvilla.it,
     myprivatevillas…) e i social nostri (instagram/x/linkedin).
  3. Dedup per URL su _system/backlinks/mentions.json.
  4. Per ogni URL nuovo: fetch → conferma che la pagina parli davvero di noi
     (contiene 'myvilla.la' o 'Mezzalama' o 'My Villa' + LA/Malibu/concrete) →
     linked (href verso myvilla.la, con rel) oppure unlinked.
  5. Unlinked → nuovo prospect in prospects.yml (engine=editorial,
     id=mention-<dominio>, status=needs_contact, contact vuoto) + riga ledger.

Come si lancia
  python3 _system/scripts/mention_monitor.py            # run reale (solo letture web + scritture locali)
  python3 _system/scripts/mention_monitor.py --dry-run  # nessuna scrittura
  python3 _system/scripts/mention_monitor.py --recheck  # riverifica linked/unlinked delle menzioni note

Output: _system/backlinks/mentions.json, prospects.yml (solo nuove righe), ledger.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
import backlinklib as bl  # noqa: E402

QUERIES = [
    '"myvilla.la"',
    '"My Villa" Mezzalama',
    '"My Villa" Malibu concrete',
    '"My Villa" "Los Angeles" Italian villa concrete',
    '"My Villa" "IT\'S Architecture"',
    '"myvilla.la" -site:myvilla.la',
]
SOCIAL_OWN = {"instagram.com", "www.instagram.com", "x.com", "twitter.com",
              "www.linkedin.com", "linkedin.com", "it.linkedin.com"}
# Piattaforme che non sono mai prospect di backlink (profili/social/store/dizionari).
SKIP_PLATFORMS = ("youtube.com", "facebook.com", "tiktok.com", "pinterest.", "imdb.com",
                  "play.google.com", "apps.apple.com", "spanishdict.com", "hotels.com",
                  "booking.com", "airbnb.", "tripadvisor.", "reddit.com", "wikipedia.org")
# Omonimi generici: qualunque dominio "myvilla*" / "my-villa*" che non sia myvilla.la.
HOMONYM_RE = re.compile(r"^(?:[a-z0-9-]+\.)*my-?villas?[a-z0-9-]*\.(?!la$)[a-z.]+$", re.I)
# Segnali che la pagina parla davvero di noi (evita "my villa" generico).
STRONG = re.compile(r"myvilla\.la|mezzalama", re.I)
WEAK_BRAND = re.compile(r"\bmy\s*villa\b", re.I)
WEAK_GEO = re.compile(r"los angeles|malibu|beverly hills|pacific palisades|bel air|brentwood", re.I)
WEAK_TOPIC = re.compile(r"concrete|cemento|italian|it'?s architecture", re.I)


# ── motori di ricerca ──────────────────────────────────────────────────
def brave_search(q: str, count: int = 20) -> list:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": q, "count": count, "safesearch": "off"})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "X-Subscription-Token": key,
                                               "User-Agent": bl.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"  [brave] {q!r}: {type(e).__name__}: {e}")
        return []
    out = []
    for item in (data.get("web") or {}).get("results") or []:
        out.append({"url": item.get("url"), "title": item.get("title", ""),
                    "snippet": item.get("description", ""), "via": "brave", "query": q})
    return out


def cse_search(q: str, num: int = 10) -> list:
    key, cx = os.environ.get("GOOGLE_CSE_API_KEY"), os.environ.get("GOOGLE_CSE_ENGINE_ID")
    if not (key and cx):
        return []
    url = "https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
        {"key": key, "cx": cx, "q": q, "num": num})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": bl.USER_AGENT}),
                                    timeout=20) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"  [cse] {q!r}: {type(e).__name__}: {e}")
        return []
    return [{"url": it.get("link"), "title": it.get("title", ""),
             "snippet": it.get("snippet", ""), "via": "cse", "query": q}
            for it in data.get("items") or []]


# ── classificazione ────────────────────────────────────────────────────
def classify(url: str) -> dict:
    status, html, final = bl.fetch(url)
    res = {"http": status, "final_url": final, "about_us": None, "linked": None,
           "rel": None, "anchor": None, "checked_at": bl.now_iso()}
    if status == 0:
        res["error"] = html[:120]
        return res
    text = bl.html_to_text(html)
    strong = bool(STRONG.search(text)) or bool(STRONG.search(html))
    # Match debole: brand + geografia LA + tema (cemento/italiano) tutti presenti.
    weak = bool(WEAK_BRAND.search(text)) and bool(WEAK_GEO.search(text)) and bool(WEAK_TOPIC.search(text))
    res["about_us"] = strong or weak
    links = bl.find_myvilla_links(html)
    res["linked"] = bool(links)
    if links:
        res["rel"] = "nofollow" if links[0]["nofollow"] else "dofollow"
        res["anchor"] = links[0]["anchor"]
    return res


def _load_mentions() -> dict:
    if bl.MENTIONS_JSON.exists():
        try:
            return json.loads(bl.MENTIONS_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"updated_at": None, "mentions": {}}


def _save_mentions(m: dict) -> None:
    m["updated_at"] = bl.now_iso()
    bl.MENTIONS_JSON.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def _add_prospect(data: dict, url: str, title: str, cls: dict) -> Optional[dict]:
    dom = bl.domain_of(url)
    pid = "mention-" + bl.slugify(dom.replace("www.", ""), 40)
    if pid in bl.prospect_ids(data):
        return None
    p = {
        "id": pid, "engine": "editorial",
        "name": f"Menzione senza link — {dom.replace('www.', '')}",
        "target_url": url, "contact_source": "to_verify", "contact": "",
        "sender": "office", "myvilla_url": "https://myvilla.la/", "anchor": "My Villa",
        "rel_expected": "dofollow", "status": "needs_contact",
        "notes": f"Trovata da mention_monitor il {bl.today()}: «{(title or '')[:80]}». "
                 "Chiedere di trasformare la menzione in link. Contatto: leggere sul sito.",
        "source_evidence_url": url,
        "found_at": bl.today(),
    }
    data["prospects"].append(p)
    return p


# ── main ───────────────────────────────────────────────────────────────
def run(*, dry_run: bool = False, recheck: bool = False) -> dict:
    bl.load_dotenv()
    have_brave = bool(os.environ.get("BRAVE_API_KEY"))
    have_cse = bool(os.environ.get("GOOGLE_CSE_API_KEY") and os.environ.get("GOOGLE_CSE_ENGINE_ID"))
    mentions = _load_mentions()
    known = mentions["mentions"]
    summary = {"queries": 0, "hits": 0, "new": 0, "linked": 0, "unlinked": 0,
               "prospects_added": 0, "skipped_not_about_us": 0}
    if not (have_brave or have_cse):
        print("mention_monitor: né BRAVE_API_KEY né GOOGLE_CSE_*: skip ricerca.")
    else:
        print(f"mention_monitor: brave={'on' if have_brave else 'off'} cse={'on' if have_cse else 'off'}")
    hits = []
    if have_brave or have_cse:
        for q in QUERIES:
            summary["queries"] += 1
            hits += brave_search(q) if have_brave else []
            hits += cse_search(q) if have_cse else []
    seen = set()
    new_urls = []
    for h in hits:
        u = (h.get("url") or "").split("#")[0].strip()
        if not u or u in seen:
            continue
        seen.add(u)
        dom = bl.domain_of(u)
        if bl.is_own_or_homonym(dom) or dom in SOCIAL_OWN or HOMONYM_RE.match(dom) \
                or any(s_ in dom for s_ in SKIP_PLATFORMS):
            continue
        summary["hits"] += 1
        if u in known:
            continue
        new_urls.append((u, h))
    if recheck:
        new_urls += [(u, {"title": m.get("title", ""), "via": "recheck", "query": ""})
                     for u, m in known.items()]
    print(f"  {summary['hits']} URL da motori, {len(new_urls)} da classificare")

    data = bl.load_prospects()
    for u, h in new_urls:
        cls = classify(u)
        entry = known.get(u) or {"first_seen": bl.today(), "title": h.get("title", ""),
                                 "via": h.get("via"), "query": h.get("query")}
        entry.update({"last_checked": cls["checked_at"], "http": cls["http"],
                      "about_us": cls["about_us"], "linked": cls["linked"],
                      "rel": cls["rel"], "anchor": cls["anchor"]})
        if cls.get("error"):
            entry["error"] = cls["error"]
        was_new = u not in known
        known[u] = entry
        if was_new:
            summary["new"] += 1
        if not cls["about_us"]:
            summary["skipped_not_about_us"] += 1
            print(f"  ~ {u[:80]} (non parla di noi / irraggiungibile)")
            continue
        if cls["linked"]:
            summary["linked"] += 1
            print(f"  ✓ linked [{cls['rel']}] {u[:80]}")
            if not dry_run and was_new:
                bl.ledger_append({"event": "mention", "url": u, "linked": True, "rel": cls["rel"]})
        else:
            summary["unlinked"] += 1
            print(f"  ✗ unlinked {u[:80]}")
            p = _add_prospect(data, u, entry.get("title", ""), cls)
            if p:
                summary["prospects_added"] += 1
                if not dry_run:
                    bl.ledger_append({"event": "mention", "url": u, "linked": False,
                                      "prospect_id": p["id"]})
    if not dry_run:
        _save_mentions(mentions)
        if summary["prospects_added"]:
            bl.save_prospects(data)
    print(f"mention_monitor: {json.dumps(summary)}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Monitor menzioni My Villa (Brave + CSE).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recheck", action="store_true", help="riverifica le menzioni già note")
    a = ap.parse_args(argv)
    try:
        run(dry_run=a.dry_run, recheck=a.recheck)
    except Exception as e:  # noqa: BLE001
        print(f"mention_monitor: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
