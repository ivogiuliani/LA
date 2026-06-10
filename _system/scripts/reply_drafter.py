#!/usr/bin/env python3
"""
Reply drafter for the MyVilla outreach system.

For a given Gmail thread where a journalist has replied to our first
outreach email, this script:

1. Loads the full thread state from `_system/outreach/replies/<thread>.json`
   (populated by `reply_monitor.py`).
2. Builds a system prompt from `_system/knowledge/reply_voice.md` plus
   facts from `project_brief.md`.
3. Asks Claude (Sonnet) to:
   - classify the journalist's intent (`request_material`,
     `request_call`, `request_both`, `polite_decline`, `needs_human`)
   - produce a send-ready subject + body that obeys reply_voice.md
   - decide which attachments to include (the canonical two PDFs, or
     none for `polite_decline`)
4. Writes the draft to `_drafts/email_replies/<thread>.json` for the
   dashboard to pick up.

The drafter never sends anything. It writes JSON. Sending is the
dashboard operator's job (two-click confirm + rate limit + signature
injection all live in `send_email.send_reply`).

CLI:
    python3 reply_drafter.py --thread <thread_id>
    python3 reply_drafter.py --all               # draft every thread with a pending reply
    python3 reply_drafter.py --thread <id> --verbose
    python3 reply_drafter.py --thread <id> --print    # don't write draft, just echo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Anthropic client is optional at import-time (same pattern as approve.py).
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Auto-model: tier risolto via model_resolver — upgrade automatico
# ai modelli più recenti appena compaiono su /v1/models (policy Ivo
# 2026-06-10). Fallback hardcoded se il resolver non è importabile:
# il modello non deve MAI bloccare la pipeline.
try:
    import sys as _sys
    if str(SCRIPT_DIR) not in _sys.path:
        _sys.path.insert(0, str(SCRIPT_DIR))
    from model_resolver import resolve as _resolve_model
except Exception:  # noqa: BLE001
    def _resolve_model(tier, _fb={"writer": "claude-fable-5",
                                  "heavy": "claude-opus-4-8",
                                  "balanced": "claude-sonnet-4-6",
                                  "cheap": "claude-haiku-4-5"}):
        return _fb.get(tier, "claude-sonnet-4-6")

PROJECT_ROOT = SCRIPT_DIR.parent.parent
REPLIES_DIR = PROJECT_ROOT / "_system" / "outreach" / "replies"
DRAFTS_DIR = PROJECT_ROOT / "_drafts" / "email_replies"
ATTACHMENTS_DIR = PROJECT_ROOT / "_system" / "outreach" / "attachments"
KNOWLEDGE_DIR = PROJECT_ROOT / "_system" / "knowledge"

REPLY_VOICE_PATH = KNOWLEDGE_DIR / "reply_voice.md"
PROJECT_BRIEF_PATH = KNOWLEDGE_DIR / "project_brief.md"

DEFAULT_ATTACHMENTS = [
    "_system/outreach/attachments/MyVilla_Press_Kit.pdf",
    "_system/outreach/attachments/MyVilla_Fact_Sheet.pdf",
]

CLAUDE_MODEL = _resolve_model("balanced")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class DraftedReply:
    """What we write to `_drafts/email_replies/<thread>.json`."""
    thread_id: str
    to: str
    to_name: str
    subject: str
    body: str
    classification: str          # request_material | request_call | request_both | polite_decline | needs_human
    confidence: float            # 0.0 – 1.0 from Claude's self-assessment
    reasoning: str               # one-liner, why Claude picked this class
    attachments: list[str]       # relative paths from project root
    needs_review: bool           # True if classification is `needs_human`
    suggested_next_step: str | None  # for `needs_human` only
    in_reply_to: str | None      # RFC-822 Message-ID header to include
    references: str | None       # RFC-822 References chain
    outreach_message_id: str | None
    outreach_sent_at: str | None
    replying_to_message_id: str  # Gmail id of the reply we're responding to
    replying_to_received_at: str
    journalist_body_preview: str  # first ~500 chars of what they wrote
    created_at: str
    model: str                   # Claude model used (or "fallback")

    def to_json(self) -> str:
        import dataclasses
        return json.dumps(dataclasses.asdict(self), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# .env loading (same pattern as approve.py / generate_radar_report.py)
# --------------------------------------------------------------------------- #

def _load_dotenv() -> None:
    """
    Load KEY=VALUE pairs from `.env` into `os.environ`. Mirrors exactly
    the pattern used by `approve.py` / `generate_radar_report.py`:
    only set the value when the env var is missing or empty (so a
    non-empty shell override always wins, but a *blank* shell var
    doesn't shadow the .env value).
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value and (key not in os.environ or not os.environ[key]):
            os.environ[key] = value


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

def _load_reply_voice() -> str:
    if not REPLY_VOICE_PATH.exists():
        raise FileNotFoundError(
            f"Reply voice file not found: {REPLY_VOICE_PATH}\n"
            "Create it from the template or restore from git."
        )
    return REPLY_VOICE_PATH.read_text(encoding="utf-8")


def _load_facts_snippet() -> str:
    """
    Return a trimmed slice of project_brief.md — we only need the core
    mission facts, not the whole dossier. Using the whole file would
    eat tokens and tempt Claude to repeat the pitch.
    """
    if not PROJECT_BRIEF_PATH.exists():
        return ""
    full = PROJECT_BRIEF_PATH.read_text(encoding="utf-8")
    # First 2500 chars covers mission + positioning + key partners.
    return full[:2500]


def _build_system_prompt() -> str:
    voice = _load_reply_voice()
    facts = _load_facts_snippet()
    return f"""You are Lisa Monelli, My Villa Media Team, drafting a reply to a
journalist who responded to our first outreach email. Follow the rules
in the voice file below EXACTLY. Do not invent facts. Do not re-pitch
the mission — it was already said in the first email.

The output MUST be a single JSON object with this schema (no prose, no
code fences, no comments):

{{
  "classification": "request_material" | "request_call" | "request_both" | "polite_decline" | "needs_human",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence — why you chose this class",
  "subject": "the Subject line (keep `Re: ` + original subject)",
  "body": "the email body WITHOUT signature (Lisa's block is appended automatically)",
  "include_attachments": true | false,
  "suggested_next_step": "required only when classification is 'needs_human', else null"
}}

Rules:
- Never include the signature in `body` — it is appended later.
- For `needs_human`, leave `subject` and `body` as empty strings,
  set `include_attachments` to false, and fill `suggested_next_step`.
- For `polite_decline`, `include_attachments` MUST be false.
- For `request_material`, `request_call`, `request_both`:
  `include_attachments` MUST be true.
- Confidence < 0.6 on any classification → downgrade to `needs_human`.
- When proposing call slots (request_call / request_both), use the
  current date as anchor — pick dates at least 48 hours ahead, Tue–Thu
  9 am–4 pm PT. Never pick dates already in the past.
- Today's date (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d')}.

================ VOICE FILE (reply_voice.md) ================

{voice}

================ PROJECT FACTS (trimmed) ================

{facts}

================ END ================
"""


def _build_user_prompt(
    *, original_subject: str, journalist_name: str, journalist_body: str,
    journalist_address: str,
) -> str:
    # Trim extremely long reply bodies — Gmail quote tails can be huge.
    # 4000 chars is plenty for intent classification + tone matching.
    trimmed = journalist_body[:4000]
    return f"""The journalist has replied. Here is what they wrote.

From: {journalist_name} <{journalist_address}>
Original subject (first email): {original_subject}
---
{trimmed}
---

Draft the reply now. Return the JSON object only.
"""


# --------------------------------------------------------------------------- #
# Claude call + parsing
# --------------------------------------------------------------------------- #

def _call_claude(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """
    Call Claude and parse the returned JSON. On any failure, raise so
    the caller can fall back to the safe `needs_human` path.
    """
    if not HAS_ANTHROPIC:
        raise RuntimeError(
            "anthropic package not installed. Run `pip3 install anthropic`."
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith(("sk-PLACEHOLDER", "sk-ant-PLACEHOLDER")):
        raise RuntimeError("ANTHROPIC_API_KEY missing or placeholder.")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    # Collect text blocks (Claude may split across multiple content blocks).
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    return _parse_json_response(text)


def _parse_json_response(text: str) -> dict[str, Any]:
    """
    Best-effort extraction of the first JSON object from Claude's reply.
    Handles stray code-fence wrapping if the model slips.
    """
    text = text.strip()
    # Strip ```json ... ``` fencing if present.
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = text.strip("`")
    # Fall back: find the first { ... } span.
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Draft synthesis
# --------------------------------------------------------------------------- #

def _safe_first_name(name: str, address: str) -> str:
    """Extract a best-effort first name for use in 'Hi {first_name},'."""
    if name:
        parts = name.strip().split()
        if parts:
            return parts[0]
    # Fall back to local part of the email.
    if address and "@" in address:
        local = address.split("@", 1)[0]
        return local.split(".")[0].capitalize()
    return ""


def _build_fallback_draft(
    *, thread_state: dict[str, Any], latest_reply: dict[str, Any],
    reason: str,
) -> DraftedReply:
    """
    Produce a safe `needs_human` draft when Claude fails (missing API
    key, JSON parse error, network issue). The dashboard will surface
    the reason and the human writes the reply manually.
    """
    addr = latest_reply.get("from_address", "")
    subject = latest_reply.get("subject") or thread_state.get(
        "outreach_subject") or "Following up from My Villa"
    return DraftedReply(
        thread_id=thread_state["thread_id"],
        to=addr,
        to_name=latest_reply.get("from_name", ""),
        subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
        body="",
        classification="needs_human",
        confidence=0.0,
        reasoning=f"Drafter fallback: {reason}",
        attachments=[],
        needs_review=True,
        suggested_next_step=(
            "Drafter was unable to auto-draft this reply. Read the "
            "journalist's message in the dashboard and write the reply by hand."
        ),
        in_reply_to=latest_reply.get("rfc822_message_id"),
        references=latest_reply.get("rfc822_message_id"),
        outreach_message_id=thread_state.get("outreach_message_id"),
        outreach_sent_at=thread_state.get("outreach_sent_at"),
        replying_to_message_id=latest_reply.get("gmail_message_id", ""),
        replying_to_received_at=latest_reply.get("received_at", ""),
        journalist_body_preview=(latest_reply.get("body") or "")[:500],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model="fallback",
    )


def draft_for_thread(
    thread_id: str, *, verbose: bool = False,
) -> DraftedReply:
    """Core entry point. Returns the DraftedReply (also written to disk)."""
    _load_dotenv()
    state_path = REPLIES_DIR / f"{thread_id}.json"
    if not state_path.exists():
        raise FileNotFoundError(
            f"No thread state for {thread_id} — run reply_monitor.py first."
        )
    thread_state = json.loads(state_path.read_text(encoding="utf-8"))
    all_entries = thread_state.get("replies") or []
    # Filter to GENUINE journalist replies — skip bounces and auto-replies.
    # The reply_monitor tags each record with `kind` ("reply" | "bounce" |
    # "auto_reply"). Legacy records without `kind` default to "reply".
    replies = [r for r in all_entries if r.get("kind", "reply") == "reply"]
    if not replies:
        kinds = sorted({r.get("kind", "reply") for r in all_entries})
        raise ValueError(
            f"Thread {thread_id} has no draftable replies "
            f"(recorded kinds: {kinds or ['<none>']})."
        )

    # Always draft a reply to the LATEST genuine message in the thread —
    # if there are multiple replies we haven't answered yet, the freshest
    # one usually carries the operative question.
    latest = sorted(replies, key=lambda r: r.get("received_at") or "")[-1]

    first_name = _safe_first_name(
        latest.get("from_name", ""), latest.get("from_address", ""),
    )
    original_subject = thread_state.get("outreach_subject") or "Following up"

    if not HAS_ANTHROPIC or not os.environ.get("ANTHROPIC_API_KEY"):
        draft = _build_fallback_draft(
            thread_state=thread_state,
            latest_reply=latest,
            reason="ANTHROPIC_API_KEY not set or anthropic package missing.",
        )
        _save_draft(draft)
        return draft

    try:
        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(
            original_subject=original_subject,
            journalist_name=latest.get("from_name") or first_name or "(unknown)",
            journalist_body=latest.get("body") or latest.get("snippet") or "",
            journalist_address=latest.get("from_address", ""),
        )
        if verbose:
            print(f"  [reply_drafter] Calling Claude for thread {thread_id}...")
        parsed = _call_claude(system_prompt, user_prompt)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  [reply_drafter] Claude call failed: {exc}")
        draft = _build_fallback_draft(
            thread_state=thread_state, latest_reply=latest,
            reason=f"{type(exc).__name__}: {exc}",
        )
        _save_draft(draft)
        return draft

    classification = parsed.get("classification", "needs_human")
    confidence = float(parsed.get("confidence", 0.0) or 0.0)
    # Safety net: low confidence → force human review.
    if confidence < 0.6 and classification != "needs_human":
        if verbose:
            print(
                f"  [reply_drafter] Confidence {confidence:.2f} < 0.6 — "
                f"downgrading {classification!r} → needs_human"
            )
        classification = "needs_human"

    include_attachments = bool(parsed.get("include_attachments", False))
    if classification == "polite_decline":
        include_attachments = False  # always enforce
    if classification == "needs_human":
        include_attachments = False

    attachments = DEFAULT_ATTACHMENTS.copy() if include_attachments else []

    subject = (parsed.get("subject") or "").strip()
    if classification != "needs_human":
        if not subject:
            subject = (
                f"Re: {original_subject}"
                if not original_subject.lower().startswith("re:")
                else original_subject
            )
        elif not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    body = (parsed.get("body") or "").strip() if classification != "needs_human" else ""

    draft = DraftedReply(
        thread_id=thread_id,
        to=latest.get("from_address", ""),
        to_name=latest.get("from_name", ""),
        subject=subject,
        body=body,
        classification=classification,
        confidence=confidence,
        reasoning=(parsed.get("reasoning") or "").strip()[:400],
        attachments=attachments,
        needs_review=(classification == "needs_human"),
        suggested_next_step=(parsed.get("suggested_next_step") or None),
        in_reply_to=latest.get("rfc822_message_id"),
        references=latest.get("rfc822_message_id"),
        outreach_message_id=thread_state.get("outreach_message_id"),
        outreach_sent_at=thread_state.get("outreach_sent_at"),
        replying_to_message_id=latest.get("gmail_message_id", ""),
        replying_to_received_at=latest.get("received_at", ""),
        journalist_body_preview=(latest.get("body") or "")[:500],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=CLAUDE_MODEL,
    )
    _save_draft(draft)
    return draft


def _save_draft(draft: DraftedReply) -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path = DRAFTS_DIR / f"{draft.thread_id}.json"
    path.write_text(draft.to_json() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _list_thread_ids_with_pending_replies() -> list[str]:
    """Thin wrapper around `reply_monitor.pending_draftable_threads` so
    the drafter shares exactly one definition of 'actionable'."""
    from reply_monitor import pending_draftable_threads
    return pending_draftable_threads()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="Draft for this thread id only.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Draft for every thread with a pending reply.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Do not write the draft to disk — print it to stdout.",
    )
    args = parser.parse_args(argv)

    if not args.thread and not args.all:
        parser.error("Provide --thread <id> or --all.")

    thread_ids: list[str]
    if args.thread:
        thread_ids = [args.thread]
    else:
        thread_ids = _list_thread_ids_with_pending_replies()
        if args.verbose:
            print(f"  [reply_drafter] Threads to draft: {len(thread_ids)}")

    rc = 0
    for tid in thread_ids:
        try:
            draft = draft_for_thread(tid, verbose=args.verbose)
            if args.print_only:
                print(draft.to_json())
            else:
                print(
                    f"  [reply_drafter] {tid}  class={draft.classification}  "
                    f"conf={draft.confidence:.2f}  "
                    f"attachments={len(draft.attachments)}  "
                    f"needs_review={draft.needs_review}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  [reply_drafter] {tid}: ERROR {type(exc).__name__}: {exc}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(_main())
