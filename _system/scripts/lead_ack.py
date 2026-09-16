#!/usr/bin/env python3
"""
lead_ack.py — conferma al lead (kind=lead_ack) + alert interno (kind=lead_alert).

Testo dell'ack: FISSO, da `_system/knowledge/lead_ack_voice.md` (nessun LLM).
Firma: `signatures.prospects` (applicata dal policy layer per i kind lead_*).
Se `brand.booking_url` è vuoto l'ack chiede due finestre (mattine LA,
Teams); altrimenti inserisce il link.

Alert: a `alerts.to`, SOLO nome, tipo progetto, area, timeline, tier,
how_found, lead_id e il comando per aprire il lead nel desk. Mai email o
telefono del lead (pii_level: minimal). Esente dai tetti giornalieri.

    from lead_ack import build_ack, send_ack, send_alert, ack_and_alert
    ack_and_alert(lead, score)       # → {"ack": SendResult-dict, "alert": SendResult-dict}

CLI:
    python3 lead_ack.py --simulate                 # stampa ack+alert di un lead finto (niente invio)
    python3 lead_ack.py --lead-id <id> [--dry-run] # ack+alert per un lead del registro
    python3 lead_ack.py --lead-id <id> --alert-only
Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from lead_settings import get as cfg_get, SYSTEM_DIR

VOICE_PATH = SYSTEM_DIR / "knowledge" / "lead_ack_voice.md"

_FALLBACK_SUBJECT = "Your private briefing with My Villa"
_FALLBACK_BODY = (
    "Dear {first_name},\n\nThank you for reaching out. Your request for a private "
    "briefing has arrived.\n\nPaolo Mezzalama, our founder, will reply to you "
    "personally {response_promise}.\n\n{scheduling}\n\nDo you already have a site "
    "or lot in mind?\n\nWarm regards,"
)
_FALLBACK_SCHED_NO = ("To make the call easy to arrange, simply reply with two windows "
                      "that suit you. Los Angeles mornings work best for a Teams call with Paolo.")
_FALLBACK_SCHED_LINK = "You can pick a slot directly here: {booking_url}"


def _sections(md: str) -> dict:
    """'## Title' → testo (strip). Ignora tutto prima del primo '##'."""
    out: dict = {}
    cur = None
    buf: list = []
    for line in md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip().lower()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def load_voice() -> dict:
    try:
        secs = _sections(VOICE_PATH.read_text(encoding="utf-8"))
    except OSError:
        secs = {}
    return {
        "subject": secs.get("subject") or _FALLBACK_SUBJECT,
        "body": secs.get("body") or _FALLBACK_BODY,
        "sched_no": secs.get("scheduling (no booking link)") or _FALLBACK_SCHED_NO,
        "sched_link": secs.get("scheduling (booking link)") or _FALLBACK_SCHED_LINK,
    }


def build_ack(lead: dict) -> tuple:
    """→ (subject, body) senza firma (la aggiunge send_email)."""
    v = load_voice()
    booking = (cfg_get("brand.booking_url", "") or "").strip()
    first = (lead.get("first_name") or "").strip() or "there"
    fields = {
        "first_name": first,
        "response_promise": cfg_get("canonical.response_promise", "within one business day"),
        "booking_url": booking,
        "contact_email": cfg_get("brand.contact_email", "info@myvilla.la"),
    }
    sched = (v["sched_link"] if booking else v["sched_no"]).format(**fields)
    fields["scheduling"] = sched
    body = v["body"].format(**fields)
    subject = v["subject"].format(**fields)
    return subject, body


def build_alert(lead: dict, score: Optional[dict] = None) -> tuple:
    """Alert interno senza PII di contatto."""
    score = score or {}
    tier = lead.get("tier") or score.get("tier") or "?"
    first = lead.get("first_name") or "(no name)"
    project = lead.get("project_type") or "-"
    area = lead.get("site_location") or "-"
    timeline = lead.get("timeline") or "-"
    how = lead.get("how_found") or "-"
    lid = lead.get("lead_id") or "?"
    subject = f"[Lead {tier}] {first} · {project} · {area}"
    lines = [
        f"New lead — tier {tier} (score {score.get('score', lead.get('score', '-'))})",
        "",
        f"Name:          {first}",
        f"Project:       {project}",
        f"Area:          {area}",
        f"Timeline:      {timeline}",
        f"How found:     {how}",
        f"Source:        {lead.get('source') or '-'}",
        f"Received:      {lead.get('received_at') or '-'}",
        f"Ack sent:      {'yes' if lead.get('_ack_ok') else 'no / pending'}",
    ]
    if score.get("needs_human"):
        lines.append("Needs human:   YES — check tier before replying")
    if score.get("reasons"):
        lines.append("Reasons:       " + "; ".join(str(r) for r in score["reasons"][:5]))
    lines += [
        "",
        "Open the lead (contact details are in the private ledger, never in email):",
        f"  python3 _system/scripts/lead_ledger.py show {lid}",
        "  Panel: http://127.0.0.1:8787/#leads  (VPS: https://content.myvilla.la/#leads)",
        "",
        "Founder SLA: reply personally within "
        f"{cfg_get('lead.sla_hours_founder', 24)}h.",
    ]
    return subject, "\n".join(lines)


def _send(kind: str, to: str, subject: str, body: str, *, dry_run: bool,
          skip_signature: bool = False) -> dict:
    from send_email import send_raw, load_config
    cfg = load_config()
    if dry_run:
        cfg.dry_run = True
    res = send_raw(to=to, subject=subject, body=body, config=cfg, kind=kind,
                   skip_signature=skip_signature, skip_rate_limit=(kind == "lead_alert"))
    return asdict(res)


def send_ack(lead: dict, *, dry_run: bool = False) -> dict:
    subject, body = build_ack(lead)
    to = (lead.get("email") or "").strip()
    if not to:
        return {"ok": False, "reason": "no_email", "dry_run": dry_run}
    return _send("lead_ack", to, subject, body, dry_run=dry_run)


def send_alert(lead: dict, score: Optional[dict] = None, *, dry_run: bool = False) -> dict:
    subject, body = build_alert(lead, score)
    recipients = cfg_get("alerts.to", ["info@myvilla.la"]) or ["info@myvilla.la"]
    if isinstance(recipients, str):
        recipients = [recipients]
    return _send("lead_alert", ", ".join(recipients), subject, body,
                 dry_run=dry_run, skip_signature=True)


def ack_and_alert(lead: dict, score: Optional[dict] = None, *, dry_run: bool = False,
                  update_ledger: bool = True) -> dict:
    """Ack al lead, poi alert interno; aggiorna il registro (stato acked, thread)."""
    out: dict = {"ack": None, "alert": None}
    ack = send_ack(lead, dry_run=dry_run)
    out["ack"] = ack
    lead["_ack_ok"] = bool(ack.get("ok"))
    if update_ledger and lead.get("lead_id"):
        try:
            import lead_ledger
            if ack.get("ok"):
                changes = {"thread_id": ack.get("thread_id") or lead.get("thread_id") or ""}
                lead_ledger._update(lead["lead_id"], changes, {
                    "ts": ack.get("timestamp"), "event": "acked",
                    "dry_run": bool(ack.get("dry_run")), "reason": ack.get("reason")})
                if not ack.get("dry_run"):
                    lead_ledger.update_state(lead["lead_id"], "acked",
                                             next_action="founder reply", force=True)
            else:
                lead_ledger._update(lead["lead_id"], {}, {
                    "ts": ack.get("timestamp"), "event": "ack_failed",
                    "reason": ack.get("reason"), "error": ack.get("error")})
        except Exception as exc:  # noqa: BLE001
            print(f"  [lead_ack] ledger update failed: {exc}", file=sys.stderr)
    out["alert"] = send_alert(lead, score, dry_run=dry_run)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _fake_lead() -> dict:
    return {"lead_id": "simulated0001", "first_name": "Alex", "last_name": "Example",
            "email": "alex.example@example.com", "project_type": "Rebuild after fire",
            "timeline": "6-12 months", "site_location": "Pacific Palisades",
            "message": "We lost our home on the bluff and are ready to rebuild in concrete.",
            "how_found": "ChatGPT", "source": "formspree", "tier": "A",
            "received_at": "2026-09-16T10:00:00+00:00"}


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ack + alert lead")
    p.add_argument("--simulate", action="store_true", help="stampa ack+alert di un lead finto, nessun invio")
    p.add_argument("--lead-id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--alert-only", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.simulate:
            lead = _fake_lead()
            s, b = build_ack(lead)
            print("=== ACK (kind=lead_ack) ===\nSubject:", s, "\n\n" + b)
            print(f"\n[{len(b.split())} words body, signature added by policy layer: "
                  f"{cfg_get('signatures.prospects','').strip().splitlines()[0]}]")
            s2, b2 = build_alert(lead, {"tier": "A", "score": 88, "reasons": ["tier-1 area", "≤12 months"]})
            print("\n=== ALERT (kind=lead_alert) → " + ", ".join(cfg_get("alerts.to", [])) + " ===\nSubject:", s2, "\n\n" + b2)
            return 0
        if a.lead_id:
            import lead_ledger
            lead = lead_ledger.get(a.lead_id)
            if not lead:
                print("[lead_ack] lead non trovato")
                return 0
            score = {"tier": lead.get("tier"), "score": lead.get("score")}
            if a.alert_only:
                print(json.dumps(send_alert(lead, score, dry_run=a.dry_run), indent=2))
            else:
                print(json.dumps(ack_and_alert(lead, score, dry_run=a.dry_run), indent=2))
            return 0
        p.print_help()
    except Exception as exc:  # noqa: BLE001
        print(f"[lead_ack] ERRORE: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
