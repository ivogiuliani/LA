#!/usr/bin/env python3
"""
High-level send interface for the MyVilla outreach system.

This is the file that `approve.py` imports. It wraps `gmail_client.py`
with the policy layer:

- dry_run: compose and log, but don't hit Gmail
- rate_limit_per_hour: refuse runaway sends
- signature injection: append the canonical Lisa Monelli block exactly
- send log: append one JSONL line per attempt (success or failure)

Do NOT use `gmail_client.GmailClient.send()` directly from approve.py —
always go through `send_draft()` or `send_raw()` here so the policy layer
is applied.

CLI:
    python3 send_email.py --to x@y.com --subject "Hi" --body-file draft.txt
    python3 send_email.py --to x@y.com --subject "Hi" --body "One-liner"
    python3 send_email.py --rate-check     # show current rate limit state
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gmail_client import GmailClient, OutreachConfig, load_config


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

@dataclass
class SendResult:
    """Structured outcome of a send attempt. Serializable to JSON."""
    ok: bool
    dry_run: bool
    message_id: str | None
    thread_id: str | None
    to: str
    subject: str
    body_chars: int
    timestamp: str
    error: str | None = None
    reason: str | None = None  # e.g. "rate_limited", "dry_run"
    # Populated when send_reply is used (not a first-touch outreach).
    kind: str = "outreach"       # "outreach" | "reply"
    in_reply_to: str | None = None
    attachments: list[str] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Policy helpers
# --------------------------------------------------------------------------- #

def compose_body(body: str, signature: str) -> str:
    """
    Append the canonical signature to `body` if not already present.

    Rule: if the body already ends with `info@myvilla.la` (case-insensitive
    substring match anywhere in the last 200 chars), assume the signature
    is already there and return unchanged. Otherwise, append `\n\n` + sig.
    """
    tail = body[-200:].lower()
    if "info@myvilla.la" in tail:
        return body.rstrip() + "\n"
    return body.rstrip() + "\n\n" + signature.rstrip() + "\n"


def _read_send_log(log_path: Path) -> list[dict[str, Any]]:
    """Read the send log as a list of dicts. Returns [] if not present."""
    if not log_path.exists():
        return []
    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip corrupted lines silently
    return entries


def _append_send_log(log_path: Path, result: SendResult) -> None:
    """Append one JSONL line to the send log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(result.to_json() + "\n")


def rate_limit_state(config: OutreachConfig) -> dict[str, Any]:
    """
    Return the current rate-limit state:
    - sends_last_hour: number of successful (non dry-run) sends in the last 3600s
    - limit: config.rate_limit_per_hour
    - would_block: True if the next send would be refused
    """
    entries = _read_send_log(config.send_log)
    now = time.time()
    recent = 0
    for e in entries:
        if not e.get("ok"):
            continue
        if e.get("dry_run"):
            continue
        ts_str = e.get("timestamp", "")
        try:
            # ISO 8601 parsing
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if now - ts <= 3600:
            recent += 1
    return {
        "sends_last_hour": recent,
        "limit": config.rate_limit_per_hour,
        "would_block": recent >= config.rate_limit_per_hour,
    }


# --------------------------------------------------------------------------- #
# Public send functions
# --------------------------------------------------------------------------- #

def send_raw(
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    config: OutreachConfig | None = None,
    skip_signature: bool = False,
    skip_rate_limit: bool = False,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[Path] | None = None,
    kind: str = "outreach",
) -> SendResult:
    """
    Send an email through the policy layer.

    Parameters
    ----------
    to, subject, body : the message
    config : optional override (defaults to loading from config.yml)
    skip_signature : if True, do NOT append the Lisa Monelli signature
    skip_rate_limit : if True, bypass the rate limit check (use sparingly)
    thread_id : Gmail thread id — set when replying to keep the message
                in the same conversation thread
    in_reply_to : RFC-822 Message-ID header of the message we're replying
                  to (e.g. `<CAG5K...@mail.gmail.com>`). Required so email
                  clients outside Gmail group the reply correctly.
    references : full References header chain; defaults to `in_reply_to`
                 when omitted and `in_reply_to` is set.
    attachments : list of Paths to attach (press kit, fact sheet, ...)
    kind : "outreach" (first touch) | "reply" — written to the send log
           so the dashboard and the reply monitor can filter.

    Returns
    -------
    SendResult — check .ok and .reason
    """
    cfg = config or load_config()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Compose final body with signature injection.
    final_body = body if skip_signature else compose_body(body, cfg.signature)

    # Blacklist guard: refuse to resend to an address that bounced before.
    # The list is maintained by reply_monitor.py every time a DSN arrives.
    # This short-circuit runs BEFORE rate limit / dry run so the dashboard
    # always gets a clear reason back.
    try:
        from reply_monitor import is_invalid_address, _load_invalid_addresses
        if is_invalid_address(to):
            entry = (_load_invalid_addresses().get("addresses") or {}).get(
                to.strip().lower(), {}
            )
            reason_detail = entry.get("reason") or "previously bounced"
            first_seen = entry.get("first_bounced_at") or "?"
            result = SendResult(
                ok=False,
                dry_run=cfg.dry_run,
                message_id=None,
                thread_id=thread_id,
                to=to,
                subject=subject,
                body_chars=len(final_body),
                timestamp=now_iso,
                error=(
                    f"Address {to!r} is on the bounce blacklist "
                    f"(first bounced {first_seen}): {reason_detail}"
                ),
                reason="blacklisted",
                kind=kind,
                in_reply_to=in_reply_to,
            )
            _append_send_log(cfg.send_log, result)
            return result
    except ImportError:
        # reply_monitor not on the path — skip the check silently. The
        # generate/send flow should still work without it.
        pass

    # Resolve attachment paths relative to the project root so that callers
    # can pass short paths like "_system/outreach/attachments/foo.pdf".
    # Any bad path fails fast here, before we hit Gmail.
    resolved_attachments: list[Path] = []
    attachment_names: list[str] = []
    if attachments:
        from gmail_client import PROJECT_ROOT  # local import avoids cycle
        for p in attachments:
            cand = Path(p)
            if not cand.is_absolute():
                cand = PROJECT_ROOT / cand
            if not cand.exists():
                err = f"Attachment not found: {cand}"
                result = SendResult(
                    ok=False, dry_run=cfg.dry_run, message_id=None,
                    thread_id=thread_id, to=to, subject=subject,
                    body_chars=len(final_body), timestamp=now_iso,
                    error=err, reason="missing_attachment",
                    kind=kind, in_reply_to=in_reply_to,
                    attachments=[str(p) for p in attachments],
                )
                _append_send_log(cfg.send_log, result)
                return result
            resolved_attachments.append(cand)
            attachment_names.append(cand.name)

    # Rate limit check.
    if not skip_rate_limit:
        state = rate_limit_state(cfg)
        if state["would_block"]:
            result = SendResult(
                ok=False,
                dry_run=cfg.dry_run,
                message_id=None,
                thread_id=thread_id,
                to=to,
                subject=subject,
                body_chars=len(final_body),
                timestamp=now_iso,
                error=(
                    f"Rate limit reached: {state['sends_last_hour']} sends in the "
                    f"last hour (limit {state['limit']}). Try again later or "
                    f"increase `rate_limit_per_hour` in config.yml."
                ),
                reason="rate_limited",
                kind=kind,
                in_reply_to=in_reply_to,
                attachments=attachment_names or None,
            )
            _append_send_log(cfg.send_log, result)
            return result

    # Dry run short-circuit.
    if cfg.dry_run:
        result = SendResult(
            ok=True,
            dry_run=True,
            message_id=None,
            thread_id=thread_id,
            to=to,
            subject=subject,
            body_chars=len(final_body),
            timestamp=now_iso,
            reason="dry_run",
            kind=kind,
            in_reply_to=in_reply_to,
            attachments=attachment_names or None,
        )
        _append_send_log(cfg.send_log, result)
        return result

    # Real send.
    try:
        client = GmailClient(cfg)
        resp = client.send(
            to=to,
            subject=subject,
            body=final_body,
            html_body=html_body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            attachments=resolved_attachments or None,
        )
        result = SendResult(
            ok=True,
            dry_run=False,
            message_id=resp.get("id"),
            thread_id=resp.get("threadId"),
            to=to,
            subject=subject,
            body_chars=len(final_body),
            timestamp=now_iso,
            kind=kind,
            in_reply_to=in_reply_to,
            attachments=attachment_names or None,
        )
    except Exception as exc:  # noqa: BLE001 — we want to log everything
        result = SendResult(
            ok=False,
            dry_run=False,
            message_id=None,
            thread_id=thread_id,
            to=to,
            subject=subject,
            body_chars=len(final_body),
            timestamp=now_iso,
            error=f"{type(exc).__name__}: {exc}",
            kind=kind,
            in_reply_to=in_reply_to,
            attachments=attachment_names or None,
        )

    _append_send_log(cfg.send_log, result)
    return result


# Convenience wrapper used by approve.py's /api/send-email endpoint.
def send_draft(
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """
    Public entry point for the dashboard (first-touch outreach). Thin
    wrapper that returns a JSON-serializable dict instead of SendResult.

    `attachments` is optional: the outreach_voice.md rule says "niente
    allegati nella prima mail", so by default none are sent. But the
    dashboard lets the user override this when they judge it appropriate
    (e.g. attaching a bespoke PDF for a specific journalist).
    """
    paths: list[Path] | None = None
    if attachments:
        paths = [Path(a) for a in attachments]
    result = send_raw(to=to, subject=subject, body=body, attachments=paths)
    return asdict(result)


# Convenience wrapper used by approve.py's /api/send-reply endpoint.
def send_reply(
    *,
    to: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send a reply inside an existing Gmail thread, optionally with
    attachments (press kit, fact sheet).

    `thread_id` is required — without it Gmail would start a new thread
    even if the Subject matches. `in_reply_to` is the RFC-822 Message-ID
    of the journalist's last message in the thread; include it so email
    clients outside Gmail group the reply correctly.

    Returns a JSON-serializable dict (like `send_draft`).
    """
    paths: list[Path] | None = None
    if attachments:
        paths = [Path(a) for a in attachments]
    result = send_raw(
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        attachments=paths,
        kind="reply",
    )
    return asdict(result)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    parser.add_argument("--to", help="Recipient email address.")
    parser.add_argument("--subject", help="Email subject.")
    parser.add_argument("--body", help="Email body (single-line / short).")
    parser.add_argument(
        "--body-file",
        help="Path to a file whose contents become the email body (overrides --body).",
    )
    parser.add_argument(
        "--rate-check",
        action="store_true",
        help="Print current rate limit state and exit.",
    )
    parser.add_argument(
        "--skip-signature",
        action="store_true",
        help="Do NOT append the Lisa Monelli signature (advanced).",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Force a real send even if config.yml has dry_run: true.",
    )
    args = parser.parse_args(argv)

    cfg = load_config()

    if args.rate_check:
        state = rate_limit_state(cfg)
        print(json.dumps(state, indent=2))
        return 0

    if not args.to or not args.subject:
        parser.error("--to and --subject are required (use --rate-check for status).")

    # Resolve body.
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    else:
        parser.error("Provide --body or --body-file.")

    # Optional override for testing.
    if args.no_dry_run:
        cfg.dry_run = False

    result = send_raw(
        to=args.to,
        subject=args.subject,
        body=body,
        config=cfg,
        skip_signature=args.skip_signature,
    )
    print(result.to_json())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
