#!/usr/bin/env python3
"""
backlink_check.py — verifica settimanale dei backlink verso myvilla.la.

Cosa fa
  Per ogni prospect di _system/backlinks/prospects.yml che ha un target_url:
    GET (timeout 15, User-Agent onesto) → cerca <a href> verso myvilla.la →
    legge rel (dofollow/nofollow) e anchor → aggiorna status:
      trovato        → live  (+ evento 'live' se prima non lo era)
      era live, ora no → lost
      altrimenti     → status invariato (todo/sent/…), solo last_checked
  Ogni controllo è una riga nel ledger (_system/backlinks/ledger.jsonl).
  Scrive _system/backlinks/status.json (per prospect + KPI) e mette a
  disposizione digest_block() → HTML "Link e citazioni" per il digest.

Come si lancia
  python3 _system/scripts/backlink_check.py               # check completo
  python3 _system/scripts/backlink_check.py --only its-vision-site
  python3 _system/scripts/backlink_check.py --dry-run     # nessuna scrittura
  python3 _system/scripts/backlink_check.py --discover    # referral GA4: NON disponibile in locale → skip con messaggio
  python3 _system/scripts/backlink_check.py --bing        # Bing Webmaster API se BING_WMT_API_KEY in .env, altrimenti skip
  python3 _system/scripts/backlink_check.py --digest      # stampa il blocco HTML del digest e basta

Uso dal digest (publish_all_drafts.py)
  from backlink_check import digest_block
  html += digest_block()      # legge status.json + ledger, nessuna rete

Exit code sempre 0 (non deve mai fermare la pipeline).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
import backlinklib as bl  # noqa: E402

STALE_DAYS = 14          # richieste "sent" senza esito da segnalare nel digest
NEW_WINDOW_DAYS = 7      # "nuovi" e "persi" negli ultimi 7 giorni


# ── check di un singolo prospect ───────────────────────────────────────
def check_prospect(p: dict) -> dict:
    url = (p.get("target_url") or "").strip()
    out = {"id": p.get("id"), "target_url": url, "http": 0, "links": [],
           "error": None, "checked_at": bl.now_iso()}
    if not url:
        out["error"] = "no target_url"
        return out
    status, html, final = bl.fetch(url)
    out["http"] = status
    out["final_url"] = final
    if status == 0:
        out["error"] = html[:160]
        return out
    if status >= 400:
        out["error"] = f"HTTP {status}"
        # Pagine che bloccano i bot (403) possono comunque contenere il link:
        # se il body c'è lo analizziamo lo stesso.
    out["links"] = bl.find_myvilla_links(html)
    return out


def _ledger_live_state(pid: str) -> Optional[str]:
    """'live' / 'lost' secondo l'ultimo evento di stato nel ledger, None se nessuno.
    Serve perché il workflow settimanale committa status.json e ledger ma NON
    prospects.yml: senza questa lettura ogni run rivedrebbe lo stesso link
    come 'nuovo'."""
    last = None
    for e in bl.ledger_read():
        if e.get("prospect_id") == pid and e.get("event") in ("live", "lost"):
            last = e["event"]
    return last


def _apply(p: dict, res: dict, dry_run: bool) -> dict:
    """Aggiorna il prospect in-place secondo l'esito; ritorna l'evento ledger."""
    before = p.get("status", "todo")
    ledger_state = _ledger_live_state(p.get("id"))
    if ledger_state == "live" and before not in ("live", "lost"):
        before = "live"
    after = before
    found = bool(res["links"])
    if found:
        after = "live"
        first = res["links"][0]
        p["live_rel"] = "nofollow" if first["nofollow"] else "dofollow"
        p["live_anchor"] = first["anchor"]
        p["live_href"] = first["href"]
        if before != "live":
            p["live_since"] = bl.today()
    elif before == "live" and res["http"] and res["http"] < 400:
        # La pagina risponde ma il link non c'è più → perso. Un 4xx/5xx o un
        # errore di rete NON declassa (potrebbe essere un blocco anti-bot).
        after = "lost"
        p["lost_at"] = bl.today()
    p["status"] = after
    p["last_checked"] = res["checked_at"]
    p["last_http"] = res["http"]
    event = {"event": "check", "prospect_id": p.get("id"), "http": res["http"],
             "links_found": len(res["links"]), "status_before": before,
             "status_after": after}
    if res.get("error"):
        event["error"] = res["error"]
    if found:
        event["rel"] = p["live_rel"]
        event["anchor"] = p["live_anchor"]
    if not dry_run:
        bl.ledger_append(event)
        if after != before:
            bl.ledger_append({"event": after, "prospect_id": p.get("id"),
                              "target_url": res["target_url"]})
    return event


# ── sorgenti opzionali ─────────────────────────────────────────────────
def discover_ga4() -> list:
    """Referral GA4: l'export non è disponibile in locale (nessuna credenziale
    Analytics Data API nel repo). Flag predisposto: quando arriverà un service
    account, qui si legge il report 'sessionSource' e si propongono prospect."""
    print("  [discover] Referral GA4 non disponibili in locale "
          "(serve Analytics Data API + service account): skip.")
    return []


def discover_bing(site: str = "https://myvilla.la/") -> list:
    """Bing Webmaster Tools API — GetUrlLinks (inbound). Richiede BING_WMT_API_KEY."""
    bl.load_dotenv()
    key = os.environ.get("BING_WMT_API_KEY")
    if not key:
        print("  [bing] BING_WMT_API_KEY assente in .env: skip.")
        return []
    import urllib.parse
    import urllib.request
    base = "https://ssl.bing.com/webmaster/api.svc/json/"
    found = []
    try:
        q = urllib.parse.urlencode({"siteUrl": site, "apikey": key})
        with urllib.request.urlopen(base + "GetLinkCounts?" + q, timeout=20) as r:
            counts = json.load(r).get("d") or {}
        print(f"  [bing] link counts: {json.dumps(counts)[:200]}")
        q = urllib.parse.urlencode({"siteUrl": site, "link": site, "page": 0, "apikey": key})
        with urllib.request.urlopen(base + "GetUrlLinks?" + q, timeout=20) as r:
            data = json.load(r).get("d") or {}
        for row in data.get("Links") or []:
            u = row.get("Url") or ""
            if u and not bl.is_own_or_homonym(bl.domain_of(u)):
                found.append({"url": u, "anchor": row.get("AnchorText", ""), "source": "bing"})
        print(f"  [bing] {len(found)} inbound URL")
    except Exception as e:  # noqa: BLE001
        print(f"  [bing] errore API: {type(e).__name__}: {e} → skip")
    return found


# ── status.json + digest ───────────────────────────────────────────────
def _kpi(prospects: list) -> dict:
    live = [p for p in prospects if p.get("status") == "live"]
    domains = {bl.domain_of(p.get("target_url") or "") for p in live}
    return {
        "prospects": len(prospects),
        "live": len(live),
        "live_domains": len({d for d in domains if d}),
        "live_dofollow": sum(1 for p in live if p.get("live_rel") == "dofollow"),
        "sent": sum(1 for p in prospects if p.get("status") == "sent"),
        "drafted": sum(1 for p in prospects if p.get("status") == "drafted"),
        "needs_contact": sum(1 for p in prospects if p.get("status") == "needs_contact"),
        "lost": sum(1 for p in prospects if p.get("status") == "lost"),
        "todo": sum(1 for p in prospects if p.get("status") == "todo"),
    }


def _stale_requests(prospects: list, days: int = STALE_DAYS) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = []
    for p in prospects:
        if p.get("status") == "sent" and (p.get("sent_at") or "9999") <= cutoff:
            out.append({"id": p["id"], "name": p.get("name"), "sent_at": p.get("sent_at")})
    return out


def _recent_events(kind: str, days: int = NEW_WINDOW_DAYS) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [e for e in bl.ledger_read() if e.get("event") == kind and e.get("ts", "") >= cutoff]


def _next_actions(prospects: list, stale: list) -> list:
    """Tre azioni concrete, in ordine di resa: follow-up scaduti, network todo, editoriale."""
    actions = []
    for s in stale[:1]:
        actions.append(f"Follow-up a «{s['name']}» (richiesta del {s['sent_at']}, nessun esito)")
    order = ["network", "editorial", "press_it", "podcast", "directory", "asset", "journo", "award"]
    for eng in order:
        for p in prospects:
            if len(actions) >= 3:
                break
            if p.get("engine") == eng and p.get("status") == "todo":
                verb = {"network": "Inviare la richiesta partner a",
                        "editorial": "Preparare la submission per",
                        "press_it": "Mandare il comunicato a",
                        "podcast": "Proporre Paolo come ospite a",
                        "directory": "Aprire il profilo su",
                        "asset": "Offrire il tracker a",
                        "journo": "Attivare l'account su",
                        "award": "Decidere l'iscrizione a"}[eng]
                actions.append(f"{verb} «{p.get('name')}»")
        if len(actions) >= 3:
            break
    return actions[:3]


def write_status(prospects: list, results: list, dry_run: bool) -> dict:
    stale = _stale_requests(prospects)
    status = {
        "generated_at": bl.now_iso(),
        "kpi": _kpi(prospects),
        "new_live_7d": [e.get("prospect_id") for e in _recent_events("live")],
        "lost_7d": [e.get("prospect_id") for e in _recent_events("lost")],
        "stale_requests": stale,
        "next_actions": _next_actions(prospects, stale),
        "prospects": [{
            "id": p.get("id"), "engine": p.get("engine"), "name": p.get("name"),
            "status": p.get("status"), "target_url": p.get("target_url"),
            "last_checked": p.get("last_checked"), "last_http": p.get("last_http"),
            "live_rel": p.get("live_rel"), "live_anchor": p.get("live_anchor"),
            "sent_at": p.get("sent_at"),
        } for p in prospects],
        "last_run_results": [{k: v for k, v in r.items() if k != "links"} | {"links_found": len(r.get("links", []))}
                             for r in results],
    }
    if not dry_run:
        bl.STATUS_JSON.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    return status


def digest_block(status: Optional[dict] = None) -> str:
    """HTML (tabella inline, stile digest) 'Link e citazioni'. Nessuna rete.
    Se status.json manca ritorna stringa vuota (il digest non cambia)."""
    if status is None:
        if not bl.STATUS_JSON.exists():
            return ""
        try:
            status = json.loads(bl.STATUS_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return ""
    k = status.get("kpi", {})
    font = "font-family:-apple-system,sans-serif;"
    lab = ('<div style="' + font + 'font-size:11px;letter-spacing:0.12em;'
           'text-transform:uppercase;color:#888;font-weight:600;">Link e citazioni</div>')
    kpi = (f'<div style="{font}font-size:13px;color:#2b2b2b;margin-top:6px;">'
           f'<strong>{k.get("live_domains", 0)}</strong> domini live '
           f'({k.get("live_dofollow", 0)} dofollow) · '
           f'<strong style="color:#5C6B4F;">+{len(status.get("new_live_7d", []))}</strong> nuovi 7gg · '
           f'<strong style="color:#a85d3f;">−{len(status.get("lost_7d", []))}</strong> persi 7gg · '
           f'{k.get("sent", 0)} richieste aperte · {k.get("needs_contact", 0)} senza contatto'
           f' · obiettivo 25 entro 03/2027</div>')
    rows = ""
    for pid in status.get("new_live_7d", []):
        rows += f'<div style="{font}font-size:13px;color:#5C6B4F;">✓ nuovo link: {escape(str(pid))}</div>'
    for pid in status.get("lost_7d", []):
        rows += f'<div style="{font}font-size:13px;color:#a85d3f;">✗ link perso: {escape(str(pid))}</div>'
    stale = status.get("stale_requests", [])
    if stale:
        names = ", ".join(escape(str(s.get("name") or s.get("id"))) for s in stale[:5])
        rows += (f'<div style="{font}font-size:13px;color:#a85d3f;margin-top:4px;">'
                 f'⏳ {len(stale)} richieste senza risposta da più di {STALE_DAYS} giorni: {names}</div>')
    acts = status.get("next_actions", [])
    if acts:
        rows += f'<div style="{font}font-size:12px;color:#888;margin-top:8px;">Prossime 3 azioni</div>'
        for a in acts:
            rows += f'<div style="{font}font-size:13px;color:#2b2b2b;">• {escape(a)}</div>'
    gen = escape(str(status.get("generated_at", ""))[:16].replace("T", " "))
    foot = f'<div style="{font}font-size:11px;color:#aaa;margin-top:6px;">backlink_check {gen} UTC</div>'
    return (f'<tr><td style="padding:18px 32px 6px 32px;">{lab}{kpi}{rows}{foot}</td></tr>')


# ── main ───────────────────────────────────────────────────────────────
def run(*, only: Optional[str] = None, dry_run: bool = False, discover: bool = False,
        bing: bool = False, workers: int = 6) -> dict:
    data = bl.load_prospects()
    prospects = data.get("prospects", [])
    targets = [p for p in prospects if p.get("target_url") and (not only or p.get("id") == only)]
    print(f"backlink_check: {len(targets)} prospect da controllare"
          f"{' (dry-run)' if dry_run else ''}")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(check_prospect, targets):
            results.append(res)
    by_id = {p.get("id"): p for p in prospects}
    for res in results:
        p = by_id.get(res["id"])
        if not p:
            continue
        ev = _apply(p, res, dry_run)
        mark = "✓ LIVE" if res["links"] else ("✗ " + (res.get("error") or "no link"))
        print(f"  {res['id']:32} http={res['http']:<3} {mark}"
              + (f" [{ev.get('rel')}] «{ev.get('anchor','')[:40]}»" if res["links"] else ""))
    if discover:
        discover_ga4()
    if bing:
        for hit in discover_bing():
            print(f"  [bing] inbound: {hit['url']}")
            if not dry_run:
                bl.ledger_append({"event": "bing_inbound", "url": hit["url"], "anchor": hit.get("anchor", "")})
    if not dry_run:
        bl.save_prospects(data)
    status = write_status(prospects, results, dry_run)
    k = status["kpi"]
    print(f"KPI: live={k['live']} domini={k['live_domains']} dofollow={k['live_dofollow']} "
          f"sent={k['sent']} needs_contact={k['needs_contact']}")
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica backlink verso myvilla.la.")
    ap.add_argument("--only", help="id di un solo prospect")
    ap.add_argument("--dry-run", action="store_true", help="niente scritture (prospects/ledger/status)")
    ap.add_argument("--discover", action="store_true", help="referral GA4 (non disponibile in locale: skip)")
    ap.add_argument("--bing", action="store_true", help="Bing Webmaster API (serve BING_WMT_API_KEY)")
    ap.add_argument("--digest", action="store_true", help="stampa il blocco HTML del digest da status.json")
    args = ap.parse_args(argv)
    try:
        if args.digest:
            print(digest_block())
            return 0
        run(only=args.only, dry_run=args.dry_run, discover=args.discover, bing=args.bing)
    except Exception as e:  # noqa: BLE001
        print(f"backlink_check: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
