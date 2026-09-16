#!/usr/bin/env python3
"""
lead_ledger.py — registro PRIVATO dei lead (fuori git).

Dove: `$MYVILLA_PRIVATE_DIR` (default ~/.myvilla-private) →
    leads/leads.jsonl   append-only, una riga per evento (add / update)
    leads/index.json    stato corrente per lead_id (ricostruibile dal jsonl)

Un lead = dict con: lead_id (hash email+ts), received_at, source
(formspree|lead_api|mailto|manual|chat), form_id, first_name, last_name,
email, phone, project_type, timeline, site_location, message, how_found,
referred_by, attribution {source_page, referrer, landing_url, utm_*},
consent {nurture: bool, ts}, tier, score, state, next_action, thread_id,
history[] (eventi con ts).

API:
    from lead_ledger import add, update_state, get, list_leads, export_csv
    lead = add({...})                       # dedup: stesso email entro 10 min → lo stesso lead
    update_state(lead_id, "acked", note=…)  # transizione validata
    get(lead_id) / list_leads(state="new")
    export_csv(path)

CLI:
    python3 lead_ledger.py add --json '{"first_name":"…","email":"…"}'
    python3 lead_ledger.py list [--state new]
    python3 lead_ledger.py show <lead_id>
    python3 lead_ledger.py set-state <lead_id> <state> [--note "…"]
    python3 lead_ledger.py export --csv /path/out.csv
    python3 lead_ledger.py --purge           # retention 24 mesi (lead.retention_months)

Nessun dato personale finisce nel repo: questo file scrive SOLO nel
private dir. Exit 0 sempre (errori stampati, mai eccezioni non gestite
in CLI).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from lead_settings import get as cfg_get, lead_states, private_dir

# Transizioni ammesse fra stati (lead.states in lead_settings.yml).
# Ogni stato può sempre andare a do_not_contact / lost / dormant.
_TRANSITIONS: dict = {
    "new": {"acked", "founder_replied", "call_booked", "lost", "dormant", "do_not_contact"},
    "acked": {"founder_replied", "call_booked", "briefing_done", "lost", "dormant", "do_not_contact"},
    "founder_replied": {"call_booked", "briefing_done", "won", "lost", "dormant", "do_not_contact"},
    "call_booked": {"briefing_done", "founder_replied", "won", "lost", "dormant", "do_not_contact"},
    "briefing_done": {"won", "lost", "dormant", "call_booked", "do_not_contact"},
    "won": {"do_not_contact"},
    "lost": {"dormant", "acked", "founder_replied", "do_not_contact"},
    "dormant": {"acked", "founder_replied", "call_booked", "lost", "do_not_contact"},
    "do_not_contact": set(),
}

DEDUP_WINDOW_MIN = 10

_FIELDS = [
    "lead_id", "received_at", "source", "form_id", "first_name", "last_name",
    "email", "phone", "project_type", "timeline", "site_location", "message",
    "how_found", "referred_by", "attribution", "consent", "tier", "score",
    "state", "next_action", "thread_id", "history",
]


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def leads_dir() -> Path:
    d = private_dir() / "leads"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def jsonl_path() -> Path:
    return leads_dir() / "leads.jsonl"


def index_path() -> Path:
    return leads_dir() / "index.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_email(e: Optional[str]) -> str:
    return (e or "").strip().lower()


def make_lead_id(email: str, ts: str) -> str:
    return hashlib.sha1(f"{_norm_email(email)}|{ts}".encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Storage primitives
# --------------------------------------------------------------------------- #

def _append_event(event: dict) -> None:
    p = jsonl_path()
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _load_index() -> dict:
    p = index_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return rebuild_index()


def _save_index(index: dict) -> None:
    p = index_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def rebuild_index() -> dict:
    """Ricostruisce index.json rileggendo tutto il jsonl (fonte di verità)."""
    index: dict = {}
    p = jsonl_path()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            lid = ev.get("lead_id")
            if not lid:
                continue
            if kind == "add":
                index[lid] = ev.get("lead") or {}
            elif kind == "update" and lid in index:
                lead = index[lid]
                lead.update(ev.get("changes") or {})
                lead.setdefault("history", []).append(ev.get("history_item") or {
                    "ts": ev.get("ts"), "event": "update"})
            elif kind == "purge" and lid in index:
                index.pop(lid, None)
    _save_index(index)
    return index


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _blank_lead() -> dict:
    return {
        "lead_id": "", "received_at": "", "source": "manual", "form_id": "",
        "first_name": "", "last_name": "", "email": "", "phone": "",
        "project_type": "", "timeline": "", "site_location": "", "message": "",
        "how_found": "", "referred_by": "",
        "attribution": {"source_page": "", "referrer": "", "landing_url": "",
                        "utm_source": "", "utm_medium": "", "utm_campaign": "",
                        "utm_term": "", "utm_content": ""},
        "consent": {"nurture": False, "ts": ""},
        "tier": "", "score": None, "state": "new", "next_action": "",
        "thread_id": "", "history": [],
    }


def find_recent_duplicate(email: str, *, window_min: int = DEDUP_WINDOW_MIN,
                          index: Optional[dict] = None) -> Optional[dict]:
    """Lead con la stessa email ricevuto negli ultimi `window_min` minuti."""
    em = _norm_email(email)
    if not em:
        return None
    idx = index if index is not None else _load_index()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    best = None
    for lead in idx.values():
        if _norm_email(lead.get("email")) != em:
            continue
        try:
            ts = datetime.fromisoformat(str(lead.get("received_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff and (best is None or ts > datetime.fromisoformat(best["received_at"])):
            best = lead
    return best


def find_by_email(email: str, *, index: Optional[dict] = None) -> list:
    em = _norm_email(email)
    idx = index if index is not None else _load_index()
    return [l for l in idx.values() if _norm_email(l.get("email")) == em]


def add(data: dict, *, dedup: bool = True) -> dict:
    """Registra un lead. Ritorna il record (con `_duplicate_of` se dedup)."""
    index = _load_index()
    email = _norm_email(data.get("email"))
    if dedup and email:
        dup = find_recent_duplicate(email, index=index)
        if dup:
            note = {"ts": _now(), "event": "duplicate_submission",
                    "source": data.get("source", "")}
            _update(dup["lead_id"], {}, note, index=index)
            out = dict(dup)
            out["_duplicate_of"] = dup["lead_id"]
            return out

    lead = _blank_lead()
    for k in _FIELDS:
        if k in data and data[k] is not None:
            if k in ("attribution", "consent") and isinstance(data[k], dict):
                lead[k].update({kk: vv for kk, vv in data[k].items() if vv is not None})
            elif k != "history":
                lead[k] = data[k]
    # utm_* / referrer passati "piatti" → attribution
    for k, v in data.items():
        if k.startswith("utm_") and v:
            lead["attribution"][k] = v
    for k in ("source_page", "referrer", "landing_url"):
        if data.get(k):
            lead["attribution"][k] = data[k]
    lead["email"] = email
    lead["received_at"] = lead["received_at"] or _now()
    lead["state"] = lead.get("state") or "new"
    if lead["state"] not in lead_states():
        lead["state"] = "new"
    lead["lead_id"] = lead.get("lead_id") or make_lead_id(email, lead["received_at"])
    # collisione improbabile ma gestita
    while lead["lead_id"] in index:
        lead["lead_id"] = make_lead_id(email, lead["received_at"] + "+" + lead["lead_id"])
    if isinstance(lead.get("consent"), dict) and lead["consent"].get("nurture") and not lead["consent"].get("ts"):
        lead["consent"]["ts"] = lead["received_at"]
    lead["history"] = [{"ts": _now(), "event": "created", "source": lead["source"]}]
    _append_event({"event": "add", "ts": _now(), "lead_id": lead["lead_id"], "lead": lead})
    index[lead["lead_id"]] = lead
    _save_index(index)
    return lead


def _update(lead_id: str, changes: dict, history_item: dict, *,
            index: Optional[dict] = None) -> Optional[dict]:
    idx = index if index is not None else _load_index()
    lead = idx.get(lead_id)
    if lead is None:
        return None
    lead.update(changes)
    lead.setdefault("history", []).append(history_item)
    _append_event({"event": "update", "ts": _now(), "lead_id": lead_id,
                   "changes": changes, "history_item": history_item})
    _save_index(idx)
    return lead


def update(lead_id: str, **changes: Any) -> Optional[dict]:
    """Aggiorna campi liberi (tier, score, thread_id, next_action, …)."""
    note = changes.pop("note", None)
    item = {"ts": _now(), "event": "update", "fields": sorted(changes.keys())}
    if note:
        item["note"] = note
    return _update(lead_id, changes, item)


def can_transition(old: str, new: str) -> bool:
    if new not in lead_states():
        return False
    if old == new:
        return True
    return new in _TRANSITIONS.get(old or "new", set())


def update_state(lead_id: str, new_state: str, *, note: str = "",
                 force: bool = False, next_action: Optional[str] = None) -> Optional[dict]:
    """Transizione di stato validata. `force=True` salta la matrice."""
    idx = _load_index()
    lead = idx.get(lead_id)
    if lead is None:
        return None
    old = lead.get("state") or "new"
    if not force and not can_transition(old, new_state):
        raise ValueError(f"transizione non valida: {old} → {new_state}")
    changes: dict = {"state": new_state, "state_changed_at": _now()}
    if next_action is not None:
        changes["next_action"] = next_action
    if new_state == "founder_replied":
        changes["founder_replied_at"] = _now()
    item = {"ts": _now(), "event": "state", "from": old, "to": new_state}
    if note:
        item["note"] = note
    return _update(lead_id, changes, item, index=idx)


def get(lead_id: str) -> Optional[dict]:
    return _load_index().get(lead_id)


def list_leads(state: Optional[str] = None, *, tier: Optional[str] = None) -> list:
    idx = _load_index()
    out = []
    for lead in idx.values():
        if state and lead.get("state") != state:
            continue
        if tier and lead.get("tier") != tier:
            continue
        out.append(lead)
    out.sort(key=lambda l: l.get("received_at", ""), reverse=True)
    return out


def export_csv(path: Path) -> int:
    rows = list_leads()
    cols = [c for c in _FIELDS if c not in ("history",)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for l in rows:
            w.writerow([json.dumps(l.get(c), ensure_ascii=False)
                        if isinstance(l.get(c), (dict, list)) else l.get(c, "")
                        for c in cols])
    return len(rows)


def purge(*, months: Optional[int] = None, dry_run: bool = False) -> list:
    """Retention: elimina dall'indice i lead più vecchi di N mesi
    (stati finali o dormienti). Il jsonl riceve un evento `purge`."""
    months = months or int(cfg_get("lead.retention_months", 24) or 24)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)
    idx = _load_index()
    gone = []
    for lid, lead in list(idx.items()):
        try:
            ts = datetime.fromisoformat(str(lead.get("received_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            gone.append(lid)
            if not dry_run:
                _append_event({"event": "purge", "ts": _now(), "lead_id": lid})
                idx.pop(lid, None)
    if not dry_run and gone:
        _save_index(idx)
    return gone


def last_contact_at(lead: dict) -> Optional[datetime]:
    """Ultimo evento "di contatto" (ack/founder/call) dalla history."""
    best = None
    for h in lead.get("history") or []:
        ev = h.get("event", "")
        if ev in ("created", "acked", "state", "founder_reply_seen", "nudge"):
            try:
                ts = datetime.fromisoformat(str(h.get("ts")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if best is None or ts > best:
                best = ts
    return best


def redact(lead: dict) -> dict:
    """Vista SENZA PII (per digest / alert / log di repo)."""
    return {
        "lead_id": lead.get("lead_id"),
        "received_at": lead.get("received_at"),
        "source": lead.get("source"),
        "first_name": lead.get("first_name"),
        "project_type": lead.get("project_type"),
        "timeline": lead.get("timeline"),
        "site_location": lead.get("site_location"),
        "how_found": lead.get("how_found"),
        "tier": lead.get("tier"),
        "score": lead.get("score"),
        "state": lead.get("state"),
        "next_action": lead.get("next_action"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Registro privato dei lead (fuori git)")
    p.add_argument("--purge", action="store_true", help="applica la retention (lead.retention_months)")
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add"); a.add_argument("--json", required=True, help="dict JSON del lead")
    a.add_argument("--no-dedup", action="store_true")
    l = sub.add_parser("list"); l.add_argument("--state"); l.add_argument("--tier")
    l.add_argument("--full", action="store_true", help="mostra anche email/telefono")
    s = sub.add_parser("show"); s.add_argument("lead_id")
    st = sub.add_parser("set-state"); st.add_argument("lead_id"); st.add_argument("state")
    st.add_argument("--note", default=""); st.add_argument("--force", action="store_true")
    e = sub.add_parser("export"); e.add_argument("--csv", required=True)
    sub.add_parser("rebuild-index")
    args = p.parse_args(argv)

    try:
        if args.purge:
            gone = purge(dry_run=args.dry_run)
            print(f"[ledger] purge {'(dry-run) ' if args.dry_run else ''}→ {len(gone)} lead: {gone}")
            return 0
        if args.cmd == "add":
            data = json.loads(args.json)
            lead = add(data, dedup=not args.no_dedup)
            if lead.get("_duplicate_of"):
                print(f"[ledger] duplicato entro {DEDUP_WINDOW_MIN} min → {lead['_duplicate_of']}")
            else:
                print(f"[ledger] aggiunto lead_id={lead['lead_id']} state={lead['state']}")
            print(json.dumps(redact(lead), indent=2, ensure_ascii=False))
        elif args.cmd == "list":
            rows = list_leads(args.state, tier=args.tier)
            print(f"[ledger] {len(rows)} lead ({index_path()})")
            for r in rows:
                v = r if args.full else redact(r)
                print(f"  {v['lead_id']}  {r.get('state','?'):16} {str(r.get('tier') or '-'):2} "
                      f"{r.get('received_at','')[:16]}  {r.get('first_name','')} "
                      f"{r.get('last_name','') if args.full else ''}  "
                      f"{r.get('project_type','')} · {r.get('site_location','')}")
        elif args.cmd == "show":
            lead = get(args.lead_id)
            print(json.dumps(lead, indent=2, ensure_ascii=False) if lead else "[ledger] lead non trovato")
        elif args.cmd == "set-state":
            lead = update_state(args.lead_id, args.state, note=args.note, force=args.force)
            print("[ledger] lead non trovato" if lead is None else
                  f"[ledger] {args.lead_id} → {lead['state']}")
        elif args.cmd == "export":
            n = export_csv(Path(args.csv))
            print(f"[ledger] esportati {n} lead in {args.csv} (file con PII: NON committare)")
        elif args.cmd == "rebuild-index":
            idx = rebuild_index()
            print(f"[ledger] index ricostruito: {len(idx)} lead")
        else:
            p.print_help()
    except Exception as exc:  # noqa: BLE001
        print(f"[ledger] ERRORE: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
