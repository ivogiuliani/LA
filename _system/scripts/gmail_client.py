#!/usr/bin/env python3
"""
Gmail API client wrapper for the MyVilla outreach system.

Handles:
- OAuth 2.0 flow with refresh-token persistence
- Sending plain-text emails with the Lisa Monelli signature
- Listing recent messages (used by the reply monitor in Sprint 2)

The wrapper does NOT enforce dry-run, rate limits, or signature
formatting — those live in `send_email.py`. This file is a thin layer
over the Gmail HTTP API.

Config is read from `_system/outreach/config.yml`.

Usage (programmatic):
    from gmail_client import GmailClient, load_config
    cfg = load_config()
    client = GmailClient(cfg)
    message_id = client.send(to='x@y.com', subject='...', body='...')

Usage (first-time authorization — opens browser once):
    python3 gmail_client.py --authorize
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
from dataclasses import dataclass
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Project root is two directories up from this script (_system/scripts/ → root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "_system" / "outreach" / "config.yml"


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

@dataclass
class OutreachConfig:
    """Strongly-typed view of `_system/outreach/config.yml`."""
    sender_email: str
    sender_name: str
    signature: str
    dry_run: bool
    rate_limit_per_hour: int
    confirm_before_send: bool
    reply_to: str | None
    send_log: Path
    client_secrets: Path
    token_path: Path
    scopes: list[str]

    @classmethod
    def from_yaml(cls, path: Path = CONFIG_PATH) -> "OutreachConfig":
        if not path.exists():
            raise FileNotFoundError(
                f"Outreach config not found: {path}\n"
                "Expected YAML file with sender/send/oauth sections."
            )
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        sender = data.get("sender", {})
        send = data.get("send", {})
        oauth = data.get("oauth", {})

        # Resolve paths relative to PROJECT_ROOT.
        def _resolve(p: str) -> Path:
            candidate = Path(p)
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / candidate
            return candidate

        return cls(
            sender_email=sender.get("email", ""),
            sender_name=sender.get("name", ""),
            signature=sender.get("signature", "").rstrip(),
            dry_run=bool(send.get("dry_run", True)),
            rate_limit_per_hour=int(send.get("rate_limit_per_hour", 10)),
            confirm_before_send=bool(send.get("confirm_before_send", True)),
            reply_to=send.get("reply_to") or None,
            send_log=_resolve(send.get("send_log", "_system/outreach/send_log.jsonl")),
            client_secrets=_resolve(oauth.get("client_secrets", "")),
            token_path=_resolve(oauth.get("token", "")),
            scopes=list(oauth.get("scopes", [])),
        )


def load_config(path: Path = CONFIG_PATH) -> OutreachConfig:
    """Public helper — callers use this instead of constructing manually."""
    return OutreachConfig.from_yaml(path)


# --------------------------------------------------------------------------- #
# Gmail client
# --------------------------------------------------------------------------- #

class GmailClient:
    """
    Thin wrapper around the Gmail API `users.messages` resource.

    Instantiation is cheap (it only loads the token). The first API call
    lazily builds the `googleapiclient` service object.
    """

    def __init__(self, config: OutreachConfig):
        self.config = config
        self._creds: Credentials | None = None
        self._service = None  # built lazily

    # ------------------------------------------------------------------ #
    # Credentials lifecycle
    # ------------------------------------------------------------------ #

    def _load_creds(self) -> Credentials:
        """Load credentials from disk, refreshing if expired."""
        cfg = self.config
        creds: Credentials | None = None

        if cfg.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(cfg.token_path), cfg.scopes
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._persist_token(creds)
            return creds

        raise RuntimeError(
            f"No valid Gmail credentials at {cfg.token_path}.\n"
            "Run `python3 _system/scripts/gmail_client.py --authorize` once "
            "to complete the OAuth consent flow."
        )

    def _persist_token(self, creds: Credentials) -> None:
        """Write refreshed credentials back to disk."""
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config.token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    def authorize_interactive(self) -> None:
        """
        Run the OAuth consent flow in a local browser.

        Call this once after downloading the OAuth client JSON. It opens
        a browser window, the user accepts scopes, and a `token.json` is
        written alongside the client secrets.
        """
        cfg = self.config
        if not cfg.client_secrets.exists():
            raise FileNotFoundError(
                f"OAuth client secrets not found at {cfg.client_secrets}.\n"
                "Download the JSON from Google Cloud Console and save it as "
                "`_system/outreach/credentials/oauth_client.json`."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.client_secrets), cfg.scopes
        )
        creds = flow.run_local_server(port=0, prompt="consent")
        self._persist_token(creds)
        print(f"[gmail_client] Token saved to {cfg.token_path}")
        print(f"[gmail_client] Authorized as: {self._whoami(creds)}")

    def _whoami(self, creds: Credentials) -> str:
        """Return the authenticated email address. Used only for logging."""
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress", "<unknown>")
        except HttpError as e:
            return f"<error: {e}>"

    # ------------------------------------------------------------------ #
    # Service factory
    # ------------------------------------------------------------------ #

    @property
    def service(self):
        """Lazy Gmail API service — built on first access."""
        if self._service is None:
            if self._creds is None:
                self._creds = self._load_creds()
            self._service = build(
                "gmail", "v1", credentials=self._creds, cache_discovery=False
            )
        return self._service

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        attachments: list[Path] | None = None,
    ) -> dict[str, Any]:
        """
        Send an email. Returns the Gmail API response dict with `id` (the
        sent message ID) and `threadId`.

        Parameters
        ----------
        to, subject, body : the message
        cc, bcc           : optional additional recipients
        thread_id         : Gmail thread id to reply inside (keeps the
                            message in the correct conversation thread)
        in_reply_to       : RFC-822 Message-ID header of the message we're
                            replying to (e.g. `<CAG5K...@mail.gmail.com>`).
                            Required for email clients outside Gmail to
                            group the reply correctly.
        references        : Space-separated chain of RFC-822 Message-IDs
                            (used for deeper reply chains). If omitted and
                            `in_reply_to` is set, we default References to
                            the same value.
        attachments       : list of Paths to attach. MIME type is
                            auto-detected from the file extension.

        The caller is responsible for:
        - Appending the signature (see send_email.compose_body)
        - Respecting dry_run / rate limits (see send_email.send_draft)
        - Validating subject/body against outreach_voice.md /
          reply_voice.md
        """
        cfg = self.config

        # Compose the message. Three possible structures:
        #
        #   1. No HTML, no attachments → plain MIMEText (legacy path,
        #      unchanged so existing journalist pitches keep going out
        #      byte-identical to before).
        #   2. HTML provided (with or without attachments) → MIMEMultipart
        #      ("mixed") wrapping a MIMEMultipart("alternative") with both
        #      text/plain and text/html parts. Email clients pick HTML
        #      when they can, fall back to plain otherwise.
        #   3. No HTML, but attachments present → MIMEMultipart("mixed")
        #      with plain body + attached files.
        attachments = attachments or []
        if html_body:
            outer: MIMEText | MIMEMultipart = MIMEMultipart("mixed")
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", _charset="utf-8"))
            alt.attach(MIMEText(html_body, "html", _charset="utf-8"))
            outer.attach(alt)
            for path in attachments:
                self._attach_file(outer, path)
            msg = outer
        elif attachments:
            msg = MIMEMultipart("mixed")
            msg.attach(MIMEText(body, _charset="utf-8"))
            for path in attachments:
                self._attach_file(msg, path)
        else:
            msg = MIMEText(body, _charset="utf-8")

        msg["To"] = to
        msg["From"] = f"{cfg.sender_name} <{cfg.sender_email}>"
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if cfg.reply_to:
            msg["Reply-To"] = cfg.reply_to

        # Threading headers — required for reply-in-thread across clients.
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        request_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            request_body["threadId"] = thread_id
        return (
            self.service.users()
            .messages()
            .send(userId="me", body=request_body)
            .execute()
        )

    @staticmethod
    def _attach_file(msg: MIMEMultipart, path: Path) -> None:
        """Attach one file to a multipart MIME message. Preserves filename."""
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        part = MIMEBase(maintype, subtype)
        part.set_payload(path.read_bytes())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", "attachment", filename=path.name,
        )
        msg.attach(part)

    def list_recent(
        self, *, query: str = "", max_results: int = 20
    ) -> list[dict[str, Any]]:
        """
        List recent messages matching `query`. Used by the reply monitor
        in Sprint 2 — not called during a normal send flow.

        `query` uses Gmail search syntax, e.g. `from:richard@example.com`.
        """
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return resp.get("messages", [])

    def list_thread_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """
        Return all messages in a Gmail thread, oldest first.

        Each entry is the full Gmail API message payload (headers + body).
        Used by the reply monitor to detect journalist replies to our
        outreach emails.
        """
        resp = (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        return resp.get("messages", [])

    def get_message(
        self, message_id: str, *, fmt: str = "full"
    ) -> dict[str, Any]:
        """
        Fetch a single message by its Gmail internal id.

        `fmt` is one of `full`, `metadata`, `minimal`, `raw`. `full` is
        the default and returns body + headers — what the reply drafter
        needs to read the text the journalist wrote.
        """
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format=fmt)
            .execute()
        )

    def get_profile(self) -> dict[str, Any]:
        """Return the authenticated Gmail profile (email address + counts)."""
        return self.service.users().getProfile(userId="me").execute()


# --------------------------------------------------------------------------- #
# Message payload helpers (module-level — used by reply_monitor.py)
# --------------------------------------------------------------------------- #

def extract_header(message: dict[str, Any], name: str) -> str | None:
    """
    Return the first header value matching `name` (case-insensitive) from a
    Gmail API message payload, or None if missing.
    """
    headers = (message.get("payload") or {}).get("headers") or []
    name_lower = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value")
    return None


def extract_plain_text(message: dict[str, Any]) -> str:
    """
    Walk the MIME tree and return the first text/plain part as a string.
    Falls back to text/html (tag-stripped) if no plain-text part exists,
    then to the message snippet as a last resort.
    """
    import re
    from html import unescape

    def _walk(part: dict[str, Any]) -> str | None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode(
                "utf-8", errors="replace"
            )
        for sub in part.get("parts") or []:
            r = _walk(sub)
            if r is not None:
                return r
        return None

    def _walk_html(part: dict[str, Any]) -> str | None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if mime == "text/html" and data:
            html = base64.urlsafe_b64decode(data).decode(
                "utf-8", errors="replace"
            )
            # Strip tags crudely — good enough for classification.
            text = re.sub(r"<[^>]+>", " ", html)
            return unescape(re.sub(r"\s+", " ", text)).strip()
        for sub in part.get("parts") or []:
            r = _walk_html(sub)
            if r is not None:
                return r
        return None

    payload = message.get("payload") or {}
    text = _walk(payload) or _walk_html(payload)
    if text:
        return text.strip()
    return (message.get("snippet") or "").strip()


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="Run the OAuth consent flow in a browser (one-time setup).",
    )
    parser.add_argument(
        "--whoami",
        action="store_true",
        help="Print the authenticated Gmail address.",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    client = GmailClient(cfg)

    if args.authorize:
        client.authorize_interactive()
        return 0

    if args.whoami:
        profile = client.get_profile()
        print(json.dumps(profile, indent=2, ensure_ascii=False))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
