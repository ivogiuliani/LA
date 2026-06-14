#!/usr/bin/env python3
"""
reddit_client.py — pubblicazione su Reddit via API ufficiale:
commenti ai thread altrui E submission dei nostri articoli (self-post).

A differenza di Instagram, Reddit PERMETTE sia di commentare thread
altrui sia di submittare post via API (OAuth2, app di tipo "script").
  • Commenti: dal pannello si approva il commento proposto sul thread
    virale → POST /api/comment → live su u/<account> in 2 secondi.
  • Submission: dal pannello si approva il self-post di un nostro
    articolo → POST /api/submit (kind=self) → live nel subreddit. Il
    link all'articolo va NEL corpo, contestualizzato — mai un link nudo
    di brand (AutoModerator lo rimuove). Solo subreddit "discussione"
    in allowlist; mai i sub immagine/arte (no-blogspam severo).

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
  - REDDIT_SUBMIT_DAILY_CAP (default 1): tetto submission/24h. Postare
    propri articoli è la cosa più ban-prone su Reddit: 1/giorno è già
    aggressivo per un account giovane. Le submission vengono spesso
    rimosse finché l'account non ha karma/storia — è normale.
  - sanitize_founder_name su ogni testo (titolo + corpo).
  - Log: reddit_comment_log.jsonl (commenti) + reddit_submission_log.jsonl
    (submission), entrambi in _system/outreach/ (audit + cap).
  - Gli errori Reddit (RATELIMIT, THREAD_LOCKED, NO_SELFS,
    SUBREDDIT_NOTALLOWED, banned) tornano leggibili al pannello.

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
SUBMIT_LOG_PATH = SYSTEM_DIR / "outreach" / "reddit_submission_log.jsonl"

# Reddit hard limit sul titolo di un post.
TITLE_MAX = 300

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


# ══════════════════════════════════════════════════════════════════════
# SUBMISSION (self-post / link-post dei nostri articoli) — POST /api/submit
# ══════════════════════════════════════════════════════════════════════
# NB: la submission di brand è la cosa più ban-prone su Reddit. Cap a
# parte (default 1/giorno), log a parte, errori mappati in italiano per
# il pannello. Default = self-post col link nel corpo: un link nudo di
# brand viene rimosso da AutoModerator nella stragrande maggioranza dei
# subreddit.

# Codici di errore Reddit più comuni sulla submission → spiegazione utile.
_SUBMIT_ERROR_HINTS = {
    "SUBREDDIT_NOEXIST": "il subreddit non esiste",
    "SUBREDDIT_NOTALLOWED": "non sei autorizzato a postare qui (account "
                            "bannato dal sub, o sub in approval-only / "
                            "karma minimo non raggiunto)",
    "NO_SELFS": "questo subreddit non accetta self-post: serve un link-post",
    "NO_LINKS": "questo subreddit non accetta link: serve un self-post",
    "RATELIMIT": "rate limit Reddit (tipico degli account giovani / poco "
                 "karma): aspetta prima di riprovare",
    "SUBMIT_VALIDATION_FAILED": "regole del subreddit non soddisfatte "
                                "(flair obbligatorio? formato del titolo?)",
    "DOMAIN_BANNED": "il dominio del link è bannato in questo subreddit",
    "BAD_SR_NAME": "nome subreddit non valido",
    "TOO_LONG": f"titolo troppo lungo (max {TITLE_MAX})",
    "NO_TEXT": "corpo del self-post mancante",
}


def _normalize_subreddit(sr: str) -> str:
    """'r/fatFIRE' / '/r/fatFIRE ' / 'fatFIRE' → 'fatFIRE'."""
    sr = (sr or "").strip().lstrip("/")
    if sr.lower().startswith("r/"):
        sr = sr[2:]
    return sr.strip().strip("/")


def _humanize_submit_errors(errors: list) -> str:
    """[['NO_SELFS', 'this subreddit ...', 'kind']] → stringa leggibile."""
    parts = []
    for e in errors:
        code = e[0] if e else ""
        raw = e[1] if len(e) > 1 else ""
        hint = _SUBMIT_ERROR_HINTS.get(code)
        parts.append(f"{code}: {hint or raw}".strip(": ").strip())
    return "; ".join(p for p in parts if p) or "errore sconosciuto"


def _submit_cap() -> int:
    try:
        return int(os.environ.get("REDDIT_SUBMIT_DAILY_CAP", 1))
    except ValueError:
        return 1


def _submissions_today() -> int:
    if not SUBMIT_LOG_PATH.exists():
        return 0
    cutoff = time.time() - 86400
    n = 0
    for line in SUBMIT_LOG_PATH.read_text(encoding="utf-8").splitlines():
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


def _log_submit(record: dict) -> None:
    record["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    SUBMIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SUBMIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _submit(kind: str, subreddit: str, title: str, *, text: str = "",
            url: str = "", flair_id: str = "", flair_text: str = "") -> dict:
    """Core submission. kind: 'self' | 'link'.
    → {ok, post_url|error, needs_setup?}."""
    sr = _normalize_subreddit(subreddit)
    title = (title or "").strip()
    if not sr:
        return {"ok": False, "error": "subreddit mancante"}
    if not title:
        return {"ok": False, "error": "titolo mancante"}
    if len(title) > TITLE_MAX:
        return {"ok": False,
                "error": f"titolo {len(title)} > {TITLE_MAX} caratteri"}
    if kind == "self" and not (text or "").strip():
        return {"ok": False, "error": "corpo del self-post vuoto"}
    if kind == "link" and not (url or "").strip():
        return {"ok": False, "error": "url mancante per il link-post"}

    # Safety net nome fondatore su titolo + corpo (come commenti/email).
    try:
        from send_email import sanitize_founder_name
        title = sanitize_founder_name(title)
        if text:
            text = sanitize_founder_name(text)
    except Exception:  # noqa: BLE001
        pass

    cap = _submit_cap()
    done = _submissions_today()
    if done >= cap:
        return {"ok": False,
                "error": f"cap submission giornaliero raggiunto ({done}/{cap}) "
                         f"— riprova domani (REDDIT_SUBMIT_DAILY_CAP in .env). "
                         f"Su Reddit l'autopromozione frequente = ban."}

    token, err = _get_token()
    if not token:
        needs = "credenziali mancanti" in err
        return {"ok": False, "error": err, "needs_setup": needs}

    c = _creds()
    fields = {
        "api_type": "json",
        "sr": sr,
        "kind": kind,
        "title": title,
        "sendreplies": "true",
        "resubmit": "true",
    }
    if kind == "self":
        fields["text"] = text
    else:
        fields["url"] = url.strip()
    if flair_id:
        fields["flair_id"] = flair_id
    if flair_text:
        fields["flair_text"] = flair_text

    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        "https://oauth.reddit.com/api/submit",
        data=data,
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": _user_agent(c["username"])},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        _log_submit({"ok": False, "subreddit": sr, "kind": kind,
                     "error": f"HTTP {e.code}"})
        return {"ok": False, "error": f"Reddit HTTP {e.code}: {body}"}
    except Exception as e:  # noqa: BLE001
        _log_submit({"ok": False, "subreddit": sr, "kind": kind,
                     "error": str(e)})
        return {"ok": False, "error": f"network: {e}"}

    jr = (d.get("json") or {})
    errors = jr.get("errors") or []
    if errors:
        msg = _humanize_submit_errors(errors)
        _log_submit({"ok": False, "subreddit": sr, "kind": kind, "error": msg})
        return {"ok": False, "error": msg}

    jd = jr.get("data") or {}
    post_url = jd.get("url", "")
    post_id = jd.get("id", "") or jd.get("name", "")
    _log_submit({"ok": True, "subreddit": sr, "kind": kind, "title": title,
                 "post_url": post_url, "post_id": post_id})
    return {"ok": True, "post_url": post_url, "post_id": post_id,
            "message": f"Pubblicato su r/{sr} ({done + 1}/{cap} oggi)"}


def submit_self(subreddit: str, title: str, body: str, *,
                flair_id: str = "", flair_text: str = "") -> dict:
    """Self-post (text). Formato brand-safe: il link all'articolo va NEL
    corpo, contestualizzato — non come link nudo. → {ok, post_url|error}."""
    return _submit("self", subreddit, title, text=body,
                   flair_id=flair_id, flair_text=flair_text)


def submit_link(subreddit: str, title: str, url: str, *,
                flair_id: str = "", flair_text: str = "") -> dict:
    """Link-post. Usare SOLO nei pochi sub che lo tollerano (es.
    r/LuxuryRealEstate): nella maggior parte dei sub un link nudo di
    brand viene rimosso da AutoModerator. → {ok, post_url|error}."""
    return _submit("link", subreddit, title, url=url,
                   flair_id=flair_id, flair_text=flair_text)


def get_link_flairs(subreddit: str) -> dict:
    """Flair disponibili per i post di un subreddit (GET link_flair_v2).
    → {ok, flairs:[{id, text}]} | {ok:false, error}. Alcuni sub li
    impongono (submit fallisce con SUBMIT_VALIDATION_FAILED senza flair);
    altri non li espongono (403 → lista vuota, non un errore)."""
    sr = _normalize_subreddit(subreddit)
    if not sr:
        return {"ok": False, "error": "subreddit mancante"}
    token, err = _get_token()
    if not token:
        return {"ok": False, "error": err,
                "needs_setup": "credenziali mancanti" in err}
    c = _creds()
    req = urllib.request.Request(
        f"https://oauth.reddit.com/r/{urllib.parse.quote(sr)}/api/link_flair_v2",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": _user_agent(c["username"])})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        # 403 = sub non espone i flair / non hai i permessi → nessuna flair
        if e.code == 403:
            return {"ok": True, "flairs": []}
        return {"ok": False, "error": f"Reddit HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    flairs = [{"id": f.get("id", ""), "text": f.get("text", "")}
              for f in (data or []) if isinstance(f, dict) and f.get("id")]
    return {"ok": True, "flairs": flairs}


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
    args = sys.argv[1:]

    if "--whoami" in args:
        r = whoami()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)

    if args and args[0] in ("--submit-self", "--submit-link"):
        rest = args[1:]
        if len(rest) < 3:
            kind_arg = "corpo" if args[0] == "--submit-self" else "url"
            print(f"uso: reddit_client.py {args[0]} <subreddit> "
                  f'"<titolo>" "<{kind_arg}>"')
            sys.exit(2)
        sr, title, payload = rest[0], rest[1], rest[2]
        r = (submit_self(sr, title, payload) if args[0] == "--submit-self"
             else submit_link(sr, title, payload))
        print(json.dumps(r, indent=2, ensure_ascii=False))
        sys.exit(0 if r.get("ok") else 1)

    print(__doc__)
