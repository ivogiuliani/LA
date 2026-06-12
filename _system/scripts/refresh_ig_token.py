#!/usr/bin/env python3
"""
My Villa — Instagram token auto-refresh

Instagram-login tokens (IGAA…) last ~60 days and can be refreshed via
  GET graph.instagram.com/refresh_access_token
      ?grant_type=ig_refresh_token&access_token=<current>

Rules enforced by Meta:
  - a token can be refreshed only after it is at least 24h old
  - it must still be valid (refresh BEFORE expiry, not after)

This script refreshes the token and rewrites IG_ACCESS_TOKEN in the repo
.env atomically. State (last refresh, expiry) lives in
_system/history/ig_token_state.json so other tools (radar health check,
dashboard) can surface days-to-expiry.

Designed to be called often and cheaply:
  - by the weekly LaunchAgent (com.myvilla.ig-token-refresh)
  - opportunistically by publish_instagram.py (maybe_refresh())
A 7-day internal gate means redundant calls are no-ops.

Usage:
  python3 refresh_ig_token.py              # refresh if >7d since last (gated)
  python3 refresh_ig_token.py --force      # refresh now regardless of gate
  python3 refresh_ig_token.py --status     # show days remaining, no refresh
  python3 refresh_ig_token.py --seed       # initialize state file (token created now)
  python3 refresh_ig_token.py --quiet      # for cron/launchd (minimal output)

Exit codes: 0 = ok/skipped, 1 = refresh failed (token may be at risk).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
ENV_FILE = ROOT_DIR / ".env"
STATE_FILE = SYSTEM_DIR / "history" / "ig_token_state.json"

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
GATE_DAYS = 7          # don't re-refresh more often than this
MIN_AGE_HOURS = 24     # Meta: token must be ≥24h old to refresh
WARN_DAYS_LEFT = 10    # status warns below this


# ── .env helpers ─────────────────────────────────────────────────────

def read_env_token() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("IG_ACCESS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def write_env_token(new_token: str):
    """Atomically replace the IG_ACCESS_TOKEN line in .env."""
    lines = ENV_FILE.read_text().splitlines(keepends=False)
    out, replaced = [], False
    for ln in lines:
        if ln.startswith("IG_ACCESS_TOKEN="):
            out.append(f"IG_ACCESS_TOKEN={new_token}")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(f"IG_ACCESS_TOKEN={new_token}")
    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(ENV_FILE.parent), delete=False, suffix=".env.tmp")
    try:
        tmp.write("\n".join(out) + "\n")
        tmp.close()
        os.replace(tmp.name, ENV_FILE)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
    # Keep current process env coherent
    os.environ["IG_ACCESS_TOKEN"] = new_token


# ── State ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str):
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def days_left(state: dict):
    exp = _parse(state.get("expires_at", ""))
    if not exp:
        return None
    return (exp - _now()).total_seconds() / 86400


# ── Refresh ──────────────────────────────────────────────────────────

def do_refresh(token: str, quiet: bool = False) -> dict:
    """Call the refresh endpoint. Returns the new state dict on success;
    raises RuntimeError with a readable message on failure."""
    url = (f"{REFRESH_URL}?grant_type=ig_refresh_token"
           f"&access_token={urllib.parse.quote(token)}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        if "24 hours" in body or "too soon" in body.lower():
            raise RuntimeError(f"token too young to refresh (<{MIN_AGE_HOURS}h): {body}")
        raise RuntimeError(f"HTTP {e.code}: {body}")

    new_token = data.get("access_token")
    expires_in = int(data.get("expires_in", 0))
    if not new_token or expires_in <= 0:
        raise RuntimeError(f"unexpected refresh response: {data}")

    write_env_token(new_token)
    state = {
        "last_refresh": _now().isoformat(timespec="seconds"),
        "expires_at": (_now() + timedelta(seconds=expires_in)).isoformat(timespec="seconds"),
        "expires_in_s": expires_in,
        "refreshed_by": "refresh_ig_token.py",
    }
    save_state(state)
    if not quiet:
        print(f"  ✓ token refreshed — valid {expires_in // 86400} more days "
              f"(until {state['expires_at'][:10]})")
    return state


def maybe_refresh(quiet: bool = True, gate_days: int = GATE_DAYS) -> bool:
    """Gated refresh for opportunistic callers (publish_instagram etc.).
    Refreshes only if last refresh is older than gate_days. Never raises —
    returns True if a refresh happened, False otherwise."""
    try:
        token = os.environ.get("IG_ACCESS_TOKEN", "") or read_env_token()
        if not token or token.startswith("PLACEHOLDER"):
            return False
        state = load_state()
        last = _parse(state.get("last_refresh", ""))
        if last and (_now() - last) < timedelta(days=gate_days):
            return False
        # Also respect the 24h minimum age when we know creation time
        if last and (_now() - last) < timedelta(hours=MIN_AGE_HOURS):
            return False
        do_refresh(token, quiet=quiet)
        return True
    except Exception as e:
        if not quiet:
            print(f"  ⚠ opportunistic refresh failed (non-fatal): {e}")
        return False


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IG token auto-refresh")
    parser.add_argument("--force", action="store_true",
                        help="Refresh now, ignoring the 7-day gate")
    parser.add_argument("--status", action="store_true",
                        help="Show token state without refreshing")
    parser.add_argument("--seed", action="store_true",
                        help="Initialize state file (use right after generating "
                             "a fresh token from the Meta dashboard)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    token = read_env_token()

    if args.seed:
        state = {
            "last_refresh": _now().isoformat(timespec="seconds"),
            "expires_at": (_now() + timedelta(days=60)).isoformat(timespec="seconds"),
            "expires_in_s": 60 * 86400,
            "refreshed_by": "seed (dashboard-generated token)",
        }
        save_state(state)
        print(f"  ✓ state seeded — expires ~{state['expires_at'][:10]}")
        return

    state = load_state()
    left = days_left(state)

    if args.status:
        if not token or token.startswith("PLACEHOLDER"):
            print("  ✗ no token in .env")
            sys.exit(1)
        if left is None:
            print("  ? no state file — run --seed (or --force to refresh now)")
        else:
            flag = "⚠" if left < WARN_DAYS_LEFT else "✓"
            print(f"  {flag} token expires in {left:.1f} days "
                  f"({state.get('expires_at', '?')[:10]}) — "
                  f"last refresh {state.get('last_refresh', '?')[:10]}")
        return

    if not token or token.startswith("PLACEHOLDER"):
        if not args.quiet:
            print("  ✗ no token in .env — generate one from the Meta dashboard first")
        sys.exit(1)

    # Gate (unless --force)
    if not args.force:
        last = _parse(state.get("last_refresh", ""))
        if last and (_now() - last) < timedelta(days=GATE_DAYS):
            if not args.quiet:
                nxt = (last + timedelta(days=GATE_DAYS)).isoformat(timespec='seconds')
                print(f"  · skipped (refreshed {state['last_refresh'][:10]}, "
                      f"next due {nxt[:10]})")
            return

    try:
        do_refresh(token, quiet=args.quiet)
    except RuntimeError as e:
        msg = str(e)
        if "too young" in msg:
            # Not an emergency: token simply <24h old. Treat as skip.
            if not args.quiet:
                print(f"  · skipped — {msg}")
            return
        print(f"  ✗ refresh FAILED: {msg}", file=sys.stderr)
        if left is not None:
            print(f"    token still valid for {left:.1f} days — "
                  f"investigate before it expires", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
