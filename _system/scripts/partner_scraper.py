#!/usr/bin/env python3
"""
My Villa — Partner Instagram Scraper (Apify-backed)

Pulls recent public posts from each partner handle defined in
editorial-calendar.yml → pillars.partner_echo.handles, filters them for
editorial usability (image present, recent, content type), caches the
result locally, and downloads thumbnails so the editorial dashboard can
render them.

Apify actor used: apify~instagram-scraper
  https://apify.com/apify/instagram-scraper
  Input shape:
    {
      "directUrls": ["https://www.instagram.com/<handle>/"],
      "resultsType": "posts",
      "resultsLimit": 20,
      "addParentData": false
    }
  Endpoint: POST https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?token=…

Cache layout (per handle):
  _system/social/partner_cache/<handle>.json
  _system/social/partner_cache/<handle>/<shortcode>.jpg   (thumbnail copy)

Cost:
  Apify charges per CU (Compute Unit) based on actor runtime. For 4 handles
  pulling 20 posts each ≈ 0.05 CU per run. With Apify's Free plan ($5 of
  monthly platform credit) this fits comfortably even with daily polling.
  Set APIFY_DAILY_BUDGET if you want a hard cap.

Usage:
  python3 partner_scraper.py                    # scrape all 4 handles
  python3 partner_scraper.py --handle buromilan # one handle
  python3 partner_scraper.py --max-age 30       # only post < 30 days old
  python3 partner_scraper.py --force            # ignore TTL, re-scrape now
  python3 partner_scraper.py --dry-run          # don't write cache
  python3 partner_scraper.py --offline          # use existing cache only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set
from urllib import error as urlerr
from urllib import request as urlreq
from urllib.parse import urlparse

import yaml

# ── Paths ────────────────────────────────────────────────────────────
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

SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
SOCIAL_DIR = SYSTEM_DIR / "social"
CACHE_DIR = SOCIAL_DIR / "partner_cache"

CONFIG_FILE = CONFIG_DIR / "editorial-calendar.yml"

DEFAULT_TTL_HOURS = 24            # re-scrape after 24h
DEFAULT_RESULTS_LIMIT = 20        # last N posts per handle
DEFAULT_MAX_AGE_DAYS = 30         # filter: post must be ≤ N days old
APIFY_ACTOR = "apify~instagram-scraper"
APIFY_RUN_SYNC_URL = (
    f"https://api.apify.com/v2/acts/{APIFY_ACTOR}"
    "/run-sync-get-dataset-items"
)


# ══════════════════════════════════════════════════════════════════════
# .env loader (matches sibling scripts)
# ══════════════════════════════════════════════════════════════════════

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
# Apify call
# ══════════════════════════════════════════════════════════════════════

def apify_scrape(handle: str, results_limit: int = DEFAULT_RESULTS_LIMIT,
                 timeout: int = 120) -> list:
    """Call Apify instagram-scraper actor and return raw posts list.

    Raises RuntimeError on missing token, HTTP error, or empty response."""
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "APIFY_API_TOKEN not set in .env — add it to enable partner scraping"
        )

    url = f"{APIFY_RUN_SYNC_URL}?token={token}"
    payload = {
        "directUrls": [f"https://www.instagram.com/{handle}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "addParentData": False,
    }
    req = urlreq.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urlerr.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Apify HTTP {e.code}: {body}") from e
    except urlerr.URLError as e:
        raise RuntimeError(f"Apify network error: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Apify returned non-JSON: {raw[:200]}") from e

    if not isinstance(data, list):
        raise RuntimeError(f"Apify returned non-list: {type(data).__name__}")

    return data


# ══════════════════════════════════════════════════════════════════════
# Post filtering & shaping
# ══════════════════════════════════════════════════════════════════════

# What we filter OUT (off-brand, not editorial-usable, or noise).
# Conservative bias: better to skip a borderline post than echo something
# off-brand that makes My Villa look like a vanity-feed aggregator.
SKIP_KEYWORDS = (
    # Celebrations & holidays
    "happy birthday", "buon compleanno", "anniversary", "anniversario",
    "merry christmas", "buon natale", "happy new year", "buon anno",
    "easter", "pasqua", "thanksgiving", "halloween",
    "🎂", "🎉", "🥂", "🎊", "🎁",
    # People milestones (retirements, hires, departures, in-memoriams)
    "farewell", "addio", "saluto", "saluti",
    "retirement", "pensione", "ritiro", "retiring", "retire",
    "leaving the company", "leaving us", "bidding farewell",
    "joining our team", "welcome to the team", "benvenuto in",
    "we're hiring", "we are hiring", "now hiring", "join our team",
    "open call", "internship", "stage",
    "in memory of", "in memoria di", "rest in peace",
    # Conferences / awards / non-project content
    "trade fair", "fiera", "salone", "booth", "stand at",
    "conference", "convegno", "panel discussion",
    "award", "premio ricevuto", "premio per", "vincitore",
    "team building", "convention", "kick off", "kick-off",
    "happy hour", "aperitivo",
    "thank you for", "grazie a tutti",
    # Lectures / generic firm-PR (low editorial relevance)
    "guest lecture", "conferenza", "keynote",
    "we attended", "we were at", "siamo stati",
)


# Topical anchors that strongly suggest a post IS on-brand (architecture,
# concrete, residential, climate engineering, Italian design).  Used by
# the relevance scorer as a fast pre-screen before calling Claude.
ON_BRAND_KEYWORDS = (
    # Architecture / construction
    "concrete", "calcestruzzo", "cemento",
    "reinforced", "armato", "precompresso",
    "structure", "struttura", "structural", "strutturale",
    "facade", "facciata", "envelope", "wall", "muro",
    "courtyard", "cortile", "podium", "podio", "portico", "pergola",
    "terraço", "terrazza", "fence", "muratura",
    "roof", "tetto", "copertura",
    "foundation", "fondazione",
    # Project types
    "villa", "residence", "residenza", "house", "casa",
    "renovation", "ristrutturazione", "rebuild",
    "interior", "interno", "design", "progetto",
    # Climate / engineering
    "climate", "clima", "thermal", "termico",
    "ventilation", "ventilazione", "passive",
    "daylight", "luce", "shading", "ombra",
    "comfort", "energy", "energia",
    # Materials / details
    "stone", "pietra", "marble", "marmo", "wood", "legno",
    "detail", "dettaglio", "section", "sezione",
    "drawing", "disegno", "render",
    # Italian heritage references
    "palladio", "scarpa", "ponti", "magistretti", "moretti", "libera",
    "kimbell", "palazzo grassi", "punta della dogana", "aman",
)


def _is_skippable_caption(caption: str) -> str | None:
    """Return reason string if the post caption looks off-brand for editorial echo."""
    if not caption:
        return None
    lc = caption.lower()
    for kw in SKIP_KEYWORDS:
        if kw.lower() in lc:
            return f"caption contains '{kw}'"
    return None


def _parse_timestamp(post: dict) -> datetime | None:
    """Apify returns 'timestamp' as ISO 8601 string. Return tz-aware datetime."""
    ts = post.get("timestamp") or post.get("takenAtTimestamp")
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        # ISO 8601, possibly with Z
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _heuristic_brand_score(caption: str) -> int:
    """Cheap pre-screen: count on-brand keyword hits in caption (case-insensitive).
    Returns 0..many. The relevance scorer uses this as a hint, but the LLM
    has the final say."""
    if not caption:
        return 0
    lc = caption.lower()
    return sum(1 for kw in ON_BRAND_KEYWORDS if kw in lc)


def _extract_all_image_urls(raw: dict) -> list:
    """Sidecar / multi-image posts expose all images in `childPosts` (with
    type + displayUrl per slide) or in `images` (flat URL list). For
    single-image posts, `displayUrl` is the only one. We extract every
    still-image URL we can find, in order, deduplicated.

    Videos inside a sidecar are skipped (we want pickable stills only)."""
    seen = set()
    out = []

    def _add(url):
        if not url or url in seen:
            return
        seen.add(url)
        out.append(url)

    # Pass A: structured childPosts (skip videos)
    children = raw.get("childPosts") or []
    if isinstance(children, list):
        for c in children:
            if not isinstance(c, dict):
                continue
            if (c.get("type") or "").lower() == "video":
                continue
            _add(c.get("displayUrl") or c.get("imageUrl") or "")

    # Pass B: flat `images` array (Apify older format) — strings or {url}
    images = raw.get("images") or []
    if isinstance(images, list):
        for img in images:
            if isinstance(img, str):
                _add(img)
            elif isinstance(img, dict):
                _add(img.get("url") or img.get("displayUrl") or "")

    # Always include the cover (displayUrl) — usually first, but ensure it's there
    cover = raw.get("displayUrl") or raw.get("imageUrl") or ""
    if cover and cover not in seen:
        out.insert(0, cover)
        seen.add(cover)

    return out


def shape_post(handle: str, raw: dict) -> dict | None:
    """Reduce an Apify post object to the fields we actually use, with
    a usability flag and a skip reason if applicable."""
    post_type = raw.get("type", "")            # "Image" | "Sidecar" | "Video"
    shortcode = raw.get("shortCode") or raw.get("shortcode") or ""
    if not shortcode:
        return None

    caption = (raw.get("caption") or "").strip()
    skip_reason = _is_skippable_caption(caption)

    # All available still-image URLs (Sidecar = many; Image = one; Video = thumbnail)
    image_urls = _extract_all_image_urls(raw)
    image_url = image_urls[0] if image_urls else ""
    is_video = post_type.lower() == "video"

    timestamp = _parse_timestamp(raw)
    age_days = None
    if timestamp:
        age_days = (datetime.now(timezone.utc) - timestamp).days

    return {
        "handle": handle,
        "shortcode": shortcode,
        "url": raw.get("url") or f"https://www.instagram.com/p/{shortcode}/",
        "type": post_type,
        "is_video": is_video,
        "caption": caption,
        "caption_excerpt": (caption[:280] + "…") if len(caption) > 280 else caption,
        "image_url": image_url,           # cover (slide 1)
        "image_urls": image_urls,         # all stills, in order — picker source
        "image_count": len(image_urls),
        "timestamp_iso": timestamp.isoformat() if timestamp else None,
        "age_days": age_days,
        "likes": raw.get("likesCount"),
        "comments": raw.get("commentsCount"),
        "alt": raw.get("alt") or "",
        "owner_username": raw.get("ownerUsername") or handle,
        "skip_reason": skip_reason,
        "usable": skip_reason is None and bool(image_url),
        "heuristic_brand_score": _heuristic_brand_score(caption),
        # Filled by score_relevance_with_claude() if API key is set:
        "relevance_score": None,        # 0..10
        "relevance_rationale": None,    # one-line reason
    }


def filter_posts(shaped: list, max_age_days: int,
                 min_relevance: Optional[int] = None) -> list:
    """Apply final freshness + usability filter to the shaped list.
    If min_relevance is set, also drop posts with relevance_score below it."""
    out = []
    for p in shaped:
        if not p:
            continue
        if not p["usable"]:
            continue
        if p["age_days"] is not None and p["age_days"] > max_age_days:
            continue
        if min_relevance is not None:
            score = p.get("relevance_score")
            if score is None or score < min_relevance:
                continue
        out.append(p)
    # Sort: highest relevance first (None last), then newest
    out.sort(key=lambda p: (
        -(p.get("relevance_score") or 0),
        -(0 if p["timestamp_iso"] is None else int(
            datetime.fromisoformat(p["timestamp_iso"]).timestamp()
        )),
    ))
    return out


# ══════════════════════════════════════════════════════════════════════
# Claude relevance scoring (0..10)
# ══════════════════════════════════════════════════════════════════════

CLAUDE_SCORING_MODEL = _resolve_model("cheap")   # cheap, fast — scoring only
CLAUDE_SCORING_TIMEOUT = 30


SCORING_SYSTEM_PROMPT = """\
You evaluate Instagram posts from architectural / engineering firms for \
their fit with the My Villa editorial channel. My Villa is an ultra-luxury \
reinforced concrete villa company in Los Angeles. The editorial voice is:
- Architectural thesis (Italian villa typology, exposed reinforced concrete)
- Brand-foundation, never sales
- Italian masters and historical references as credentials
- Resilience as design continuity, never as fear

A post is HIGH relevance if it shows: residential/cultural architecture, \
exposed concrete craft, structural detail, climate/passive engineering, \
courtyards, podiums, porticos, materials, or one of the partner's \
references for My Villa (Renzo Piano / Kimbell / Palazzo Grassi / Punta \
della Dogana / Aman / Casa Malaparte / Villa Rotonda / Villa Adriana).

A post is LOW relevance if it is: a team photo, retirement / farewell, \
hiring, generic firm anniversary, conference / panel, award trophy, \
holiday greeting, or anything not about the architectural object itself.

Scoring scale:
  10 — Perfect editorial fit (a Kimbell concrete shot, a Tomba Brion detail)
   8 — Strong architectural content, on-brand
   6 — Tangentially relevant (e.g. industrial structure, infrastructure)
   4 — Weak (corporate / process-only without project)
   2 — Off-brand (people, anniversaries, awards)
   0 — Should never echo (politics, holidays, personal)

Output ONLY JSON:
  {"score": <int 0-10>, "rationale": "one short sentence"}
"""


def score_relevance_with_claude(post: dict, model: str = CLAUDE_SCORING_MODEL) -> Optional[dict]:
    """Score one post 0-10 for editorial relevance. Returns
    {"score": int, "rationale": str} or None on error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    caption = (post.get("caption") or "").strip()
    if not caption:
        # No text → score by image presence + heuristic only
        return {
            "score": 5 if post.get("image_url") else 0,
            "rationale": "No caption to evaluate, defaulting on image presence.",
        }

    user_prompt = (
        f"Partner handle: @{post.get('handle', '')}\n"
        f"Post type: {post.get('type', '')}\n"
        f"Heuristic on-brand keyword hits: {post.get('heuristic_brand_score', 0)}\n"
        f"Caption (verbatim, may be Italian or English):\n\"\"\"\n"
        f"{caption[:800]}\n\"\"\"\n\n"
        f"Score this post for My Villa editorial relevance."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=160,
            system=SCORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=CLAUDE_SCORING_TIMEOUT,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
        score = int(result.get("score", 0))
        score = max(0, min(10, score))
        return {
            "score": score,
            "rationale": str(result.get("rationale", ""))[:200],
        }
    except Exception as e:
        return {"score": 0, "rationale": f"scorer error: {type(e).__name__}"}


def score_all_unscored(shaped: list, *, force: bool = False, verbose: bool = True) -> int:
    """In-place: score each post that's currently usable but unscored.
    Returns the count of posts scored. Cheap heuristic posts (score>=5 hits)
    skip the LLM and get assigned a heuristic score; everything else uses
    Claude Haiku for accuracy."""
    scored = 0
    for p in shaped:
        if not p.get("usable"):
            continue
        if p.get("relevance_score") is not None and not force:
            continue
        # Cheap path: caption with NO on-brand keywords AND NO image alt → 1
        if p.get("heuristic_brand_score", 0) == 0 and not p.get("alt"):
            p["relevance_score"] = 2
            p["relevance_rationale"] = "Heuristic: no on-brand keywords found in caption."
            scored += 1
            continue
        result = score_relevance_with_claude(p)
        if result:
            p["relevance_score"] = result["score"]
            p["relevance_rationale"] = result["rationale"]
            scored += 1
            if verbose:
                cap_preview = (p.get("caption_excerpt") or "")[:80]
                print(f"      score={result['score']:2d}  {p['shortcode']}  {cap_preview}")
    return scored


# ══════════════════════════════════════════════════════════════════════
# Thumbnail download
# ══════════════════════════════════════════════════════════════════════

def _project_rel(p: Path) -> str:
    project_root = SYSTEM_DIR.parent.resolve()
    try:
        return str(p.resolve().relative_to(project_root))
    except ValueError:
        return str(p)


def _http_download_to(path: Path, url: str, timeout: int = 30) -> bool:
    """Download `url` to `path`. Returns True on success."""
    try:
        req = urlreq.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (My Villa partner cache)"},
        )
        with urlreq.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        path.write_bytes(data)
        return True
    except Exception as e:
        print(f"    ⚠ download failed: {type(e).__name__}: {e}")
        return False


def download_thumbnail(post: dict, dest_dir: Path) -> Optional[str]:
    """Download the COVER image (slide 1) only.

    For multi-image posts use download_all_thumbnails() — this preserves
    the original signature for backwards compatibility (it's still used
    e.g. by the legacy backfill loop).

    Returns project-relative path on success, None on failure.
    `dest_dir` is resolved to absolute internally so callers can pass
    either relative or absolute paths."""
    if not post.get("image_url"):
        return None
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{post['shortcode']}.jpg"

    if dest.exists() and dest.stat().st_size > 1024:
        return _project_rel(dest)
    if _http_download_to(dest, post["image_url"]):
        return _project_rel(dest)
    return None


def download_all_thumbnails(post: dict, dest_dir: Path) -> list:
    """Download every still-image URL exposed by the post (Sidecar = many,
    Image = one). Stores as `<shortcode>.jpg` (cover, slide 1) and
    `<shortcode>-2.jpg`, `<shortcode>-3.jpg`, … for the rest.

    Returns the list of project-relative paths in order, including the
    cover at index 0. Existing files are reused (no re-download)."""
    urls = post.get("image_urls") or []
    if not urls and post.get("image_url"):
        urls = [post["image_url"]]
    if not urls:
        return []

    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    sc = post["shortcode"]

    paths = []
    for i, url in enumerate(urls):
        # Naming: cover = <sc>.jpg (back-compat), rest = <sc>-N.jpg from i=1
        fname = f"{sc}.jpg" if i == 0 else f"{sc}-{i+1}.jpg"
        dest = dest_dir / fname
        if dest.exists() and dest.stat().st_size > 1024:
            paths.append(_project_rel(dest))
            continue
        if _http_download_to(dest, url):
            paths.append(_project_rel(dest))
            time.sleep(0.15)
        else:
            # Stop on first failure — Apify CDN URLs in one post tend to
            # share auth and either all work or all 403.
            break

    return paths


# ══════════════════════════════════════════════════════════════════════
# Cache I/O
# ══════════════════════════════════════════════════════════════════════

def cache_path(handle: str) -> Path:
    return CACHE_DIR / f"{handle}.json"


def load_cache(handle: str) -> dict:
    p = cache_path(handle)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_cache(handle: str, data: dict, dry_run: bool = False):
    if dry_run:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(handle).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def cache_is_fresh(handle: str, ttl_hours: int) -> bool:
    cache = load_cache(handle)
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        when = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when) < timedelta(hours=ttl_hours)


def get_cached_posts(handle: str) -> list:
    """Read cached posts (used by editorial_generator.py at slot-fill time)."""
    return load_cache(handle).get("posts", [])


# ══════════════════════════════════════════════════════════════════════
# Main per-handle scrape
# ══════════════════════════════════════════════════════════════════════

def scrape_handle(handle: str, *, max_age_days: int, force: bool,
                  ttl_hours: int, dry_run: bool, offline: bool,
                  results_limit: int) -> dict:
    """Scrape one handle, write cache, return summary."""
    print(f"\n  [{handle}]")

    if offline:
        cache = load_cache(handle)
        usable = filter_posts(cache.get("posts", []), max_age_days)
        print(f"    offline: {len(usable)} usable posts (cache age: {cache.get('fetched_at','never')})")
        return {"handle": handle, "fetched": False, "usable": len(usable)}

    if not force and cache_is_fresh(handle, ttl_hours):
        cache = load_cache(handle)
        usable = filter_posts(cache.get("posts", []), max_age_days)
        print(f"    cache fresh ({cache.get('fetched_at','?')}) — {len(usable)} usable posts")
        return {"handle": handle, "fetched": False, "usable": len(usable), "cached": True}

    if dry_run:
        print(f"    [dry-run] would call Apify with results_limit={results_limit}")
        return {"handle": handle, "fetched": False, "dry_run": True}

    try:
        raw_posts = apify_scrape(handle, results_limit=results_limit)
    except RuntimeError as e:
        print(f"    ✗ {e}")
        return {"handle": handle, "fetched": False, "error": str(e)}

    print(f"    Apify returned {len(raw_posts)} raw posts")
    shaped = [shape_post(handle, p) for p in raw_posts]
    shaped = [s for s in shaped if s]

    # Score every usable post for editorial relevance (0-10) via Claude
    # Haiku. Costs ~$0.005/post for low input/output. Skipped silently
    # if ANTHROPIC_API_KEY missing (relevance_score stays None).
    print(f"    Scoring {sum(1 for p in shaped if p.get('usable'))} usable posts for editorial relevance…")
    score_all_unscored(shaped, verbose=True)

    # Download ALL still images for every usable shaped post. Sidecar
    # posts often have 5-10 images — we want them all so the dashboard
    # image picker has the full carousel to choose from.
    # `local_thumbnail` (singular) = cover only, kept for back-compat.
    # `local_thumbnails` (plural) = full list including cover at index 0.
    thumbs_dir = CACHE_DIR / handle
    for p in shaped:
        if not p.get("usable"):
            continue
        paths = download_all_thumbnails(p, thumbs_dir)
        if paths:
            p["local_thumbnail"] = paths[0]
            p["local_thumbnails"] = paths
            p["local_thumbnail_count"] = len(paths)

    # Persist full shaped list (so we can re-filter without re-fetching)
    cache_payload = {
        "handle": handle,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results_limit": results_limit,
        "raw_count": len(raw_posts),
        "shaped_count": len(shaped),
        "posts": shaped,
    }
    save_cache(handle, cache_payload, dry_run=dry_run)

    usable = filter_posts(shaped, max_age_days)
    print(f"    {len(usable)} usable posts within {max_age_days}d (skipped: {len(shaped)-len(usable)})")
    return {"handle": handle, "fetched": True, "usable": len(usable), "raw": len(raw_posts)}


# ══════════════════════════════════════════════════════════════════════
# Slot picker — used by editorial_generator.py for partner_echo
# ══════════════════════════════════════════════════════════════════════

DEFAULT_MIN_RELEVANCE = 7    # Pass-1 preferred floor (model 7+/10)
DEFAULT_HARD_FLOOR = 5       # Pass-2 fallback floor — never publish below


def pick_post_for_slot(handle: str, *, used_shortcodes: Optional[set] = None,
                       max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                       min_relevance: int = DEFAULT_MIN_RELEVANCE,
                       hard_floor: int = DEFAULT_HARD_FLOOR,
                       allow_any_usable: bool = False) -> Optional[dict]:
    """Pick the best (highest-relevance, recent, unused) post for a
    partner_echo slot. Returns the shaped post dict or None if nothing fits.

    Pass design:
      Pass 1 — score >= min_relevance (default 7) — preferred editorial grade
      Pass 2 — score >= hard_floor    (default 5) — acceptable fallback
      Pass 3 — ANY usable post (no relevance filter) — DISABLED by default.
               Set allow_any_usable=True to enable. The default behaviour is
               to return None when nothing meets hard_floor, so the caller
               (editorial_generator) skips the partner_echo slot rather than
               echoing weak content.
    """
    cache = load_cache(handle)
    used = used_shortcodes or set()
    all_posts = cache.get("posts", [])

    # Pass 1: high-relevance only
    posts = filter_posts(all_posts, max_age_days, min_relevance=min_relevance)
    for p in posts:
        if p["shortcode"] not in used:
            return p

    # Pass 2: relax to hard_floor
    posts = filter_posts(all_posts, max_age_days, min_relevance=hard_floor)
    for p in posts:
        if p["shortcode"] not in used:
            return p

    # Pass 3: any usable post — opt-in. Returns None by default.
    if allow_any_usable:
        posts = filter_posts(all_posts, max_age_days, min_relevance=None)
        for p in posts:
            if p["shortcode"] not in used:
                return p

    return None


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="My Villa — Partner IG Scraper (Apify)")
    parser.add_argument("--handle",
                        help="Single handle (default: all from editorial-calendar.yml)")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_DAYS,
                        help=f"Max post age in days (default {DEFAULT_MAX_AGE_DAYS})")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_HOURS,
                        help=f"Cache TTL in hours (default {DEFAULT_TTL_HOURS})")
    parser.add_argument("--results-limit", type=int, default=DEFAULT_RESULTS_LIMIT,
                        help=f"Posts per handle (default {DEFAULT_RESULTS_LIMIT})")
    parser.add_argument("--force", action="store_true",
                        help="Ignore TTL, scrape now")
    parser.add_argument("--offline", action="store_true",
                        help="Don't call Apify, just report what's in cache")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score existing cache via Claude without re-scraping")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"\nMy Villa — Partner IG Scraper (Apify)")
    print(f"{'=' * 50}")

    # Resolve handles list
    if args.handle:
        handles = [args.handle]
    else:
        if not CONFIG_FILE.exists():
            print(f"  ✗ {CONFIG_FILE} not found")
            sys.exit(1)
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        handles = [
            h["handle"]
            for h in cfg.get("pillars", {}).get("partner_echo", {}).get("handles", [])
        ]

    if not handles:
        print("  ✗ No handles to scrape.")
        sys.exit(1)

    print(f"  Handles: {', '.join(handles)}")
    print(f"  Max age: {args.max_age}d · TTL: {args.ttl}h · "
          f"Limit/handle: {args.results_limit}")
    if not os.environ.get("APIFY_API_TOKEN") and not args.offline:
        print(f"\n  ⚠ APIFY_API_TOKEN not set in .env — running in --offline mode\n")
        args.offline = True

    summaries = []
    for h in handles:
        if args.rescore:
            cache = load_cache(h)
            posts = cache.get("posts", [])
            if not posts:
                print(f"\n  [{h}] no cache to rescore")
                summaries.append({"handle": h, "fetched": False, "rescored": 0})
                continue
            print(f"\n  [{h}] rescoring {sum(1 for p in posts if p.get('usable'))} usable posts…")
            count = score_all_unscored(posts, force=True, verbose=True)
            cache["posts"] = posts
            save_cache(h, cache, dry_run=args.dry_run)
            summaries.append({"handle": h, "fetched": False, "rescored": count})
            continue

        s = scrape_handle(
            h,
            max_age_days=args.max_age,
            force=args.force,
            ttl_hours=args.ttl,
            dry_run=args.dry_run,
            offline=args.offline,
            results_limit=args.results_limit,
        )
        summaries.append(s)

    # Final report
    print(f"\n  ━━━━━━━ Summary ━━━━━━━")
    for s in summaries:
        if s.get("error"):
            print(f"    ✗ {s['handle']:35} ERROR: {s['error'][:60]}")
        elif s.get("dry_run"):
            print(f"    · {s['handle']:35} (dry-run)")
        elif s.get("cached"):
            print(f"    · {s['handle']:35} cache hit · {s['usable']} usable")
        elif s.get("fetched"):
            print(f"    ✓ {s['handle']:35} fetched · {s['usable']}/{s['raw']} usable")
        else:
            print(f"    · {s['handle']:35} {s.get('usable', 0)} usable (offline)")
    print()


if __name__ == "__main__":
    main()
