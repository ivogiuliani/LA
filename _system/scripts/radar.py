#!/usr/bin/env python3
"""
My Villa Engagement Radar — Standalone Research Pipeline
Fase 1: multi-source search → dedup → AI scoring (Sonnet) → structured JSON

Usage:
  python3 radar.py --output radar_2026-04-09.json
  python3 radar.py --model claude-sonnet-4-6 --lookback 15 --dry-run
  python3 radar.py --skip-grok --skip-gemini  # local test without paid APIs

Output: JSON with scored opportunities, ready for generate_radar_report.py
"""

import json
import os
import re
import sys
import time
import argparse
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

# Optional imports
try:
    import feedparser
    FEEDPARSER_OK = True
except ImportError:
    FEEDPARSER_OK = False

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False
    print("WARNING: 'anthropic' SDK not installed. Run: pip install anthropic")

# ── Paths (relative to this script) ──────────────────────────────────
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
    def _resolve_model(tier, _fb={"writer": "claude-opus-4-8",
                                  "heavy": "claude-opus-4-8",
                                  "balanced": "claude-sonnet-4-6",
                                  "cheap": "claude-haiku-4-5"}):
        return _fb.get(tier, "claude-sonnet-4-6")
_BALANCED_MODEL = _resolve_model("balanced")
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
KNOWLEDGE_DIR = SYSTEM_DIR / "knowledge"
RADAR_DIR = SYSTEM_DIR / "radar"

# ── Load .env if present ─────────────────────────────────────────────
def load_dotenv():
    """Minimal .env loader — no dependency needed."""
    env_file = SYSTEM_DIR.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v

load_dotenv()


# ══════════════════════════════════════════════════════════════════════
# API HEALTH CHECK
# Verifies that every external provider used by the radar is reachable
# *before* the long pipeline runs. Result is persisted into the radar
# JSON so the dashboard / markdown report can show a status banner —
# the operator can immediately see which sources contributed (or didn't)
# to the day's results.
#
# Each check has a short timeout and never raises; a network blip on one
# provider does not abort the radar.
# ══════════════════════════════════════════════════════════════════════

def _ping_apollo(key, timeout):
    import urllib.request, urllib.error, json as _j
    req = urllib.request.Request(
        "https://api.apollo.io/api/v1/auth/health",
        headers={
            "X-Api-Key": key,
            "User-Agent": "MyVillaRadar/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _j.loads(r.read().decode("utf-8", "replace"))
            healthy = body.get("healthy") is True
            logged = body.get("is_logged_in") is True
            ok = healthy and logged
            return ok, f"healthy={healthy}, logged_in={logged}"
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return True, f"HTTP {e.code} (auth reachable, endpoint moved)"
        return False, f"HTTP {e.code}"


def _ping_anthropic(key, timeout):
    import urllib.request, urllib.error, json as _j
    payload = _j.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = _j.loads(r.read())
        return True, f"model={body.get('model','?')}"


def _ping_xai(key, timeout):
    import urllib.request, json as _j
    req = urllib.request.Request(
        "https://api.x.ai/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = _j.loads(r.read())
        n = len(body.get("data", []))
        return True, f"{n} models available"


def _ping_gemini(key, timeout):
    import urllib.request, json as _j
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = _j.loads(r.read())
        n = len(body.get("models", []))
        return True, f"{n} models"


def _ping_google_cse(key, timeout):
    import urllib.request, urllib.error, json as _j
    cx = os.environ.get("GOOGLE_CSE_ENGINE_ID", "").strip()
    # Treat PENDING / empty as not-configured; the radar's CSE step will
    # still run (and likely return zero), but we report it as 'skip'.
    if not cx or cx.upper() == "PENDING":
        return None, "GOOGLE_CSE_ENGINE_ID not configured"
    url = (
        f"https://www.googleapis.com/customsearch/v1"
        f"?key={key}&cx={cx}&q=test&num=1"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = _j.loads(r.read())
            n = len(body.get("items", []))
            return True, f"{n} results returned for ping query"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"


def _ping_brave(key, timeout):
    """Verify the Brave Search API key with a cheap one-word query.

    Returns (True, "<plan-info>") on success, (False, "...") on failure.
    Brave's /res/v1/web/search endpoint costs nothing on the free tier
    until quota is hit, so this ping is safe to run every radar cycle.
    """
    import urllib.request, urllib.error, json as _j
    req = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?q=test&count=1",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _j.loads(r.read())
            n = len((body.get("web") or {}).get("results") or [])
            # Try to surface the rate-limit headers (X-RateLimit-Remaining)
            # so the operator sees free-tier quota burn.
            remaining = r.headers.get("X-RateLimit-Remaining", "?")
            return True, f"{n} results, monthly quota remaining: {remaining}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"HTTP {e.code} (invalid key)"
        if e.code == 429:
            return False, "HTTP 429 (quota exhausted)"
        return False, f"HTTP {e.code}"


def _ping_apify(key, timeout):
    """Verify the Apify token by calling the cheap /users/me endpoint.
    Returns (True, "<plan> — $X.XX credit left") on success."""
    import urllib.request, urllib.error, json as _j
    url = f"https://api.apify.com/v2/users/me?token={key}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = _j.loads(r.read())
            data = body.get("data", body)
            plan = (data.get("plan") or {}).get("id") or data.get("planId") or "?"
            usage = data.get("usage", {})
            limits = data.get("currentBillingPeriodUsage", {})
            # Apify monthly free credit info (best-effort; field names vary
            # by account type)
            credit_used = limits.get("usageUsd") or usage.get("monthlyUsageUsd") or 0
            extras = ""
            if credit_used:
                extras = f", ${credit_used:.2f} used this month"
            return True, f"plan={plan}{extras}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "HTTP 401 (invalid token)"
        return False, f"HTTP {e.code}"


# Provider name → (env var, ping fn). Order is the order shown in reports.
# Apollo removed 2026-05-04 (subscription cancelled; the apollo_lookup
# module still imports gracefully and short-circuits to None when the
# env var is missing — see apollo_lookup.lookup()). To restore Apollo,
# uncomment the line below and re-add APOLLO_API_KEY to .env.
_API_CHECKS = (
    ("anthropic",  "ANTHROPIC_API_KEY",   _ping_anthropic),
    # ("apollo",     "APOLLO_API_KEY",      _ping_apollo),
    ("xai_grok",   "XAI_API_KEY",         _ping_xai),
    ("gemini",     "GEMINI_API_KEY",      _ping_gemini),
    ("google_cse", "GOOGLE_CSE_API_KEY",  _ping_google_cse),
    ("brave",      "BRAVE_API_KEY",       _ping_brave),
    ("apify",      "APIFY_API_TOKEN",     _ping_apify),
)


def api_health_check(timeout=10):
    """Probe every external API the radar relies on.

    Returns a dict { provider: {status, detail, env_var} } where status is
    one of: "ok", "fail", "missing", "skip".
      - ok:      reachable and authenticated
      - fail:    key set but request failed (network, auth, rate limit)
      - missing: env var not set
      - skip:    secondary config missing (e.g. CSE engine id) — feature
                 effectively disabled but key exists
    """
    out = {}
    for name, env_var, fn in _API_CHECKS:
        # Allow GROK_API_KEY as fallback for XAI (radar.py uses both names).
        key = os.environ.get(env_var, "").strip()
        if not key and env_var == "XAI_API_KEY":
            key = os.environ.get("GROK_API_KEY", "").strip()
        if not key:
            out[name] = {
                "status": "missing",
                "detail": f"{env_var} not set",
                "env_var": env_var,
            }
            continue
        try:
            result = fn(key, timeout)
            # `_ping_google_cse` may return (None, reason) for skip
            if result is None or result[0] is None:
                detail = result[1] if result else "skipped"
                out[name] = {
                    "status": "skip", "detail": detail, "env_var": env_var,
                }
                continue
            ok, detail = result
            out[name] = {
                "status": "ok" if ok else "fail",
                "detail": detail,
                "env_var": env_var,
            }
        except Exception as e:  # noqa: BLE001 — network/parse/etc.
            out[name] = {
                "status": "fail",
                "detail": f"{type(e).__name__}: {str(e)[:120]}",
                "env_var": env_var,
            }
    return out


def print_api_health(health):
    """Pretty-print health dict to stdout. Pure side-effect."""
    sym = {"ok": "✓", "fail": "✗", "missing": "–", "skip": "○"}
    print("API health check:")
    for name, info in health.items():
        s = info.get("status", "?")
        print(f"  {sym.get(s, '?'):2s} {name:12s} {s:8s} {info.get('detail', '')}")


# ══════════════════════════════════════════════════════════════════════
# CONFIG LOADING
# ══════════════════════════════════════════════════════════════════════

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_keywords_config():
    return load_yaml(CONFIG_DIR / "radar-keywords.yml")


def load_brand_voice():
    return load_yaml(CONFIG_DIR / "brand-voice.yml")


def load_dedup_index(path=None):
    """Load previously reported articles for deduplication."""
    path = path or (RADAR_DIR / "previously_reported.json")
    if not path.exists():
        return {"reported_articles": []}
    with open(path) as f:
        return json.load(f)


def get_reported_urls(dedup_data):
    """Extract set of known URLs from dedup index."""
    urls = set()
    for article in dedup_data.get("reported_articles", []):
        url = article.get("url")
        if url:
            urls.add(url)
    return urls


def get_reported_titles(dedup_data):
    """Extract set of known titles (lowercased) for fuzzy dedup."""
    return {a.get("title", "").lower().strip() for a in dedup_data.get("reported_articles", [])}


# ══════════════════════════════════════════════════════════════════════
# SEARCH SOURCES
# ══════════════════════════════════════════════════════════════════════

def google_cse_search(api_key, cx, clusters, date_restrict="d7"):
    """Search Google Custom Search Engine."""
    # Treat placeholders like "PENDING", "TODO", empty string, or known bad
    # prefixes as "not configured" so we don't blast Google with 65 doomed
    # queries on every radar scan. The user must create a Programmable
    # Search Engine at https://programmablesearchengine.google.com/ and
    # paste the resulting cx ID into .env as GOOGLE_CSE_ENGINE_ID=xxx.
    _CX_PLACEHOLDERS = {"", "PENDING", "TODO", "TBD", "CHANGEME", "YOUR_CX_HERE"}
    if not api_key or not cx or cx.strip().upper() in _CX_PLACEHOLDERS:
        if cx and cx.strip().upper() in _CX_PLACEHOLDERS:
            print(
                f"  [CSE] Skipped — GOOGLE_CSE_ENGINE_ID is {cx!r} (placeholder). "
                f"Create a search engine at https://programmablesearchengine.google.com/ "
                f"and replace the value in .env."
            )
        else:
            print("  [CSE] Skipped — no API key or search engine ID configured")
        return []

    results = []
    base_url = "https://www.googleapis.com/customsearch/v1"
    query_count = 0

    consecutive_failures = 0
    for cluster_id, cluster_data in clusters.items():
        if consecutive_failures >= 3:
            break
        keywords = cluster_data.get("keywords", [])
        for q in keywords:
            if query_count >= 80:
                print(f"  [CSE] Query limit reached ({query_count}), stopping")
                break
            if consecutive_failures >= 3:
                # Con la key in 403 da settimane, prima di questo abort
                # si sparavano ~80 richieste condannate OGNI giorno.
                print("  [CSE] 3 errori consecutivi — abort (key/quota KO)")
                break
            try:
                resp = requests.get(base_url, params={
                    "key": api_key,
                    "cx": cx,
                    "q": q,
                    "dateRestrict": date_restrict,
                    "num": 5,
                }, timeout=10)
                query_count += 1  # conta ANCHE gli errori (prima no →
                # il cap non scattava mai con errori persistenti)
                resp.raise_for_status()
                data = resp.json()
                consecutive_failures = 0

                for item in data.get("items", []):
                    results.append({
                        "source": "google_cse",
                        "cluster": cluster_id,
                        "query": q,
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", ""),
                        "publication": item.get("displayLink", ""),
                        # or-guard: metatags=[] vuoto causava IndexError
                        "date": (item.get("pagemap", {}).get("metatags") or [{}])[0].get(
                            "article:published_time", ""),
                    })
                time.sleep(0.5)
            except Exception as e:
                consecutive_failures += 1
                # NON stampare l'eccezione raw: requests include l'URL
                # completo con ?key=AIza... → API key nei log (anche CI).
                status = getattr(getattr(e, "response", None), "status_code", "?")
                print(f"  [CSE] Error '{q}': {type(e).__name__} (HTTP {status})")

    print(f"  [CSE] {len(results)} results from {query_count} queries")
    return results


# Hosts to discard from Brave results before downstream scoring.
# These are domains that frequently match our keywords but never produce
# actionable content for outreach: encyclopedias, video platforms, social
# networks, listing aggregators, and obvious construction-vendor SEO farms.
# The AI scoring step would eventually discard them anyway, but pruning
# them here saves a few cents per radar run on Sonnet tokens.
_NOISE_HOST_BLACKLIST = {
    # Encyclopedias / wikis
    "wikipedia.org", "en.wikipedia.org",
    # Video / social
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "instagram.com", "www.instagram.com",
    "pinterest.com", "www.pinterest.com",
    "tiktok.com", "www.tiktok.com",
    # Generic listing / shopping
    "yelp.com", "www.yelp.com", "yellowpages.com",
    "amazon.com", "ebay.com",
    # Q&A / forums (low signal)
    "quora.com", "www.quora.com",
    "answers.com", "ask.com",
    # Short-term rental marketplaces — they often appear for "luxury
    # villa California" type queries but never produce actionable PR
    # outreach. They're listing aggregators, not publications.
    "airbnb.com", "www.airbnb.com",
    "vrbo.com", "www.vrbo.com",
    "homeaway.com", "www.homeaway.com",
    "booking.com", "www.booking.com",
}


def _is_noise_host(host):
    """True if a result host is on the global noise blacklist.

    Used by both brave_search() at fetch time and deduplicate() at the
    pipeline-merge stage, so the same low-signal domain is filtered no
    matter which source surfaced it (Gemini, RSS, Reddit, etc).
    """
    if not host:
        return False
    h = host.lower().strip().lstrip(".")
    if h in _NOISE_HOST_BLACKLIST:
        return True
    # Strip the leading subdomain once and re-check, so blog.foo.com
    # matches a foo.com entry. We don't go further than one strip to
    # avoid over-matching (e.g. foo.bar.org → bar.org).
    parts = h.split(".")
    if len(parts) > 2 and ".".join(parts[1:]) in _NOISE_HOST_BLACKLIST:
        return True
    return False


def _host_from_url(url):
    """Best-effort URL → netloc (host) without throwing on garbage input."""
    if not url:
        return ""
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _interleave_cluster_keywords(clusters):
    """Yield (cluster_id, keyword) pairs round-robin across clusters.

    Why: if we just iterated clusters in YAML order and hit a query cap,
    we'd burn the entire budget on the first 2-3 clusters (insurance,
    materials, regulation) and never touch architecture / Italian villa.
    Round-robin guarantees every cluster gets a fair share even under
    aggressive caps. With a cap of 30 across 7 clusters, each gets ~4
    queries before any cluster runs out, which is exactly what we want
    to balance the radar's current insurance bias.
    """
    cluster_iters = [
        (cid, iter(c.get("keywords", []) or []))
        for cid, c in clusters.items()
    ]
    while cluster_iters:
        next_iters = []
        for cid, it in cluster_iters:
            try:
                yield cid, next(it)
                next_iters.append((cid, it))
            except StopIteration:
                # This cluster is exhausted; drop it from the rotation.
                pass
        cluster_iters = next_iters


def _lookback_to_brave_freshness(lookback_days):
    """Map a numeric lookback window onto Brave's freshness parameter.

    Brave accepts pd/pw/pm/py (past day/week/month/year). For arbitrary
    day counts we round up to the next bucket so we don't accidentally
    drop relevant items just inside the window.
    """
    if lookback_days <= 1:  return "pd"
    if lookback_days <= 7:  return "pw"
    if lookback_days <= 31: return "pm"
    return "py"


def brave_search(api_key, clusters, lookback_days=7, query_cap=30):
    """Search via Brave Web Search API — drop-in alternative to Google CSE.

    Why this exists: Google CSE has been returning 403 PERMISSION_DENIED
    on this account since 2026-04-27 (ticket open with Google). Brave's
    Web Search has an independent index and the same coverage of luxury
    /architecture publications we care about. Enabled by setting
    BRAVE_API_KEY in .env (get one at https://api.search.brave.com/).

    When CSE comes back, both sources will run in parallel — Brave's
    independent index often surfaces results the Google index doesn't
    (and vice versa), so keeping both is a net win.

    Pricing math (as of 2026-05): Brave Search is $5 per 1,000 requests,
    but they apply a $5 monthly credit automatically. So the first 1,000
    requests/month cost $0. With a daily radar cycle we'd burn
    query_cap × 30 requests/month. We default to 30/day = 900/month,
    leaving a ~10% safety margin under the free credit.

    Args:
      api_key: BRAVE_API_KEY (empty string → skip without burning credits)
      clusters: same clusters dict consumed by google_cse_search
      lookback_days: forwarded to freshness mapping
      query_cap: hard cap on queries per run. Default 30 keeps daily
        scheduling under the free $5/month credit (30 × 30 = 900 < 1000).
        Raise it only if you're OK paying $5 per extra 1,000 queries.
    """
    if not api_key or not api_key.strip():
        print("  [Brave] Skipped — BRAVE_API_KEY not configured. "
              "Get a free key at https://api.search.brave.com/ and "
              "add BRAVE_API_KEY=… to .env to enable.")
        return []

    results = []
    base_url = "https://api.search.brave.com/res/v1/web/search"
    freshness = _lookback_to_brave_freshness(lookback_days)
    query_count = 0
    consecutive_failures = 0

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key.strip(),
        "User-Agent": "MyVillaRadar/1.0 (+https://myvilla.la)",
    }

    aborted = False
    for cluster_id, q in _interleave_cluster_keywords(clusters):
        if query_count >= query_cap:
            print(f"  [Brave] Query cap reached ({query_count}), stopping")
            break
        if consecutive_failures >= 5:
            print(f"  [Brave] 5 consecutive failures, aborting")
            break
        try:
            resp = requests.get(base_url, params={
                "q": q,
                "count": 5,
                "freshness": freshness,
                "country": "us",
                "safesearch": "moderate",
            }, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            query_count += 1
            consecutive_failures = 0

            for item in data.get("web", {}).get("results", []):
                # Brave returns netloc-style host (e.g. "www.dezeen.com")
                # in profile.long_name or meta_url.hostname; fall back to
                # parsing the URL ourselves so the field is always set.
                url = item.get("url", "")
                host = (item.get("meta_url") or {}).get("hostname") or ""
                if not host and url:
                    try:
                        host = urllib.parse.urlparse(url).netloc
                    except Exception:
                        host = ""

                # Filter out Wikipedia / YouTube / social / listing farms
                # so the AI scorer doesn't waste tokens on them.
                if _is_noise_host(host):
                    continue

                pub = (
                    (item.get("profile") or {}).get("long_name") or host
                )

                results.append({
                    "source": "brave",
                    "cluster": cluster_id,
                    "query": q,
                    "title": item.get("title", ""),
                    "url": url,
                    "snippet": item.get("description", ""),
                    "publication": pub,
                    # Brave gives age as a human string like "2 days ago"
                    # or sometimes a real ISO date in page_age.
                    "date": item.get("page_age") or item.get("age") or "",
                })
            # Free tier: 1 qps. Sleep slightly above 1s to avoid 429s.
            time.sleep(1.05)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                # Rate limit — back off harder and continue.
                print(f"  [Brave] 429 rate-limited on {q!r}; backing off 5s")
                time.sleep(5)
                consecutive_failures += 1
            elif status in (401, 403):
                print(f"  [Brave] HTTP {status} — key invalid or quota exhausted. Stopping.")
                aborted = True
                break
            else:
                print(f"  [Brave] HTTP {status} on {q!r}")
                consecutive_failures += 1
        except Exception as e:
            print(f"  [Brave] Error '{q}': {type(e).__name__}: {e}")
            consecutive_failures += 1

    print(f"  [Brave] {len(results)} results from {query_count} queries"
          f"{' (aborted on auth/quota)' if aborted else ''}")
    return results


def reddit_search(config, lookback_days=7):
    """Discover relevant Reddit threads via Brave (site:reddit.com).

    Reddit ha chiuso gli endpoint JSON pubblici (403 anche su old.reddit,
    verificato 2026-06-14) e la Data API è gated dalla Responsible Builder
    Policy 2025 ai soli casi di moderazione → niente accesso diretto per
    noi. Brave indicizza Reddit: lo usiamo per scoprire i thread su cui
    vale la pena commentare. Si pubblica a mano dal pannello (tasto
    "Reply manually": copia il commento già pronto + apre il thread).

    Brave non fornisce engagement (upvotes/commenti): gli item Reddit
    passano come opportunità in base al SOLO filtro tema, non alla soglia
    di engagement (vedi _filter_viral_opportunities).
    """
    keywords = config.get("reddit", {}).get("search_keywords", [])
    if not keywords:
        print("  [Reddit] Skipped — no search_keywords in config")
        return []

    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        print("  [Reddit] Skipped — serve BRAVE_API_KEY (Reddit JSON = 403; "
              "discovery via Brave site:reddit.com)")
        return []

    # Budget Brave: il credito gratuito è $5/mese (~1000 query) e il radar
    # ne usa già ~30/giorno per i cluster → resta poco margine. Default basso
    # (3/run) per stare sotto il free tier; alza brave_query_cap in
    # radar-keywords.yml solo con un piano Brave a pagamento.
    cap = int(config.get("reddit", {}).get("brave_query_cap", 3))
    results = []
    seen = set()
    for keyword in keywords[:cap]:
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": f"site:reddit.com {keyword}", "count": 10},
                headers={"X-Subscription-Token": api_key,
                         "Accept": "application/json"},
                timeout=12)
            if r.status_code != 200:
                print(f"  [Reddit/Brave] HTTP {r.status_code} su '{keyword}'")
                time.sleep(1)
                continue
            web = (r.json().get("web") or {}).get("results") or []
        except Exception as e:  # noqa: BLE001
            print(f"  [Reddit/Brave] Errore '{keyword}': {e}")
            time.sleep(1)
            continue

        for x in web:
            url = x.get("url", "") or ""
            m = re.search(r"reddit\.com/r/([^/]+)/comments/", url, re.I)
            if not m:  # solo thread di commenti (no profili/sub/listing)
                continue
            canon = url.split("?")[0].split("#")[0]
            if canon in seen:
                continue
            seen.add(canon)
            title = re.sub(r"<[^>]+>", "", x.get("title", "") or "").strip()
            # Brave formatta i titoli come "r/<sub> on Reddit: <titolo>" → pulisci
            title = re.sub(r"^r/[^:]+ on Reddit:\s*", "", title, flags=re.I).strip()
            # salta thread rimossi/cancellati: niente da commentare
            if not title or "removed by moderator" in title.lower() \
                    or re.search(r"\[\s*(removed|deleted)\s*\]", title, re.I):
                continue
            snippet = re.sub(r"<[^>]+>", "",
                             x.get("description", "") or "")[:500].strip()
            results.append({
                "source": "reddit",
                "cluster": "reddit",
                "query": keyword,
                "title": title,
                "url": canon,
                "snippet": snippet,
                "publication": f"r/{m.group(1)}",
                "author": "",
                "date": x.get("age", "") or x.get("page_age", "") or "",
                "discovery": "brave",
                "engagement": {},  # Brave non dà upvotes/commenti
            })
        time.sleep(1)  # Brave free tier: 1 qps

    print(f"  [Reddit] {len(results)} thread trovati via Brave "
          f"({len(keywords[:cap])} query)")
    return results


def rss_scan(config, lookback_days=14):
    """Scan RSS feeds and filter entries matching keywords."""
    if not FEEDPARSER_OK:
        print("  [RSS] Skipped — feedparser not installed")
        return []

    # (feed_url, cluster) — cluster drives the journal section downstream.
    rss_feeds = [
        # ── Architecture & design (international) ──
        ("https://www.dezeen.com/feed/", "architecture_blogs"),
        ("https://www.archdaily.com/feed", "architecture_blogs"),
        ("https://www.designboom.com/feed/", "architecture_blogs"),
        ("https://www.dwell.com/feed/", "architecture_blogs"),
        ("https://robbreport.com/shelter/feed/", "luxury_real_estate"),
        ("https://www.architecturaldigest.com/feed/rss", "architecture_blogs"),
        # ── LA / California real estate & market (added 2026-05-04 to reduce
        # insurance-bias in the radar output; these feeds publish daily on
        # LA luxury market, new construction, and architect-designed homes). ──
        ("https://therealdeal.com/la/feed/", "luxury_real_estate"),
        ("https://dirt.com/feed/", "luxury_real_estate"),
        ("https://www.latimes.com/business/real-estate/rss2.0.xml", "luxury_real_estate"),
        ("https://www.latimes.com/california/rss2.0.xml", "insurance_regulation"),
        ("https://la.curbed.com/rss/index.xml", "luxury_real_estate"),
        ("https://www.bisnow.com/los-angeles/feed", "luxury_real_estate"),
        # ── Local LA/SoCal community press (added 2026-06-01, RSS verified) ──
        # The local papers we "follow" for articles to cover + journalists to
        # contact. Only feeds confirmed to return entries are kept here;
        # outlets without public RSS (DIGS, LA Mag, BH Courier, Montecito,
        # Malibu Times, The Acorn) are still targeted via feature_pitch.py.
        ("https://www.palipost.com/feed/", "luxury_real_estate"),   # Palisadian-Post
        ("https://www.canyon-news.com/feed/", "luxury_real_estate"), # Bel-Air/BH/Brentwood
    ]

    # Strategy-aligned priority keywords. A feed entry must contain at least
    # one of these in title+summary to pass through. Tuned 2026-05-04 to
    # widen architecture surface area (mid-century, modernist, Spanish
    # revival, etc.) so architectural feeds aren't filtered down to zero by
    # geo+commercial keywords alone.
    priority_keywords = [
        # Tier-1/2 geographies (lead)
        "malibu", "beverly hills", "bel air", "holmby hills", "brentwood",
        "hidden hills", "calabasas", "westside", "pacific palisades",
        # Insurability + underwriting (primary commercial cluster)
        "insurable", "insurance", "fair plan", "ibhs", "mercury", "wui code",
        "safer from wildfires", "non-renewal", "wildfire prepared home",
        # Materials + newbuild (primary cluster)
        "reinforced concrete", "icf", "fire-resistant", "fire-resilient",
        "class a roof", "non-combustible", "ember-resistant", "zone 0",
        # Italian/Mediterranean villa typology (brand differentiator)
        "italian villa", "mediterranean villa", "tuscan", "palazzo",
        "italian design", "courtyard villa",
        # Newbuild framing
        "custom home", "new construction", "luxury home builder", "luxury villa",
        "spec home", "architect-designed",
        # ── Architecture styles & inspiration (added 2026-05-04) ──
        # These widen the net so feeds like Dezeen/ArchDaily catch
        # California-relevant architecture even when the article doesn't
        # explicitly name a city or use "concrete" / "fire-resistant".
        "modernist", "mid-century", "midcentury", "mid century",
        "spanish revival", "spanish colonial", "spanish-style",
        "california modern", "hollywood regency",
        "contemporary villa", "modern villa", "modern residence",
        "luxury residence", "private residence", "single-family home",
        # Architects / firms common in LA luxury residential
        "marmol radziner", "kaa design", "walker workshop",
        "belzberg architects", "william hefner", "standard architecture",
        # Secondary (still in scope, lower priority)
        "rebuild", "palisades", "altadena", "wildfire",
        "concrete", "residential concrete", "brutalist",
        "los angeles", "california home",
    ]

    # published_parsed è una struct_time UTC: il cutoff va calcolato in
    # UTC (il confronto con l'ora locale sbagliava di 7-9h al margine).
    import calendar
    cutoff_utc = datetime.utcnow() - timedelta(days=lookback_days)
    results = []

    for feed_url, feed_cluster in rss_feeds:
        try:
            # feedparser.parse(url) fa I/O di rete SENZA timeout: un
            # feed muto bloccava l'intera pipeline. Scarichiamo noi con
            # timeout e passiamo i byte al parser.
            resp = requests.get(feed_url, timeout=10, headers={
                "User-Agent": "MyVillaRadar/1.0 (+https://myvilla.la)"})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:20]:
                # try per-entry: una sola entry malformata non deve
                # buttare via tutte le altre del feed.
                try:
                    title = entry.get("title", "").lower()
                    summary = entry.get("summary", "").lower()
                    combined = title + " " + summary

                    if not any(kw in combined for kw in priority_keywords):
                        continue

                    published = entry.get("published_parsed")
                    if published:
                        pub_date = datetime.utcfromtimestamp(
                            calendar.timegm(published))
                        if pub_date < cutoff_utc:
                            continue

                    results.append({
                        "source": "rss",
                        "cluster": feed_cluster,
                        "query": "rss_feed",
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "snippet": entry.get("summary", "")[:300],
                        "publication": feed.feed.get("title", feed_url),
                        "date": entry.get("published", ""),
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"  [RSS] Error {feed_url}: {type(e).__name__}")

    print(f"  [RSS] {len(results)} entries found")
    return results


# ══════════════════════════════════════════════════════════════════════
# VIRALITY SCORING
# ══════════════════════════════════════════════════════════════════════

import math as _math

def compute_virality_score(likes, retweets, replies, views, post_date,
                           today, platform="x"):
    """Compute a 0-100 virality score.

    Signals:
      - Engagement volume (log-scaled)
      - Recency (higher score for last 24h, decays over 7 days)
      - Reply weight (replies = conversation = comment opportunity)

    Returns 0-100 integer.
    """
    try:
        dt_post = datetime.strptime(post_date[:10], "%Y-%m-%d") if post_date else datetime.now()
        dt_today = datetime.strptime(today[:10], "%Y-%m-%d") if today else datetime.now()
        age_hours = max(0, (dt_today - dt_post).total_seconds() / 3600)
    except (ValueError, TypeError):
        age_hours = 24

    # Engagement weight (replies matter most — they = conversation)
    raw_eng = likes + 2 * retweets + 5 * replies + 0.01 * views
    if raw_eng <= 0:
        return 0

    # Log-scale: 10 eng → 25, 100 → 50, 1000 → 75, 10000 → 100
    eng_score = min(100, int(25 * _math.log10(max(raw_eng, 1))))

    # Recency multiplier: 1.0 for <24h, 0.7 for <48h, 0.4 for <7d, 0.1 older
    if age_hours < 24:
        recency = 1.0
    elif age_hours < 48:
        recency = 0.75
    elif age_hours < 96:
        recency = 0.55
    elif age_hours < 168:  # 7 days
        recency = 0.35
    else:
        recency = 0.10

    return min(100, int(eng_score * recency))


# ── Viral topic filters ─────────────────────────────────────────────
# We only want posts where a My Villa reply makes sense — i.e., someone
# thinking about building/rebuilding a luxury home in LA. Everything else
# (bathroom remodels, generic landlord disputes, national real estate news,
# political drama) gets filtered out even if it's high-engagement.

VIRAL_TOPIC_KEYWORDS = [
    # --- LA rebuild / wildfire recovery (highest intent) ---
    "rebuild", "rebuilding", "reconstruction", "post-fire", "after the fire",
    "palisades fire", "eaton fire", "altadena fire", "woolsey fire",
    "pacific palisades", "palisades rebuild", "altadena rebuild",
    "malibu rebuild", "topanga rebuild", "bel air rebuild",

    # --- Luxury LA construction / architecture ---
    "custom home", "custom build", "luxury home", "luxury villa", "luxury estate",
    "new construction", "spec home", "dream home los angeles", "la architect",
    "modern villa", "hillside home", "mediterranean villa",
    "$10m", "$15m", "$20m", "$25m", "$30m", "$50m", "hundred million",
    "trophy home", "mega-mansion", "megamansion",

    # --- LA luxury geographies ---
    "malibu", "bel air", "bel-air", "beverly hills", "brentwood",
    "pacific palisades", "holmby hills", "hidden hills", "calabasas",
    "hancock park", "trousdale", "mandeville canyon", "manhattan beach",
    "montecito", "santa barbara", "newport coast",

    # --- Construction materials / methods (on-brand core) ---
    "reinforced concrete", "concrete home", "concrete house", "concrete villa",
    "icf ", "insulated concrete form", "cmu wall", "masonry home",
    "non-combustible", "fire-resistant home", "fire resistant house",
    "fire-resilient", "fireproof house", "stucco wall construction",

    # --- Insurance / insurability ---
    "california insurance", "insurance crisis", "non-renewal", "non renewal",
    "fair plan", "fair-plan", "state farm california", "allstate california",
    "underwriting", "insurability", "insurable home", "uninsurable",
    "wph plus", "wildfire prepared", "ibhs", "wui code", "zone 0",

    # --- Fire mitigation / defensible space (adjacent-on-topic) ---
    "defensible space", "ember resistant", "ember-resistant",
    "class a roof", "wildfire hardening", "home hardening",

    # --- Permitting / rebuild logistics ---
    "rebuild permit", "palisades permit", "la dbs", "la building permit",
    "fire rebuild permit", "lacbs", "rebuild timeline",

    # --- Design / architecture / lifestyle (allargato 2026-06-14) ---
    # Intercetta le conversazioni del target oltre il puro rebuild/vendita:
    # architettura residenziale, design della casa, lifestyle Malibu/LA.
    # Ancorati a casa/architettura/Malibu; blacklist off-geo + qualificatore
    # tengono fuori il rumore generico non-californiano.
    "residential architecture", "home architecture", "architectural design",
    "home design", "house design", "modern home design", "contemporary home",
    "home renovation", "home tour", "luxury living", "design build",
    "living in malibu", "malibu lifestyle", "malibu home", "hancock park home",
]

# Disqualifying terms — a post containing any of these is almost certainly
# NOT a luxury-rebuild opportunity, even if it matches a keyword above.
VIRAL_BLACKLIST = [
    # generic home-improvement / tenant issues
    "bathroom remodel", "kitchen remodel", "laminate floor", "drywall repair",
    "my landlord", "landlord won't", "landlord wont", "security deposit",
    "rental property", "tenant rights", "eviction", "month-to-month",
    "apartment", "apt ", "roommate",
    # generic legal / HOA not tied to rebuild
    "hoa dispute", "hoa fees", "encroachment", "easement dispute",
    "property line", "quiet title",
    # off-topic domains
    "fatfire", "early retirement", "401k", "retirement plan",
    "student loan", "crypto", "day trading",
    # political / drama (we must not engage)
    "trump", "biden", "newsom recall", "antifa", "democrat", "republican",
    "woke", "stolen election",
    # remote / not LA
    "new york city", "manhattan rebuild", "miami luxury", "dubai",
    "london", "austin texas", "phoenix arizona",
]


def _matches_luxury_rebuild_topic(item):
    """Return True if a post is plausibly about luxury rebuild / building
    / insuring a high-end home in LA.

    Strategy: whitelist match on CONTENT (title+snippet) AND not blacklist match.
    We intentionally EXCLUDE `query` from the haystack because the radar's
    seed queries always contain topic words — matching on query would pass
    every post regardless of its actual content.
    """
    # Only match against the post's actual content, not the seed query
    hay = " ".join([
        (item.get("title") or ""),
        (item.get("snippet") or ""),
    ]).lower()

    # Subreddit/publication as a soft signal (require a luxury/rebuild sub
    # for the match to be meaningful — this filters generic r/HomeImprovement)
    publication = (item.get("publication") or "").lower()

    # First pass: must match at least one whitelist keyword in content
    matches = [kw for kw in VIRAL_TOPIC_KEYWORDS if kw in hay]
    if not matches:
        return False

    # Second pass: exclude if any blacklist term hit (anywhere, inc. publication)
    full_hay = hay + " " + publication
    for bad in VIRAL_BLACKLIST:
        if bad in full_hay:
            return False

    # Third pass: if publication is a clear off-topic sub, bail out
    off_topic_subs = [
        "r/homeimprovement", "r/homeimprovementtips", "r/homebuilding",
        "r/realestate", "r/firsttimehomebuyer", "r/personalfinance",
        "r/fatfire", "r/richpeoplepf", "r/investing", "r/stocks",
    ]
    for sub in off_topic_subs:
        if sub in publication:
            # Only keep if the POST CONTENT is strongly on-topic (2+ whitelist matches)
            if len(matches) < 2:
                return False

    return True


def is_viral_opportunity(item, min_virality=30):
    """Return True if an item meets the threshold for viral engagement
    AND matches the luxury-rebuild-LA topic filter.

    Conservative defaults (tweak in _filter_viral_opportunities):
      - X: ≥50 likes OR ≥10 replies, last 48h
      - Reddit: ≥20 upvotes OR ≥15 comments, last 72h
    """
    eng = item.get("engagement") or {}
    virality = eng.get("virality_score", 0)

    if virality < min_virality:
        return False

    if not item.get("url"):
        return False

    return _matches_luxury_rebuild_topic(item)


def _filter_viral_opportunities(all_results, known_urls=None):
    """Return a separate list of viral opportunities (not mutating all_results).

    Logic: topic filter runs FIRST. If a post is on-topic (luxury rebuild LA),
    we use relaxed engagement thresholds — because an on-topic post with
    5 likes from the right person is more valuable than a 500-like post
    about bathroom remodels.

    Off-topic posts are rejected regardless of engagement.

    Thresholds:
      - On-topic X: ≥3 likes OR ≥2 replies (low — we want early signal)
      - On-topic Reddit: ≥5 upvotes OR ≥3 comments
      - Off-topic: rejected
    """
    known_urls = known_urls or set()
    viral = []
    rejected_topic = 0
    rejected_thresh = 0
    rejected_dup = 0
    for item in all_results:
        eng = item.get("engagement") or {}
        source = item.get("source", "")

        if source not in ("grok_x", "reddit", "instagram"):
            continue

        # Topic filter FIRST (strictest gate)
        if not _matches_luxury_rebuild_topic(item):
            rejected_topic += 1
            continue

        # Dedup
        if item.get("url") in known_urls:
            rejected_dup += 1
            continue

        # Relaxed engagement thresholds for on-topic posts
        if source == "grok_x":
            if eng.get("likes", 0) < 3 and eng.get("replies", 0) < 2:
                rejected_thresh += 1
                continue
        elif source == "reddit":
            # Item da Brave (discovery web) non hanno engagement: si tengono
            # in base al SOLO filtro tema (la rilevanza la fa il filtro +
            # l'occhio umano nel pannello). Quelli CON engagement (se un
            # domani torna l'accesso API) restano soggetti alla soglia.
            if eng and eng.get("score", 0) < 5 and eng.get("num_comments", 0) < 3:
                rejected_thresh += 1
                continue
        elif source == "instagram":
            # Hashtag di nicchia: soglie più alte di X (su IG il rumore
            # è enorme) ma raggiungibili da post di settore.
            if eng.get("likes", 0) < 30 and eng.get("replies", 0) < 5:
                rejected_thresh += 1
                continue

        viral.append(item)

    if rejected_topic or rejected_thresh or rejected_dup:
        print(f"  [Viral] {len(viral)} kept · "
              f"{rejected_topic} off-topic · "
              f"{rejected_thresh} low-engagement · "
              f"{rejected_dup} already-handled")

    viral.sort(key=lambda x: x.get("engagement", {}).get("virality_score", 0),
               reverse=True)
    return viral


def _filter_early_signals(all_results, known_urls=None, viral_urls=None):
    """Return posts that are on-topic but below viral engagement thresholds.

    These are 'early signals' — posts from small accounts or new threads
    where the conversation hasn't started yet. Good for monitoring; we don't
    auto-generate a reply draft.

    A post qualifies as an early signal if:
      - It's from X or Reddit (we can engage)
      - It matches the luxury-rebuild-LA topic filter
      - It has SOME engagement (≥1 like/upvote OR posted in last 72h)
      - It's NOT already in viral or known URLs
    """
    known_urls = known_urls or set()
    viral_urls = viral_urls or set()
    early = []

    today_dt = datetime.now()
    for item in all_results:
        source = item.get("source", "")
        if source not in ("grok_x", "reddit"):
            continue
        if not _matches_luxury_rebuild_topic(item):
            continue

        url = item.get("url", "")
        if not url or url in known_urls or url in viral_urls:
            continue

        eng = item.get("engagement") or {}
        likes = eng.get("likes", 0) if source in ("grok_x", "instagram") else eng.get("score", 0)
        replies = eng.get("replies", 0) if source in ("grok_x", "instagram") else eng.get("num_comments", 0)

        # Recency (fresh posts get a pass even with 0 engagement)
        try:
            post_date = item.get("date", "")[:10]
            dt_post = datetime.strptime(post_date, "%Y-%m-%d") if post_date else today_dt
            fresh_hours = (today_dt - dt_post).total_seconds() / 3600
        except (ValueError, TypeError):
            fresh_hours = 9999

        is_fresh = fresh_hours < 72
        has_minimal_eng = (likes >= 1 or replies >= 1)

        if is_fresh or has_minimal_eng:
            early.append(item)

    # Sort: fresh + engagement > fresh alone > engagement alone
    def _early_sort_key(x):
        eng = x.get("engagement") or {}
        src = x.get("source", "")
        likes = eng.get("likes", 0) if src in ("grok_x", "instagram") else eng.get("score", 0)
        replies = eng.get("replies", 0) if src in ("grok_x", "instagram") else eng.get("num_comments", 0)
        try:
            post_date = x.get("date", "")[:10]
            dt_post = datetime.strptime(post_date, "%Y-%m-%d") if post_date else today_dt
            fresh_hours = (today_dt - dt_post).total_seconds() / 3600
        except (ValueError, TypeError):
            fresh_hours = 9999
        # Prefer fresher posts, then more engagement
        return (-max(0, 72 - fresh_hours), -(likes + 2 * replies))

    early.sort(key=_early_sort_key)
    return early[:15]  # cap at 15 for signal quality


def instagram_viral_scan(config, lookback_days=7):
    """Scansione hashtag Instagram via Apify per post virali da
    commentare (flusso assistito — il commento si pubblica a mano).

    Usa lo stesso attore apify~instagram-scraper del partner_scraper,
    in modalità ricerca hashtag. Budget: ~6 hashtag × 25 post/giorno
    ≈ $10-12/mese. Config in radar-keywords.yml → instagram:.
    """
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        print("  [IG-viral] Skipped — APIFY_API_TOKEN non configurato")
        return []
    ig_cfg = (config or {}).get("instagram") or {}
    hashtags = ig_cfg.get("hashtags") or []
    if not hashtags:
        print("  [IG-viral] Skipped — nessun hashtag in config")
        return []
    per_tag = int(ig_cfg.get("results_per_hashtag", 25))

    # Attore DEDICATO agli hashtag (il generico instagram-scraper non
    # supporta la ricerca hashtag: "no_items"). Una sola run per tutti
    # i tag: resultsLimit è per-hashtag.
    run_url = ("https://api.apify.com/v2/acts/apify~instagram-hashtag-scraper"
               f"/run-sync-get-dataset-items?token={token}")
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    results = []

    payload = {
        "hashtags": [t.lower().lstrip("#") for t in hashtags],
        "resultsLimit": per_tag,
    }
    try:
        resp = requests.post(run_url, json=payload, timeout=300)
        resp.raise_for_status()
        posts = resp.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"  [IG-viral] Apify error: {type(e).__name__} (HTTP {status})")
        if status in (401, 402, 403):
            print("  [IG-viral] token/crediti Apify KO — "
                  "controlla console.apify.com")
        return []

    if not isinstance(posts, list):
        print("  [IG-viral] risposta inattesa dall'attore")
        return []
    kept = 0
    if True:
        for post in posts:
            if post.get("error"):
                continue
            url = post.get("url") or ""
            caption = (post.get("caption") or "").strip()
            if not url or not caption:
                continue
            # lookback
            ts = post.get("timestamp") or ""
            try:
                pd = datetime.fromisoformat(ts.replace("Z", ""))
                if pd < cutoff:
                    continue
            except (ValueError, TypeError):
                pd = None
            likes = int(post.get("likesCount") or 0)
            comments = int(post.get("commentsCount") or 0)
            owner = post.get("ownerUsername") or "?"
            vscore = compute_virality_score(
                likes, 0, comments, 0,
                pd.strftime("%Y-%m-%d") if pd else "",
                datetime.utcnow().strftime("%Y-%m-%d"))
            results.append({
                "source": "instagram",
                "cluster": "instagram_viral",
                "query": "#" + ((post.get("hashtags") or ["?"])[0]
                                 if isinstance(post.get("hashtags"), list)
                                 else "instagram"),
                "title": f"@{owner}: {caption[:90]}",
                "url": url,
                "snippet": caption[:300],
                "publication": f"Instagram @{owner}",
                "date": ts,
                "engagement": {
                    "likes": likes,
                    "replies": comments,
                    "virality_score": vscore,
                },
            })
            kept += 1

    print(f"  [IG-viral] {kept} post nel lookback su {len(posts)} "
          f"raccolti da {len(hashtags)} hashtag")
    return results


def instagram_engagement_scan(config, lookback_days=7, known_urls=None):
    """Post recenti di account STRATEGICI (architetti, realtor di lusso,
    pagine design) dove un commento UMANO porta traffico al profilo
    @myvilla.la. Flusso assistito: la bozza si pubblica a mano.

    A differenza di instagram_viral_scan NON filtra per viralità: conta
    il PUBBLICO dell'account, non i like del singolo post. Tiene 1 post
    (il più recente nel lookback) per account, max engagement_max.
    Attore profili apify~instagram-scraper (directUrls), stesso di
    partner_scraper. Config: radar-keywords.yml → instagram.engagement_targets.
    """
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        print("  [IG-engage] Skipped — APIFY_API_TOKEN non configurato")
        return []
    ig_cfg = (config or {}).get("instagram") or {}
    targets = ig_cfg.get("engagement_targets") or []
    handles = []
    for t in targets:
        h = str(t or "").strip().lstrip("@").rstrip("/").split("/")[-1].strip()
        if h:
            handles.append(h)
    if not handles:
        return []
    per_acct = int(ig_cfg.get("engagement_posts_per_account", 3))
    max_targets = int(ig_cfg.get("engagement_max", 5))
    known = set(known_urls or [])

    run_url = ("https://api.apify.com/v2/acts/apify~instagram-scraper"
               f"/run-sync-get-dataset-items?token={token}")
    payload = {
        "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
        "resultsType": "posts",
        "resultsLimit": per_acct,
        "addParentData": False,
    }
    try:
        resp = requests.post(run_url, json=payload, timeout=300)
        resp.raise_for_status()
        posts = resp.json()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"  [IG-engage] Apify error: {type(e).__name__} (HTTP {status})")
        if status in (401, 402, 403):
            print("  [IG-engage] token/crediti Apify KO — console.apify.com")
        return []
    if not isinstance(posts, list):
        print("  [IG-engage] risposta inattesa dall'attore")
        return []

    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    by_owner = {}  # owner → [item, ...] (post freschi dell'account)
    for post in posts:
        if post.get("error"):
            continue
        url = post.get("url") or ""
        caption = (post.get("caption") or "").strip()
        if not url or not caption or url in known:
            continue
        ts = post.get("timestamp") or ""
        try:
            pd = datetime.fromisoformat(ts.replace("Z", ""))
            if pd < cutoff:
                continue
        except (ValueError, TypeError):
            continue  # senza data affidabile non sappiamo se è "fresco"
        owner = (post.get("ownerUsername") or "?").lower()
        likes = int(post.get("likesCount") or 0)
        comments = int(post.get("commentsCount") or 0)
        item = {
            "source": "instagram",
            "cluster": "instagram_engagement",
            "engagement_target": True,
            "query": f"@{owner}",
            "title": f"@{owner}: {caption[:90]}",
            "url": url,
            "snippet": caption[:300],
            "publication": f"Instagram @{owner}",
            "date": ts,
            "engagement": {
                "likes": likes,
                "replies": comments,
                "virality_score": compute_virality_score(
                    likes, 0, comments, 0,
                    pd.strftime("%Y-%m-%d"), today),
            },
        }
        by_owner.setdefault(owner, []).append(item)

    # Max 2 post più recenti per account (no spam su un solo profilo, ma
    # se l'ultimo è off-topic — es. dezeen sui Mondiali — il penultimo dà
    # una seconda chance). L'AI-skip a valle scarta i non pertinenti.
    cand = []
    for owner, lst in by_owner.items():
        lst.sort(key=lambda c: c.get("date") or "", reverse=True)
        cand.extend(lst[:2])
    items = sorted(cand, key=lambda c: c.get("date") or "",
                   reverse=True)[:max_targets]
    print(f"  [IG-engage] {len(items)} account target con post recente "
          f"(su {len(handles)} monitorati)")
    return items


def grok_x_search(api_key, config, lookback_days=7):
    """Search X/Twitter via Grok Responses API with x_search tool."""
    if not api_key:
        print("  [Grok] Skipped — no API key")
        return []

    results = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = "https://api.x.ai/v1/responses"
    model = "grok-4-fast-non-reasoning"

    x_queries = config.get("twitter", {}).get("search_queries", [])
    if not x_queries:
        x_queries = [
            "Pacific Palisades rebuild 2026",
            "concrete home fire resistant California",
            "LA wildfire rebuild insurance",
            "#LArebuild reinforced concrete OR fire-resistant",
            "Malibu Altadena rebuild concrete 2026",
            "luxury home Los Angeles new construction",
            "#LuxuryHomesLA custom home OR villa OR architect",
            "concrete architecture residential villa",
        ]

    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    for query in x_queries:
        try:
            resp = requests.post(endpoint, headers=headers, json={
                "model": model,
                "input": [{
                    "role": "user",
                    "content": (
                        f"Search X/Twitter for posts published between {start} and {today} "
                        f"about: {query}. Return up to 5 real posts. For each, output on separate lines: "
                        f"Author: @username | Text: <full tweet text> | URL: <x.com link> | "
                        f"Date: YYYY-MM-DD | Likes: N | Retweets: N | Replies: N | Views: N | "
                        f"Author followers: N. Use 0 if a metric isn't available."
                    ),
                }],
                "tools": [{"type": "x_search"}],
                "temperature": 0,
                "max_output_tokens": 2000,
            }, timeout=45)
            resp.raise_for_status()
            data = resp.json()

            output_items = data.get("output", [])
            tweet_text = ""
            for item in output_items:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            tweet_text += part.get("text", "")

            urls = re.findall(r'https://x\.com/[^\s)\]"\'<>|]+', tweet_text)
            tweet_blocks = re.split(r'\n(?=\d+\.\s|\*\*Post by|Author:)', tweet_text)
            for i, block in enumerate(tweet_blocks[:5]):
                if not block.strip() or len(block) < 30:
                    continue
                block_urls = re.findall(r'https://x\.com/[^\s)\]"\'<>|]+', block)
                block_url = block_urls[0] if block_urls else (
                    urls[i] if i < len(urls) else "")
                block_authors = re.findall(r'@(\w+)', block)
                author = block_authors[0] if block_authors else "unknown"

                # Granular engagement metrics
                def _num(pat):
                    m = re.search(pat, block, re.IGNORECASE)
                    if not m:
                        return 0
                    raw = m.group(1).replace(",", "").lower()
                    try:
                        if raw.endswith("k"):
                            return int(float(raw[:-1]) * 1000)
                        if raw.endswith("m"):
                            return int(float(raw[:-1]) * 1_000_000)
                        return int(raw)
                    except (ValueError, AttributeError):
                        return 0

                likes = _num(r'likes?:?\s*([\d,]+\.?\d*[kmKM]?)')
                retweets = _num(r'(?:retweets?|reposts?):?\s*([\d,]+\.?\d*[kmKM]?)')
                replies = _num(r'(?:repl(?:y|ies)|comments?):?\s*([\d,]+\.?\d*[kmKM]?)')
                views = _num(r'views?:?\s*([\d,]+\.?\d*[kmKM]?)')
                followers = _num(r'followers?:?\s*([\d,]+\.?\d*[kmKM]?)')

                # Parse date if present (YYYY-MM-DD or similar)
                date_match = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', block)
                post_date = date_match.group(1) if date_match else today

                # Basic engagement score (for backward compatibility)
                engagement_total = likes + 2 * retweets + 5 * replies

                # Virality signal: is this post hot?
                virality = compute_virality_score(
                    likes=likes, retweets=retweets, replies=replies,
                    views=views, post_date=post_date, today=today,
                    platform="x",
                )

                results.append({
                    "source": "grok_x",
                    "cluster": "twitter",
                    "query": query,
                    "title": f"@{author}: {block[:80].strip()}...",
                    "url": block_url,
                    "snippet": block[:500].strip(),
                    "publication": "X/Twitter",
                    "date": post_date,
                    "author": f"@{author}",
                    "engagement": {
                        "score": min(engagement_total, 10000),
                        "likes": likes,
                        "retweets": retweets,
                        "replies": replies,
                        "views": views,
                        "author_followers": followers,
                        "virality_score": virality,
                    },
                })
            time.sleep(2)
        except Exception as e:
            _grok_fails = locals().get("_grok_fails", 0) + 1
            status = getattr(getattr(e, "response", None), "status_code", "?")
            print(f"  [Grok] Error '{query}': {type(e).__name__} (HTTP {status})")
            if status == 403:
                # 403 = crediti esauriti / spending limit (messaggio xAI
                # verificato 2026-06-11): inutile insistere sulle altre
                # query, abort immediato.
                print("  [Grok] 403 permission-denied — crediti xAI "
                      "esauriti o spending limit raggiunto. Abort. "
                      "Ricarica su https://console.x.ai/ → Billing.")
                break
            if _grok_fails >= 3:
                print("  [Grok] 3 errori consecutivi — abort")
                break

    print(f"  [Grok] {len(results)} tweets found")
    return results


def _resolve_gemini_redirect(url, fallback_title=""):
    """Follow Gemini/Vertex grounding redirect → return (final_url, real_title).

    Vertex AI grounding returns short-lived redirect URLs and often just the
    domain as the title. Resolve to the real article URL and try to extract
    a better title from <title>, og:title, or the first <h1>.
    """
    if not url or "vertexaisearch.cloud.google.com" not in url:
        return url, fallback_title
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
        final_url = resp.url or url
        html_text = resp.text[:20000] if resp.text else ""
        title = ""
        # Prefer og:title
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
        if not title:
            m = re.search(r"<title[^>]*>([^<]+)</title>", html_text, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
        if not title:
            m = re.search(r"<h1[^>]*>([^<]+)</h1>", html_text, re.IGNORECASE)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
        # Decode all HTML entities (named + numeric)
        if title:
            import html as _html
            title = _html.unescape(title)
            title = re.sub(r"\s+", " ", title).strip()
        return final_url, (title or fallback_title)
    except Exception as e:
        return url, fallback_title


def gemini_search(api_key, config, lookback_days=7):
    """Use Gemini 2.5-flash with Google Search Grounding."""
    if not api_key:
        print("  [Gemini] Skipped — no API key")
        return []

    results = []
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    headers = {"Content-Type": "application/json"}

    window = f"last {lookback_days} days"
    # Strategy-aligned Gemini prompts. Leading with tier-1/2 geographies and
    # insurability/newbuild framing per content_strategy.md.
    gemini_queries = [
        (
            f"Search Google for news in the {window} (2026) about California home "
            "insurance crisis for HIGH-VALUE homes: FAIR Plan luxury coverage, non-renewals, "
            "IBHS Wildfire Prepared Home Plus certification, Mercury Sustainable Insurance "
            "Strategy, AAA/USAA California homeowner policy changes, or Department of "
            "Insurance rulings affecting UHNW policyholders. Describe 5 articles: publication, "
            "date, main point. Plain text, no JSON.",
            "insurance_regulation",
        ),
        (
            f"Search Google for news ({window}) about LUXURY NEW CONSTRUCTION in "
            "Malibu, Beverly Hills, Bel Air, Brentwood, Hidden Hills, or Calabasas — "
            "custom-home builders, architect-designed spec homes, or ground-up residential "
            "projects above $10M. Prefer Mansion Global, Robb Report, WSJ, The Real Deal, "
            "Bisnow, Architectural Digest. Describe 5 articles: publication, date, main topic. "
            "Plain text, no JSON.",
            "luxury_real_estate_LA",
        ),
        (
            f"Search Google for articles ({window}) about REINFORCED CONCRETE or "
            "FIRE-RESILIENT residential construction in California: ICF homes, Class A "
            "roofs, non-combustible assemblies, 2026 California WUI Code compliance, "
            "Safer from Wildfires measures, Zone 0 ember-resistant perimeter, IBHS "
            "Wildfire Prepared Home Plus certifications. Describe 4 articles: publication, "
            "date, main topic. Plain text, no JSON.",
            "materials_construction",
        ),
        (
            f"Search Google for articles ({window}) about ITALIAN or MEDITERRANEAN "
            "villa architecture in California: Italian architects working in Los Angeles, "
            "Tuscan-style or palazzo-style luxury homes in Malibu / Beverly Hills / Bel Air, "
            "modern Italian residential design in California, European architects building "
            "in LA. Describe 4 articles: publication, date, main topic. Plain text, no JSON.",
            "italian_mediterranean_villa",
        ),
        (
            f"Search Google for articles ({window}) about concrete architecture and "
            "museum-grade residential design: Tadao Ando residential, Renzo Piano houses, "
            "reinforced concrete villa projects, exposed concrete luxury homes, "
            "brutalist residential architecture. Prefer Dezeen, ArchDaily, Wallpaper, "
            "Architectural Digest, Dwell. Describe 4 articles: publication, date, main "
            "topic. Plain text, no JSON.",
            "concrete_architecture",
        ),
        (
            # Kept last + narrower: rebuild is SECONDARY per strategy (PR value only)
            f"Search Google for news in the {window} (2026) about Pacific Palisades, "
            "Altadena, or Malibu wildfire REBUILD progress, permit counts, and rebuild "
            "cost trends — especially stories that touch insurance, construction material "
            "choice, or compliance with the 2026 California WUI Code. Describe 4 articles: "
            "publication, date, main point. Plain text, no JSON.",
            "rebuild_direct",
        ),
    ]

    for prompt_text, cluster in gemini_queries:
        try:
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "tools": [{"google_search": {}}],
                "generation_config": {
                    "temperature": 0,
                    "max_output_tokens": 8192,
                },
            }
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=45)
            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                continue

            candidate = candidates[0]
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            if finish_reason == "RECITATION":
                print(f"  [Gemini] RECITATION on '{cluster}' — skipping")
                continue

            grounding = candidate.get("groundingMetadata", {})
            chunks = grounding.get("groundingChunks", [])
            text_content = ""
            for part in candidate.get("content", {}).get("parts", []):
                text_content += part.get("text", "")

            if chunks:
                for chunk in chunks:
                    web = chunk.get("web", {})
                    title = web.get("title", "")
                    uri = web.get("uri", "")
                    if not title or not uri:
                        continue
                    # Resolve Gemini grounding redirect → real article URL
                    resolved_url, resolved_title = _resolve_gemini_redirect(uri, title)
                    results.append({
                        "source": "gemini",
                        "cluster": cluster,
                        "query": prompt_text[:80],
                        "title": resolved_title or title,
                        "url": resolved_url or uri,
                        "snippet": text_content[:300],
                        "publication": title.split(" - ")[-1] if " - " in title else title[:40],
                        "date": "",
                    })
            else:
                results.append({
                    "source": "gemini",
                    "cluster": cluster,
                    "query": prompt_text[:80],
                    "title": f"Gemini summary — {cluster}",
                    "url": "",
                    "snippet": text_content[:400],
                    "publication": "Gemini Search",
                    "date": "",
                })
            time.sleep(2)
        except Exception as e:
            # Niente eccezione raw: l'URL Gemini contiene ?key=... (leak)
            status = getattr(getattr(e, "response", None), "status_code", "?")
            print(f"  [Gemini] Error '{cluster}': {type(e).__name__} (HTTP {status})")

    print(f"  [Gemini] {len(results)} results (grounding)")
    return results


# ══════════════════════════════════════════════════════════════════════
# DEDUP & SCORING
# ══════════════════════════════════════════════════════════════════════

def deduplicate(results, known_urls, known_titles):
    """Remove already-reported URLs, internal duplicates, and noise hosts.

    Three filters in one pass:
      1. Known-already URLs (radar's persistent dedup index).
      2. Within-batch duplicates (URL or fuzzy-equal lowercased title).
      3. Noise hosts (Wikipedia, YouTube, social, Airbnb / Vrbo
         marketplaces, etc.) — see _NOISE_HOST_BLACKLIST.
    """
    seen_urls = set(known_urls)
    seen_titles = set(known_titles)
    unique = []
    noise_skipped = 0
    for r in results:
        url = r.get("url", "")
        title_key = r.get("title", "").lower().strip()

        # Skip empty URLs
        if not url and not title_key:
            continue

        # Filter noise hosts (Airbnb / Vrbo / Wikipedia / YouTube / etc.)
        # regardless of which source surfaced this result.
        host = _host_from_url(url) or (r.get("publication") or "").lower().strip()
        if _is_noise_host(host):
            noise_skipped += 1
            continue

        # URL dedup
        if url and url in seen_urls:
            continue
        # Title fuzzy dedup (exact lowercase match)
        if title_key and title_key in seen_titles:
            continue

        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.add(title_key)
        unique.append(r)

    removed = len(results) - len(unique)
    if noise_skipped:
        print(f"  [Dedup] {removed} removed ({noise_skipped} noise hosts + "
              f"{removed - noise_skipped} duplicates), {len(unique)} unique")
    else:
        print(f"  [Dedup] {removed} duplicates removed, {len(unique)} unique")
    return unique


# ── Module-level strategic config (populated from yml in main()) ──────
# Holds priority_geographies + cluster_priority_boost for scoring.
STRATEGIC_CONFIG = {
    "geo_tier": {},           # "malibu" -> 1, "bel air" -> 2, etc. (lowercased)
    "cluster_priority": {},   # yml cluster key -> int priority (1/2/3)
    "cluster_boost": {1: 3, 2: 1, 3: 0},  # priority -> bonus points
}


def _load_strategic_config(config):
    """Build STRATEGIC_CONFIG from the loaded radar-keywords.yml."""
    # Geography tiers
    geos = config.get("priority_geographies", {}) or {}
    tier_map = {
        "tier_1": 1,
        "tier_2": 2,
        "tier_3": 3,
        "tier_4_secondary_ca": 4,
    }
    geo_tier = {}
    for key, tier in tier_map.items():
        for name in geos.get(key, []) or []:
            geo_tier[name.lower()] = tier
    STRATEGIC_CONFIG["geo_tier"] = geo_tier

    # Cluster priorities (yml top-level `clusters`)
    clusters = config.get("clusters", {}) or {}
    cluster_priority = {}
    for cl_key, cl_data in clusters.items():
        p = int(cl_data.get("priority", 2) or 2)
        cluster_priority[cl_key] = p
    STRATEGIC_CONFIG["cluster_priority"] = cluster_priority

    # Cluster boost (from scoring block)
    boost_cfg = (config.get("scoring", {}) or {}).get("cluster_priority_boost", {}) or {}
    STRATEGIC_CONFIG["cluster_boost"] = {
        1: int(boost_cfg.get("priority_1", 3)),
        2: int(boost_cfg.get("priority_2", 1)),
        3: int(boost_cfg.get("priority_3", 0)),
    }


# Cross-source cluster label → yml cluster key (for priority lookup).
# Some sources emit semantic cluster labels (rss/gemini) rather than yml keys.
_CLUSTER_ALIAS = {
    # gemini prompt clusters
    "rebuild_direct":         "rebuild_secondary",
    "luxury_architecture_LA": "italian_mediterranean_villa",
    "luxury_real_estate_LA":  "luxury_real_estate",
    "insurance_regulation":   "insurance_regulation",
    "concrete_architecture":  "concrete_architecture",
    # rss + reddit + twitter: use fallback keyword inference
    "architecture_blogs":     None,
    "reddit":                 None,
    "twitter":                None,
}


def _resolve_cluster_priority(cluster_id, title_snippet):
    """Map any cluster string → priority int (1/2/3). Infer by keywords if unknown."""
    cp = STRATEGIC_CONFIG["cluster_priority"] or {}
    if cluster_id in cp:
        return cp[cluster_id]
    alias = _CLUSTER_ALIAS.get(cluster_id)
    if alias and alias in cp:
        return cp[alias]
    # Fallback — infer priority from content keywords
    text = title_snippet
    # priority 1 markers (insurability / materials / newbuild / Italian villa)
    p1 = ["insurable", "insurance", "fair plan", "ibhs", "mercury", "wui code",
          "safer from wildfires", "non-renewal", "prop 103",
          "reinforced concrete", "icf", "class a roof", "non-combustible",
          "fire-resistant", "fire-resilient", "fireproof",
          "italian villa", "mediterranean villa", "tuscan", "palazzo",
          "luxury home builder", "custom home builder", "new construction luxury",
          "insurable luxury"]
    if any(kw in text for kw in p1):
        return 1
    # priority 3 markers (rebuild-first framing without luxury anchor)
    p3 = ["altadena", "palisades rebuild", "eaton fire rebuild", "camp fire rebuild",
          "wildfire victim", "fire survivor"]
    if any(kw in text for kw in p3):
        return 3
    return 2


def _geography_tier(text):
    """Return best (lowest) tier number for geographies mentioned in text. 99 if none."""
    tier_map = STRATEGIC_CONFIG["geo_tier"] or {}
    best = 99
    for geo, tier in tier_map.items():
        if geo in text:
            best = min(best, tier)
    # Fallback for generic geo signals
    if best == 99:
        if "los angeles" in text or " la " in text or "westside" in text:
            best = 5  # soft geo signal
        elif "california" in text:
            best = 6
    return best


def preliminary_score(result):
    """Heuristic pre-score (0-25), 5 dimensions × 5 pts. Strategy-aware.

    Dimensions:
      1. RELEVANCE       — strategic keyword density (insurability + materials +
                            Italian/Mediterranean villa + custom-home framing)
      2. GEOGRAPHY TIER  — Malibu/Beverly Hills (tier 1) ranked above
                            Palisades/Altadena (tier 3) per content_strategy.md
      3. ENGAGEMENT      — comments / upvotes / publication weight
      4. AUDIENCE FIT    — UHNW publication alignment
      5. CLUSTER+DIALOG  — cluster priority boost + Reddit question detection
    """
    score = 0
    title = (result.get("title", "") + " " + result.get("snippet", "")).lower()
    source = result.get("source", "")
    cluster = result.get("cluster", "")

    # ── 1. RELEVANCE (0-5) — strategic keyword alignment ─────────────
    insurability = ["insurable", "insurance", "fair plan", "ibhs", "mercury",
                    "wui code", "safer from wildfires", "non-renewal",
                    "prop 103", "cdi", "insurance commissioner",
                    "wildfire prepared home", "underwriting"]
    materials = ["reinforced concrete", "icf", "class a roof", "non-combustible",
                 "fire-resistant", "fire-resilient", "fireproof",
                 "ember-resistant", "zone 0", "defensible space"]
    typology = ["italian villa", "mediterranean villa", "tuscan", "palazzo",
                "italian design", "palladian", "courtyard villa"]
    newbuild = ["custom home", "new construction", "luxury home builder",
                "spec home", "ground-up", "architect-designed",
                "insurable new construction"]
    rel = 0
    if any(kw in title for kw in insurability): rel += 2
    if any(kw in title for kw in materials):    rel += 2
    if any(kw in title for kw in typology):     rel += 2
    if any(kw in title for kw in newbuild):     rel += 1
    # Weak fallback to avoid starving unrelated results
    if rel == 0:
        weak = ["luxury home", "luxury villa", "concrete", "wildfire",
                "california home", "fire"]
        if any(kw in title for kw in weak):
            rel = 1
    score += min(rel, 5)

    # ── 2. GEOGRAPHY TIER (0-5) — per priority_geographies ───────────
    tier = _geography_tier(title)
    geo_score = {1: 5, 2: 3, 3: 1, 4: 1, 5: 1, 6: 1}.get(tier, 0)
    score += geo_score

    # ── 3. ENGAGEMENT (0-5) ─────────────────────────────────────────
    eng = result.get("engagement", {})
    comments = eng.get("num_comments", 0)
    post_score = eng.get("score", 0)
    if comments > 50 or post_score > 200:
        score += 5
    elif comments > 10 or post_score > 50:
        score += 3
    elif source == "reddit":
        score += 2
    else:
        score += 2

    # ── 4. AUDIENCE FIT (0-5) — UHNW publication signal ─────────────
    pub = result.get("publication", "").lower()
    uhnw = ["robb report", "architectural digest", "wallpaper", "bloomberg",
            "wsj", "mansion global", "luxury listed", "barron", "financial times",
            "ft.com"]
    design = ["dezeen", "archdaily", "designboom", "dwell", "fast company",
              "curbed", "the real deal", "bisnow", "dirt.com", "the agency"]
    if any(p in pub for p in uhnw):
        score += 5
    elif any(p in pub for p in design):
        score += 3
    elif "reddit" in pub or source == "reddit":
        score += 3
    else:
        score += 2

    # ── 5. CLUSTER PRIORITY + DIALOGUE (0-5) ────────────────────────
    priority = _resolve_cluster_priority(cluster, title)
    boost_map = STRATEGIC_CONFIG["cluster_boost"] or {1: 3, 2: 1, 3: 0}
    cd = boost_map.get(priority, 1)
    # Dialogue openness on top
    question_signals = ["why", "how", "should", "worth", "?", "what if",
                        "opinion", "recommend", "looking for", "advice"]
    if any(s in title for s in question_signals) and source == "reddit":
        cd += 2
    elif source == "reddit":
        cd += 1
    elif cluster in ("luxury_architecture_LA", "luxury_real_estate_LA",
                     "concrete_architecture", "italian_mediterranean_villa"):
        cd += 1
    score += min(cd, 5)

    # Annotate for downstream observability (useful in generate_radar_report.py)
    result["_strategy_tier"] = tier
    result["_strategy_priority"] = priority

    return min(score, 25)


# ══════════════════════════════════════════════════════════════════════
# AI SCORING (Claude Sonnet)
# ══════════════════════════════════════════════════════════════════════

SCORING_SYSTEM_PROMPT = """\
You are the My Villa Engagement Radar scoring engine. My Villa designs \
INSURABLE luxury reinforced-concrete villas for the Los Angeles market \
(Italian-villa typology, 2026 California WUI Code compliant, IBHS Wildfire \
Prepared Home Plus certified).

STRATEGIC PRIORITY ORDER (per content_strategy.md):
  PRIMARY target buyer = UHNW commissioning or rebuilding in Malibu / \
  Beverly Hills / Bel Air / Hidden Hills / Calabasas — who wants the home \
  to stay INSURABLE and physically RESILIENT through future fire cycles.
  NOT the primary target = pure "fire-victim rebuild help at any price" \
  stories. Those remain IN SCOPE for PR/backlinks but must NOT outrank \
  luxury-insurability-newbuild stories.

Score each item on 0-25 across 5 dimensions (0-5 each):

1. RELEVANCE — Does the item directly intersect our PRIMARY commercial \
   clusters: (a) California insurance / FAIR Plan / IBHS / Mercury / WUI \
   Code; (b) reinforced-concrete / ICF / Class-A / fire-resilient NEW \
   construction; (c) Italian/Mediterranean villa typology in LA; \
   (d) UHNW luxury newbuild in tier-1/2 LA geographies?
   Give 5 if it hits the primary commercial intent clearly; 3 if it's \
   adjacent (design/architecture only, no insurance/material angle); \
   1 if it's rebuild-news without a luxury or insurance hook.

2. RECENCY — Last 48h = 5; last 7 days = 3; older = 1.

3. ENGAGEMENT — Traction (comments/shares, or high-profile outlet).

4. AUDIENCE FIT — Robb Report / Mansion Global / WSJ / Bloomberg / \
   Architectural Digest reader = 5. Dezeen / Dwell / The Real Deal = 3. \
   General news = 2. Reddit subreddit = depends on sub (fatFIRE/LuxuryRE = 4).

5. DIALOGUE OPENNESS — Reddit question in a wealthy sub = 5; finished \
   news article = 2. For news articles, consider: does it leave a \
   technical/commercial question we can answer in the Journal?

BIAS: when a story is about Palisades or Altadena WITHOUT a luxury \
or insurance-reform angle, cap total at 14 (watchlist, not qualified).

Return a JSON array. For each item include:
- "index": original item index
- "score": total score (0-25)
- "scores": {"relevance": N, "recency": N, "engagement": N, "audience_fit": N, "dialogue": N}
- "action_type": "qualified" (>=15), "watchlist" (12-14), or "skip" (<12)
- "cluster_override": if the item better fits a different cluster, suggest it
- "summary": 1-sentence summary of what this is about (REQUIRED for every item, \
  including low-score ones — it is displayed in Early Signals cards)
- "engagement_angle": a short actionable note. Required for EVERY item:
  * For score >= 15 (qualified): how My Villa could engage — favor angles that \
    connect the news back to insurability, material choice, or Italian-villa typology.
  * For score 12-14 (watchlist): what to monitor, or which thread could grow \
    into a qualified opportunity.
  * For score < 12 (early signals / skip): a one-line "why this is worth \
    watching" or "signal to track" note — e.g. "small account but topic \
    adjacent, watch for replies from builders" or "niche thread worth \
    bookmarking if concrete-ADU coverage picks up". Keep it practical and \
    brief (≤ 20 words).
  The angle should be useful to a media strategist scanning for leverage.

Return ONLY valid JSON array, no markdown fences."""


def ai_score_batch(results, model=_BALANCED_MODEL):
    """Use Claude Sonnet to score a batch of results."""
    if not ANTHROPIC_OK:
        print("  [AI Score] Skipped — anthropic SDK not installed")
        return results

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [AI Score] Skipped — no valid ANTHROPIC_API_KEY")
        # Fall back to preliminary scores
        for r in results:
            r["ai_score"] = r.get("preliminary_score", 0)
            r["action_type"] = (
                "qualified" if r["ai_score"] >= 15
                else "watchlist" if r["ai_score"] >= 12
                else "skip"
            )
        return results

    client = anthropic.Anthropic(api_key=api_key)

    # Process in batches of 20
    batch_size = 20
    for batch_start in range(0, len(results), batch_size):
        batch = results[batch_start:batch_start + batch_size]
        items_text = json.dumps([{
            "index": i + batch_start,
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")[:200],
            "source": r.get("source", ""),
            "cluster": r.get("cluster", ""),
            "publication": r.get("publication", ""),
            "date": r.get("date", ""),
        } for i, r in enumerate(batch)], indent=2)

        try:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=SCORING_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Score these {len(batch)} items:\n\n{items_text}",
                }],
            )
            response_text = response.content[0].text.strip()
            if getattr(response, "stop_reason", None) == "max_tokens":
                print(f"  [AI Score] batch troncato a max_tokens — "
                      f"possibile perdita parziale")
            # Strip fence markdown: senza, un ```json in testa faceva
            # fallire il parse e perdere l'INTERO batch in silenzio.
            if response_text.startswith("```"):
                response_text = re.sub(r'^```(?:json)?\n?', '', response_text)
                response_text = re.sub(r'\n?```$', '', response_text)

            # Parse JSON response
            scored = json.loads(response_text)
            for item in scored:
                idx = item.get("index", 0)
                if batch_start <= idx < batch_start + len(batch):
                    r = results[idx]
                    r["ai_score"] = item.get("score", r.get("preliminary_score", 0))
                    r["ai_scores"] = item.get("scores", {})
                    r["action_type"] = item.get("action_type", "skip")
                    r["summary"] = item.get("summary", "")
                    r["engagement_angle"] = item.get("engagement_angle", "")
                    if item.get("cluster_override"):
                        r["cluster_suggested"] = item["cluster_override"]

            print(f"  [AI Score] Batch {batch_start//batch_size + 1}: "
                  f"{len(scored)} items scored")

        except json.JSONDecodeError as e:
            print(f"  [AI Score] JSON parse error: {e}")
            # Fall back to preliminary scores for this batch
            for r in batch:
                r["ai_score"] = r.get("preliminary_score", 0)
                r["action_type"] = (
                    "qualified" if r["ai_score"] >= 15
                    else "watchlist" if r["ai_score"] >= 12
                    else "skip"
                )
        except Exception as e:
            print(f"  [AI Score] Error: {e}")
            for r in batch:
                r["ai_score"] = r.get("preliminary_score", 0)
                r["action_type"] = (
                    "qualified" if r["ai_score"] >= 15
                    else "watchlist" if r["ai_score"] >= 12
                    else "skip"
                )

        time.sleep(1)  # Rate limit courtesy

    return results


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

# ── Byline / author extraction ─────────────────────────────────────────
# Fetches article URLs and extracts the journalist's name from common meta
# tags, JSON-LD schema, or visible byline patterns. Keeps it to ~8s timeout
# per article so a slow publication doesn't stall the whole pipeline.

_BYLINE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Noise values sometimes appearing in author meta tags
_AUTHOR_BLACKLIST = {
    "", "null", "none", "admin", "editor", "editorial", "staff",
    "newsroom", "reuters", "associated press", "ap news", "press release",
    "pr newswire", "business wire", "globe newswire",
}

# Tokens that, when appearing as a standalone word in a "name", indicate the
# value is actually an agency/brand/SEO tool, not a journalist. Filter
# defensively — we'd rather drop a real name than email an agency by mistake.
_AGENCY_TOKENS = {
    "agency", "drive", "media", "marketing", "seo", "digital", "studio",
    "studios", "group", "team", "llc", "inc", "ltd", "co", "corp", "labs",
    "solutions", "communications", "interactive", "creative", "consulting",
    "consultants", "ventures", "partners", "network", "networks",
}

# TLD endings that disqualify a "name" — it's actually a domain.
_DOMAIN_TLDS = (
    ".com", ".net", ".org", ".io", ".co", ".biz", ".us", ".info",
    ".co.uk", ".tv", ".news", ".media",
)


def _normalize_author_name(raw):
    """Clean and validate an extracted author name.

    Returns the name or "" if it looks like junk (handle, domain, role,
    agency name, etc.).
    """
    if not raw:
        return ""
    name = str(raw).strip()
    # Strip common prefixes
    name = re.sub(r"^\s*(by|written by|author[:\s]+)\s+", "", name, flags=re.I)
    # Trailing role/outlet: "Jane Doe, Staff Writer" → "Jane Doe"
    name = re.split(r"[,|]|\s+for\s+|\s+at\s+|\s+/\s+", name)[0].strip()
    # Strip extra whitespace
    name = re.sub(r"\s+", " ", name)
    # Junk filters
    if not name or name.lower() in _AUTHOR_BLACKLIST:
        return ""
    if "@" in name or name.startswith("http"):
        return ""
    if len(name) > 80 or len(name) < 3:
        return ""
    # Require at least one letter
    if not re.search(r"[A-Za-zÀ-ÿ]", name):
        return ""
    # Reject domain-shaped strings: "CaliforniaContractorInsurance.com",
    # "example.net", etc. The site stuffed its own domain into <meta author>.
    name_lower = name.lower()
    if any(name_lower.endswith(tld) for tld in _DOMAIN_TLDS):
        return ""
    if "." in name and " " not in name:
        # Single token containing a dot is almost always a domain or handle.
        return ""
    # Reject agency/brand-shaped strings: "Bliss drive", "Acme Media",
    # "Foo Marketing Group". A real journalist's surname is virtually never
    # one of these tokens.
    tokens = name_lower.split()
    if any(tok in _AGENCY_TOKENS for tok in tokens):
        return ""
    # Suspicious capitalization: real bylines capitalize each word
    # ("Jane Doe", not "Bliss drive"). If a multi-word name has a fully
    # lowercase non-particle word, treat it as agency-shaped.
    particles = {"de", "del", "della", "di", "da", "van", "von", "der", "le",
                 "la", "el", "bin", "ibn", "y"}
    parts = name.split()
    if len(parts) >= 2:
        for tok in parts[1:]:
            if tok.lower() in particles:
                continue
            # Real surname tokens start with an uppercase letter or a
            # quote/apostrophe/hyphen-uppercase ("O'Brien", "Saint-Just").
            if tok[0].islower():
                return ""
    return name


def extract_article_author(url, timeout=8):
    """Fetch `url` and try to extract the journalist's byline.

    Strategy (first match wins):
      1. <meta name="author" content="...">
      2. <meta property="article:author" content="...">
      3. JSON-LD Schema.org → Article.author.name
      4. <a rel="author">...</a>
      5. CMS class blocks: byline / post-author / entry-author / author-name
      6. Visible "By Jane Doe" pattern near the top of the document

    Returns a cleaned string, or "" if nothing usable is found.
    Never raises — any failure returns "".
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    # Skip known social/aggregator URLs where scraping the byline is pointless
    skip_hosts = (
        "x.com", "twitter.com", "reddit.com",
        "facebook.com", "instagram.com", "tiktok.com",
        "news.google.com", "vertexaisearch.cloud.google.com",
    )
    if any(h in url for h in skip_hosts):
        return ""

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _BYLINE_USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        html = resp.text[:500_000]  # cap at ~500KB to avoid pathological pages
    except Exception:
        return ""

    # 1. <meta name="author" content="...">
    m = re.search(
        r'<meta\s+[^>]*?name=["\']author["\'][^>]*?content=["\']([^"\']+)["\']',
        html, flags=re.I)
    if not m:
        m = re.search(
            r'<meta\s+[^>]*?content=["\']([^"\']+)["\'][^>]*?name=["\']author["\']',
            html, flags=re.I)
    if m:
        name = _normalize_author_name(m.group(1))
        if name:
            return name

    # 2. <meta property="article:author" content="...">
    m = re.search(
        r'<meta\s+[^>]*?property=["\']article:author["\'][^>]*?content=["\']([^"\']+)["\']',
        html, flags=re.I)
    if not m:
        m = re.search(
            r'<meta\s+[^>]*?content=["\']([^"\']+)["\'][^>]*?property=["\']article:author["\']',
            html, flags=re.I)
    if m:
        val = m.group(1)
        # Sometimes it's a URL (e.g. Facebook profile) — skip those
        if not val.startswith("http"):
            name = _normalize_author_name(val)
            if name:
                return name

    # 3. JSON-LD (schema.org Article)
    for block in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.I | re.S):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            # Check @graph nested structure too
            graph = obj.get("@graph") if isinstance(obj.get("@graph"), list) else []
            for node in [obj] + graph:
                if not isinstance(node, dict):
                    continue
                author = node.get("author")
                if isinstance(author, dict):
                    name = _normalize_author_name(author.get("name", ""))
                    if name:
                        return name
                elif isinstance(author, list) and author:
                    first = author[0]
                    if isinstance(first, dict):
                        name = _normalize_author_name(first.get("name", ""))
                        if name:
                            return name
                    elif isinstance(first, str):
                        name = _normalize_author_name(first)
                        if name:
                            return name
                elif isinstance(author, str):
                    name = _normalize_author_name(author)
                    if name:
                        return name

    # 4. <a rel="author">...</a>
    m = re.search(
        r'<a\s+[^>]*?rel=["\']author["\'][^>]*>\s*([^<]+?)\s*</a>',
        html, flags=re.I)
    if m:
        name = _normalize_author_name(m.group(1))
        if name:
            return name

    # 5. CMS-specific class-based blocks: <div class="byline">,
    # <span class="post-author">, <p class="entry-author">, etc.
    # We extract the inner block, strip nested tags, then look inside for a
    # capitalized human name. This guards against blocks that contain only
    # a date or only a category (e.g. firerescue1's `Page-byline` wraps
    # only the publication date).
    class_patterns = (
        r'<[a-z]+[^>]*?class=["\'][^"\']*\bbyline\b[^"\']*["\'][^>]*>'
        r'(.{0,2000}?)</[a-z]+>',
        r'<[a-z]+[^>]*?class=["\'][^"\']*\b(?:post|article|entry|c)-author'
        r'[^"\']*["\'][^>]*>(.{0,2000}?)</[a-z]+>',
        r'<[a-z]+[^>]*?class=["\'][^"\']*\bauthor-name\b[^"\']*["\'][^>]*>'
        r'(.{0,2000}?)</[a-z]+>',
    )
    for pat in class_patterns:
        for cm in re.finditer(pat, html, flags=re.I | re.S):
            inner = re.sub(r"<[^>]+>", " ", cm.group(1))
            inner = re.sub(r"\s+", " ", inner).strip()
            if not inner:
                continue
            nm = re.search(
                r"\b[A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+){1,3}\b",
                inner,
            )
            if nm:
                name = _normalize_author_name(nm.group(0))
                if name:
                    return name

    # 6. Visible "By Jane Doe" near the top of the document (first 20KB)
    head = html[:20_000]
    # Strip tags for the pattern search
    head_text = re.sub(r"<[^>]+>", " ", head)
    m = re.search(
        r"\b[Bb]y\s+([A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+){1,3})\b",
        head_text)
    if m:
        name = _normalize_author_name(m.group(1))
        if name:
            return name

    return ""


def _split_first_last(full_name):
    """Return (first, last) for a byline. Returns ("", "") for empty input."""
    if not full_name:
        return "", ""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


# ── Main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="My Villa Engagement Radar — Standalone Pipeline")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output JSON path (default: _system/radar/reports/radar_YYYY-MM-DD.json)")
    parser.add_argument("--model", type=str, default=_BALANCED_MODEL,
                        help="Claude model for AI scoring (default: auto, tier balanced)")
    parser.add_argument("--lookback", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    parser.add_argument("--dedup", type=str, default=None,
                        help="Path to previously_reported.json (default: auto)")
    parser.add_argument("--keywords", type=str, default=None,
                        help="Path to radar-keywords.yml (default: auto)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without saving output")
    parser.add_argument("--skip-grok", action="store_true")
    parser.add_argument("--skip-instagram", action="store_true",
                        help="Salta lo scan hashtag Instagram (Apify)")
    parser.add_argument("--skip-gemini", action="store_true")
    parser.add_argument("--skip-reddit", action="store_true")
    parser.add_argument("--skip-rss", action="store_true")
    parser.add_argument("--skip-cse", action="store_true")
    parser.add_argument("--skip-brave", action="store_true",
                        help="Skip Brave Search (the CSE alternative)")
    parser.add_argument("--skip-ai-score", action="store_true",
                        help="Skip AI scoring, use heuristic only")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  My Villa Engagement Radar — {today}")
    print(f"  Lookback: {args.lookback} days | Model: {args.model}")
    print(f"{'='*60}\n")

    # API health probe — runs before the long pipeline so the operator
    # learns about a dead key in ~5 seconds instead of after 10 minutes
    # of "0 results from Gemini" messages. Result is also persisted to
    # the radar JSON for the dashboard banner.
    api_health = api_health_check()
    print_api_health(api_health)
    print()

    # Load config
    kw_path = Path(args.keywords) if args.keywords else None
    config = load_yaml(kw_path or (CONFIG_DIR / "radar-keywords.yml"))
    clusters = config.get("clusters", {})
    # Hydrate strategic scoring config (geography tiers + cluster priorities)
    _load_strategic_config(config)
    print(f"Strategy config: {len(STRATEGIC_CONFIG['geo_tier'])} geos, "
          f"{len(STRATEGIC_CONFIG['cluster_priority'])} clusters, "
          f"boosts={STRATEGIC_CONFIG['cluster_boost']}")

    # Load dedup
    dedup_path = Path(args.dedup) if args.dedup else None
    dedup_data = load_dedup_index(dedup_path)
    known_urls = get_reported_urls(dedup_data)
    known_titles = get_reported_titles(dedup_data)
    print(f"Dedup index: {len(known_urls)} URLs, {len(known_titles)} titles\n")

    all_results = []

    # 1. Google CSE
    if not args.skip_cse:
        print("1. Google Custom Search...")
        cse_key = os.environ.get("GOOGLE_CSE_API_KEY", "")
        cse_id = os.environ.get("GOOGLE_CSE_ENGINE_ID", "")
        cse_results = google_cse_search(
            cse_key, cse_id, clusters, f"d{args.lookback}")
        all_results.extend(cse_results)
    else:
        print("1. Google CSE — skipped")

    # 1b. Brave Search (alternative web search). Runs alongside CSE — when
    # CSE comes back, both contribute and the dedup step collapses the
    # overlap. While CSE is broken (the 403 issue with Google), this is
    # the only working web-search source for top-tier publications.
    if not args.skip_brave:
        print("1b. Brave Search...")
        brave_key = os.environ.get("BRAVE_API_KEY", "")
        brave_results = brave_search(
            brave_key, clusters, lookback_days=args.lookback)
        all_results.extend(brave_results)
    else:
        print("1b. Brave Search — skipped")

    # 2. Reddit
    if not args.skip_reddit:
        print("2. Reddit JSON feeds...")
        reddit_results = reddit_search(config, lookback_days=args.lookback)
        all_results.extend(reddit_results)
    else:
        print("2. Reddit — skipped")

    # 3. RSS
    if not args.skip_rss:
        print("3. RSS feeds...")
        rss_results = rss_scan(config, lookback_days=args.lookback)
        all_results.extend(rss_results)
    else:
        print("3. RSS — skipped")

    # 4b. Instagram virali (Apify hashtag scan)
    ig_engage_results = []
    if not getattr(args, "skip_instagram", False):
        print("4b. Instagram hashtag (Apify)...")
        ig_results = instagram_viral_scan(config, lookback_days=args.lookback)
        all_results.extend(ig_results)
        # 4c. Account strategici dove commentare a mano (scan profili).
        # Tenuti SEPARATI: non passano dal filtro viralità (vedi sotto).
        print("4c. Instagram engagement targets (Apify profili)...")
        ig_engage_results = instagram_engagement_scan(
            config, lookback_days=args.lookback, known_urls=known_urls)
    else:
        print("4b/4c. Instagram — skipped")

    # 4. Grok/X
    if not args.skip_grok:
        print("4. Grok/xAI (X/Twitter)...")
        grok_key = os.environ.get("GROK_API_KEY", os.environ.get("XAI_API_KEY", ""))
        grok_results = grok_x_search(grok_key, config, lookback_days=args.lookback)
        all_results.extend(grok_results)
    else:
        print("4. Grok/X — skipped")

    # 5. Gemini
    if not args.skip_gemini:
        print("5. Gemini Search Grounding...")
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        gemini_results = gemini_search(gemini_key, config, lookback_days=args.lookback)
        all_results.extend(gemini_results)
    else:
        print("5. Gemini — skipped")

    print(f"\nTotal raw results: {len(all_results)}")

    # Dedup
    print("\nDeduplicating...")
    unique_results = deduplicate(all_results, known_urls, known_titles)

    # Pre-score (heuristic)
    print("Pre-scoring (heuristic)...")
    for r in unique_results:
        r["preliminary_score"] = preliminary_score(r)

    # Sort by preliminary score
    unique_results.sort(key=lambda x: x["preliminary_score"], reverse=True)

    # AI scoring
    if not args.skip_ai_score:
        # Only send top candidates to AI (score >= 8) to save tokens
        candidates = [r for r in unique_results if r["preliminary_score"] >= 8]
        low_score = [r for r in unique_results if r["preliminary_score"] < 8]
        for r in low_score:
            r["ai_score"] = r["preliminary_score"]
            r["action_type"] = "skip"

        if candidates:
            print(f"\nAI scoring {len(candidates)} candidates (model: {args.model})...")
            candidates = ai_score_batch(candidates, model=args.model)
            unique_results = candidates + low_score
    else:
        print("\nAI scoring — skipped (heuristic only)")
        for r in unique_results:
            r["ai_score"] = r["preliminary_score"]
            r["action_type"] = (
                "qualified" if r["ai_score"] >= 15
                else "watchlist" if r["ai_score"] >= 12
                else "skip"
            )

    # Final sort by AI score
    unique_results.sort(
        key=lambda x: x.get("ai_score", x.get("preliminary_score", 0)),
        reverse=True)

    # Byline extraction — fetch each qualified or watchlist article and pull
    # the author name from meta tags / JSON-LD / visible byline. Skips social
    # URLs where author is already captured as the handle. ~1-2s per article,
    # so ~15-30s total for a typical batch of 8-15 items.
    #
    # NOTE: we include `watchlist` so the outreach generator can still try
    # Apollo lookup + author-routed email when the operator promotes a
    # watchlist item. Earlier this only ran on `qualified`, which left every
    # watchlist item with `author_name=""` and forced an editorial fallback
    # even when the byline was clearly extractable.
    items_for_bylines = [
        r for r in unique_results
        if r.get("action_type") in ("qualified", "watchlist")
    ]
    if items_for_bylines:
        print(f"\nExtracting bylines ({len(items_for_bylines)} articles)...")
        for r in items_for_bylines:
            src = (r.get("source") or "").lower()
            # For X/Reddit, keep the @handle as author; don't fetch byline
            if src in ("grok_x", "reddit"):
                r.setdefault("author_name", "")
                continue
            url = r.get("url", "")
            name = extract_article_author(url)
            r["author_name"] = name
            if name:
                print(f"  [byline] {name} — {r.get('publication', '')[:30]}")
            else:
                print(f"  [byline] (none) — {url[:60]}")

    # Summary
    qualified = [r for r in unique_results if r.get("action_type") == "qualified"]
    watchlist = [r for r in unique_results if r.get("action_type") == "watchlist"]

    # Viral opportunities: high-engagement X/Reddit posts we should comment on.
    # Runs against ALL results (qualified + watchlist + skipped) because a tweet
    # may be virally interesting even if its content score is low.
    viral = _filter_viral_opportunities(unique_results, known_urls=known_urls)

    # Engagement targets: post di account strategici dove commentare a
    # mano. NON passano dal filtro viralità (conta il pubblico, non i
    # like). PREPEND così finiscono nei primi 5 card visibili del
    # pannello (è il "compito del giorno" che Ivo ha chiesto). Stessa
    # rail dei virali → bozza commento nel report + card. Dedup per URL.
    if ig_engage_results:
        viral_urls_now = {v.get("url") for v in viral if v.get("url")}
        fresh_targets = [it for it in ig_engage_results
                         if it.get("url") and it["url"] not in viral_urls_now]
        viral = fresh_targets + viral

    # Early signals: on-topic but below viral engagement threshold. Good for
    # monitoring — threads that could grow, small accounts worth watching.
    # Also exclude anything already surfaced as qualified/watchlist so the
    # same URL never appears twice across the dashboard.
    viral_urls = {v.get("url") for v in viral if v.get("url")}
    surfaced_urls = {
        r.get("url") for r in unique_results
        if r.get("action_type") in ("qualified", "watchlist") and r.get("url")
    }
    combined_known = set(known_urls) | surfaced_urls
    early_signals = _filter_early_signals(unique_results, known_urls=combined_known,
                                          viral_urls=viral_urls)

    print(f"\n{'='*60}")
    print(f"  RESULTS: {len(unique_results)} unique")
    print(f"  Qualified (≥15): {len(qualified)}")
    print(f"  Watchlist (12-14): {len(watchlist)}")
    print(f"  Viral opportunities: {len(viral)}")
    print(f"  Early signals: {len(early_signals)}")
    print(f"{'='*60}")

    # Build output
    output = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "model": args.model,
        "lookback_days": args.lookback,
        # Health check captured at the start of this run. Drives the
        # banner at the top of the dashboard and the markdown header.
        "api_health": api_health,
        "stats": {
            "total_raw": len(all_results),
            "total_unique": len(unique_results),
            "qualified": len(qualified),
            "watchlist": len(watchlist),
            "viral": len(viral),
            "early_signals": len(early_signals),
        },
        "qualified": [r for r in unique_results if r.get("action_type") == "qualified"],
        "watchlist": [r for r in unique_results if r.get("action_type") == "watchlist"],
        "viral_opportunities": viral,
        "early_signals": early_signals,
        "skipped_count": len([r for r in unique_results if r.get("action_type") == "skip"]),
    }

    # Save
    if not args.dry_run:
        if args.output:
            output_path = Path(args.output)
        else:
            output_path = RADAR_DIR / "reports" / f"radar_{today}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nSaved: {output_path}")
    else:
        print("\n[Dry run — not saving]")
        if qualified:
            print("\nTop 3 qualified:")
            for r in qualified[:3]:
                print(f"  [{r.get('ai_score', '?')}] {r.get('title', '')[:70]}")
                print(f"       {r.get('url', '')}")

    return output


if __name__ == "__main__":
    main()
