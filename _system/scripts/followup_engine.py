#!/usr/bin/env python3
"""
followup_engine.py — automated 3-touch cadence for journalist outreach.

Strategy (configured with the user):

  Touch 1 — cold pitch (already done by the radar/draft generator)
  Touch 2 — at +7 days, "data bump" — short follow-up with one concrete
            data point the cold pitch didn't have
  Touch 3 — at +14 days from Touch 2, "founder call" — close the loop
            and offer a 30-min call with Paolo
  STOP after Touch 3 — never spam a journalist past 3 touches

Driver loop:
  1. Hydrate ledger from send_log.jsonl  (populate historical contacts)
  2. Mark replies/bounces  (status: replied | bounced | exhausted)
  3. Find contacts whose next_touch_at <= today AND status == in_cadence
  4. Generate body via Claude (Sonnet) for each due touch
  5. Send via send_email.send_draft, respecting the 10/h rate limit
  6. Save ledger; return a dict the digest builder can render

Output dict (same shape as _send_ready_pitches):
  {
    "sent":    [ {to, subject, body, touch_n, publication, ...}, ... ],
    "skipped": [ {to, reason}, ... ],
    "errors":  [ {to, error}, ... ],
  }

Run standalone:  python3 followup_engine.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
OUTREACH_DIR = SYSTEM_DIR / "outreach"
RADAR_REPORTS_DIR = SYSTEM_DIR / "radar" / "reports"

LEDGER_PATH = OUTREACH_DIR / "contact_ledger.json"


def _load_dotenv() -> None:
    """Read .env into os.environ if present. Same logic as
    publish_all_drafts._load_dotenv but standalone so this script
    can run without that module being importable."""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=" not in raw:
                continue
            k, _, v = raw.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            # Overwrite if missing OR if the existing value is empty
            # (Claude desktop seeds some env vars as empty by default,
            # which our previous check failed to override).
            if k and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v
    except OSError:
        pass


_load_dotenv()

# Cadence: Touch 2 at +7d from Touch 1, Touch 3 at +14d from Touch 2.
# `next_touch_offset_days[n]` = days from previous touch to schedule Touch n+1.
CADENCE_OFFSETS = {
    1: 7,   # after Touch 1, schedule Touch 2 in 7 days
    2: 14,  # after Touch 2, schedule Touch 3 in 14 days
    3: None,  # after Touch 3, stop
}

# Per-run cap. We have a 10/h rate limit on the Gmail sender so this
# stays under it even if multiple things send in the same hour
# (digest mail, new cold pitches, follow-ups). Spillover slides to
# next day's run.
MAX_FOLLOWUPS_PER_RUN = 8

# Internal domains — same as publish_all_drafts._INTERNAL_DOMAINS.
# Kept here too so the engine doesn't depend on importing that module.
_INTERNAL_DOMAINS = {
    "me.com", "myvilla.la", "gmail.com", "example.com",
}


# ─────────────────────────────────────────────────────────────────────
# Ledger I/O
# ─────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def _is_internal(addr: str) -> bool:
    return _domain(addr) in _INTERNAL_DOMAINS


def _load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {"version": 1, "contacts": {}}
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [ledger] read error: {e} — starting fresh")
        return {"version": 1, "contacts": {}}


def _save_ledger(ledger: dict) -> None:
    OUTREACH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(LEDGER_PATH)


# ─────────────────────────────────────────────────────────────────────
# Hydrate from history
# ─────────────────────────────────────────────────────────────────────

def _read_send_log() -> list[dict]:
    log_path = OUTREACH_DIR / "send_log.jsonl"
    if not log_path.exists():
        return []
    rows = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not r.get("ok") or r.get("dry_run"):
            continue
        to = (r.get("to") or "").strip().lower()
        if not to or _is_internal(to):
            continue
        rows.append(r)
    return rows


def _read_replies_log() -> tuple[set[str], set[str]]:
    """Returns (replied_thread_ids, bounced_thread_ids).

    Internal-domain "replies" (Ivo replying to himself) are filtered
    out — they don't represent journalist responses.
    """
    replies_path = OUTREACH_DIR / "replies_log.jsonl"
    replied = set()
    bounced = set()
    if not replies_path.exists():
        return replied, bounced
    for raw in replies_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            continue
        fr = (r.get("from_address") or "").lower()
        tid = r.get("thread_id")
        if not tid:
            continue
        if "mailer-daemon" in fr or "postmaster" in fr:
            bounced.add(tid)
        elif _is_internal(fr):
            continue
        else:
            replied.add(tid)
    return replied, bounced


def _read_invalid_addresses() -> set[str]:
    path = OUTREACH_DIR / "invalid_addresses.json"
    if not path.exists():
        return set()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return set((d.get("addresses") or {}).keys())
    except (OSError, json.JSONDecodeError):
        return set()


def _load_radar_drafts() -> dict[str, dict]:
    """Map email → original draft from the most recent radar reports.

    Used to recover the cold pitch body + source URL when we need to
    pass them as context to the LLM follow-up generator. Radar reports
    accumulate over time, so we scan all of them and keep the most
    recent draft per email.
    """
    drafts: dict[str, dict] = {}
    if not RADAR_REPORTS_DIR.exists():
        return drafts
    files = sorted(RADAR_REPORTS_DIR.glob("radar_*.json"))
    for fp in files:
        try:
            radar = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for it in (radar.get("qualified") or []):
            d = it.get("draft") or {}
            if (d.get("type") or "").lower() != "email":
                continue
            em = (d.get("contact_email") or "").strip().lower()
            if not em:
                continue
            # later files win (sorted ascending)
            drafts[em] = {
                "subject": d.get("subject", ""),
                "body": d.get("body", ""),
                "title": (it.get("title") or "")[:200],
                "url": it.get("url") or "",
                "publication": (it.get("publication") or "").strip(),
            }
    return drafts


def hydrate_ledger(ledger: dict, *, retroactive_recovery: bool = True) -> None:
    """Populate the ledger from send_log + replies + radar drafts.

    Idempotent: re-running picks up new sends without rewriting old
    touches.

    If `retroactive_recovery` is True (default for the FIRST run), any
    contact whose last touch is >= 7 days ago and who hasn't replied
    or bounced gets `next_touch_at = today` so they enter the cadence
    immediately. After the first run this still adds new sends to the
    ledger but doesn't fast-track them — normal cadence applies.
    """
    sends = _read_send_log()
    replied_threads, bounced_threads = _read_replies_log()
    bounced_addrs = _read_invalid_addresses()
    radar_drafts = _load_radar_drafts()

    contacts = ledger.setdefault("contacts", {})
    now = datetime.now(timezone.utc)

    for s in sends:
        to = s["to"].lower()
        ts_iso = s.get("timestamp") or _now_iso()
        try:
            ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        except Exception:
            ts = now

        contact = contacts.get(to)
        if contact is None:
            # New contact — anchor on this first send
            draft = radar_drafts.get(to, {})
            contact = {
                "email": to,
                "publication": draft.get("publication", ""),
                "first_seen_at": ts_iso,
                "touches": [],
                "next_touch_at": None,
                "status": "in_cadence",
                "source_url": draft.get("url", ""),
                "source_title": draft.get("title", ""),
                "cold_pitch_body": draft.get("body", ""),
                "cold_pitch_subject": draft.get("subject", s.get("subject", "")),
            }
            contacts[to] = contact

        # Has this send already been recorded as a touch?
        message_id = s.get("message_id")
        already_logged = any(
            t.get("message_id") == message_id and message_id
            for t in contact["touches"]
        )
        if not already_logged:
            n = len(contact["touches"]) + 1
            contact["touches"].append({
                "n": n,
                "type": "cold" if n == 1 else (
                    "data" if n == 2 else
                    "founder_call" if n == 3 else "extra"
                ),
                "sent_at": ts_iso,
                "message_id": message_id,
                "thread_id": s.get("thread_id"),
                "subject": s.get("subject", ""),
            })
            # Recompute next_touch_at based on the last touch
            last_n = contact["touches"][-1]["n"]
            offset = CADENCE_OFFSETS.get(last_n)
            if offset is None:
                contact["next_touch_at"] = None
                contact["status"] = "exhausted"
            else:
                next_dt = ts + timedelta(days=offset)
                contact["next_touch_at"] = next_dt.strftime("%Y-%m-%d")

        # Update status based on replies/bounces
        thread_ids = {t.get("thread_id") for t in contact["touches"]
                      if t.get("thread_id")}
        if to in bounced_addrs or any(tid in bounced_threads for tid in thread_ids):
            contact["status"] = "bounced"
            contact["next_touch_at"] = None
        elif any(tid in replied_threads for tid in thread_ids):
            contact["status"] = "replied"
            contact["next_touch_at"] = None

    # Retroactive recovery: bring stale in_cadence contacts to "due today"
    if retroactive_recovery:
        cutoff = now - timedelta(days=7)
        for to, contact in contacts.items():
            if contact["status"] != "in_cadence":
                continue
            if not contact["touches"]:
                continue
            try:
                last_sent = datetime.fromisoformat(
                    contact["touches"][-1]["sent_at"].replace("Z", "+00:00")
                )
            except Exception:
                continue
            if last_sent > cutoff:
                continue  # too recent — let normal cadence handle it
            # Stale: schedule next touch for today
            nta = contact.get("next_touch_at")
            today = _today_iso()
            if not nta or nta > today:
                contact["next_touch_at"] = today

    ledger["last_hydrated_at"] = _now_iso()


# ─────────────────────────────────────────────────────────────────────
# Find due
# ─────────────────────────────────────────────────────────────────────

def find_due_touches(ledger: dict, *, today: str | None = None) -> list[dict]:
    today = today or _today_iso()
    due = []
    for to, c in (ledger.get("contacts") or {}).items():
        if c.get("status") != "in_cadence":
            continue
        nta = c.get("next_touch_at")
        if not nta or nta > today:
            continue
        last_touch_n = c["touches"][-1]["n"] if c["touches"] else 0
        next_n = last_touch_n + 1
        if next_n > 3:
            # Should already be marked exhausted, but belt-and-suspenders
            c["status"] = "exhausted"
            c["next_touch_at"] = None
            continue
        due.append({
            "email": to,
            "publication": c.get("publication") or "",
            "next_touch_n": next_n,
            "source_url": c.get("source_url", ""),
            "source_title": c.get("source_title", ""),
            "cold_pitch_body": c.get("cold_pitch_body", ""),
            "cold_pitch_subject": c.get("cold_pitch_subject", ""),
            "touches": c.get("touches", []),
        })
    # Prefer higher-tier publications first
    PRIORITY_PUBS = {
        "latimes.com": 100, "nytimes.com": 100, "wsj.com": 95,
        "theguardian.com": 90, "robbreport.com": 85, "dezeen.com": 85,
        "sfchronicle.com": 80, "newsweek.com": 75, "kqed.org": 70,
    }
    def _priority(d):
        return PRIORITY_PUBS.get(_domain(d["email"]), 50)
    due.sort(key=lambda d: (-_priority(d), d["email"]))
    return due


# ─────────────────────────────────────────────────────────────────────
# Body generation (LLM)
# ─────────────────────────────────────────────────────────────────────

# Data points for Touch 2 — concrete numbers Lisa can drop into the
# bump. These are anchored on My Villa's actual build profile and on
# public CA market data; the LLM picks the one most aligned with the
# journalist's beat.
_DATA_POINTS = [
    "ICF + reinforced-concrete construction premium: roughly 25-35% "
    "over Type V wood frame for comparable square footage in LA.",
    "LA County permits for noncombustible residential construction "
    "have risen sharply since 2025 — the line is starting to bend.",
    "Mediterranean countries (Italy, Spain, France) treat noncombustible "
    "residential construction as the default, not a premium upgrade. "
    "What the CA insurance market is now pricing as a hedge has been "
    "baseline practice for generations there.",
    "Insurance carriers still writing in CA WUI ZIPs underwrite "
    "concrete-shell homes meaningfully cleaner than wood frame — "
    "the gap is now showing up in renewal cycles.",
    "Cost-per-sqft delta between a fireproofed concrete envelope and "
    "an unprotected Type V build in WUI ZIPs has held around "
    "$80-120/sqft — large but increasingly recoverable through "
    "insurability and resale.",
]


def _build_followup_prompt(contact: dict, touch_n: int) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for Claude.

    Touch 2 = "data bump": short, one new data point.
    Touch 3 = "founder call": close the loop, offer a 30-min call with Paolo.
    """
    publication = contact.get("publication") or "the team"
    source_title = contact.get("source_title") or "your recent piece"
    cold_subject = contact.get("cold_pitch_subject") or ""
    cold_body = contact.get("cold_pitch_body") or ""

    common_voice = (
        "You are Lisa Monelli, writing on behalf of My Villa Media Team. "
        "Tone: warm, professional, never pushy. Write like a real PR person, "
        "not a marketing email. American English. No exclamation marks. "
        "No subject line — body only.\n\n"
        "GREETING RULES:\n"
        "- If the recipient is an individual (first.last@...), greet by first name.\n"
        "- If it's a newsroom alias (info@, editorial@, tips@, newsroom@), greet "
        "with 'Hello,' or 'Hi team,' — NEVER say 'Hi Kqed team' or 'Hi Robbreport "
        "team'. Publication names that are ugly when lowercased and squished get "
        "skipped; a plain 'Hello,' is always safe.\n\n"
        "Sign off EXACTLY with these four lines:\n"
        "Best,\nLisa Monelli\nMy Villa Media Team\ninfo@myvilla.la · myvilla.la"
    )

    if touch_n == 2:
        system = common_voice + (
            "\n\nWrite a TOUCH-2 follow-up to a journalist who didn't reply "
            "to a cold pitch 7 days ago. Rules:\n"
            "- 50–80 words total (very short)\n"
            "- Open with a natural acknowledgement that the previous mail "
            "may have slipped through (do NOT say 'just bumping')\n"
            "- Add ONE specific data point from the list you'll be given — "
            "pick the one most relevant to the journalist's beat\n"
            "- Soft close: 'happy to share the underlying data' or "
            "'in case useful for an upcoming piece'\n"
            "- Do NOT repeat the cultural-framing argument from the cold pitch — "
            "the data IS the new angle"
        )
        data_list = "\n".join(f"  • {d}" for d in _DATA_POINTS)
        user = (
            f"Publication: {publication}\n"
            f"Original article topic: {source_title}\n"
            f"Cold pitch subject: \"{cold_subject}\"\n"
            f"Cold pitch body (so you don't repeat it):\n---\n{cold_body}\n---\n\n"
            f"Data points you can pick ONE from:\n{data_list}\n\n"
            f"Write the touch-2 follow-up body now."
        )
    elif touch_n == 3:
        system = common_voice + (
            "\n\nWrite a TOUCH-3 final follow-up. This is the LAST email. "
            "Rules:\n"
            "- 40–60 words total (very short)\n"
            "- Open by gracefully closing the loop ('Just closing the loop on this')\n"
            "- Make ONE concrete offer: a 30-minute call with Paolo "
            "(My Villa's founder), available if/when their next relevant "
            "piece comes up\n"
            "- NO data points, NO cultural argument, NO pressure\n"
            "- No guilt ('haven't heard back'), no apology"
        )
        user = (
            f"Publication: {publication}\n"
            f"Original article topic: {source_title}\n"
            f"Cold pitch subject: \"{cold_subject}\"\n\n"
            f"Write the touch-3 closing follow-up body now."
        )
    else:
        raise ValueError(f"Unsupported touch_n: {touch_n}")

    return system, user


def _generate_body_with_claude(contact: dict, touch_n: int) -> str | None:
    """Calls Claude (Sonnet) to draft the follow-up body. Returns None
    on any failure — caller decides whether to skip or queue for retry.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        print("  [ai] anthropic SDK not installed — falling back to template")
        return _template_body(contact, touch_n)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [ai] ANTHROPIC_API_KEY not set — falling back to template")
        return _template_body(contact, touch_n)

    system, user = _build_followup_prompt(contact, touch_n)
    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            blk.text for blk in resp.content if getattr(blk, "type", "") == "text"
        ).strip()
        return text or None
    except Exception as e:  # noqa: BLE001
        print(f"  [ai] Claude call failed: {type(e).__name__}: {e}")
        return _template_body(contact, touch_n)


def _template_body(contact: dict, touch_n: int) -> str:
    """Deterministic fallback used when LLM is unavailable. Better than
    no follow-up, worse than the LLM version. Same voice/signature.
    """
    pub = contact.get("publication") or "team"
    if touch_n == 2:
        # Pick a default data point — the cultural one is the most
        # universal and least likely to feel off-topic.
        data_pt = _DATA_POINTS[2]  # Mediterranean baseline
        return (
            f"Hi {pub.split()[0] if pub else 'team'},\n\n"
            f"In case the earlier note slipped through — one data point "
            f"from our side that might be useful: {data_pt}\n\n"
            f"Happy to share the underlying numbers if relevant to anything "
            f"on your desk.\n\n"
            f"Best,\nLisa Monelli\nMy Villa Media Team\n"
            f"info@myvilla.la · myvilla.la"
        )
    if touch_n == 3:
        return (
            f"Hi {pub.split()[0] if pub else 'team'},\n\n"
            f"Just closing the loop on this thread. If a relevant piece "
            f"comes up down the line, Paolo — our founder — is happy to "
            f"do a 30-minute call. Otherwise, no need to reply.\n\n"
            f"Best,\nLisa Monelli\nMy Villa Media Team\n"
            f"info@myvilla.la · myvilla.la"
        )
    raise ValueError(f"Unsupported touch_n: {touch_n}")


def _build_subject(contact: dict, touch_n: int) -> str:
    """Subject line for the follow-up. Touch 2 prefixes 'Re:' to thread
    naturally; Touch 3 mirrors the original subject."""
    original = contact.get("cold_pitch_subject") or "Quick follow-up"
    if original.lower().startswith("re:"):
        return original
    return f"Re: {original}"


# ─────────────────────────────────────────────────────────────────────
# Send
# ─────────────────────────────────────────────────────────────────────

def send_followups(ledger: dict, *, dry_run: bool = False,
                   max_per_run: int = MAX_FOLLOWUPS_PER_RUN) -> dict:
    """Send all due follow-ups. Returns a digest-shaped result dict."""
    sent, skipped, errors = [], [], []

    due_list = find_due_touches(ledger)
    if not due_list:
        return {"sent": sent, "skipped": skipped, "errors": errors}

    # Lazy import so the module loads even when send_email's deps
    # (Google API client) aren't installed in test envs.
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from send_email import send_draft
    except ImportError as e:
        print(f"  [followup] send_email module unavailable: {e}")
        return {"sent": [], "skipped": [], "errors": []}

    for contact in due_list:
        if len(sent) >= max_per_run:
            skipped.append({
                "to": contact["email"],
                "reason": f"batch cap reached ({max_per_run}) — will retry next run",
                "touch_n": contact["next_touch_n"],
            })
            continue

        touch_n = contact["next_touch_n"]
        body = _generate_body_with_claude(contact, touch_n)
        if not body:
            errors.append({
                "to": contact["email"],
                "error": "body generation failed",
                "touch_n": touch_n,
            })
            continue
        subject = _build_subject(contact, touch_n)

        if dry_run:
            sent.append({
                "to": contact["email"],
                "subject": subject,
                "body": body,
                "publication": contact["publication"],
                "touch_n": touch_n,
                "source_url": contact["source_url"],
                "title": contact["source_title"],
            })
            # IMPORTANT: do NOT record the touch in dry-run. Otherwise
            # the ledger "remembers" the fake send and skips it on the
            # next real run. The dry-run is meant to preview, not
            # consume state.
            continue

        # Real send
        try:
            result = send_draft(to=contact["email"], subject=subject, body=body)
        except Exception as e:  # noqa: BLE001
            errors.append({
                "to": contact["email"],
                "error": f"{type(e).__name__}: {e}",
                "touch_n": touch_n,
            })
            continue

        ok = result.get("ok", False)
        reason = result.get("reason") or ""
        err = result.get("error") or ""

        if ok and result.get("dry_run"):
            skipped.append({
                "to": contact["email"],
                "reason": "outreach config in dry_run mode",
                "touch_n": touch_n,
            })
            continue
        if ok:
            sent.append({
                "to": contact["email"],
                "subject": subject,
                "body": body,
                "publication": contact["publication"],
                "touch_n": touch_n,
                "source_url": contact["source_url"],
                "title": contact["source_title"],
            })
            _record_touch(ledger, contact["email"], touch_n, subject,
                          message_id=result.get("message_id"),
                          thread_id=result.get("thread_id"),
                          sent_at=result.get("timestamp") or _now_iso())
        elif reason == "rate_limited":
            skipped.append({
                "to": contact["email"],
                "reason": "rate-limited (10/h) — will retry next run",
                "touch_n": touch_n,
            })
            print(f"  [followup] rate limit hit at {len(sent)} sent — stopping")
            break
        elif reason == "blacklisted":
            skipped.append({
                "to": contact["email"],
                "reason": "address blacklisted (previously bounced)",
                "touch_n": touch_n,
            })
            # mark contact as bounced so we stop trying
            ledger["contacts"][contact["email"]]["status"] = "bounced"
            ledger["contacts"][contact["email"]]["next_touch_at"] = None
        else:
            errors.append({
                "to": contact["email"],
                "error": f"{reason} {err}".strip() or "unknown",
                "touch_n": touch_n,
            })

    return {"sent": sent, "skipped": skipped, "errors": errors}


def _record_touch(ledger: dict, email: str, n: int, subject: str,
                  *, message_id, thread_id, sent_at: str) -> None:
    c = ledger["contacts"].get(email)
    if not c:
        return
    c["touches"].append({
        "n": n,
        "type": "data" if n == 2 else "founder_call" if n == 3 else "extra",
        "sent_at": sent_at,
        "message_id": message_id,
        "thread_id": thread_id,
        "subject": subject,
    })
    offset = CADENCE_OFFSETS.get(n)
    if offset is None:
        c["next_touch_at"] = None
        c["status"] = "exhausted"
    else:
        try:
            base = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        except Exception:
            base = datetime.now(timezone.utc)
        c["next_touch_at"] = (base + timedelta(days=offset)).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# Orchestration entry point
# ─────────────────────────────────────────────────────────────────────

def run(*, dry_run: bool = False,
        retroactive_recovery: bool | None = None) -> dict:
    """End-to-end run. Imported and called by publish_all_drafts.py.

    `retroactive_recovery` defaults to True on the very first run
    (no ledger file yet) and False thereafter, so the one-time
    recovery doesn't keep happening every day.
    """
    first_run = not LEDGER_PATH.exists()
    if retroactive_recovery is None:
        retroactive_recovery = first_run

    ledger = _load_ledger()
    hydrate_ledger(ledger, retroactive_recovery=retroactive_recovery)
    result = send_followups(ledger, dry_run=dry_run)
    # Don't write the ledger in dry-run — that would consume state.
    if not dry_run:
        _save_ledger(ledger)

    # Status counts for the digest
    by_status = {}
    for c in ledger["contacts"].values():
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1

    result["ledger_status_counts"] = by_status
    result["ledger_total_contacts"] = len(ledger["contacts"])
    return result


def main():
    parser = argparse.ArgumentParser(description="My Villa follow-up engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually send; show what would be sent")
    parser.add_argument("--force-recovery", action="store_true",
                        help="Run retroactive recovery even after first run")
    args = parser.parse_args()

    result = run(
        dry_run=args.dry_run,
        retroactive_recovery=True if args.force_recovery else None,
    )

    print()
    print("=" * 60)
    print(f"Follow-up engine — {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)
    print(f"  Sent:    {len(result['sent'])}")
    print(f"  Skipped: {len(result['skipped'])}")
    print(f"  Errors:  {len(result['errors'])}")
    print()
    print("Ledger status:")
    for status, n in (result.get("ledger_status_counts") or {}).items():
        print(f"  {status:14} {n}")
    print(f"  total contacts: {result.get('ledger_total_contacts')}")
    print()
    if result["sent"]:
        print("--- SENT ---")
        for s in result["sent"]:
            print(f"  T{s['touch_n']} → {s['to']}  ({s['publication']})")
            print(f"      subject: {s['subject']}")
    if result["skipped"]:
        print("--- SKIPPED ---")
        for s in result["skipped"]:
            print(f"  T{s.get('touch_n','?')} → {s['to']}: {s['reason']}")
    if result["errors"]:
        print("--- ERRORS ---")
        for e in result["errors"]:
            print(f"  T{e.get('touch_n','?')} → {e['to']}: {e['error']}")


if __name__ == "__main__":
    main()
