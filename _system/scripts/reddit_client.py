#!/usr/bin/env python3
"""
reddit_client.py — pubblicazione commenti su Reddit via API ufficiale.

A differenza di Instagram, Reddit PERMETTE di commentare thread altrui
via API (OAuth2, app di tipo "script"). Flusso: dal pannello si approva
il commento proposto sul thread virale → POST /api/comment → live su
u/<account> in 2 secondi.

Setup (una tantum, ~3 min — vedi _system/docs/reddit_setup.md):
  1. https://www.reddit.com/prefs/apps → "create app" → tipo: script
  2. In .env:
       REDDIT_CLIENT_ID=...        (sotto il nome dell'app)
       REDDIT_CLIENT_SECRET=...
       REDDIT_USERNAME=...         (l'account che commenta)
       REDDIT_PASSWORD=...
  (password-grant: è il flusso UFFICIALE per le app script personali)

Safety:
  - REDDIT_DAILY_CAP (default 5): tetto commenti/24h — su Reddit lo
    spam si paga caro (ban subreddit + shadowban). Approvare con
    giudizio: 2-3 commenti/giorno di valore battono 10 mediocri.
  - sanitize_founder_name su ogni testo.
  - Log: _system/outreach/reddit_comment_log.jsonl (audit + cap).
  - Gli errori Reddit (RATELIMIT, THREAD_LOCKED, banned) tornano
    leggibili al pannello.

NOTA account nuovi: AutoModerator di molti subreddit filtra i commenti
di account con poco karma/età. I primi giorni conviene commentare da
account con un minimo di storia, o aspettarsi qualche rimozione.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
LOG_PATH = SYSTEM_DIR / "outreach" / "reddit_comment_log.jsonl"

_TOKEN_CACHE: dict = {}


def _load_dotenv():
    env = ROOT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


def _creds():
    _load_dotenv()
    return {
        "client_id": os.environ.get("REDDIT_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("REDDIT_CLIENT_SECRET", "").strip(),
        "username": os.environ.get("REDDIT_USERNAME", "").strip(),
        "password": os.environ.get("REDDIT_PASSWORD", "").strip(),
    }


def _user_agent(username: str) -> str:
    return os.environ.get(
        "REDDIT_USER_AGENT",
        f"macos:myvilla-radar:1.0 (by /u/{username or 'myvilla'})")


def _get_token() -> tuple[str | None, str]:
    """→ (bearer_token, error). Cache in-memory fino a scadenza."""
    now = time.time()
    if _TOKEN_CACHE.get("token") and _TOKEN_CACHE.get("exp", 0) > now + 30:
        return _TOKEN_CACHE["token"], ""

    c = _creds()
    if not all(c.values()):
        missing = [k for k, v in c.items() if not v]
        return None, f"credenziali mancanti in .env: {', '.join(missing)}"

    auth = base64.b64encode(
        f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "username": c["username"],
        "password": c["password"],
    }).encode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=data,
        headers={"Authorization": f"Basic {auth}",
                 "User-Agent": _user_agent(c["username"])},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        return None, f"auth HTTP {e.code}: {body}"
    except Exception as e:  # noqa: BLE001
        return None, f"auth error: {e}"

    tok = d.get("access_token")
    if not tok:
        # 2FA attiva? password grant fallisce con invalid_grant
        return None, (f"token mancante nella risposta ({d.get('error','?')}) "
                      "— se l'account ha la 2FA, usa password+codice "
                      "'password:codice2fa' o disattiva 2FA per l'app script")
    _TOKEN_CACHE["token"] = tok
    _TOKEN_CACHE["exp"] = now + int(d.get("expires_in", 3600))
    return tok, ""


def _thing_id_from_url(url: str) -> str | None:
    """https://www.reddit.com/r/X/comments/abc123/titolo/ → t3_abc123"""
    m = re.search(r"/comments/([a-z0-9]+)", url or "", re.I)
    return f"t3_{m.group(1)}" if m else None


def _comments_today() -> int:
    if not LOG_PATH.exists():
        return 0
    cutoff = time.time() - 86400
    n = 0
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("ok"):
            continue
        try:
            ts = datetime.fromisoformat(r.get("timestamp", "")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts > cutoff:
            n += 1
    return n


def _log(record: dict) -> None:
    record["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def post_comment(post_url: str, text: str) -> dict:
    """Commenta il post. → {ok, comment_url|error, needs_setup?}."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "testo vuoto"}

    # Safety net nome fondatore (stesso sanitizer delle email/caption)
    try:
        from send_email import sanitize_founder_name
        text = sanitize_founder_name(text)
    except Exception:  # noqa: BLE001
        pass

    cap = int(os.environ.get("REDDIT_DAILY_CAP", 5))
    done = _comments_today()
    if done >= cap:
        return {"ok": False,
                "error": f"cap giornaliero raggiunto ({done}/{cap}) — "
                         f"riprova domani (REDDIT_DAILY_CAP in .env)"}

    thing_id = _thing_id_from_url(post_url)
    if not thing_id:
        return {"ok": False, "error": f"URL non riconosciuto: {post_url}"}

    token, err = _get_token()
    if not token:
        needs = "credenziali mancanti" in err
        return {"ok": False, "error": err, "needs_setup": needs}

    c = _creds()
    data = urllib.parse.urlencode({
        "api_type": "json",
        "thing_id": thing_id,
        "text": text,
    }).encode()
    req = urllib.request.Request(
        "https://oauth.reddit.com/api/comment",
        data=data,
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": _user_agent(c["username"])},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        _log({"ok": False, "url": post_url, "error": f"HTTP {e.code}"})
        return {"ok": False, "error": f"Reddit HTTP {e.code}: {body}"}
    except Exception as e:  # noqa: BLE001
        _log({"ok": False, "url": post_url, "error": str(e)})
        return {"ok": False, "error": f"network: {e}"}

    jr = (d.get("json") or {})
    errors = jr.get("errors") or []
    if errors:
        # es. [["RATELIMIT", "you are doing that too much...", "ratelimit"]]
        msg = "; ".join(e[1] if len(e) > 1 else e[0] for e in errors)
        _log({"ok": False, "url": post_url, "error": msg})
        return {"ok": False, "error": msg}

    things = ((jr.get("data") or {}).get("things") or [])
    comment_url = ""
    if things:
        cd = things[0].get("data") or {}
        permalink = cd.get("permalink", "")
        comment_url = f"https://www.reddit.com{permalink}" if permalink else ""

    _log({"ok": True, "url": post_url, "comment_url": comment_url,
          "chars": len(text)})
    return {"ok": True, "comment_url": comment_url,
            "message": f"Commento pubblicato ({done + 1}/{cap} oggi)"}


def whoami() -> dict:
    """Valida credenziali → {ok, username, karma} | {ok: False, error}."""
    token, err = _get_token()
    if not token:
        return {"ok": False, "error": err}
    c = _creds()
    req = urllib.request.Request(
        "https://oauth.reddit.com/api/v1/me",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": _user_agent(c["username"])})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        return {"ok": True, "username": d.get("name"),
                "karma": d.get("total_karma"),
                "created": d.get("created_utc")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    if "--whoami" in sys.argv:
        r = whoami()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("ok") else 1)
    print(__doc__)
