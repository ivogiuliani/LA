#!/usr/bin/env python3
"""
send_draft.py — invia (o simula) una bozza email Markdown con frontmatter
attraverso il policy layer send_email.py: firma, rate limit, tetto per kind,
kill-switch dry_run_kinds, gate CAN-SPAM, blacklist bounce, log JSONL.

È lo strumento della Fase 3 "io scrivo, tu invii": le bozze prodotte da
Claude (data PR, richieste backlink, memo ai broker...) restano file .md;
Ivo le rilegge e le manda con un comando. Nessuno script invia da solo.

Formato bozza (Markdown con frontmatter YAML):
  ---
  to: newsroom@example.com          # obbligatorio (casella generica verificata)
  subject: Eight words minimum here  # obbligatorio
  kind: outreach                     # default outreach (vedi lead_settings.budgets_per_day)
  cc: a@b.com, c@d.com               # opzionale
  attachments: [_system/outreach/attachments/MyVilla_Fact_Sheet.pdf]  # opzionale
  signature: lisa | prospects | none # default lisa (stampa); prospects = "The partners at My Villa"
  send_after: 2026-09-18             # opzionale: prima di quella data non parte
  thread_id: 1a0040c78bdb2436       # opzionale: RISPOSTA nello stesso thread Gmail (kind reply, firma Lisa)
  in_reply_to: <Message-ID>          # opzionale con thread_id: header RFC-822 dell'ultimo messaggio del giornalista
  ---
  corpo in testo semplice (righe vuote = paragrafi). Niente firma: la mette il policy layer.

Uso:
  python3 _system/scripts/send_draft.py <file.md>              # anteprima + esito dei gate, NON invia
  python3 _system/scripts/send_draft.py <file.md> --send       # invio reale
  python3 _system/scripts/send_draft.py <cartella> --send      # tutti i .md (rispetta send_after, salta i già inviati)
  python3 _system/scripts/send_draft.py <file.md> --send --to me@x.com   # prova su un indirizzo tuo

Dopo un invio riuscito scrive nel frontmatter `sent: <ISO ts>` e `message_id:`
(idempotente: non rimanda una bozza già `sent`). Exit 0 sempre.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_draft(path: Path) -> tuple[dict, str, str]:
    """Ritorna (frontmatter dict, body, raw frontmatter text)."""
    import yaml
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        raise ValueError("frontmatter mancante (--- ... ---)")
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():].strip("\n")
    return fm, body, m.group(1)


def _signature_override(fm: dict) -> tuple[str | None, bool]:
    """(signature_override, skip_signature) secondo il campo `signature`."""
    sig = str(fm.get("signature") or "lisa").strip().lower()
    if sig == "none":
        return None, True
    if sig == "prospects":
        try:
            import lead_settings
            txt = (lead_settings.get("signatures.prospects") or "").strip()
            return (txt or "The partners at My Villa\ninfo@myvilla.la · myvilla.la"), False
        except Exception:  # noqa: BLE001
            return "The partners at My Villa\ninfo@myvilla.la · myvilla.la", False
    return None, False


def _blacklisted(to: str):
    try:
        from reply_monitor import is_invalid_address
        return bool(is_invalid_address(to))
    except Exception:  # noqa: BLE001
        return None


def _mark_sent(path: Path, raw_fm: str, message_id: str | None) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = path.read_text(encoding="utf-8")
    add = f"sent: {ts}\nmessage_id: {message_id or ''}\n"
    new_fm = raw_fm.rstrip("\n") + "\n" + add
    path.write_text(text.replace(raw_fm, new_fm, 1), encoding="utf-8")


def handle(path: Path, *, send: bool, to_override: str | None, force: bool) -> dict:
    rec = {"file": str(path), "ok": False, "reason": None}
    try:
        fm, body, raw_fm = parse_draft(path)
    except Exception as e:  # noqa: BLE001
        rec["reason"] = f"parse: {e}"
        return rec
    to = (to_override or fm.get("to") or "").strip()
    subject = str(fm.get("subject") or "").strip()
    kind = str(fm.get("kind") or "outreach").strip()
    if fm.get("sent") and not force:
        rec["reason"] = f"già inviata il {fm['sent']} (usa --force per rimandare)"
        return rec
    if not to:
        rec["reason"] = "campo `to` vuoto: usa il form/contatto in contact_note o passa --to"
        rec["contact_note"] = fm.get("contact_note")
        return rec
    if not subject or not body:
        rec["reason"] = "subject o corpo vuoti"
        return rec
    sa = fm.get("send_after")
    if sa and not force:
        sa_d = sa if isinstance(sa, date) else date.fromisoformat(str(sa))
        if date.today() < sa_d:
            rec["reason"] = f"send_after {sa_d}: non ancora"
            return rec
    words = len(body.split())
    rec.update({"to": to, "subject": subject, "kind": kind, "words": words})

    import send_email
    cfg = send_email.load_config()
    sig_override, skip_sig = _signature_override(fm)
    attachments = [ROOT / a for a in (fm.get("attachments") or [])]
    missing = [str(a) for a in attachments if not a.exists()]
    if missing:
        rec["reason"] = f"allegati mancanti: {missing}"
        return rec
    cc = fm.get("cc")
    if isinstance(cc, list):
        cc = ", ".join(cc)

    if not send:
        bstate = send_email.kind_budget_state(kind, cfg)
        try:
            import lead_settings
            lead_cfg = lead_settings.load_settings()
        except Exception:  # noqa: BLE001
            lead_cfg = {}
        gates = {
            "config_dry_run": bool(getattr(cfg, "dry_run", False)),
            "kind_dry_run": kind in set(lead_cfg.get("dry_run_kinds") or []),
            "commercial_kind": kind in set(lead_cfg.get("commercial_kinds") or []),
            "postal_address_set": bool((lead_cfg.get("brand") or {}).get("postal_address")),
            "blacklisted": _blacklisted(to),
            "budget": bstate,
        }
        rec.update({"ok": True, "reason": "preview" + (" (risposta in thread)" if fm.get("thread_id") else ""), "gates": gates,
                    "signature": "none" if skip_sig else ("prospects" if sig_override else "lisa"),
                    "preview": body[:600]})
        return rec

    thread_id = str(fm.get("thread_id") or "").strip()
    if thread_id:
        # Risposta dentro un thread esistente: passa da send_raw con thread_id
        # (kind "reply" → tetto reply, firma Lisa salvo override).
        res = send_email.send_raw(to=to, subject=subject, body=body, cc=cc, config=cfg,
                                  kind="reply" if kind == "outreach" else kind,
                                  thread_id=thread_id,
                                  in_reply_to=(str(fm.get("in_reply_to") or "").strip() or None),
                                  references=(str(fm.get("references") or "").strip() or None),
                                  attachments=attachments or None,
                                  signature_override=sig_override, skip_signature=skip_sig)
    else:
        res = send_email.send_raw(to=to, subject=subject, body=body, cc=cc, config=cfg,
                                  kind=kind, attachments=attachments or None,
                                  signature_override=sig_override, skip_signature=skip_sig)
    rec.update({"ok": res.ok, "dry_run": res.dry_run, "reason": res.reason,
                "error": res.error, "message_id": res.message_id})
    if res.ok and not res.dry_run:
        _mark_sent(path, raw_fm, res.message_id)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Invia bozze Markdown via policy layer send_email")
    ap.add_argument("path", help="file .md o cartella")
    ap.add_argument("--send", action="store_true", help="invio reale (default: anteprima)")
    ap.add_argument("--to", dest="to_override", help="forza il destinatario (test)")
    ap.add_argument("--force", action="store_true", help="ignora sent/send_after")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    p = Path(args.path)
    files = sorted(p.glob("*.md")) if p.is_dir() else [p]
    files = [f for f in files if f.name.lower() != "readme.md"]
    out = []
    for f in files:
        rec = handle(f, send=args.send, to_override=args.to_override, force=args.force)
        out.append(rec)
        if not args.json:
            tag = "OK" if rec.get("ok") else "--"
            print(f"[{tag}] {f.name}: {rec.get('reason')}"
                  + (f" → {rec.get('to')} | {rec.get('subject')} ({rec.get('words')} parole)" if rec.get("to") else ""))
            if rec.get("gates"):
                g = rec["gates"]
                print(f"     gate: config_dry_run={g['config_dry_run']} kind_dry_run={g['kind_dry_run']} "
                      f"commercial={g['commercial_kind']} postal_address={g['postal_address_set']} "
                      f"blacklisted={g['blacklisted']} budget={g['budget'].get('sent_today', '?')}/{g['budget'].get('limit', '∞')}")
            if rec.get("contact_note"):
                print(f"     contatto alternativo: {rec['contact_note']}")
            if rec.get("error"):
                print(f"     errore: {rec['error']}")
    if args.json:
        import json
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
