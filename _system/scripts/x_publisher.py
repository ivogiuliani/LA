#!/usr/bin/env python3
"""
My Villa — X (Twitter) Publisher

Closes the last mile for X: posts the tweet drafts that generate_social.py
already produces (`_drafts/social/<date>-x-<slug>.md`, body = tweet text) to
@myvilla_la via the X API v2 — the same role publish_instagram.py plays for IG.

Auth: OAuth 1.0a user-context (no extra deps — pure urllib + hmac, like the
rest of the repo). The 4 keys live in .env (setup: _system/docs/x_setup.md):

    X_API_KEY              consumer (API) key
    X_API_SECRET           consumer (API) key secret
    X_ACCESS_TOKEN         @myvilla_la access token (Read+Write)
    X_ACCESS_TOKEN_SECRET  access token secret

NB: X_API_KEY is NOT XAI_API_KEY (that one is Grok/xAI, the LLM).

Safety: posting NEVER happens without --publish-live. Without it (or with
--dry-run) you only get a preview. Daily cap via X_DAILY_CAP (default 4).

Usage:
  # Auth health (GET /2/users/me):
  python3 x_publisher.py --whoami

  # Post an arbitrary text (preview, then real):
  python3 x_publisher.py --text "hello world"                 # preview only
  python3 x_publisher.py --text "hello world" --publish-live  # real post

  # Post a draft file (body = tweet):
  python3 x_publisher.py --draft 2026-06-13-x-some-slug.md                 # preview
  python3 x_publisher.py --draft 2026-06-13-x-some-slug.md --publish-live  # real

  # Batch all approved X drafts in _drafts/social/:
  python3 x_publisher.py --dir --status approved --publish-live
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
DRAFTS_DIR = ROOT_DIR / "_drafts" / "social"            # generate_social.py output
PUBLISHED_DIR = SYSTEM_DIR / "social" / "posts" / "published"
PUBLISH_LOG = SYSTEM_DIR / "history" / "x_publish_log.json"

# ── X API config ─────────────────────────────────────────────────────
API_BASE = "https://api.x.com/2"
ACCOUNT_HANDLE = "myvilla_la"
TWEET_MAX = 280
DEFAULT_DAILY_CAP = 4


def load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v


load_dotenv()


# ══════════════════════════════════════════════════════════════════════
# OAuth 1.0a user-context client (urllib + hmac, no tweepy)
# ══════════════════════════════════════════════════════════════════════

def _credentials():
    ck = os.environ.get("X_API_KEY", "").strip()
    cs = os.environ.get("X_API_SECRET", "").strip()
    at = os.environ.get("X_ACCESS_TOKEN", "").strip()
    ats = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
    missing = [n for n, v in (("X_API_KEY", ck), ("X_API_SECRET", cs),
                              ("X_ACCESS_TOKEN", at),
                              ("X_ACCESS_TOKEN_SECRET", ats)) if not v]
    if missing:
        raise RuntimeError(f"missing in .env: {', '.join(missing)} "
                           f"(setup: _system/docs/x_setup.md)")
    return ck, cs, at, ats


def _pe(s):
    """RFC 3986 percent-encoding (OAuth-safe)."""
    return urllib.parse.quote(str(s), safe="-._~")


def _auth_header(method, url, ck, cs, at, ats, query=None):
    """Build the OAuth 1.0a Authorization header. A JSON request body is NOT
    part of the signature base — only oauth_* params (and any URL query
    params) are signed."""
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": at,
        "oauth_version": "1.0",
    }
    sign_params = {**(query or {}), **oauth}
    base_params = "&".join(f"{_pe(k)}={_pe(sign_params[k])}"
                           for k in sorted(sign_params))
    base = "&".join([method.upper(), _pe(url), _pe(base_params)])
    key = f"{_pe(cs)}&{_pe(ats)}".encode()
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pe(k)}="{_pe(v)}"'
                                for k, v in sorted(oauth.items()))


def _request(method, path, *, query=None, body=None, timeout=30):
    """Signed request to the X API. Returns (status_code, parsed_json)."""
    ck, cs, at, ats = _credentials()
    url = f"{API_BASE}/{path}"
    full_url = url + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Authorization": _auth_header(method, url, ck, cs, at, ats,
                                             query=query)}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(full_url, data=data, headers=headers,
                                 method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": "unparseable", "status": e.code}


def whoami(verbose=True):
    """GET /2/users/me — validate auth. Returns the username or None."""
    try:
        _credentials()
    except RuntimeError as e:
        if verbose:
            print(f"  ✗ {e}")
        return None
    code, data = _request("GET", "users/me")
    if code == 200 and "data" in data:
        u = data["data"]
        if verbose:
            print(f"  ✓ auth valid — @{u.get('username')} "
                  f"(id {u.get('id')}, name {u.get('name')!r})")
        return u.get("username")
    if verbose:
        print(f"  ✗ auth failed (HTTP {code}): {json.dumps(data)[:300]}")
    return None


def post_tweet(text, reply_to=None, quote_of=None):
    """POST /2/tweets. reply_to: reply to that tweet id. quote_of: quote that
    tweet id (a quote tweet — NOT subject to the target's 'who can reply'
    setting). Returns (ok, info) with id/url or error."""
    body = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    if quote_of:
        body["quote_tweet_id"] = str(quote_of)
    code, data = _request("POST", "tweets", body=body, timeout=45)
    if code in (200, 201) and "data" in data:
        tid = data["data"].get("id", "")
        return True, {"id": tid,
                      "url": f"https://x.com/{ACCOUNT_HANDLE}/status/{tid}"}
    return False, {"status": code, "error": data}


# ══════════════════════════════════════════════════════════════════════
# Drafts (generate_social.py format: channel: x, body = tweet text)
# ══════════════════════════════════════════════════════════════════════

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def parse_draft(path):
    raw = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return None, raw.strip()
    return yaml.safe_load(m.group(1)), m.group(2).strip()


def find_x_drafts(status=None):
    """X drafts in _drafts/social/ (channel: x), optionally filtered by status."""
    out = []
    if not DRAFTS_DIR.exists():
        return out
    for f in sorted(DRAFTS_DIR.glob("*-x-*.md")):
        fm, body = parse_draft(f)
        if not fm or fm.get("channel") != "x":
            continue
        if status and fm.get("status") != status:
            continue
        out.append((f, fm, body))
    return out


# ══════════════════════════════════════════════════════════════════════
# Daily cap + publish log
# ══════════════════════════════════════════════════════════════════════

def _load_log():
    if PUBLISH_LOG.exists():
        try:
            return json.loads(PUBLISH_LOG.read_text())
        except Exception:
            return []
    return []


def _append_log(entry):
    log = _load_log()
    log.append(entry)
    PUBLISH_LOG.parent.mkdir(parents=True, exist_ok=True)
    PUBLISH_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False))


def posted_today():
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for e in _load_log() if e.get("date") == today)


def daily_cap():
    try:
        return int(os.environ.get("X_DAILY_CAP", DEFAULT_DAILY_CAP))
    except ValueError:
        return DEFAULT_DAILY_CAP


# ══════════════════════════════════════════════════════════════════════
# Publish a draft (mark published mirrors the IG publisher)
# ══════════════════════════════════════════════════════════════════════

def _mark_published(path, fm, body, info):
    fm = dict(fm)
    fm["status"] = "published"
    fm["published_at"] = datetime.now().isoformat(timespec="seconds")
    fm["x_post_url"] = info["url"]
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    dst = PUBLISHED_DIR / path.name
    dst.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False,
                   allow_unicode=True) + "---\n\n" + body + "\n")
    if path.exists() and path.resolve() != dst.resolve():
        path.unlink()
    _append_log({"date": datetime.now().strftime("%Y-%m-%d"),
                 "id": info["id"], "url": info["url"], "draft": path.name})
    # Ledger condiviso: il pannello e lo sweep self-healing non riproporranno
    # questo contenuto (companion → chiave slug, reattivi → fingerprint testo).
    try:
        import social_ledger
        social_ledger.record("x", text=body, slug=fm.get("journal_slug"),
                             url=info.get("url", ""), note="x_publisher")
    except Exception:  # noqa: BLE001
        pass


def publish_text(text, *, live, draft=None):
    """Preview (default) or really post one tweet. Returns True on success
    (a successful preview counts as success)."""
    text = text.strip()
    n = len(text)
    flag = "" if n <= TWEET_MAX else f"  ⚠ {n} chars > {TWEET_MAX}"
    print(f"  tweet ({n} chars){flag}:")
    print("  " + text.replace("\n", "\n  "))

    if not live:
        print("  [preview] not posted (add --publish-live to post for real)")
        return True
    if n > TWEET_MAX:
        print(f"  ✗ refusing to post: {n} > {TWEET_MAX} chars")
        return False

    ok, info = post_tweet(text)
    if ok:
        print(f"  ✓ POSTED → {info['url']}")
        if draft is not None:
            path, fm, body = draft
            _mark_published(path, fm, body, info)
            print(f"    marked published → "
                  f"{(PUBLISHED_DIR / path.name).relative_to(ROOT_DIR)}")
    else:
        print(f"  ✗ post failed (HTTP {info['status']}): "
              f"{json.dumps(info['error'])[:300]}")
    return ok


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="My Villa — X (Twitter) Publisher")
    p.add_argument("--whoami", action="store_true",
                   help="Validate auth (GET /2/users/me)")
    p.add_argument("--text", help="Post arbitrary text")
    p.add_argument("--draft", help="Draft filename in _drafts/social/ (or a path)")
    p.add_argument("--dir", action="store_true",
                   help="Batch all X drafts in _drafts/social/")
    p.add_argument("--status", default="approved",
                   help="Status filter for --dir (default: approved)")
    p.add_argument("--publish-live", action="store_true",
                   help="Actually post (without this flag: preview only)")
    p.add_argument("--dry-run", action="store_true",
                   help="Force preview even with --publish-live")
    args = p.parse_args()

    live = args.publish_live and not args.dry_run
    print(f"\nMy Villa — X Publisher ({'LIVE' if live else 'PREVIEW'} MODE)")
    print("=" * 50)

    if args.whoami:
        sys.exit(0 if whoami() else 1)

    # In live mode, exit clean (not crash) if X credentials aren't set —
    # matches ig_publisher's "esce pulito" behavior in the cloud rail.
    if live:
        try:
            _credentials()
        except RuntimeError as e:
            print(f"  ⚠ {e}")
            print("  → no X credentials — nothing posted (clean exit)")
            sys.exit(0)

    # daily cap guard (only matters when actually posting)
    cap = daily_cap()
    if live and posted_today() >= cap:
        print(f"  ✗ daily cap reached ({posted_today()}/{cap}) — set X_DAILY_CAP to raise")
        sys.exit(1)

    if args.text:
        sys.exit(0 if publish_text(args.text, live=live) else 1)

    if args.draft:
        path = Path(args.draft)
        if not path.is_absolute() and not path.exists():
            path = DRAFTS_DIR / args.draft
        if not path.exists():
            print(f"  ✗ draft not found: {path}")
            sys.exit(1)
        fm, body = parse_draft(path)
        if not body:
            print(f"  ✗ empty draft body: {path}")
            sys.exit(1)
        sys.exit(0 if publish_text(body, live=live,
                                   draft=(path, fm or {}, body)) else 1)

    if args.dir:
        drafts = find_x_drafts(status=args.status)
        if not drafts:
            print(f"  No X drafts with status={args.status} in "
                  f"{DRAFTS_DIR.relative_to(ROOT_DIR)}")
            return
        print(f"  {len(drafts)} draft(s) status={args.status}"
              f"{' · cap ' + str(posted_today()) + '/' + str(cap) if live else ''}\n")
        done = 0
        for path, fm, body in drafts:
            if live and posted_today() >= cap:
                print(f"  ⏸ daily cap {cap} reached — stopping")
                break
            print(f"• {path.name}")
            if publish_text(body, live=live, draft=(path, fm, body)):
                done += 1
            print()
        print(f"  ✓ {done}/{len(drafts)} {'posted' if live else 'previewed'}")
        return

    p.error("Specify --whoami, --text, --draft, or --dir")


if __name__ == "__main__":
    main()
