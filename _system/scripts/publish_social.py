#!/usr/bin/env python3
"""
My Villa — Social Publishing Module
Handles direct posting to X (Twitter) and Instagram via their APIs.

Credentials are read from .env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
  IG_ACCESS_TOKEN, IG_BUSINESS_ACCOUNT_ID
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent


def load_dotenv():
    """Load .env into os.environ (only vars not already set). Called at the
    start of the public entry points so credentials added to .env after the
    review server started are picked up without needing a restart."""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v and (k not in os.environ or not os.environ[k]):
            os.environ[k] = v


def _get_env(key):
    """Get env var, treating placeholder values as empty."""
    val = os.environ.get(key, "").strip()
    if not val or val == "PLACEHOLDER" or val.startswith("sk-PLACEHOLDER"):
        return ""
    return val


def _get_x_access_secret():
    """X access-token secret. Canonical name is X_ACCESS_TOKEN_SECRET
    (matches x_publisher.py, .env.example, tweepy). Falls back to the older
    X_ACCESS_SECRET so existing .env files keep working."""
    return _get_env("X_ACCESS_TOKEN_SECRET") or _get_env("X_ACCESS_SECRET")


# ══════════════════════════════════════════════════════════════════════
# CREDENTIAL CHECK
# ══════════════════════════════════════════════════════════════════════

def check_credentials():
    """Return dict with platform availability."""
    load_dotenv()
    x_ok = all([
        _get_env("X_API_KEY"),
        _get_env("X_API_SECRET"),
        _get_env("X_ACCESS_TOKEN"),
        _get_x_access_secret(),
    ])
    ig_ok = all([
        _get_env("IG_ACCESS_TOKEN"),
        _get_env("IG_BUSINESS_ACCOUNT_ID"),
    ])
    return {
        "x": {
            "configured": x_ok,
            "message": "" if x_ok else "Add X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET to .env",
        },
        "instagram": {
            "configured": ig_ok,
            "message": "" if ig_ok else "Add IG_ACCESS_TOKEN, IG_BUSINESS_ACCOUNT_ID to .env",
        },
    }


# ══════════════════════════════════════════════════════════════════════
# X (TWITTER) PUBLISHING — OAuth 1.0a via API v2
# ══════════════════════════════════════════════════════════════════════

def _oauth1_header(method, url, params, consumer_key, consumer_secret,
                   token, token_secret):
    """Build OAuth 1.0a Authorization header (HMAC-SHA1).
    Minimal implementation — no external dependencies.
    """
    import hashlib
    import hmac
    import time
    import uuid
    import base64

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }

    # Combine all params for signing
    all_params = {**oauth_params, **params}
    param_string = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )

    base_string = (
        f"{method.upper()}&"
        f"{urllib.parse.quote(url, safe='')}&"
        f"{urllib.parse.quote(param_string, safe='')}"
    )

    signing_key = (
        f"{urllib.parse.quote(consumer_secret, safe='')}&"
        f"{urllib.parse.quote(token_secret, safe='')}"
    )

    signature = base64.b64encode(
        hmac.new(
            signing_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    oauth_params["oauth_signature"] = signature

    auth_header = "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )
    return auth_header


def publish_to_x(text):
    """Post a tweet using X API v2. Returns dict with ok, tweet_id, url."""
    load_dotenv()
    consumer_key = _get_env("X_API_KEY")
    consumer_secret = _get_env("X_API_SECRET")
    access_token = _get_env("X_ACCESS_TOKEN")
    access_secret = _get_x_access_secret()

    if not all([consumer_key, consumer_secret, access_token, access_secret]):
        return {
            "ok": False,
            "error": "X API credentials not configured",
            "needs_setup": True,
            "setup_instructions": (
                "To publish directly to X:\n"
                "1. Go to developer.x.com and create a Free-tier app\n"
                "2. Enable OAuth 1.0a with Read+Write permissions\n"
                "3. Generate Access Token & Secret\n"
                "4. Add to your .env file:\n"
                "   X_API_KEY=your_api_key\n"
                "   X_API_SECRET=your_api_secret\n"
                "   X_ACCESS_TOKEN=your_access_token\n"
                "   X_ACCESS_TOKEN_SECRET=your_access_token_secret\n"
                "5. Restart the review server"
            ),
        }

    # X API v2 - Create Tweet
    url = "https://api.twitter.com/2/tweets"
    payload = json.dumps({"text": text}).encode("utf-8")

    auth = _oauth1_header(
        "POST", url, {},
        consumer_key, consumer_secret,
        access_token, access_secret,
    )

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            tweet_id = result.get("data", {}).get("id", "")
            return {
                "ok": True,
                "tweet_id": tweet_id,
                "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
                "message": f"Published to X (tweet {tweet_id})",
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(error_body)
            detail = err_data.get("detail", "") or err_data.get("title", "")
            errors = err_data.get("errors", [])
            if errors:
                detail = errors[0].get("message", detail)
        except json.JSONDecodeError:
            detail = error_body[:200]
        out = {"ok": False, "error": f"X API error ({e.code}): {detail}"}
        # 403 "duplicate content" means the tweet is ALREADY live on X. For
        # our queue that's proof-of-published, not a failure to retry — flag
        # it so the panel clears the card instead of looping on it forever.
        if e.code == 403 and "duplicate" in detail.lower():
            out["duplicate"] = True
        return out
    except Exception as e:
        return {"ok": False, "error": f"Network error: {e}"}


# ══════════════════════════════════════════════════════════════════════
# INSTAGRAM PUBLISHING — Meta Graph API (Container-based)
# ══════════════════════════════════════════════════════════════════════

def publish_to_instagram(caption, image_url=None):
    """Post to Instagram via Meta Graph API.

    For feed posts, an image_url is required (must be publicly accessible).
    If no image_url, this creates a text-only story or returns instructions.

    Returns dict with ok, post_id, url.
    """
    access_token = _get_env("IG_ACCESS_TOKEN")
    account_id = _get_env("IG_BUSINESS_ACCOUNT_ID")

    if not access_token or not account_id:
        return {
            "ok": False,
            "error": "Instagram API credentials not configured",
            "needs_setup": True,
            "setup_instructions": (
                "To publish directly to Instagram:\n"
                "1. Create a Meta Developer App at developers.facebook.com\n"
                "2. Link your Instagram Business/Creator account to a Facebook Page\n"
                "3. Get a long-lived access token with instagram_content_publish permission\n"
                "4. Find your IG Business Account ID via the Graph API\n"
                "5. Add to your .env file:\n"
                "   IG_ACCESS_TOKEN=your_long_lived_token\n"
                "   IG_BUSINESS_ACCOUNT_ID=your_account_id\n"
                "6. Restart the review server\n\n"
                "Note: Instagram feed posts require a publicly accessible image URL."
            ),
        }

    if not image_url:
        return {
            "ok": False,
            "error": "Instagram feed posts require an image. Use copy+paste for text-only posts.",
            "needs_image": True,
        }

    # Safety net sul nome del fondatore anche nelle caption social
    # (stesso sanitizer delle email: "Paolo Giordano" → "Paolo Mezzalama").
    try:
        from send_email import sanitize_founder_name
        caption = sanitize_founder_name(caption)
    except Exception:  # noqa: BLE001 — mai bloccare il publish per questo
        pass
    # Limite caption Instagram: 2200 caratteri.
    if len(caption) > 2200:
        caption = caption[:2197].rstrip() + "…"

    # Host/versione configurabili: con il flusso "Instagram API with
    # Instagram Login" (consigliato dal 2024, NON richiede pagina FB)
    # l'host è graph.instagram.com; col vecchio flusso Facebook Login
    # è graph.facebook.com. Default: instagram (la nostra guida usa quello).
    graph_host = _get_env("IG_GRAPH_HOST") or "graph.instagram.com"
    api_version = _get_env("IG_API_VERSION") or "v23.0"
    base = f"https://{graph_host}/{api_version}"

    # Step 1: Create media container
    container_url = f"{base}/{account_id}/media"
    container_params = urllib.parse.urlencode({
        "image_url": image_url,
        "caption": caption,
        "access_token": access_token,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(container_url, data=container_params, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            container_data = json.loads(resp.read().decode("utf-8"))
            container_id = container_data.get("id")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(error_body).get("error", {})
            detail = err.get("message", error_body[:200])
        except json.JSONDecodeError:
            detail = error_body[:200]
        return {"ok": False, "error": f"IG API error (container): {detail}"}
    except Exception as e:
        return {"ok": False, "error": f"Network error: {e}"}

    if not container_id:
        return {"ok": False, "error": "Failed to create media container"}

    # Step 1b: attendi che il container sia pronto. Per immagini grandi
    # il publish immediato fallisce con "media not ready" — poll dello
    # status_code fino a FINISHED (max ~30s).
    import time as _time
    status_url = (f"{base}/{container_id}"
                  f"?fields=status_code&access_token="
                  f"{urllib.parse.quote(access_token)}")
    for _ in range(10):
        try:
            with urllib.request.urlopen(status_url, timeout=15) as resp:
                sc = json.loads(resp.read().decode("utf-8")).get("status_code")
            if sc == "FINISHED":
                break
            if sc == "ERROR":
                return {"ok": False,
                        "error": "IG container in stato ERROR (immagine "
                                 "non scaricabile o formato non valido?)"}
        except Exception:  # noqa: BLE001 — lo status è best-effort
            pass
        _time.sleep(3)

    # Step 2: Publish the container
    publish_url = f"{base}/{account_id}/media_publish"
    publish_params = urllib.parse.urlencode({
        "creation_id": container_id,
        "access_token": access_token,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(publish_url, data=publish_params, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            publish_data = json.loads(resp.read().decode("utf-8"))
            post_id = publish_data.get("id", "")
            # Il media id NON è lo shortcode: il permalink vero va chiesto
            # all'API (instagram.com/p/<media_id>/ era un link rotto).
            permalink = ""
            if post_id:
                try:
                    pl_url = (f"{base}/{post_id}?fields=permalink"
                              f"&access_token="
                              f"{urllib.parse.quote(access_token)}")
                    with urllib.request.urlopen(pl_url, timeout=15) as r2:
                        permalink = json.loads(
                            r2.read().decode("utf-8")).get("permalink", "")
                except Exception:  # noqa: BLE001
                    permalink = ""
            return {
                "ok": True,
                "post_id": post_id,
                "url": permalink,
                "message": f"Published to Instagram (post {post_id})",
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(error_body).get("error", {})
            detail = err.get("message", error_body[:200])
        except json.JSONDecodeError:
            detail = error_body[:200]
        return {"ok": False, "error": f"IG API error (publish): {detail}"}
    except Exception as e:
        return {"ok": False, "error": f"Network error: {e}"}
