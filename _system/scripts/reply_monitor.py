#!/usr/bin/env python3
"""
Reply monitor for the MyVilla outreach system.

Reads the send log, pulls every Gmail thread we sent an outreach email
into, detects new replies from the journalist, and persists them into a
local store that the reply drafter and the dashboard consume.

Files this script touches:

- Reads:  `_system/outreach/send_log.jsonl` (source of thread ids)
- Reads:  Gmail API (`users.threads.get` for each thread)
- Writes: `_system/outreach/replies/<thread_id>.json`
          (one file per thread, merged across runs — holds every reply
           the journalist has sent so far in that thread)
- Writes: `_system/outreach/replies_log.jsonl`
          (append-only audit trail, one row per "new reply detected")

The monitor NEVER sends anything. It only reads Gmail and writes local
state. Generating the actual draft reply is a separate step —
`reply_drafter.py` — which this script can optionally chain via
`--with-drafts`.

CLI:
    python3 reply_monitor.py                 # normal scan
    python3 reply_monitor.py --verbose       # print every thread visited
    python3 reply_monitor.py --thread <id>   # limit scan to one thread
    python3 reply_monitor.py --since 2026-04-20   # filter outreach sends
    python3 reply_monitor.py --with-drafts   # also invoke reply_drafter
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gmail_client import (
    GmailClient,
    PROJECT_ROOT,
    extract_header,
    extract_plain_text,
    load_config,
)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
REPLIES_DIR = PROJECT_ROOT / "_system" / "outreach" / "replies"
REPLIES_LOG = PROJECT_ROOT / "_system" / "outreach" / "replies_log.jsonl"
DRAFTS_DIR = PROJECT_ROOT / "_drafts" / "email_replies"
# Persistent blacklist of addresses that bounced at least once. Reads
# by `send_email.py` before every send to short-circuit retry-on-dead.
INVALID_ADDRESSES_PATH = PROJECT_ROOT / "_system" / "outreach" / "invalid_addresses.json"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class ReplyRecord:
    """One message in the thread authored by somebody other than us.

    `kind` tells downstream consumers how to treat it:
    - "reply"      : a genuine reply from the journalist — draft a response
    - "bounce"     : mailer-daemon / delivery failure — do NOT draft
    - "auto_reply" : out-of-office / vacation responder — do NOT draft
                     automatically, surface to human

    Only `kind == "reply"` flows into the drafter.
    """
    thread_id: str
    gmail_message_id: str       # Gmail internal id (opaque)
    rfc822_message_id: str | None  # RFC-822 Message-ID header (for In-Reply-To)
    from_address: str
    from_name: str
    subject: str
    body: str                   # plain-text body
    snippet: str
    received_at: str            # ISO-8601 UTC
    kind: str = "reply"         # "reply" | "bounce" | "auto_reply"
    kind_reason: str = ""       # why we classified it this way (debug)
    label_ids: list[str] = field(default_factory=list)
    outreach_message_id: str | None = None   # the email we sent first
    outreach_subject: str | None = None
    outreach_to: str | None = None
    outreach_sent_at: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThreadState:
    """Per-thread state stored under `replies/<thread_id>.json`."""
    thread_id: str
    outreach_message_id: str | None
    outreach_subject: str | None
    outreach_to: str | None
    outreach_sent_at: str | None
    replies: list[ReplyRecord] = field(default_factory=list)
    last_checked_at: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_outreach_sends(
    send_log_path: Path,
    *,
    since: datetime | None = None,
    thread_filter: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return the distinct real outreach sends from the send log (dedup'd by
    thread_id, picking the *first* send per thread).

    Only includes: `ok=true`, `dry_run=false`, `kind in ("outreach", None)`.
    Replies (kind="reply") are intentionally excluded — we only scan the
    original first-touch emails.
    """
    if not send_log_path.exists():
        return []

    seen_threads: set[str] = set()
    outreach: list[dict[str, Any]] = []
    with open(send_log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("ok") or entry.get("dry_run"):
                continue
            kind = entry.get("kind") or "outreach"
            if kind != "outreach":
                continue
            thread_id = entry.get("thread_id")
            if not thread_id:
                continue
            if thread_filter and thread_id != thread_filter:
                continue
            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                ts = None
            if since and ts and ts < since:
                continue
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            outreach.append(entry)
    return outreach


def _load_thread_state(thread_id: str) -> ThreadState | None:
    """Load the cached ThreadState for a given thread_id, if present."""
    path = REPLIES_DIR / f"{thread_id}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    replies = [ReplyRecord(**r) for r in data.get("replies", [])]
    return ThreadState(
        thread_id=data["thread_id"],
        outreach_message_id=data.get("outreach_message_id"),
        outreach_subject=data.get("outreach_subject"),
        outreach_to=data.get("outreach_to"),
        outreach_sent_at=data.get("outreach_sent_at"),
        replies=replies,
        last_checked_at=data.get("last_checked_at"),
    )


def _save_thread_state(state: ThreadState) -> None:
    """Persist the ThreadState to `replies/<thread_id>.json`."""
    REPLIES_DIR.mkdir(parents=True, exist_ok=True)
    path = REPLIES_DIR / f"{state.thread_id}.json"
    path.write_text(state.to_json() + "\n", encoding="utf-8")


def _append_replies_log(entry: dict[str, Any]) -> None:
    """Append one JSONL row to the replies audit trail."""
    REPLIES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REPLIES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _parse_from_header(value: str | None) -> tuple[str, str]:
    """
    Split an email From header into (address, display_name). Handles both
    `"Name" <x@y>` and bare `x@y`.
    """
    if not value:
        return ("", "")
    import re
    m = re.match(r"\s*(?:\"?([^\"<]+?)\"?\s*)?<?([^<>\s]+@[^<>\s]+)>?", value)
    if m:
        name = (m.group(1) or "").strip().strip('"')
        addr = (m.group(2) or "").strip()
        return (addr, name)
    return (value.strip(), "")


def _is_from_us(from_addr: str, sender_addr: str) -> bool:
    """Case-insensitive match with basic normalization."""
    return from_addr.strip().lower() == sender_addr.strip().lower()


# --------------------------------------------------------------------------- #
# Message kind classifier
# --------------------------------------------------------------------------- #

# Local parts commonly used by mail systems for bounces and delivery
# notifications. Matched case-insensitively on the local part (`x@`).
_BOUNCE_LOCAL_PARTS = {
    "mailer-daemon",
    "postmaster",
    "bounces",
    "bounce",
    "mailer-daemon-no-reply",
    "mail-delivery-system",
    "double-bounce",
    "noreply-bounce",
}

# Subject fragments that strongly indicate a bounce/DSN (multi-language).
# Checked case-insensitively.
_BOUNCE_SUBJECT_FRAGMENTS = (
    "delivery status notification",
    "undeliverable",
    "undelivered mail",
    "mail delivery failed",
    "mail delivery subsystem",
    "returned mail",
    "failure notice",
    "recapito non riuscito",
    "posta non recapitata",
    "address not found",
)


def _classify_message_kind(
    msg: dict[str, Any], from_address: str, subject: str,
) -> tuple[str, str]:
    """
    Return (kind, reason) tuple for a Gmail message payload. Does NOT
    modify the message.
    """
    addr_low = (from_address or "").strip().lower()
    subj_low = (subject or "").strip().lower()

    # Bounce detection — two complementary signals.
    local = addr_low.split("@", 1)[0] if "@" in addr_low else addr_low
    if local in _BOUNCE_LOCAL_PARTS:
        return (
            "bounce",
            f"Sender local part {local!r} matches bounce pattern",
        )
    for fragment in _BOUNCE_SUBJECT_FRAGMENTS:
        if fragment in subj_low:
            return ("bounce", f"Subject contains bounce fragment {fragment!r}")

    # Auto-reply detection — Auto-Submitted header is the RFC-3834 signal.
    # Values other than "no" (e.g. "auto-replied", "auto-generated") mean
    # this is a machine-generated reply.
    from gmail_client import extract_header  # local import to avoid cycle
    auto_submitted = (extract_header(msg, "Auto-Submitted") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return ("auto_reply", f"Auto-Submitted: {auto_submitted}")

    # Some vacation responders use X-Autoreply / X-Autorespond or
    # Precedence: auto_reply / bulk / junk.
    x_autoreply = extract_header(msg, "X-Autoreply")
    if x_autoreply:
        return ("auto_reply", f"X-Autoreply: {x_autoreply}")
    precedence = (extract_header(msg, "Precedence") or "").lower()
    if precedence in ("auto_reply", "bulk", "junk"):
        return ("auto_reply", f"Precedence: {precedence}")

    return ("reply", "")


# --------------------------------------------------------------------------- #
# Bounce extraction + blacklist persistence
# --------------------------------------------------------------------------- #

# Patterns tried in priority order: the more structured ones (RFC-3464
# Final-Recipient, angle-bracketed <addr>) come first because they're the
# most trustworthy, then Gmail-specific phrases, then a permissive last
# resort. The first pattern that matches a non-self / non-daemon address
# wins.
_BOUNCE_ADDRESS_PATTERNS = (
    r"(?i)Final-Recipient:\s*\S+;\s*([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    r"(?i)Original-Recipient:\s*\S+;\s*([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    r"(?i)message wasn't delivered to\s+([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    r"(?i)to the following recipient[s]?:\s*[\r\n]+\s*([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
    r"(?i)Delivery to the following recipient failed[^<]*<([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})>",
    r"(?i)<([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})>\s*(?::|\n|\s+\d{3}\s)",
    r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
)

# Reason extraction — keep the short SMTP code + human-readable suffix so
# the dashboard can display something meaningful.
_BOUNCE_REASON_PATTERNS = (
    r"(5\d\d[ -][^\n]{0,160})",                         # 550 / 551 / 554 …
    r"(The email account that you tried to reach[^.\n]+)",
    r"(Diagnostic-Code:\s*[^\n]+)",
    r"(Status:\s*5\.\d\.\d[^\n]*)",
)


def _extract_bounce_info(
    body: str, *, exclude: Iterable[str] = ()
) -> tuple[str | None, str | None]:
    """
    Extract (failed_address, reason) from a DSN body.

    `exclude` is a set of lowercase addresses to ignore when scanning
    (typically: our own sender address and the mailer-daemon itself).
    Returns (None, None) if nothing usable is found.
    """
    import re
    skip = {a.strip().lower() for a in exclude if a}
    skip |= {
        "mailer-daemon@googlemail.com",
        "mailer-daemon@google.com",
        "postmaster@googlemail.com",
    }
    failed = None
    for pat in _BOUNCE_ADDRESS_PATTERNS:
        for m in re.finditer(pat, body or ""):
            cand = (m.group(1) or "").strip().lower()
            if cand and cand not in skip and "@" in cand:
                failed = cand
                break
        if failed:
            break

    reason = None
    for pat in _BOUNCE_REASON_PATTERNS:
        m = re.search(pat, body or "")
        if m:
            reason = m.group(1).strip()[:200]
            break
    return failed, reason


def _load_invalid_addresses() -> dict[str, Any]:
    """Load the blacklist, or return an empty skeleton."""
    if not INVALID_ADDRESSES_PATH.exists():
        return {"version": 1, "addresses": {}}
    try:
        data = json.loads(INVALID_ADDRESSES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "addresses": {}}
        data.setdefault("version", 1)
        data.setdefault("addresses", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "addresses": {}}


def _save_invalid_addresses(data: dict[str, Any]) -> None:
    """Atomic write so concurrent polls can't corrupt the file."""
    INVALID_ADDRESSES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INVALID_ADDRESSES_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(INVALID_ADDRESSES_PATH)


def record_bounced_address(
    address: str,
    *,
    reason: str | None = None,
    thread_id: str | None = None,
    outreach_to: str | None = None,
    detected_at: str | None = None,
) -> None:
    """
    Add (or update) `address` in the invalid-addresses blacklist.

    Safe to call multiple times — tracks a counter and latest reason.
    This is the function `send_email.py` reads via
    `is_invalid_address()` before every send.
    """
    addr = (address or "").strip().lower()
    if not addr or "@" not in addr:
        return
    data = _load_invalid_addresses()
    now = detected_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = data["addresses"].get(addr) or {
        "first_bounced_at": now,
        "bounce_count": 0,
        "source_threads": [],
    }
    entry["last_bounced_at"] = now
    entry["bounce_count"] = int(entry.get("bounce_count", 0)) + 1
    if reason:
        entry["reason"] = reason
    if thread_id and thread_id not in entry.get("source_threads", []):
        entry.setdefault("source_threads", []).append(thread_id)
    if outreach_to and outreach_to.strip().lower() != addr:
        # The DSN can carry a different address than what we sent to
        # (e.g. forwarding); note the discrepancy.
        entry["also_sent_to"] = outreach_to
    data["addresses"][addr] = entry
    _save_invalid_addresses(data)


def is_invalid_address(address: str) -> bool:
    """Check whether `address` is in the bounced-addresses blacklist."""
    if not address:
        return False
    data = _load_invalid_addresses()
    return (address or "").strip().lower() in (data.get("addresses") or {})


# --------------------------------------------------------------------------- #
# Core scan
# --------------------------------------------------------------------------- #

def scan_thread(
    client: GmailClient,
    *,
    outreach_entry: dict[str, Any],
    verbose: bool = False,
) -> tuple[ThreadState, list[ReplyRecord]]:
    """
    Pull a Gmail thread and return (updated_state, new_replies).

    "New" means: present on Gmail but NOT already recorded locally, and
    sent by somebody other than us (so not our own outreach + not our
    own replies).
    """
    cfg = client.config
    thread_id = outreach_entry["thread_id"]
    state = _load_thread_state(thread_id) or ThreadState(
        thread_id=thread_id,
        outreach_message_id=outreach_entry.get("message_id"),
        outreach_subject=outreach_entry.get("subject"),
        outreach_to=outreach_entry.get("to"),
        outreach_sent_at=outreach_entry.get("timestamp"),
    )
    known_ids = {r.gmail_message_id for r in state.replies}

    try:
        messages = client.list_thread_messages(thread_id)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  [reply_monitor] {thread_id}: ERROR fetching thread — {exc}")
        return state, []

    new_replies: list[ReplyRecord] = []
    for msg in messages:
        gmail_id = msg.get("id")
        if not gmail_id or gmail_id in known_ids:
            continue
        from_hdr = extract_header(msg, "From")
        addr, name = _parse_from_header(from_hdr)
        if _is_from_us(addr, cfg.sender_email):
            # Our own message — skip.
            continue
        subject = extract_header(msg, "Subject") or ""
        rfc822 = extract_header(msg, "Message-ID") or extract_header(
            msg, "Message-Id"
        )
        body = extract_plain_text(msg)
        internal_date_ms = msg.get("internalDate")
        try:
            received_at = (
                datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
                .isoformat(timespec="seconds")
            ) if internal_date_ms else datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
        except (ValueError, TypeError):
            received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        kind, kind_reason = _classify_message_kind(msg, addr, subject)

        reply = ReplyRecord(
            thread_id=thread_id,
            gmail_message_id=gmail_id,
            rfc822_message_id=rfc822,
            from_address=addr,
            from_name=name,
            subject=subject,
            body=body,
            snippet=(msg.get("snippet") or "").strip(),
            received_at=received_at,
            kind=kind,
            kind_reason=kind_reason,
            label_ids=list(msg.get("labelIds") or []),
            outreach_message_id=state.outreach_message_id,
            outreach_subject=state.outreach_subject,
            outreach_to=state.outreach_to,
            outreach_sent_at=state.outreach_sent_at,
        )
        new_replies.append(reply)

    if new_replies:
        state.replies.extend(new_replies)
    state.last_checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_thread_state(state)
    return state, new_replies


def run_scan(
    *,
    since: datetime | None = None,
    thread_filter: str | None = None,
    verbose: bool = False,
) -> list[ReplyRecord]:
    """
    Run a full scan across every outreach-sent thread. Returns the list
    of newly-detected replies (those not seen before this run).
    """
    cfg = load_config()
    client = GmailClient(cfg)
    outreach = _load_outreach_sends(
        cfg.send_log, since=since, thread_filter=thread_filter
    )
    if verbose:
        print(f"  [reply_monitor] Scanning {len(outreach)} thread(s)...")

    all_new: list[ReplyRecord] = []
    for entry in outreach:
        thread_id = entry["thread_id"]
        state, new_replies = scan_thread(
            client, outreach_entry=entry, verbose=verbose
        )
        if verbose:
            print(
                f"  [reply_monitor] {thread_id}  "
                f"to={entry.get('to'):<40}  "
                f"total_replies={len(state.replies)}  "
                f"new={len(new_replies)}"
            )
        for r in new_replies:
            _append_replies_log({
                "event": f"new_{r.kind}",   # new_reply / new_bounce / new_auto_reply
                "thread_id": r.thread_id,
                "gmail_message_id": r.gmail_message_id,
                "from_address": r.from_address,
                "subject": r.subject,
                "kind": r.kind,
                "kind_reason": r.kind_reason,
                "received_at": r.received_at,
                "detected_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            })
            # On bounce: extract the failed recipient from the DSN body
            # and persist it into the invalid-addresses blacklist. This
            # is what lets `send_email.py` short-circuit future retries.
            if r.kind == "bounce":
                failed, reason = _extract_bounce_info(
                    r.body, exclude=[cfg.sender_email],
                )
                if failed:
                    record_bounced_address(
                        failed,
                        reason=reason,
                        thread_id=r.thread_id,
                        outreach_to=entry.get("to"),
                        detected_at=r.received_at,
                    )
                    if verbose:
                        print(
                            f"  [reply_monitor] ❌ blacklisted {failed}  "
                            f"reason={reason or '?'}"
                        )
        all_new.extend(new_replies)
    return all_new


def pending_draftable_threads() -> list[str]:
    """
    Return thread_ids that have at least one reply with kind=='reply'
    and no up-to-date draft yet. Shared by the drafter and the
    dashboard to list actionable threads.
    """
    out: list[str] = []
    if not REPLIES_DIR.exists():
        return out
    drafts_dir = PROJECT_ROOT / "_drafts" / "email_replies"
    for state_file in sorted(REPLIES_DIR.glob("*.json")):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        replies = [
            r for r in (state.get("replies") or [])
            if r.get("kind", "reply") == "reply"
        ]
        if not replies:
            continue
        latest = sorted(replies, key=lambda r: r.get("received_at") or "")[-1]
        thread_id = state["thread_id"]
        draft_file = drafts_dir / f"{thread_id}.json"
        if not draft_file.exists():
            out.append(thread_id)
            continue
        try:
            draft = json.loads(draft_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out.append(thread_id)
            continue
        if (draft.get("replying_to_message_id") !=
                latest.get("gmail_message_id")):
            out.append(thread_id)
    return out


# --------------------------------------------------------------------------- #
# Drafter chaining
# --------------------------------------------------------------------------- #

def invoke_drafter(thread_id: str, *, verbose: bool = False) -> int:
    """
    Shell out to `reply_drafter.py --thread <id>`. We keep this as a
    subprocess call rather than an import so the two scripts stay
    loosely coupled — the drafter has heavier deps (Anthropic client).
    """
    drafter = SCRIPT_DIR / "reply_drafter.py"
    if not drafter.exists():
        if verbose:
            print(f"  [reply_monitor] Drafter not found: {drafter}")
        return 0  # not a failure — drafter is optional
    cmd = [sys.executable, str(drafter), "--thread", thread_id]
    if verbose:
        cmd.append("--verbose")
    try:
        proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), timeout=120)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print(f"  [reply_monitor] Drafter timed out for thread {thread_id}")
        return 124


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid --since value: {s!r}. Use ISO-8601 (e.g. 2026-04-22)."
        )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--thread", help="Limit scan to this Gmail thread id.")
    parser.add_argument(
        "--since",
        type=_parse_since,
        help="Only consider outreach sends after this ISO-8601 date.",
    )
    parser.add_argument(
        "--with-drafts",
        action="store_true",
        help="After the scan, invoke reply_drafter.py for each new reply.",
    )
    args = parser.parse_args(argv)

    try:
        new_replies = run_scan(
            since=args.since,
            thread_filter=args.thread,
            verbose=args.verbose,
        )
    except RuntimeError as exc:
        # Usually: missing OAuth token — ask the user to run --authorize.
        print(f"  [reply_monitor] ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"  [reply_monitor] ERROR: {type(exc).__name__}: {exc}")
        return 1

    # Break down by kind so the operator knows whether any drafter
    # action is warranted.
    kind_counts: dict[str, int] = {}
    for r in new_replies:
        kind_counts[r.kind] = kind_counts.get(r.kind, 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(kind_counts.items())) \
        if kind_counts else "—"
    print(
        f"  [reply_monitor] New messages detected: {len(new_replies)} "
        f"({breakdown})"
    )
    for r in new_replies:
        kind_tag = r.kind if r.kind != "reply" else " "
        print(
            f"    ↳ [{kind_tag:>10}]  {r.thread_id}  "
            f"from={r.from_address}  subj={r.subject[:60]!r}"
        )

    if args.with_drafts and new_replies:
        # Only draft for threads that have a GENUINE reply among the new
        # messages. Bounces and auto_replies don't trigger the drafter.
        draftable = {r.thread_id for r in new_replies if r.kind == "reply"}
        for tid in sorted(draftable):
            rc = invoke_drafter(tid, verbose=args.verbose)
            if rc != 0:
                print(f"  [reply_monitor] drafter rc={rc} for thread {tid}")
        if not draftable:
            print(
                "  [reply_monitor] Skipping drafter: no genuine replies "
                "among new messages (bounces/auto-replies only)."
            )

    return 0


if __name__ == "__main__":
    sys.exit(_main())
