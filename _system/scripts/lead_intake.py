#!/usr/bin/env python3
"""
lead_intake.py — poller Gmail delle notifiche Formspree → registro → score → ack + alert.

Ad ogni run:
  1. cerca in info@myvilla.la le notifiche non ancora processate
     (from:formspree.io OR subject:"Private Briefing Request" OR
      subject:"New submission") senza la label `MV/Lead-Processed`
     (creata se manca);
  2. parser robusto delle coppie campo/valore (testo e HTML);
  3. lead_ledger.add (dedup email+10 min) → lead_score → lead_ack
     (ack al lead + alert interno) → label Gmail.
Idempotente: una notifica già etichettata non viene mai riprocessata;
un doppione entro 10 min non riceve un secondo ack. Exit 0 sempre.

Modalità:
    python3 lead_intake.py                       # run normale
    python3 lead_intake.py --dry-run             # niente invii, niente label
    python3 lead_intake.py --simulate            # notifica finta → parser → registro (temp) → ack/alert in dry-run
    python3 lead_intake.py --add-manual --json '{"first_name":"…","email":"…",…}'   # lead via mailto/telefono
    python3 lead_intake.py --ephemeral           # rail GitHub Actions: registro temporaneo, SOLO ack+alert+label
    python3 lead_intake.py --no-llm --max 20

Rail cloud (--ephemeral): il runner non ha il private dir → il record
completo NON viene conservato; ack+alert partono e la notifica riceve la
label. Il Mac/VPS ricostruisce il record al run successivo con
`--rebuild-from-label` (rilegge le notifiche etichettate degli ultimi 30
giorni e aggiunge al registro quelle mancanti, SENZA nuovo ack).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from html import unescape
from typing import Any, Optional

from lead_settings import get as cfg_get, private_dir

LABEL_NAME = "MV/Lead-Processed"
QUERY = ('(from:formspree.io OR subject:"Private Briefing Request" OR '
         'subject:"New submission") -label:"MV/Lead-Processed" newer_than:60d')
QUERY_LABELED = '(from:formspree.io OR subject:"Private Briefing Request" OR subject:"New submission") label:"MV/Lead-Processed" newer_than:30d'

# alias campo (lowercase, senza spazi/underscore) → campo canonico
_ALIASES = {
    "firstname": "first_name", "first": "first_name", "name": "name", "fullname": "name",
    "yourname": "name", "lastname": "last_name", "surname": "last_name",
    "email": "email", "emailaddress": "email", "youremail": "email", "replyto": "email", "_replyto": "email",
    "phone": "phone", "phonenumber": "phone", "telephone": "phone", "tel": "phone",
    "projecttype": "project_type", "project": "project_type", "interest": "project_type",
    "imexploring": "project_type", "iamexploring": "project_type", "exploring": "project_type",
    "timeline": "timeline", "timing": "timeline", "when": "timeline",
    "sitelocation": "site_location", "location": "site_location", "site": "site_location",
    "area": "site_location", "neighborhood": "site_location", "lot": "site_location",
    "message": "message", "projectdescription": "message", "description": "message",
    "tellusaboutyourproject": "message", "notes": "message", "details": "message",
    "howfound": "how_found", "howdidyoufindus": "how_found", "source": "how_found",
    "howdidyouhearaboutus": "how_found", "foundus": "how_found",
    "referredby": "referred_by", "referral": "referred_by",
    "consent": "consent_nurture", "nurture": "consent_nurture", "newsletter": "consent_nurture",
    "keepmeposted": "consent_nurture",
    "_subject": "_subject", "subject": "_subject", "_gotcha": "_gotcha", "gotcha": "_gotcha",
    "sourcepage": "source_page", "page": "source_page", "referrer": "referrer",
    "landingurl": "landing_url", "landing": "landing_url", "formid": "form_id",
    "utmsource": "utm_source", "utmmedium": "utm_medium", "utmcampaign": "utm_campaign",
    "utmterm": "utm_term", "utmcontent": "utm_content", "utmid": "utm_id",
    "oppref": "oppref", "gclid": "gclid", "pagetype": "page_type", "ctasrc": "cta_src",
    "eventid": "event_id",
}
_IGNORE_KEYS = {"submittedat", "date", "time", "ip", "ipaddress", "useragent", "formspree",
                "newsubmission", "viewsubmission", "unsubscribe", "manage", "http", "https",
                "you", "note", "reply", "to", "from", "sent"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_KEY_LINE_RE = re.compile(r"^\s*\*{0,2}([A-Za-z_][A-Za-z0-9 _'\-/?]{0,48}?)\*{0,2}\s*:\s*(.*?)\s*$")


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (k or "").strip().lower().replace(" ", ""))


def _canon(k: str) -> Optional[str]:
    nk = _norm_key(k)
    if nk in _ALIASES:
        return _ALIASES[nk]
    nk2 = nk.replace("_", "")
    if nk2 in _ALIASES:
        return _ALIASES[nk2]
    if nk.startswith("utm"):
        return "utm_" + nk.replace("utm", "", 1).replace("_", "")
    return None


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

def _html_to_lines(html: str) -> str:
    """HTML → testo a righe, preservando celle/righe di tabella e <br>."""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)</t[dh]>", ": ", h)             # <td>key</td><td>value</td> → "key: value"
    h = re.sub(r"(?i)</tr>|<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = unescape(h)
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip(" :\t") for l in h.splitlines()]
    return "\n".join(l for l in lines if l)


def parse_pairs(text: str) -> dict:
    """Coppie 'key: value' (stessa riga) o 'key:' + righe seguenti."""
    fields: dict = {}
    lines = [l.rstrip() for l in (text or "").splitlines()]
    i = 0
    current: Optional[str] = None
    buf: list = []

    def _flush():
        nonlocal current, buf
        if current:
            val = "\n".join(buf).strip()
            if current not in fields or not fields[current]:
                fields[current] = val
        current, buf = None, []

    while i < len(lines):
        line = lines[i]
        m = _KEY_LINE_RE.match(line)
        if m and _norm_key(m.group(1)) not in _IGNORE_KEYS and len(m.group(1)) <= 48:
            canon = _canon(m.group(1))
            if canon:
                _flush()
                current = canon
                val = m.group(2).strip()
                if val:
                    buf = [val]
                    # "key: value" su una riga: chiudi subito se la riga seguente è un'altra key
                i += 1
                continue
        if current is not None:
            if line.strip():
                buf.append(line.strip())
            elif buf:
                _flush()
        i += 1
    _flush()

    # Multi-riga: per message teniamo tutto; per gli altri solo la prima riga
    for k, v in list(fields.items()):
        if k != "message" and "\n" in v:
            fields[k] = v.split("\n", 1)[0].strip()
    # message: taglia code tipiche Formspree
    if fields.get("message"):
        msg = fields["message"]
        msg = re.split(r"(?i)\n(?:submitted at|view (?:this )?submission|unsubscribe|"
                       r"you can reply|manage (?:your )?notifications|reply to this)", msg)[0]
        fields["message"] = msg.strip()
    return fields


def parse_notification(plain: str, html: str = "", subject: str = "") -> dict:
    fields = parse_pairs(plain or "")
    if html:
        hf = parse_pairs(_html_to_lines(html))
        for k, v in hf.items():
            if v and not fields.get(k):
                fields[k] = v
    # name → first/last
    if fields.get("name") and not fields.get("first_name"):
        parts = fields["name"].split()
        fields["first_name"] = parts[0] if parts else ""
        fields["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else fields.get("last_name", "")
    fields.pop("name", None)
    # email fallback: primo indirizzo nel corpo che non sia formspree/myvilla
    if not fields.get("email"):
        for cand in _EMAIL_RE.findall((plain or "") + " " + _html_to_lines(html or "")):
            d = cand.lower().split("@", 1)[1]
            if "formspree" in d or "myvilla" in d:
                continue
            fields["email"] = cand
            break
    if fields.get("email"):
        m = _EMAIL_RE.search(fields["email"])
        fields["email"] = m.group(0).lower() if m else ""
    fields["_notification_subject"] = subject
    return fields


_TEST_RE = re.compile(r"(^|[^A-Za-z])TEST([^A-Za-z]|$)")


def is_test_lead(fields: dict, lead: Optional[dict] = None) -> bool:
    """True se l'invio è dichiaratamente un test: 'TEST' (maiuscolo) nel nome/cognome,
    '[TEST]' nel messaggio o nell'oggetto. Un test non riceve ack né alert e finisce nel
    registro con is_test=True e tier TEST (visibile nel desk, escluso dai conteggi)."""
    lead = lead or {}
    for k in ("first_name", "last_name"):
        if _TEST_RE.search(str(lead.get(k) or fields.get(k) or "")):
            return True
    msg = str(lead.get("message") or fields.get("message") or "")
    subj = str(fields.get("_subject") or "") + " " + str(fields.get("_notification_subject") or "")
    return "[TEST]" in msg.upper() or "[TEST]" in subj.upper()


def fields_to_lead(fields: dict, *, source: str = "formspree", form_id: str = "") -> dict:
    consent_raw = str(fields.get("consent_nurture", "")).strip().lower()
    consent = consent_raw in ("yes", "true", "on", "1", "checked", "y")
    attribution = {k: fields.get(k, "") for k in
                   ("source_page", "referrer", "landing_url", "utm_source", "utm_medium",
                    "utm_campaign", "utm_term", "utm_content", "utm_id", "oppref", "gclid",
                    "page_type", "cta_src", "event_id")}
    if not attribution.get("landing_url") and fields.get("_subject"):
        attribution["source_page"] = attribution.get("source_page") or fields["_subject"]
    return {
        "source": source,
        "form_id": fields.get("form_id") or form_id or str(cfg_get("formspree_id", "")),
        "first_name": fields.get("first_name", "").strip(),
        "last_name": fields.get("last_name", "").strip(),
        "email": fields.get("email", "").strip().lower(),
        "phone": fields.get("phone", "").strip(),
        "project_type": fields.get("project_type", "").strip(),
        "timeline": fields.get("timeline", "").strip(),
        "site_location": fields.get("site_location", "").strip(),
        "message": fields.get("message", "").strip(),
        "how_found": fields.get("how_found", "").strip(),
        "referred_by": fields.get("referred_by", "").strip(),
        "attribution": attribution,
        "consent": {"nurture": consent, "ts": ""},
    }


# --------------------------------------------------------------------------- #
# Gmail helpers
# --------------------------------------------------------------------------- #

def _gmail():
    from gmail_client import GmailClient, load_config
    return GmailClient(load_config())


def ensure_label(client, name: str = LABEL_NAME) -> str:
    svc = client.service
    resp = svc.users().labels().list(userId="me").execute()
    for lab in resp.get("labels", []):
        if lab.get("name") == name:
            return lab["id"]
    created = svc.users().labels().create(userId="me", body={
        "name": name, "labelListVisibility": "labelShow",
        "messageListVisibility": "show"}).execute()
    print(f"  [intake] label creata: {name}")
    return created["id"]


def _walk_parts(part: dict, mime: str) -> Optional[str]:
    body = part.get("body") or {}
    if part.get("mimeType") == mime and body.get("data"):
        return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
    for sub in part.get("parts") or []:
        r = _walk_parts(sub, mime)
        if r:
            return r
    return None


def message_bodies(msg: dict) -> tuple:
    payload = msg.get("payload") or {}
    return (_walk_parts(payload, "text/plain") or "", _walk_parts(payload, "text/html") or "")


def label_message(client, message_id: str, label_id: str) -> None:
    client.service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()


def unlabel_message(client, message_id: str, label_id: str) -> None:
    client.service.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": [label_id]}).execute()


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

def process_lead(lead_data: dict, *, dry_run: bool, use_llm: bool = True,
                 send: bool = True, ledger_add: bool = True) -> dict:
    """Registro → score → ack+alert. Ritorna un riassunto SENZA PII."""
    import lead_ledger
    from lead_score import score_lead
    from lead_ack import ack_and_alert

    summary: dict = {"ok": True, "duplicate": False, "acked": False, "alerted": False}
    lead = lead_ledger.add(lead_data) if ledger_add else dict(lead_data, lead_id=lead_data.get("lead_id") or "ephemeral")
    if lead.get("_duplicate_of"):
        summary.update({"duplicate": True, "lead_id": lead["_duplicate_of"]})
        return summary
    summary["lead_id"] = lead["lead_id"]

    score = score_lead(lead, use_llm=use_llm)
    lead["tier"], lead["score"] = score["tier"], score["score"]
    next_action = ("review (vendor/spam)" if score.get("vendor") else
                   "human check tier" if score.get("needs_human") else "founder reply")
    lead["next_action"] = next_action
    if ledger_add:
        lead_ledger.update(lead["lead_id"], tier=score["tier"], score=score["score"],
                           next_action=next_action, score_reasons=score.get("reasons", []),
                           needs_human=bool(score.get("needs_human")),
                           note=f"scored ({score.get('method')})")
    summary.update({"tier": score["tier"], "score": score["score"],
                    "needs_human": bool(score.get("needs_human")), "vendor": bool(score.get("vendor"))})

    if send and not score.get("vendor"):
        res = ack_and_alert(lead, score, dry_run=dry_run, update_ledger=ledger_add)
        summary["acked"] = bool((res.get("ack") or {}).get("ok"))
        summary["ack_reason"] = (res.get("ack") or {}).get("reason")
        summary["alerted"] = bool((res.get("alert") or {}).get("ok"))
        summary["alert_reason"] = (res.get("alert") or {}).get("reason")
    elif score.get("vendor"):
        summary["ack_reason"] = "skipped_vendor"
    return summary


def run(*, dry_run: bool = False, use_llm: bool = True, max_items: int = 25,
        ephemeral: bool = False, rebuild_from_label: bool = False) -> int:
    try:
        client = _gmail()
        label_id = ensure_label(client)
    except Exception as exc:  # noqa: BLE001
        print(f"  [intake] Gmail non disponibile: {type(exc).__name__}: {exc}")
        return 0

    query = QUERY_LABELED if rebuild_from_label else QUERY
    try:
        msgs = client.list_recent(query=query, max_results=max_items)
    except Exception as exc:  # noqa: BLE001
        print(f"  [intake] list failed: {exc}")
        return 0
    if not msgs:
        print("  [intake] nessuna notifica nuova.")
        return 0

    import lead_ledger
    known_emails_ts = None
    if rebuild_from_label:
        known_emails_ts = {(l.get("email"), l.get("received_at", "")[:16]) for l in lead_ledger.list_leads()}

    processed = 0
    for m in msgs:
        mid = m.get("id")
        try:
            msg = client.get_message(mid, fmt="full")
        except Exception as exc:  # noqa: BLE001
            print(f"  [intake] get {mid} failed: {exc}")
            continue
        if not rebuild_from_label and label_id in (msg.get("labelIds") or []):
            continue   # già processata (race con un altro rail)
        from gmail_client import extract_header
        subject = extract_header(msg, "Subject") or ""
        date_hdr = extract_header(msg, "Date") or ""
        plain, html = message_bodies(msg)
        fields = parse_notification(plain, html, subject)
        if fields.get("_gotcha"):
            print(f"  [intake] {mid}: honeypot pieno → spam, solo label")
            if not dry_run:
                label_message(client, mid, label_id)
            continue
        lead_data = fields_to_lead(fields, source="formspree")
        # received_at dall'header Date (fallback: internalDate)
        try:
            from email.utils import parsedate_to_datetime
            lead_data["received_at"] = parsedate_to_datetime(date_hdr).astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            try:
                lead_data["received_at"] = datetime.fromtimestamp(
                    int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                pass
        lead_data["gmail_message_id"] = mid
        lead_data["thread_id"] = msg.get("threadId", "")
        test = is_test_lead(fields, lead_data)
        if test:
            lead_data["is_test"] = True

        if rebuild_from_label:
            key = (lead_data.get("email"), lead_data.get("received_at", "")[:16])
            if key in known_emails_ts or not lead_data.get("email"):
                continue
            lead = lead_ledger.add(lead_data, dedup=False)
            if test:
                lead_ledger.update(lead["lead_id"], tier="TEST", score=0, next_action="test: ignore",
                                   note="rebuilt from Gmail label (test submission)")
                print(f"  [intake] rebuilt {lead['lead_id']} as TEST (no ack)")
                processed += 1
                continue
            from lead_score import score_lead
            sc = score_lead(lead, use_llm=use_llm)
            lead_ledger.update(lead["lead_id"], tier=sc["tier"], score=sc["score"],
                               next_action="founder reply", note="rebuilt from Gmail label (acked by cloud rail)")
            lead_ledger.update_state(lead["lead_id"], "acked", note="ack sent by cloud rail", force=True)
            print(f"  [intake] rebuilt {lead['lead_id']} tier={sc['tier']} (no new ack)")
            processed += 1
            continue

        if not lead_data.get("email"):
            print(f"  [intake] {mid}: nessuna email nel corpo → registro con needs_human, nessun ack "
                  f"(subject: {subject[:60]!r})")
            lead_data["message"] = (lead_data.get("message") or "") + f"\n[unparsed notification gmail:{mid}]"
            if not ephemeral:
                lead = lead_ledger.add(lead_data, dedup=False)
                lead_ledger.update(lead["lead_id"], tier="C", score=0, next_action="human: read Gmail notification",
                                   needs_human=True, note="unparsed")
            if not dry_run:
                label_message(client, mid, label_id)
            continue

        if test:
            # Invio di test dichiarato: registro (tier TEST), label, MAI ack né alert.
            print(f"  [intake] {mid}: TEST submission → registro come test, nessun ack/alert "
                  f"(campaign: {lead_data.get('attribution', {}).get('utm_campaign') or '-'})")
            if not ephemeral and not dry_run:
                lead = lead_ledger.add(lead_data, dedup=False)
                lead_ledger.update(lead["lead_id"], tier="TEST", score=0, next_action="test: ignore",
                                   note="test submission (no ack/alert)")
            if not dry_run:
                label_message(client, mid, label_id)
            processed += 1
            continue

        # CLAIM prima di processare: la label è il lock fra i rail (Mac /
        # VPS / Actions). Chi la mette per primo processa; se poi il
        # processing fallisce la togliamo così il run successivo riprova.
        if not dry_run:
            try:
                label_message(client, mid, label_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  [intake] {mid}: claim label failed ({exc}) → skip, retry next run")
                continue
        try:
            summary = process_lead(lead_data, dry_run=dry_run, use_llm=use_llm,
                                   ledger_add=not ephemeral)
        except Exception as exc:  # noqa: BLE001
            print(f"  [intake] {mid}: processing failed: {type(exc).__name__}: {exc} → label rimossa, retry")
            if not dry_run:
                try:
                    unlabel_message(client, mid, label_id)
                except Exception:  # noqa: BLE001
                    pass
            continue
        print(f"  [intake] {mid}: " + json.dumps(summary, ensure_ascii=False))
        if ephemeral:
            print("  [intake] EPHEMERAL rail: record non conservato; il Mac/VPS lo ricostruisce "
                  "con `lead_intake.py --rebuild-from-label`.")
        processed += 1
    print(f"  [intake] processate {processed} notifiche ({'dry-run' if dry_run else 'live'}).")
    return 0


# --------------------------------------------------------------------------- #
# Simulazione end-to-end (nessun Gmail)
# --------------------------------------------------------------------------- #

_SIM_PLAIN = """New submission from Private Briefing Request — myvilla.la

first_name:
Alex

last_name:
Example

email:
alex.example@example.com

phone:
+1 310 555 0100

project_type:
Rebuild after fire

timeline:
6-12 months

site_location:
Pacific Palisades

message:
We lost our house in January and own the lot on the bluff.
Looking at concrete this time; we'd like to understand your process.

how_found:
ChatGPT

_subject:
Private Briefing Request — myvilla.la

Submitted at: 2026-09-16 10:00 UTC
View submission: https://formspree.io/forms/mgoljyjl/submissions
"""

_SIM_HTML = """<html><body><h2>New submission</h2><table>
<tr><td><strong>First Name</strong></td><td>Alex</td></tr>
<tr><td>Email</td><td>alex.example@example.com</td></tr>
<tr><td>Project type</td><td>Rebuild after fire</td></tr>
<tr><td>Timeline</td><td>6-12 months</td></tr>
<tr><td>Site location</td><td>Pacific Palisades</td></tr>
<tr><td>Message</td><td>We lost our house in January and own the lot on the bluff.<br>Looking at concrete this time.</td></tr>
</table><p>Submitted at 2026-09-16</p></body></html>"""


def simulate(*, use_llm: bool = False) -> int:
    tmp = tempfile.mkdtemp(prefix="myvilla-sim-")
    os.environ[str(cfg_get("private_dir_env", "MYVILLA_PRIVATE_DIR"))] = tmp
    print(f"  [simulate] private dir temporaneo: {tmp}")
    f1 = parse_notification(_SIM_PLAIN, "", "Private Briefing Request — myvilla.la")
    f2 = parse_notification("", _SIM_HTML, "New submission from Private Briefing Request")
    print("  [simulate] parsed (plain): " + json.dumps({k: v for k, v in f1.items() if k not in ('email', 'phone')}, ensure_ascii=False))
    print("  [simulate] parsed (html):  " + json.dumps({k: v for k, v in f2.items() if k not in ('email', 'phone')}, ensure_ascii=False))
    assert f1.get("email") == "alex.example@example.com" and f2.get("email") == "alex.example@example.com"
    lead_data = fields_to_lead(f1)
    s1 = process_lead(lead_data, dry_run=True, use_llm=use_llm)
    print("  [simulate] lead 1: " + json.dumps(s1))
    s2 = process_lead(fields_to_lead(f2), dry_run=True, use_llm=use_llm)
    print("  [simulate] lead 2 (stessa email entro 10 min): " + json.dumps(s2))
    import lead_ledger
    lead = lead_ledger.get(s1["lead_id"])
    print("  [simulate] stato registro: state=%s tier=%s history=%s" % (
        lead.get("state"), lead.get("tier"), [h.get("event") for h in lead.get("history", [])]))
    log = os.path.join(tmp, "leads", "send_log.jsonl")
    if os.path.exists(log):
        for line in open(log, encoding="utf-8"):
            e = json.loads(line)
            print(f"  [simulate] send_log {e['kind']:10} ok={e['ok']} reason={e.get('reason')} → {e['to'][:3]}…  subject={e['subject']!r}")
            if e["kind"] == "lead_ack":
                print("  ---- ACK BODY ----\n" + e["body"] + "  ------------------")
    return 0


def add_manual(payload: dict, *, dry_run: bool, use_llm: bool, send: bool) -> int:
    payload.setdefault("source", "mailto")
    lead_data = fields_to_lead({**payload, "email": payload.get("email", "")}, source=payload["source"])
    for k in ("received_at", "how_found", "referred_by", "form_id"):
        if payload.get(k):
            lead_data[k] = payload[k]
    # attribuzione anche come dict annidato (oltre ai campi piatti utm_*)
    if isinstance(payload.get("attribution"), dict):
        lead_data["attribution"].update({k: v for k, v in payload["attribution"].items() if v})
    if payload.get("is_test") or is_test_lead(payload, lead_data):
        # test dichiarato: registro con tier TEST, mai ack/alert
        import lead_ledger
        lead_data["is_test"] = True
        lead = lead_ledger.add(lead_data, dedup=False)
        lead_ledger.update(lead["lead_id"], tier="TEST", score=0, next_action="test: ignore",
                           note="manual test submission (no ack/alert)")
        print("  [intake] manual TEST: " + json.dumps({"lead_id": lead["lead_id"], "tier": "TEST"}))
        return 0
    s = process_lead(lead_data, dry_run=dry_run, use_llm=use_llm, send=send)
    print("  [intake] manual: " + json.dumps(s, ensure_ascii=False))
    return 0


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Intake lead da Gmail (Formspree)")
    p.add_argument("--dry-run", action="store_true", help="niente invii, niente label")
    p.add_argument("--simulate", action="store_true", help="test end-to-end senza Gmail")
    p.add_argument("--add-manual", action="store_true", help="registra un lead manuale (--json)")
    p.add_argument("--json", help="dict JSON per --add-manual")
    p.add_argument("--no-ack", action="store_true", help="con --add-manual: registra senza ack/alert")
    p.add_argument("--ephemeral", action="store_true", help="rail cloud: registro temporaneo, solo ack+alert+label")
    p.add_argument("--rebuild-from-label", action="store_true", help="ricostruisce nel registro i lead già etichettati (senza ack)")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--max", type=int, default=25)
    a = p.parse_args(argv)
    try:
        if a.simulate:
            return simulate(use_llm=not a.no_llm)
        if a.add_manual:
            if not a.json:
                print("  [intake] --add-manual richiede --json")
                return 0
            return add_manual(json.loads(a.json), dry_run=a.dry_run, use_llm=not a.no_llm, send=not a.no_ack)
        if a.ephemeral:
            tmp = tempfile.mkdtemp(prefix="myvilla-ephemeral-")
            os.environ[str(cfg_get("private_dir_env", "MYVILLA_PRIVATE_DIR"))] = tmp
        return run(dry_run=a.dry_run, use_llm=not a.no_llm, max_items=a.max,
                   ephemeral=a.ephemeral, rebuild_from_label=a.rebuild_from_label)
    except Exception as exc:  # noqa: BLE001
        print(f"  [intake] ERRORE non fatale: {type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(_main())
