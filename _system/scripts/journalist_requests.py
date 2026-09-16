#!/usr/bin/env python3
"""
journalist_requests.py — legge da Gmail le newsletter di richieste giornalisti
(Source of Sources, Featured/HARO, Qwoted) instradate nella label
"MV/JournoRequests", estrae le singole richieste, le scora contro i cluster di
_system/config/radar-keywords.yml e, per quelle sopra soglia, genera una BOZZA
di risposta in voce Paolo. NON invia mai (kind=journo_reply resta umano).

Setup Gmail (una tantum, Ivo)
  1. Iscrivere info@myvilla.la a: sourceofsources.com (newsletter SOS),
     featured.com (HARO digest gratuito), qwoted.com (free plan).
  2. Gmail → Impostazioni → Filtri → Crea filtro:
        Da: (sourceofsources.com OR featured.com OR helpareporter.com OR qwoted.com)
        Azioni: "Applica etichetta: MV/JournoRequests", "Salta la Posta in arrivo (archivia)"
     La label viene creata dallo script al primo run se manca (scope gmail.modify).
  3. Lanciare lo script (launchd/cron quotidiano o a mano).

Come si lancia
  python3 _system/scripts/journalist_requests.py                # ultimi 3 giorni, max 3 bozze/settimana
  python3 _system/scripts/journalist_requests.py --days 7 --threshold 4
  python3 _system/scripts/journalist_requests.py --dry-run      # parse + score, nessuna bozza/ledger
  python3 _system/scripts/journalist_requests.py --setup-help   # stampa le istruzioni del filtro Gmail

Output
  _drafts/journo_requests/<date>-<slug>.md  (frontmatter: outlet, deadline, reply_to, status: review)
  ledger: eventi 'journo_seen' (dedup) e 'journo_draft'.
Tetto: 3 bozze per settimana mobile (budget umano). Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "backlinks"))
sys.path.insert(0, str(SCRIPT_DIR))
import backlinklib as bl  # noqa: E402

LABEL = "MV/JournoRequests"
OUT_DIR = bl.DRAFTS_DIR / "journo_requests"
KEYWORDS_YML = bl.SYSTEM_DIR / "config" / "radar-keywords.yml"
WEEKLY_CAP = 3
PLATFORM_DOMAINS = ("sourceofsources.com", "featured.com", "helpareporter.com", "qwoted.com")
SETUP_HELP = __doc__.split("Setup Gmail")[1].split("Come si lancia")[0]

STOP = {"the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "with", "home", "homes",
        "house", "california", "los", "angeles", "2026", "luxury", "new", "design"}


# ── Gmail ──────────────────────────────────────────────────────────────
def _gmail():
    from gmail_client import GmailClient, load_config  # type: ignore
    return GmailClient(load_config())


def ensure_label(client) -> Optional[str]:
    svc = client.service
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l.get("name") == LABEL:
            return l["id"]
    created = svc.users().labels().create(userId="me", body={
        "name": LABEL, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    print(f"  [gmail] label creata: {LABEL}")
    return created.get("id")


def _walk_parts(payload: dict):
    yield payload
    for p in payload.get("parts") or []:
        yield from _walk_parts(p)


def message_text(msg: dict) -> str:
    """Testo del messaggio: text/plain se c'è, altrimenti text/html ripulito."""
    plain, html = [], []
    for part in _walk_parts(msg.get("payload") or {}):
        data = ((part.get("body") or {}).get("data"))
        if not data:
            continue
        try:
            txt = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        mt = part.get("mimeType", "")
        if mt == "text/plain":
            plain.append(txt)
        elif mt == "text/html":
            html.append(txt)
    if plain:
        return "\n".join(plain)
    return bl.html_to_text("\n".join(html))


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ── parser tollerante ──────────────────────────────────────────────────
FIELD_RE = re.compile(r"^\s*(summary|subject|title|name|category|email|reply to|reply-to|media outlet|outlet|"
                      r"publication|deadline|query|request|requirements|details|topic)\s*:\s*(.*)$", re.I)
SPLIT_RE = re.compile(r"\n\s*(?:[-=_*]{4,}|#{2,}\s*\d+|\d+\)\s|Summary\s*:)", re.I)


def parse_requests(text: str, source: str) -> list:
    """Divide una newsletter in richieste. Ritorna dict con campi normalizzati."""
    text = text.replace("\r", "")
    # Spezza su separatori tipici; reinserisce "Summary:" che il regex consuma.
    chunks = re.split(r"\n(?=\s*Summary\s*:)", text, flags=re.I)
    if len(chunks) < 2:
        chunks = [c for c in SPLIT_RE.split(text) if c and len(c.strip()) > 60]
    out = []
    for ch in chunks:
        fields = {}
        cur = None
        for line in ch.splitlines():
            m = FIELD_RE.match(line)
            if m:
                cur = m.group(1).lower().replace("-", " ")
                fields[cur] = m.group(2).strip()
            elif cur in ("query", "request", "details", "requirements", "summary") and line.strip():
                fields[cur] = (fields.get(cur, "") + " " + line.strip()).strip()
        body = fields.get("query") or fields.get("request") or fields.get("details") or ""
        title = fields.get("summary") or fields.get("subject") or fields.get("title") or fields.get("topic") or ""
        if not (title or body):
            # fallback: primo rigo come titolo, resto come corpo
            lines = [l.strip() for l in ch.splitlines() if l.strip()]
            if len(lines) < 3:
                continue
            title, body = lines[0][:120], " ".join(lines[1:])[:1500]
        if len((title + body).strip()) < 40:
            continue
        outlet = fields.get("media outlet") or fields.get("outlet") or fields.get("publication") or ""
        reply = fields.get("email") or fields.get("reply to") or ""
        out.append({"title": title[:160], "body": body[:2500], "outlet": outlet[:120],
                    "category": fields.get("category", "")[:80], "deadline": fields.get("deadline", "")[:60],
                    "requirements": fields.get("requirements", "")[:400], "reply_to": reply.strip(),
                    "source": source,
                    "hash": hashlib.sha1((title + body[:300]).lower().encode()).hexdigest()[:12]})
    return out


# ── scoring ────────────────────────────────────────────────────────────
def load_clusters() -> list:
    import yaml
    d = yaml.safe_load(KEYWORDS_YML.read_text(encoding="utf-8")) or {}
    out = []
    for key, c in (d.get("clusters") or {}).items():
        out.append({"key": key, "label": c.get("label", key), "priority": int(c.get("priority", 2)),
                    "keywords": [k.lower() for k in c.get("keywords") or []]})
    geos = d.get("priority_geographies") or {}
    out.append({"key": "geo", "label": "Geography", "priority": 1,
                "keywords": [g.lower() for tier in geos.values() for g in tier]})
    return out


def score_request(req: dict, clusters: list) -> tuple:
    text = (req["title"] + " " + req["body"] + " " + req["category"]).lower()
    words = set(re.findall(r"[a-z][a-z\-']+", text)) - STOP
    total, hits = 0.0, []
    for c in clusters:
        w = {1: 3.0, 2: 2.0, 3: 1.0}.get(c["priority"], 1.0)
        for kw in c["keywords"]:
            if kw in text:
                total += w; hits.append(f"{c['label']}: {kw}")
                continue
            toks = [t for t in kw.split() if t not in STOP and len(t) > 3]
            if toks and sum(1 for t in toks if t in words) >= max(2, len(toks) - 1):
                total += w * 0.5; hits.append(f"{c['label']}~{kw}")
    # segnali forti sul nostro terreno
    for strong, w in (("concrete", 2), ("wildfire", 1.5), ("insur", 1.5), ("architect", 1), ("italian", 1.5), ("villa", 1)):
        if strong in text:
            total += w; hits.append(f"strong:{strong}")
    return round(total, 1), hits[:8]


# ── bozza ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You write short replies to journalist source requests on behalf of Paolo Mezzalama, founder of My Villa (myvilla.la), an Italian design team's project for private villas in exposed reinforced concrete in Los Angeles.
Voice: first person, Paolo speaking, warm, precise, no hype. Introduce him once as "architect registered in Italy and France" (never "licensed in California", never just "architect" without that qualifier).
Rules:
- 100 to 140 words in the body. Answer the journalist's actual question first.
- Exactly ONE data point, taken verbatim from the DATA list provided, with its source named in the sentence.
- Exactly ONE deep link to a myvilla.la article from the DATA list (the article the datum comes from), placed naturally, not as a CTA.
- Never claim My Villa has built or completed a villa; if a project is mentioned, call it a "concept design".
- Forbidden words: bunker, fortress, dream home, protect your family, survive the next fire. No fear-based language.
- No attachments offered. No press kit. Offer a short call only if the request asks for interviews.
- Write "IT'S Architecture" only if strictly needed, with a capital S.
- End with a one-line signature: "Paolo Mezzalama · My Villa · info@myvilla.la · myvilla.la".
Output only: a line "Subject: ..." then a blank line then the body."""


def draft_reply(req: dict, journal: list) -> Optional[str]:
    data_lines = bl.key_data_lines(journal[:40], max_per_article=2)
    # tieni i dati più pertinenti alla richiesta (overlap di parole)
    words = set(re.findall(r"[a-z]{4,}", (req["title"] + " " + req["body"]).lower())) - STOP
    scored = sorted(data_lines, key=lambda l: -len(words & set(re.findall(r"[a-z]{4,}", l.lower()))))
    data = "\n".join(scored[:12])
    prompt = (f"JOURNALIST REQUEST\nOutlet: {req['outlet'] or 'unknown'}\nCategory: {req['category']}\n"
              f"Deadline: {req['deadline']}\nTitle: {req['title']}\nQuery: {req['body']}\n"
              f"Requirements: {req['requirements']}\n\nDATA (choose one datum and its article link):\n{data}\n")
    return bl.claude_text(prompt, system=SYSTEM_PROMPT, tier="heavy", max_tokens=700)


def _weekly_count() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return sum(1 for e in bl.ledger_read() if e.get("event") == "journo_draft" and e.get("ts", "") >= cutoff)


def _seen_hashes() -> set:
    return {e.get("hash") for e in bl.ledger_read() if e.get("event") in ("journo_seen", "journo_draft")}


def save_draft(req: dict, score: float, hits: list, text: str, gmail_id: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = bl.slugify(req["title"] or req["outlet"] or req["hash"], 50)
    path = OUT_DIR / f"{bl.today()}-{slug}.md"
    reply_to = req["reply_to"] if any(d in req["reply_to"].lower() for d in PLATFORM_DOMAINS) else ""
    note = "" if reply_to else "  # indirizzo personale del giornalista: leggerlo nel messaggio Gmail (gmail_message_id), non nel repo"
    fm = (f"---\nkind: journo_reply\nstatus: review\noutlet: \"{req['outlet']}\"\ndeadline: \"{req['deadline']}\"\n"
          f"reply_to: \"{reply_to}\"{note}\ngmail_message_id: {gmail_id}\nsource: {req['source']}\n"
          f"score: {score}\nmatches: {json.dumps(hits)}\ncreated: {bl.today()}\nsender: paolo\n"
          f"request_hash: {req['hash']}\n---\n\n")
    body = (f"## Request\n\n**{req['title']}**\n\n{req['body']}\n\n"
            f"{('Requirements: ' + req['requirements']) if req['requirements'] else ''}\n\n## Draft reply\n\n{text}\n")
    path.write_text(fm + body, encoding="utf-8")
    return path


# ── main ───────────────────────────────────────────────────────────────
def run(*, days: int = 3, threshold: float = 4.0, dry_run: bool = False, max_drafts: Optional[int] = None) -> dict:
    bl.load_dotenv()
    summary = {"messages": 0, "requests": 0, "above_threshold": 0, "drafted": 0, "skipped_cap": 0}
    try:
        client = _gmail()
        ensure_label(client)
    except Exception as e:  # noqa: BLE001
        print(f"journalist_requests: Gmail non disponibile ({type(e).__name__}: {e}) → skip")
        return summary
    msgs = client.list_recent(query=f"label:{LABEL} newer_than:{days}d", max_results=40)
    summary["messages"] = len(msgs)
    print(f"journalist_requests: {len(msgs)} messaggi in {LABEL} (ultimi {days} gg)")
    clusters = load_clusters()
    journal = bl.load_journal(limit=60)
    seen = _seen_hashes()
    cap = WEEKLY_CAP if max_drafts is None else max_drafts
    used = _weekly_count()
    candidates = []
    for m in msgs:
        try:
            full = client.get_message(m["id"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! get_message {m.get('id')}: {e}")
            continue
        sender = _header(full, "From").lower()
        source = next((d for d in PLATFORM_DOMAINS if d in sender), "other")
        reqs = parse_requests(message_text(full), source)
        summary["requests"] += len(reqs)
        for r in reqs:
            if r["hash"] in seen:
                continue
            seen.add(r["hash"])
            sc, hits = score_request(r, clusters)
            if not dry_run:
                bl.ledger_append({"event": "journo_seen", "hash": r["hash"], "score": sc,
                                  "outlet": r["outlet"], "source": source})
            if sc >= threshold:
                candidates.append((sc, hits, r, m["id"]))
    candidates.sort(key=lambda t: -t[0])
    summary["above_threshold"] = len(candidates)
    for sc, hits, r, gid in candidates:
        print(f"  ★ {sc:>5} {r['outlet'][:28]:28} {r['title'][:70]}")
        if dry_run:
            continue
        if used >= cap:
            summary["skipped_cap"] += 1
            print(f"    ⏸ tetto settimanale ({cap}) raggiunto: resta in lista")
            continue
        text = draft_reply(r, journal)
        if not text:
            print("    ✗ bozza non generata")
            continue
        path = save_draft(r, sc, hits, text, gid)
        bl.ledger_append({"event": "journo_draft", "hash": r["hash"], "score": sc, "outlet": r["outlet"],
                          "draft": str(path.relative_to(bl.ROOT))})
        used += 1
        summary["drafted"] += 1
        print(f"    ✎ {path.name}")
    print(f"journalist_requests: {json.dumps(summary)}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Richieste giornalisti → bozze in voce Paolo (mai invio).")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=4.0)
    ap.add_argument("--max", type=int, default=None, help="override tetto settimanale (default 3)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--setup-help", action="store_true")
    a = ap.parse_args(argv)
    if a.setup_help:
        print(SETUP_HELP)
        return 0
    try:
        run(days=a.days, threshold=a.threshold, dry_run=a.dry_run, max_drafts=a.max)
    except Exception as e:  # noqa: BLE001
        print(f"journalist_requests: errore non fatale: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
