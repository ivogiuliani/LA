#!/usr/bin/env python3
"""
linkedin_publisher.py — pubblica sulla pagina LinkedIn My Villa
l'articolo del giorno del Journal, via Community Management API.

Setup (una tantum, 2026-09-09):
  - App "My Villa Publisher" (Client ID 77luvadpik6bwg), verificata dalla
    pagina My Villa (urn:li:organization:144803403), redirect registrato
    http://localhost:8914/callback.
  - Accesso Community Management API richiesto; in attesa approvazione
    LinkedIn (form dati societari).

Flow quotidiano (GitHub Actions, 16:00 UTC = 9:00 PT = 18:00 CEST):
  1. Sceglie l'articolo più RECENTE di blog/*.json non ancora postato
     (ledger _system/social/linkedin_posted.json), max 7 giorni di età —
     gli arretrati più vecchi restano alla gestione manuale dal pannello.
  2. Caption = _li_proposal() del pannello (≤400 char, tono esplicativo).
  3. Carica la hero come thumbnail (initializeUpload) e crea il post
     come organizzazione via /rest/posts (link con anteprima + UTM).
  4. Aggiorna il ledger (committato dal workflow).

Credenziali (env o .env — MAI committate):
  LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
  LINKEDIN_ACCESS_TOKEN (60 gg) / LINKEDIN_REFRESH_TOKEN (365 gg)

Senza token: exit 0 con avviso (pipeline mai bloccata).

Uso:
  python3 linkedin_publisher.py --authorize   # una tantum: OAuth in browser
  python3 linkedin_publisher.py --dry-run     # mostra cosa posterebbe
  python3 linkedin_publisher.py               # run reale
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent
BLOG = ROOT / "blog"
LEDGER = ROOT / "_system" / "social" / "linkedin_posted.json"
ENV_FILE = ROOT / ".env"

ORG_URN = "urn:li:organization:144803403"
API = "https://api.linkedin.com"
LINKEDIN_VERSION = "202506"
REDIRECT_URI = "http://localhost:8914/callback"
SCOPES = "w_organization_social r_organization_social"
MAX_AGE_DAYS = 7

sys.path.insert(0, str(SCRIPT_DIR))
from publish_social_panel import _li_proposal, _utm  # noqa: E402


# ── credenziali ──────────────────────────────────────────────────────


def _load_env() -> dict:
    """Env di processo + fallback .env (senza dipendere da dotenv)."""
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    vals.update({k: v for k, v in os.environ.items() if k.startswith("LINKEDIN_")})
    return vals


def _save_env_tokens(access: str, refresh: str | None) -> None:
    """Persiste i token nel .env locale (gitignored). In Actions è no-op."""
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text().splitlines()
    def upsert(key: str, val: str):
        nonlocal lines
        pref = key + "="
        if any(l.startswith(pref) for l in lines):
            lines = [f"{pref}{val}" if l.startswith(pref) else l for l in lines]
        else:
            lines.append(f"{pref}{val}")
    upsert("LINKEDIN_ACCESS_TOKEN", access)
    if refresh:
        upsert("LINKEDIN_REFRESH_TOKEN", refresh)
    ENV_FILE.write_text("\n".join(lines) + "\n")


# ── HTTP helpers ─────────────────────────────────────────────────────


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _api(method: str, path: str, token: str, payload: dict | None = None,
         raw: bytes | None = None, content_type: str = "application/json"):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("LinkedIn-Version", LINKEDIN_VERSION)
    req.add_header("X-Restli-Protocol-Version", "2.0.0")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    elif raw is not None:
        data = raw
        req.add_header("Content-Type", content_type)
    req.data = data
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode()
        return (json.loads(txt) if txt.strip() else {}), dict(r.headers)


# ── OAuth ────────────────────────────────────────────────────────────


def authorize(env: dict) -> int:
    """Flusso una tantum: browser → consenso → code su localhost:8914 →
    scambio token → salvataggio in .env."""
    cid, secret = env.get("LINKEDIN_CLIENT_ID"), env.get("LINKEDIN_CLIENT_SECRET")
    if not cid or not secret:
        print("Metti LINKEDIN_CLIENT_ID e LINKEDIN_CLIENT_SECRET nel .env prima "
              "(dalla tab Auth dell'app su developer.linkedin.com).")
        return 1
    import http.server, secrets as pysecrets, threading, webbrowser
    state = pysecrets.token_urlsafe(16)
    auth_url = ("https://www.linkedin.com/oauth/v2/authorization?" +
                urllib.parse.urlencode({
                    "response_type": "code", "client_id": cid,
                    "redirect_uri": REDIRECT_URI, "state": state,
                    "scope": SCOPES,
                }))
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Fatto — puoi chiudere questa finestra.</h2>".encode())
        def log_message(self, *a):  # silenzia
            pass

    srv = http.server.HTTPServer(("localhost", 8914), H)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    print("Apro il browser per il consenso (login LinkedIn dell'admin pagina)…")
    webbrowser.open(auth_url)
    t.join(timeout=300)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        print("Autorizzazione non completata (timeout o stato errato).")
        return 1
    tok = _post_form("https://www.linkedin.com/oauth/v2/accessToken", {
        "grant_type": "authorization_code", "code": got["code"],
        "client_id": cid, "client_secret": secret, "redirect_uri": REDIRECT_URI,
    })
    access, refresh = tok["access_token"], tok.get("refresh_token")
    _save_env_tokens(access, refresh)
    print("Token salvati nel .env. Per GitHub Actions, aggiungi i secrets:")
    print("  LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET /")
    print("  LINKEDIN_ACCESS_TOKEN / LINKEDIN_REFRESH_TOKEN")
    return 0


def _refresh(env: dict) -> str | None:
    cid, secret = env.get("LINKEDIN_CLIENT_ID"), env.get("LINKEDIN_CLIENT_SECRET")
    rt = env.get("LINKEDIN_REFRESH_TOKEN")
    if not (cid and secret and rt):
        return None
    try:
        tok = _post_form("https://www.linkedin.com/oauth/v2/accessToken", {
            "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": cid, "client_secret": secret,
        })
    except urllib.error.HTTPError as e:
        print(f"[linkedin] refresh fallito: HTTP {e.code}")
        return None
    access = tok.get("access_token")
    if access:
        _save_env_tokens(access, tok.get("refresh_token") or rt)
    return access


# ── selezione articolo ───────────────────────────────────────────────


def _ledger() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"posted": {}}


def pick_article() -> tuple[dict, str] | None:
    """L'articolo pubblicato più recente non ancora postato (max 7 gg)."""
    led = _ledger()["posted"]
    today = date.today()
    best = None
    for jf in BLOG.glob("*.json"):
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or jf.stem
        if slug in led or not (BLOG / f"{slug}.html").exists():
            continue
        try:
            adate = date.fromisoformat(str(d.get("_date")))
        except (TypeError, ValueError):
            continue
        if adate > today or (today - adate).days > MAX_AGE_DAYS:
            continue
        if best is None or adate > best[1]:
            best = (d, adate, slug)
    if not best:
        return None
    return best[0], best[2]


# ── pubblicazione ────────────────────────────────────────────────────


def _upload_thumbnail(token: str, d: dict) -> str | None:
    """Carica la hero dell'articolo come image URN per la link-card."""
    hero = d.get("hero_image") or {}
    base = Path(hero.get("local_path", "")).name if isinstance(hero, dict) else ""
    local = BLOG / "assets" / "img" / base
    if not base or not local.exists():
        return None
    try:
        init, _ = _api("POST", "/rest/images?action=initializeUpload", token,
                       payload={"initializeUploadRequest": {"owner": ORG_URN}})
        up = init["value"]["uploadUrl"]
        urn = init["value"]["image"]
        req = urllib.request.Request(up, data=local.read_bytes(), method="PUT")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/octet-stream")
        urllib.request.urlopen(req, timeout=120).read()
        return urn
    except Exception as e:  # noqa: BLE001 — thumbnail best-effort
        print(f"[linkedin] thumbnail saltata: {e}")
        return None


def publish(dry: bool) -> int:
    env = _load_env()
    token = env.get("LINKEDIN_ACCESS_TOKEN")
    if not token and not dry:
        print("[linkedin] Nessun LINKEDIN_ACCESS_TOKEN — skip (setup incompleto).")
        return 0

    picked = pick_article()
    if not picked:
        print("[linkedin] Nessun articolo nuovo da postare.")
        return 0
    d, slug = picked
    url = _utm(f"https://myvilla.la/blog/{slug}.html", "linkedin")
    commentary = _li_proposal(d, url)

    print(f"[linkedin] Candidato: {d.get('_date')} — {slug}")
    if dry:
        print("--- commentary ---")
        print(commentary)
        print("--- url:", url)
        return 0

    def _try_post(tok: str):
        thumb = _upload_thumbnail(tok, d)
        article = {"source": url, "title": d.get("title") or slug}
        desc = d.get("subtitle") or d.get("meta_description")
        if desc:
            article["description"] = desc[:250]
        if thumb:
            article["thumbnail"] = thumb
        payload = {
            "author": ORG_URN,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {"article": article},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        return _api("POST", "/rest/posts", tok, payload=payload)

    try:
        _, headers = _try_post(token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("[linkedin] Token scaduto, provo il refresh…")
            token = _refresh(env)
            if not token:
                print("[linkedin] Refresh impossibile: rifare --authorize.")
                return 1
            _, headers = _try_post(token)
        else:
            print(f"[linkedin] Errore API {e.code}: {e.read().decode()[:300]}")
            return 1

    post_id = headers.get("x-restli-id") or headers.get("x-linkedin-id") or ""
    led = _ledger()
    led["posted"][slug] = {
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "post_id": post_id,
        "date": str(d.get("_date")),
    }
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[linkedin] PUBBLICATO ✓ {slug} (post {post_id})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--authorize", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.authorize:
        return authorize(_load_env())
    return publish(dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
