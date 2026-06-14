#!/usr/bin/env python3
"""
My Villa — Radar Report Generator
Converts radar JSON output → interactive HTML dashboard + email/tweet drafts (Opus)

Usage:
  python3 generate_radar_report.py \
    --radar _system/radar/reports/radar_2026-04-09.json \
    --output _system/radar/reports/radar_dashboard_2026-04-09.html

  python3 generate_radar_report.py \
    --radar radar.json --model claude-opus-4-8 --skip-drafts
"""

import json
import os
import re
import sys
import argparse
import html
from datetime import datetime
from pathlib import Path

import yaml

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

from api_health_banner import (
    render_api_health_banner_html,
    render_api_health_banner_md,
    classify_overall_status,
    OVERALL_SYMBOLS,
    CSS_DARK as API_HEALTH_CSS,
)

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
_HEAVY_MODEL = _resolve_model("heavy")
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
KNOWLEDGE_DIR = SYSTEM_DIR / "knowledge"


def load_dotenv():
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


def load_brand_voice():
    path = CONFIG_DIR / "brand-voice.yml"
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f)
    return {}


def load_radar_json(path):
    with open(path) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════
# DRAFT GENERATION (Claude Opus)
# ══════════════════════════════════════════════════════════════════════

# ── Publication authority / tier ranking ──────────────────────────────
# Tiers reflect domain authority, editorial reach, and relevance to My Villa's
# target audience (UHNW LA homeowners, architecture/design readers, insurance
# decision-makers). Higher tier = higher priority.
#
#   S = National mass reach, top-tier editorial (Bloomberg, WSJ, NYT)
#   A = National lifestyle / premium design (AD, Dwell, Robb Report)
#   B = Trade / regional with strong LA relevance (LA Times, TRD, LAist)
#   C = Niche or emerging (industry blogs, smaller local outlets)
#   D = Unverified / low-signal sources
PUBLICATION_RANK = {
    # ── Tier S ──────────────────────────────────────────────────────
    "bloomberg": {"tier": "S", "traffic": "95M+/mo", "label": "Bloomberg"},
    "bloomberg.com": {"tier": "S", "traffic": "95M+/mo", "label": "Bloomberg"},
    "wsj": {"tier": "S", "traffic": "85M+/mo", "label": "Wall Street Journal"},
    "wall street journal": {"tier": "S", "traffic": "85M+/mo", "label": "Wall Street Journal"},
    "wsj.com": {"tier": "S", "traffic": "85M+/mo", "label": "Wall Street Journal"},
    "nytimes": {"tier": "S", "traffic": "600M+/mo", "label": "New York Times"},
    "new york times": {"tier": "S", "traffic": "600M+/mo", "label": "New York Times"},
    "nytimes.com": {"tier": "S", "traffic": "600M+/mo", "label": "New York Times"},
    "forbes": {"tier": "S", "traffic": "150M+/mo", "label": "Forbes"},
    "forbes.com": {"tier": "S", "traffic": "150M+/mo", "label": "Forbes"},
    # ── Tier A ──────────────────────────────────────────────────────
    "architectural digest": {"tier": "A", "traffic": "12M+/mo", "label": "Architectural Digest"},
    "archdigest.com": {"tier": "A", "traffic": "12M+/mo", "label": "Architectural Digest"},
    "ad": {"tier": "A", "traffic": "12M+/mo", "label": "Architectural Digest"},
    "dwell": {"tier": "A", "traffic": "4M+/mo", "label": "Dwell"},
    "dwell.com": {"tier": "A", "traffic": "4M+/mo", "label": "Dwell"},
    "robb report": {"tier": "A", "traffic": "3M+/mo", "label": "Robb Report"},
    "robb report shelter": {"tier": "A", "traffic": "3M+/mo", "label": "Robb Report"},
    "robbreport.com": {"tier": "A", "traffic": "3M+/mo", "label": "Robb Report"},
    "dezeen": {"tier": "A", "traffic": "6M+/mo", "label": "Dezeen"},
    "dezeen.com": {"tier": "A", "traffic": "6M+/mo", "label": "Dezeen"},
    "archdaily": {"tier": "A", "traffic": "10M+/mo", "label": "ArchDaily"},
    "archdaily.com": {"tier": "A", "traffic": "10M+/mo", "label": "ArchDaily"},
    "wallpaper": {"tier": "A", "traffic": "2M+/mo", "label": "Wallpaper*"},
    "wallpaper*": {"tier": "A", "traffic": "2M+/mo", "label": "Wallpaper*"},
    "wallpaper.com": {"tier": "A", "traffic": "2M+/mo", "label": "Wallpaper*"},
    "mansion global": {"tier": "A", "traffic": "2M+/mo", "label": "Mansion Global"},
    "mansionglobal.com": {"tier": "A", "traffic": "2M+/mo", "label": "Mansion Global"},
    "variety": {"tier": "A", "traffic": "25M+/mo", "label": "Variety"},
    "variety.com": {"tier": "A", "traffic": "25M+/mo", "label": "Variety"},
    "the hollywood reporter": {"tier": "A", "traffic": "18M+/mo", "label": "Hollywood Reporter"},
    "hollywoodreporter.com": {"tier": "A", "traffic": "18M+/mo", "label": "Hollywood Reporter"},
    # ── Tier B ──────────────────────────────────────────────────────
    "la times": {"tier": "B", "traffic": "35M+/mo", "label": "LA Times"},
    "los angeles times": {"tier": "B", "traffic": "35M+/mo", "label": "LA Times"},
    "latimes.com": {"tier": "B", "traffic": "35M+/mo", "label": "LA Times"},
    "laist": {"tier": "B", "traffic": "3M+/mo", "label": "LAist"},
    "laist.com": {"tier": "B", "traffic": "3M+/mo", "label": "LAist"},
    "the real deal": {"tier": "B", "traffic": "5M+/mo", "label": "The Real Deal"},
    "therealdeal.com": {"tier": "B", "traffic": "5M+/mo", "label": "The Real Deal"},
    "curbed": {"tier": "B", "traffic": "3M+/mo", "label": "Curbed"},
    "curbed la": {"tier": "B", "traffic": "3M+/mo", "label": "Curbed"},
    "curbed.com": {"tier": "B", "traffic": "3M+/mo", "label": "Curbed"},
    "los angeles magazine": {"tier": "B", "traffic": "1M+/mo", "label": "LA Magazine"},
    "lamag": {"tier": "B", "traffic": "1M+/mo", "label": "LA Magazine"},
    "lamag.com": {"tier": "B", "traffic": "1M+/mo", "label": "LA Magazine"},
    "la business journal": {"tier": "B", "traffic": "500K/mo", "label": "LA Business Journal"},
    "labusinessjournal.com": {"tier": "B", "traffic": "500K/mo", "label": "LA Business Journal"},
    "abc7": {"tier": "B", "traffic": "20M+/mo", "label": "ABC7 LA"},
    "abc7.com": {"tier": "B", "traffic": "20M+/mo", "label": "ABC7 LA"},
    # ── Tier C ──────────────────────────────────────────────────────
    "nationaltoday.com": {"tier": "C", "traffic": "300K/mo", "label": "National Today"},
    "slvpost.com": {"tier": "C", "traffic": "<100K/mo", "label": "SLV Post"},
    "reddit": {"tier": "C", "traffic": "varies", "label": "Reddit"},
    "reddit.com": {"tier": "C", "traffic": "varies", "label": "Reddit"},
    "twitter": {"tier": "C", "traffic": "varies", "label": "X / Twitter"},
    "x": {"tier": "C", "traffic": "varies", "label": "X / Twitter"},
}

TIER_META = {
    "S": {"label": "S-Tier", "color": "#1a4d2e", "desc": "National mass reach"},
    "A": {"label": "A-Tier", "color": "#2d5a3d", "desc": "Premium lifestyle / design"},
    "B": {"label": "B-Tier", "color": "#8b6914", "desc": "Regional / trade"},
    "C": {"label": "C-Tier", "color": "#7a5c3f", "desc": "Niche / emerging"},
    "D": {"label": "D-Tier", "color": "#6b6b6b", "desc": "Unverified"},
}


def lookup_publication_rank(publication, url=""):
    """Return tier/traffic/label for a publication, or default D-tier."""
    if not publication and not url:
        return {"tier": "D", "traffic": "unknown", "label": publication or "Unknown"}
    pub_norm = (publication or "").lower().strip()
    if pub_norm in PUBLICATION_RANK:
        return PUBLICATION_RANK[pub_norm]
    for key, meta in PUBLICATION_RANK.items():
        if key in pub_norm or pub_norm in key:
            return meta
    if url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.lower().replace("www.", "")
            if domain in PUBLICATION_RANK:
                return PUBLICATION_RANK[domain]
            parts = domain.split(".")
            if len(parts) >= 2:
                root = ".".join(parts[-2:])
                if root in PUBLICATION_RANK:
                    return PUBLICATION_RANK[root]
        except Exception:
            pass
    return {"tier": "D", "traffic": "unknown", "label": publication or "Unknown"}


# ── Reach estimation for unknown publications ─────────────────────────
# When a publication isn't in approve.py PUBLICATION_REACH, we ask Opus
# once per unknown pub for its best estimate of monthly unique visitors.
# Cached in _system/radar/reach_cache.json so we don't re-ask.

REACH_CACHE_PATH = SCRIPT_DIR.parent / "radar" / "reach_cache.json"


def _load_reach_cache():
    if not REACH_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(REACH_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_reach_cache(cache):
    try:
        REACH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        REACH_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:
        print(f"  [Reach] Warning: could not save cache: {e}")


def _known_publication_reach(pub, url=""):
    """Mirror of approve.py PUBLICATION_REACH lookup — kept here so this
    script can tell whether a publication is already known (skip AI call).
    """
    try:
        # Import lazily to share the single source of truth
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        from approve import PUBLICATION_REACH
    except Exception:
        return None
    if not pub and not url:
        return None
    pub_l = (pub or "").lower().strip()
    url_l = (url or "").lower()
    if pub_l in PUBLICATION_REACH:
        return PUBLICATION_REACH[pub_l]
    for k, v in PUBLICATION_REACH.items():
        if k in pub_l or pub_l in k:
            return v
    for k, v in PUBLICATION_REACH.items():
        if k in url_l:
            return v
    return None


def estimate_unknown_reach(items, model=_HEAVY_MODEL):
    """For each item whose publication is NOT in the static PUBLICATION_REACH,
    ask Opus to estimate monthly unique visitors (in millions). Mutates each
    `item` in place adding `item["reach_estimate"]` (float, 0 if can't guess).

    Caches results in _system/radar/reach_cache.json to avoid re-estimating
    the same publication on future runs.
    """
    if not ANTHROPIC_OK:
        return items

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return items

    # Collect unknown publications (dedup)
    unknown = {}  # pub_lower → (pub_original, url_sample)
    for item in items:
        pub = (item.get("publication") or "").strip()
        if not pub:
            continue
        src = (item.get("source") or "").lower()
        # Skip social — reach there comes from follower counts, not pub map
        if src in ("grok_x", "reddit", "twitter"):
            continue
        url = item.get("url", "")
        if _known_publication_reach(pub, url) is not None:
            continue
        key = pub.lower().strip()
        if key not in unknown:
            unknown[key] = (pub, url)

    if not unknown:
        return items

    cache = _load_reach_cache()
    to_estimate = [(k, v) for k, v in unknown.items() if k not in cache]

    if to_estimate:
        print(f"  [Reach] Estimating {len(to_estimate)} unknown publications via Opus...")
        pub_list = "\n".join(
            f"- {orig} (seen at {url})" for _, (orig, url) in to_estimate
        )
        prompt = f"""Estimate monthly unique visitors for each publication below.

Return ONLY a JSON object mapping each publication (lowercase, exactly as listed) to a number in MILLIONS of monthly visitors. Use SimilarWeb-style rough estimates.

Examples:
- nytimes.com → 450
- housingwire.com → 1.5
- claimsjournal.com → 0.5
- A very small local blog → 0.05
- A domain you've never heard of → 0.1 (conservative guess)

Publications to estimate:
{pub_list}

Return format (strict JSON, no markdown):
{{"publication1.com": 1.5, "publication2.com": 0.3}}"""

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Strip code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*\n?", "", text)
                text = re.sub(r"\n?```\s*$", "", text).strip()
            # Extract JSON object
            if not text.startswith("{"):
                s = text.find("{")
                e = text.rfind("}")
                if s != -1 and e > s:
                    text = text[s:e + 1]
            estimates = json.loads(text)
            for pub_key, mvalue in estimates.items():
                try:
                    f = float(mvalue)
                    if 0 < f < 2000:
                        cache[pub_key.lower().strip()] = f
                        print(f"    [Reach] {pub_key}: {f}M")
                except (TypeError, ValueError):
                    continue
            _save_reach_cache(cache)
        except Exception as e:
            print(f"  [Reach] AI estimate failed: {e}")

    # Apply cached estimates to items
    for item in items:
        pub = (item.get("publication") or "").strip().lower()
        if pub in cache:
            item["reach_estimate"] = cache[pub]

    return items


# ── Editorial email fallbacks for known publications ──────────────────
# Used when Opus can't infer a specific journalist email from the article.
# These are publicly listed newsroom/editorial addresses.
EDITORIAL_EMAILS = {
    "laist": "tips@laist.com",
    "la times": "letters@latimes.com",
    "los angeles times": "letters@latimes.com",
    "latimes.com": "letters@latimes.com",
    "the real deal": "newsdesk@therealdeal.com",
    "therealdeal.com": "newsdesk@therealdeal.com",
    "realdeal": "newsdesk@therealdeal.com",
    "abc7": "abc7eyewitnessnews@abc.com",
    "abc7.com": "abc7eyewitnessnews@abc.com",
    "curbed": "tips@curbed.com",
    "curbed la": "tips@curbed.com",
    "curbed.com": "tips@curbed.com",
    "dezeen": "editorial@dezeen.com",
    "dezeen.com": "editorial@dezeen.com",
    "archdaily": "editorial@archdaily.com",
    "archdaily.com": "editorial@archdaily.com",
    "dwell": "letters@dwell.com",
    "dwell.com": "letters@dwell.com",
    "architectural digest": "contact@archdigest.com",
    "archdigest.com": "contact@archdigest.com",
    "ad": "contact@archdigest.com",
    "los angeles magazine": "editorial@lamag.com",
    "lamag": "editorial@lamag.com",
    "lamag.com": "editorial@lamag.com",
    "la business journal": "newsroom@labusinessjournal.com",
    "labusinessjournal.com": "newsroom@labusinessjournal.com",
    "wallpaper": "contact@wallpaper.com",
    "wallpaper.com": "contact@wallpaper.com",
    "wallpaper*": "contact@wallpaper.com",
    "bloomberg": "newstips@bloomberg.net",
    "bloomberg.com": "newstips@bloomberg.net",
    "wsj": "wsjcontact@wsj.com",
    "wall street journal": "wsjcontact@wsj.com",
    "wsj.com": "wsjcontact@wsj.com",
    "the hollywood reporter": "editorial@hollywoodreporter.com",
    "hollywoodreporter.com": "editorial@hollywoodreporter.com",
    "variety": "news@variety.com",
    "variety.com": "news@variety.com",
    "robb report": "editorial@robbreport.com",
    "robbreport.com": "editorial@robbreport.com",
    "mansion global": "editorial@mansionglobal.com",
    "mansionglobal.com": "editorial@mansionglobal.com",
    "nationaltoday.com": "editorial@nationaltoday.com",
    "slvpost.com": "editor@slvpost.com",
    # ── Extended 2026-04-24 ──
    # Added after analyzing last 14 days of radar output: these are the
    # publications that recurred without an editorial fallback.
    # Addresses below are from each publication's public press/contact page
    # or Media Kit, NOT inferred. If a domain's editorial address is not
    # documented publicly, leave it out — the editorial_scraper will try
    # to pull one off the article page / /contact / /about before we
    # fall back here. No email at all is safer than a bouncer.
    "washington post": "letters@washpost.com",
    "washingtonpost.com": "letters@washpost.com",
    "the washington post": "letters@washpost.com",
    "sf chronicle": "citydesk@sfchronicle.com",
    "sfchronicle.com": "citydesk@sfchronicle.com",
    "san francisco chronicle": "citydesk@sfchronicle.com",
    "calmatters": "info@calmatters.org",
    "calmatters.org": "info@calmatters.org",
    "cal matters": "info@calmatters.org",
    "realtor.com": "news@move.com",  # Move Inc. runs Realtor.com; news@ is the public PR/newsroom address
    "realtor": "news@move.com",
    "claims journal": "editorial@wellsmedia.com",  # Wells Media publishes Claims Journal; editorial@ is canonical
    "claimsjournal.com": "editorial@wellsmedia.com",
    "insurancenewsnet.com": "editor@insurancenewsnet.com",
    "insurance news net": "editor@insurancenewsnet.com",
    "insurance newsnet": "editor@insurancenewsnet.com",
    "fox business": "yourquestions@foxbusiness.com",
    "foxbusiness.com": "yourquestions@foxbusiness.com",
    "the acorn": "editor@theacorn.com",
    "mpacorn.com": "editor@theacorn.com",
    "theacorn.com": "editor@theacorn.com",
    "archeyes": "hello@archeyes.com",
    "archeyes.com": "hello@archeyes.com",
    "malibu rebuilds": "info@maliburebuilds.org",
    "maliburebuilds.org": "info@maliburebuilds.org",
    # ── Extended 2026-04-27 ──
    # Added after radar 2026-04-27 returned 0 contact emails — these
    # publications recurred without an editorial fallback, despite
    # publishing a contact address on their site. Verified manually
    # against the listed page.
    # firerescue1.com/contact-information → "editor@firerescue1.com"
    "firerescue1": "editor@firerescue1.com",
    "firerescue1.com": "editor@firerescue1.com",
    "fire rescue 1": "editor@firerescue1.com",
}


def _extract_domain(publication, url=""):
    """Return a clean second-level domain from publication or URL, or ""."""
    pub = (publication or "").lower().strip()
    # If publication is itself a domain-like string
    if "." in pub and " " not in pub:
        parts = pub.replace("www.", "").split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
    # Otherwise, fall back to URL
    if url:
        try:
            from urllib.parse import urlparse
            netloc = urlparse(url).netloc.lower().replace("www.", "")
            parts = netloc.split(".")
            if len(parts) >= 2:
                return ".".join(parts[-2:])
        except Exception:
            pass
    return ""


def guess_journalist_email(author_name, publication="", url=""):
    """Guess a journalist email from their byline + publication domain.

    Returns firstname.lastname@domain, or "" if inputs are insufficient.

    ⚠️  DEPRECATED (2026-04): this function is no longer called by the
    draft pipeline. Historical bounce rate was ~50%, which is
    unacceptable. It's kept in the file for reference / manual use, but
    the live fallback chain is now:

        Apollo verified → editorial_scraper → editorial_fallback table

    See `generate_drafts()` for the current logic and `editorial_scraper.py`
    for the replacement (scrapes real newsroom addresses off the article
    page and common /contact /about /press paths).
    """
    if not author_name:
        return ""
    domain = _extract_domain(publication, url)
    if not domain:
        return ""
    # Skip aggregators / social / wire services — no per-author addresses
    skip_domains = {
        "yahoo.com", "msn.com", "google.com", "news.google.com",
        "x.com", "twitter.com", "reddit.com", "facebook.com",
        "prnewswire.com", "businesswire.com", "globenewswire.com",
        "accesswire.com", "newswire.com", "apnews.com",
    }
    if domain in skip_domains:
        return ""
    # Split name
    parts = [p for p in re.split(r"\s+", author_name.strip()) if p]
    if len(parts) < 2:
        return ""
    first = parts[0].lower()
    last = parts[-1].lower()
    # Strip accents for ASCII email compatibility
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)
    if not first or not last:
        return ""
    return f"{first}.{last}@{domain}"


def lookup_editorial_email(publication, url=""):
    """Return a best-guess editorial email for a publication, or empty string."""
    if not publication and not url:
        return ""
    # Try publication name
    pub_norm = (publication or "").lower().strip()
    if pub_norm in EDITORIAL_EMAILS:
        return EDITORIAL_EMAILS[pub_norm]
    # Try partial match on publication name
    for key, email in EDITORIAL_EMAILS.items():
        if key in pub_norm or pub_norm in key:
            return email
    # Try URL domain
    if url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.lower().replace("www.", "")
            if domain in EDITORIAL_EMAILS:
                return EDITORIAL_EMAILS[domain]
            # Match root domain
            parts = domain.split(".")
            if len(parts) >= 2:
                root = ".".join(parts[-2:])
                if root in EDITORIAL_EMAILS:
                    return EDITORIAL_EMAILS[root]
        except Exception:
            pass
    return ""


def _robust_json_parse(text):
    """Try to parse a JSON array returned by Opus, attempting several recovery
    strategies when the model produces almost-valid JSON (typically unescaped
    apostrophes, smart quotes, or trailing text).

    Returns the parsed list, or None if all strategies fail.
    """
    if not text:
        return None

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: delegate to a more permissive parser if available
    # (json5 or demjson handle unescaped apostrophes; try if installed)
    for mod_name in ("json5", "demjson3", "demjson"):
        try:
            mod = __import__(mod_name)
            return mod.loads(text)
        except (ImportError, Exception):
            continue

    # Strategy 3: parse object-by-object using brace matching.
    # This sidesteps JSON-array-level errors by extracting each {...} block
    # and parsing it independently, replacing problematic unescaped quotes.
    objects = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, c in enumerate(text):
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start != -1:
                block = text[start:i + 1]
                # Try parsing this object directly
                try:
                    objects.append(json.loads(block))
                except json.JSONDecodeError:
                    # Final attempt: replace smart quotes and stray single
                    # quotes around the string values; very heuristic
                    fixed = block.replace("\u2019", "'").replace("\u2018", "'")
                    try:
                        objects.append(json.loads(fixed))
                    except json.JSONDecodeError:
                        pass  # skip malformed object
                start = -1
    return objects if objects else None


OUTREACH_VOICE_PATH = SCRIPT_DIR.parent / "knowledge" / "outreach_voice.md"


def _load_outreach_voice():
    """Load the outreach voice knowledge file. Falls back to a minimal \
    inline prompt if the file is missing."""
    try:
        return OUTREACH_VOICE_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  [Drafts] Warning: could not load outreach_voice.md: {e}")
        return ""


def _build_draft_system_prompt():
    """Compose the Opus system prompt for draft generation.

    The core voice/structure rules come from the canonical knowledge file
    (_system/knowledge/outreach_voice.md). This function wraps it with
    invariant technical instructions (JSON output format, recipient
    handling, tweet/reddit drafts) that don't belong in the knowledge file.
    """
    voice_spec = _load_outreach_voice()
    fallback = """\
You are Lisa Monelli, My Villa Media Team. Write friendly, warm, personal, \
SHORT first-contact emails to journalists — like writing to a colleague, \
not through PR. The email exists to open a conversation, not to pitch. \
MISSION to communicate: My Villa brings to Los Angeles the resilience of \
European construction culture AND the design and livability of Italian \
spaces, in homes built in exposed reinforced concrete (cemento a vista). \
ONE follow-up angle, never three. Never mention "Rome, Paris, LA opening \
soon" or apologize for our LA track record. NEVER describe My Villa as \
"a Los Angeles studio" / "an LA studio" / "a LA-based studio" — it sounds \
misleading; instead lead with "at My Villa, we bring..." with no \
geographic self-qualifier. NEVER attach anything to the first email (no \
press kit, no fact sheet, no PDFs). The body MUST end with an open \
question terminating in "?" inviting either more material later or a \
short call with our founder Paolo Mezzalama. Paolo is named ONLY in that \
closing question. Keep body 80-130 words. Sign as "Lisa Monelli / My Villa \
Media Team / info@myvilla.la · myvilla.la"."""
    voice_body = voice_spec or fallback

    return f"""\
You are Lisa Monelli, My Villa Media Team. You write first-contact outreach \
emails to journalists on behalf of My Villa.

The canonical voice, tone, structure, and content rules are defined in the \
knowledge file embedded below. **Follow them exactly.** When in doubt, the \
knowledge file is the tiebreaker.

================= BEGIN KNOWLEDGE FILE: outreach_voice.md =================

{voice_body}

================== END KNOWLEDGE FILE: outreach_voice.md ==================

ADDITIONAL TECHNICAL INSTRUCTIONS (not in the knowledge file):

TWEETS / REDDIT DRAFTS (non-email items only):
- Tweets: ≤ 280 chars, no hashtags, sharp single insight, conversational.
- Reddit: community-native, helpful, 80-160 words. Add [My Villa — \
disclosure] if mentioning the company.

EMAIL RECIPIENT RULES (for JSON output):
- If `author_name` is provided in the item, set `contact_name` to that \
name and greet by first name in the body.
- ALWAYS leave `contact_email` empty (""). Do NOT invent or pattern-guess \
addresses like `firstname.lastname@domain`. A downstream enrichment step \
fills the address from verified sources only (Apollo people-match → \
editorial scraper of the publication's own /contact /about /press pages → \
a curated table of public newsroom addresses). If none of those finds \
something, the email is dispatched to the editorial fallback or held back \
for manual review — never sent to a guessed address.
- For `contact_role`, state the role you can verify from the byline / \
about page (e.g. "Staff writer, LAist", "Senior Editor, Digital Insurance"). \
Do NOT append "(pattern guess)" or any other speculation marker.

JSON OUTPUT RULES:
- When embedded inside JSON strings, every apostrophe must be written as \
a plain ASCII quote ('), NEVER a curly/smart one (').
- Escape internal double quotes with \\".
- Prefer "we are" / "it is" / "do not" over contractions that require \
escaping where possible.

LENGTH ENFORCEMENT:
- Email body hard max: 150 words. Target 100-130.
- Subject: 6-12 words, NO em-dashes in subject.
- If the draft exceeds the budget, cut the follow-up angle before the \
positioning (the mission statement must stay)."""


# Build the prompt once at module load. To refresh after editing the
# knowledge file, restart the Python process.
DRAFT_SYSTEM_PROMPT = _build_draft_system_prompt()


# ══════════════════════════════════════════════════════════════════════
# EDITORIAL INTRO REWRITER
# ══════════════════════════════════════════════════════════════════════
# When the fallback chain routes an email to the newsroom instead of the
# original author (editorial_scraped or editorial_fallback), Opus's draft
# still opens with "Hi [Firstname]," which is wrong. This helper rewrites
# only the greeting + inserts a short disclaimer so the newsroom knows
# why they're getting the pitch. The rest of the body is left untouched.

_SALUTE_RE = re.compile(
    # Matches both "Hi Richard," (with name) and "Hello," (bare — which
    # Opus sometimes emits when it can't resolve contact_name).
    r"^\s*(Hi|Hello|Hey|Dear|Ciao)(\s+[^,\n]+?)?,\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# Brand-name overrides for publications whose proper capitalization
# would be lost to naive title-casing ("calmatters" -> "Calmatters"
# should really be "CalMatters"). Keys are the case-folded, TLD-stripped
# root; values are the canonical display name.
_PUBLICATION_DISPLAY_OVERRIDES = {
    "calmatters": "CalMatters",
    "housingwire": "HousingWire",
    "myvilla": "MyVilla",
    "sfchronicle": "SF Chronicle",
    "latimes": "LA Times",
    "wsj": "WSJ",
    "nytimes": "NY Times",
    "nypost": "NY Post",
    "cnn": "CNN",
    "bbc": "BBC",
    "npr": "NPR",
    "axios": "Axios",
    "propublica": "ProPublica",
    "realtor": "Realtor.com",
    "curbed": "Curbed",
    "dezeen": "Dezeen",
    "dwell": "Dwell",
    "archeyes": "ArchEyes",
    "archdaily": "ArchDaily",
    "archpaper": "The Architect's Newspaper",
    "mansionglobal": "Mansion Global",
    "archinect": "Archinect",
    "architecturaldigest": "Architectural Digest",
    "ibhs": "IBHS",
    "nahb": "NAHB",
    "fema": "FEMA",
    "calfire": "CAL FIRE",
    "foxbusiness": "Fox Business",
    "foxnews": "Fox News",
    "theacorn": "The Acorn",
    "malibutimes": "The Malibu Times",
    "insurancenewsnet": "Insurance News Net",
    "claimsjournal": "Claims Journal",
    "washpost": "Washington Post",
    "washingtonpost": "Washington Post",
}


def _clean_publication_display(publication: str) -> str:
    """Clean a publication name for use in a salutation.

    Examples:
      "The Washington Post"  -> "Washington Post"
      "sfchronicle.com"      -> "SF Chronicle"
      "calmatters.org"       -> "CalMatters"
      ""                     -> "newsroom"
    """
    pub = (publication or "").strip()
    if not pub:
        return "newsroom"

    def _norm(s: str) -> str:
        """Normalize for override lookup: lowercase, strip punct/spaces."""
        return re.sub(r"[^a-z0-9]", "", s.lower())

    # Try override on the raw input first (so "theacorn" still matches).
    hit = _PUBLICATION_DISPLAY_OVERRIDES.get(_norm(pub))
    if hit:
        return hit

    # Drop leading "The "
    if pub.lower().startswith("the "):
        pub = pub[4:]
        hit = _PUBLICATION_DISPLAY_OVERRIDES.get(_norm(pub))
        if hit:
            return hit

    # Drop trailing TLDs so "sfchronicle.com" becomes "sfchronicle", which
    # we then look up in the override table / fall back to title-casing.
    if "." in pub and " " not in pub:
        root = pub.split(".")[0]
        pub = root
        hit = _PUBLICATION_DISPLAY_OVERRIDES.get(_norm(pub))
        if hit:
            return hit

    # Don't over-capitalize brands that use lowercase
    if pub.islower() and len(pub) > 2:
        pub = pub.title()
    return pub


def _rewrite_for_editorial(
    body: str,
    author_name: str = "",
    publication: str = "",
) -> str:
    """Rewrite the intro of an outreach draft when routing to a newsroom.

    The body is assumed to start with a personal salutation like
    "Hi Richard,". We replace it with "Hi <Publication> team," and insert
    a one-line disclaimer explaining why the newsroom is receiving the
    pitch instead of the author. The rest of the body (which already
    contains the editorial hook and pitch copy) is preserved verbatim.

    If no salutation is detected the disclaimer is prepended and a
    generic newsroom greeting is added at the top.
    """
    if not body:
        return body

    pub_display = _clean_publication_display(publication)

    # Brands like "The Malibu Times" already carry the article. Drop it
    # from the greeting ("Hi Malibu Times team,") and keep it capitalized
    # in the disclaimer ("the Malibu Times newsroom") — no double "the".
    if pub_display.lower().startswith("the "):
        greeting_name = pub_display[4:]
        newsroom_phrase = f"the {pub_display[4:]}"
    else:
        greeting_name = pub_display
        newsroom_phrase = f"the {pub_display}" if pub_display != "newsroom" else "our"

    author_first = ""
    if author_name:
        author_first = author_name.split()[0].strip()

    if author_first:
        disclaimer = (
            f"(Writing to {newsroom_phrase} newsroom rather than "
            f"{author_first} directly — we couldn't locate a direct "
            f"contact. Feel free to forward if it's a better fit for "
            f"them or the desk.)"
        )
    else:
        disclaimer = (
            f"(Writing to {newsroom_phrase} newsroom directly — this is "
            f"meant for whichever desk is closest to the topic below. "
            f"Feel free to forward as appropriate.)"
        )

    new_greeting = f"Hi {greeting_name} team,"

    match = _SALUTE_RE.search(body)
    if not match:
        # No salutation found — prepend greeting + disclaimer.
        return f"{new_greeting}\n\n{disclaimer}\n\n{body.lstrip()}"

    # Replace the existing salutation line.
    before = body[: match.start()]
    after = body[match.end():]
    # Strip leading blank lines from `after` so we control spacing.
    after = after.lstrip("\n")

    # Opus sometimes writes TWO salutations back-to-back when the
    # recipient name is missing: first line "Hi <empty>," then a second
    # generic "Hello," at the top of the body. Strip the second one if
    # we find it, otherwise the newsroom sees two greetings + a
    # disclaimer wedged between them.
    second_match = _SALUTE_RE.match(after)
    if second_match:
        after = after[second_match.end():].lstrip("\n")

    return f"{before}{new_greeting}\n\n{disclaimer}\n\n{after}"


def generate_drafts(items, model=_HEAVY_MODEL):
    """Generate email/tweet/reddit drafts for qualified items using Opus."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [Drafts] Skipped — no valid Anthropic API key")
        return items

    client = anthropic.Anthropic(api_key=api_key)

    # Build prompt with all items
    items_desc = []
    for i, item in enumerate(items):
        source = item.get("source", "")
        action_type = "tweet" if source in ("grok_x", "twitter") else (
            "reddit_comment" if source == "reddit" else "email")
        items_desc.append({
            "index": i,
            "type": action_type,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "publication": item.get("publication", ""),
            "author_name": item.get("author_name", ""),
            "snippet": item.get("snippet", "")[:200],
            "summary": item.get("summary", ""),
            "engagement_angle": item.get("engagement_angle", ""),
            "score": item.get("ai_score", item.get("preliminary_score", 0)),
        })

    prompt = f"""Generate outreach drafts for these {len(items_desc)} radar opportunities.

For each item, based on its type, generate:

EMAIL items → Follow the outreach_voice.md knowledge file EXACTLY:
  - Tone: friendly, warm, personal — like writing to a colleague whose \
work you admire, NOT through a PR channel. Not salesy, not formal.
  - Subject: 6-12 words, NO em-dashes (use commas/periods), curiosity + \
specificity (NOT a pitch).
  - Body: 80-130 words (hard max 150), structure = \
Greeting / \
Appreciation (1-2 warm sent. referencing THEIR article specifically) / \
Mission + Positioning (1-2 sent., MUST convey the mission: bringing \
European construction resilience + Italian design & livability to homes \
in Los Angeles, in exposed reinforced concrete / cemento a vista; \
NEVER describe us as "a Los Angeles studio"/"an LA studio"/ \
"LA-based studio" — lead with "at My Villa, we bring..." without \
geographic self-qualifier; NEVER mention Rome/Paris/LA-opening-soon) / \
ONE simple follow-up angle (1-2 sent., big-picture observation — NEVER \
propose 2 or 3 angles, NEVER propose technical/certification mappings) / \
Open question (1 sent., MUST end with "?", offering either more material \
or a short call with our founder Paolo Mezzalama — Paolo named ONLY here) / \
Sign-off.
  - NO ATTACHMENTS in the first email (no press kit, no fact sheet, no \
PDFs, no renders). Material is offered LATER if the journalist shows \
interest.
  - The body MUST close with a question terminating in "?".
  - Recipient: contact_name + contact_role only. Leave contact_email \
empty — it is filled downstream from verified sources, never guessed.

TWEET items → tweet text (max 280 chars, no hashtags, sharp insight).
REDDIT_COMMENT items → comment text (conversational, helpful, 100-200 words).

Items:
{json.dumps(items_desc, indent=2)}

Return a JSON array with one object per item:
{{
  "index": 0,
  "type": "email|tweet|reddit_comment",
  "subject": "Email subject line (email only, 6-12 words, no em-dashes)",
  "body": "Draft text",
  "contact_name": "Journalist name from byline, or editorial team (email only)",
  "contact_role": "Their role/publication, e.g. 'Staff writer, LAist' or 'Newsroom' (email only)",
  "contact_email": "ALWAYS empty string \"\". Do not guess or invent addresses; downstream enrichment fills this from verified sources only."
}}

Return ONLY valid JSON, no markdown fences."""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=DRAFT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if response_text.startswith("```"):
            response_text = re.sub(r"^```(?:json)?\s*\n?", "", response_text)
            response_text = re.sub(r"\n?```\s*$", "", response_text)
            response_text = response_text.strip()
        # Extract JSON array if preamble text precedes it
        if not response_text.startswith("["):
            start = response_text.find("[")
            end = response_text.rfind("]")
            if start != -1 and end != -1 and end > start:
                response_text = response_text[start : end + 1]
        drafts = _robust_json_parse(response_text)
        if drafts is None:
            print(f"  [Drafts] JSON parse failed, returning unchanged items")
            return items

        # Lazy import — apollo_lookup has zero cost when APOLLO_API_KEY
        # is missing (single warning, then every call returns None).
        try:
            from apollo_lookup import lookup as apollo_lookup
        except ImportError:
            apollo_lookup = None  # defensive: new module, older checkout

        # Editorial scraper — NEW. Scrapes the article page itself plus
        # standard /contact /about /press fallback paths, looking for a
        # real newsroom address. Replaces the old pattern_guess fallback,
        # which had ~50% bounce rate. Scraper returns None if nothing
        # useful is found; the hardcoded `editorial_fallback` table picks
        # up the slack in that case.
        try:
            from editorial_scraper import lookup as editorial_scrape
        except ImportError:
            editorial_scrape = None

        # Same for the bounce blacklist — if an Apollo or scraped
        # hit returns an address that's already been blacklisted we
        # should skip straight to the next fallback.
        try:
            from reply_monitor import is_invalid_address
        except ImportError:
            is_invalid_address = None

        def _is_blacklisted(addr):
            if not addr or is_invalid_address is None:
                return False
            return is_invalid_address(addr)

        for draft in drafts:
            idx = draft.get("index", -1)
            if 0 <= idx < len(items):
                # Opus's email guess is discarded on principle — it had a
                # ~50% bounce rate and contributes nothing that the three
                # verified channels below don't already cover. We keep
                # contact_name / contact_role from the draft only.
                contact_name = (draft.get("contact_name") or "").strip()
                contact_role = (draft.get("contact_role") or "").strip()
                # Prefer the byline name extracted by radar.py over Opus's guess
                radar_author = (items[idx].get("author_name") or "").strip()
                if radar_author:
                    contact_name = radar_author
                contact_email = ""
                email_source = ""
                # Default routing: author. Flipped to "editorial" when
                # we can't find the author's address and fall back to a
                # newsroom/scraped address — downstream template logic
                # switches the intro copy accordingly (see Step 3).
                email_routing = "author"

                if draft.get("type") == "email":
                    pub = items[idx].get("publication", "")
                    url = items[idx].get("url", "")
                    domain = _extract_domain(pub, url)

                    # ── Channel 1: Apollo verified lookup ────────────
                    # Highest confidence: real person, verified email.
                    # If found, we're done — route to the author directly.
                    if apollo_lookup is not None and radar_author:
                        hit = apollo_lookup(
                            name=radar_author,
                            organization=pub,
                            domain=domain,
                            verbose=False,
                        )
                        if hit and hit.get("email") and not _is_blacklisted(hit["email"]):
                            contact_email = hit["email"]
                            email_source = hit["email_source"]
                            email_routing = "author"
                            # If Apollo returned a richer title, prefer
                            # it over the role Opus made up.
                            if hit.get("title"):
                                contact_role = hit["title"]

                    # ── Channel 2: Editorial scraper ─────────────────
                    # Visit the article page + /contact /about /press
                    # /masthead /tips … looking for a newsroom address
                    # that the publication itself publishes. This is
                    # route=editorial (we did NOT find the author).
                    if not contact_email and editorial_scrape is not None and url:
                        try:
                            hit = editorial_scrape(
                                url=url,
                                domain=domain,
                                author_email="",
                                verbose=False,
                            )
                        except Exception as e:  # noqa: BLE001
                            print(f"  [editorial_scraper] error: {type(e).__name__}: {e}")
                            hit = None
                        if hit and hit.get("email") and not _is_blacklisted(hit["email"]):
                            contact_email = hit["email"]
                            email_source = "editorial_scraped"
                            email_routing = "editorial"

                    # ── Channel 3: Hardcoded editorial table ─────────
                    # Curated per-domain newsroom addresses for the big
                    # outlets (Washington Post, CalMatters, LA Times, …).
                    # Last resort — still safe, since these are public.
                    if not contact_email:
                        fallback = lookup_editorial_email(pub, url)
                        if fallback and not _is_blacklisted(fallback):
                            contact_email = fallback
                            email_source = "editorial_fallback"
                            email_routing = "editorial"

                draft_body = draft.get("body", "")
                # When routing to a newsroom instead of the author,
                # rewrite the greeting + insert a one-line disclaimer.
                # Opus drafts always open "Hi <Firstname>," which is
                # wrong when we only have a tips@/editor@/newsroom@.
                if (
                    draft.get("type") == "email"
                    and email_routing == "editorial"
                    and draft_body
                ):
                    draft_body = _rewrite_for_editorial(
                        draft_body,
                        author_name=radar_author,
                        publication=items[idx].get("publication", ""),
                    )

                items[idx]["draft"] = {
                    "type": draft.get("type", "email"),
                    "subject": draft.get("subject", ""),
                    "body": draft_body,
                    "contact_name": contact_name,
                    "contact_role": contact_role,
                    "contact_email": contact_email,
                    "email_source": email_source,
                    "email_routing": email_routing,
                }

        print(f"  [Drafts] Generated {len(drafts)} drafts")
    except json.JSONDecodeError as e:
        print(f"  [Drafts] JSON parse error: {e}")
    except Exception as e:
        print(f"  [Drafts] Error: {e}")

    return items


# ══════════════════════════════════════════════════════════════════════
# VIRAL REPLY DRAFTS (dedicated prompt for high-engagement posts)
# ══════════════════════════════════════════════════════════════════════

VIRAL_REPLY_SYSTEM_PROMPT = """\
You write PUBLIC reply drafts for My Villa (myvilla.la), a luxury reinforced \
concrete villa company in Los Angeles, responding to high-engagement social \
posts. Each reply is attached to SOMEONE ELSE'S post, so it reflects on My \
Villa AND on the author.

THE GOLDEN RULE — we comment on other people's posts ONLY as a generous, \
positive peer, and ONLY when we can say something TRUE. We never make the \
author or their property look bad. Two hard limits; breaking either is a \
brand-safety failure worse than staying silent:

1) NEVER critical, cautionary, contrarian, or doubt-casting ABOUT THE AUTHOR'S \
OWN PROPERTY OR PROJECT. If the post is a seller / agent / broker / builder / \
architect showing THEIR OWN listing or build, reply ONLY with sincere \
appreciation for what is genuinely good (design, proportion, siting, light, \
craft, materials). Do NOT add "the one thing I'd watch", do NOT raise Zone-0 / \
clearance / framing / insurability / underwriting / fire-risk caveats, do NOT \
imply their home is deficient in ANY way. Scaring their buyers is the single \
worst thing we can do. If the only thing you'd add is a caveat → SKIP.

2) NEVER praise a quality the post doesn't actually have. Affirm ONLY what is \
clearly evidenced. If a build is wood-framed / combustible, do NOT call it \
resilient, insurable, or concrete — congratulating it on "resilience" is \
dishonest and makes us look foolish → SKIP.

WHEN a data point or technical nuance IS welcome: a GENERAL DISCUSSION / news / \
policy / market thread that is NOT anyone's own listing (an insurance-reform \
debate, a market-trend post, a Reddit thread). There you may add a substantive, \
NON-personal observation — about the topic, never aimed at diminishing a \
specific person's home.

We comment on a SELLER's listing ONLY if we can honestly admire it — genuine \
design quality, OR clearly concrete / non-combustible / insurable construction. \
If it's not genuinely admirable, SKIP (don't fake praise, don't critique).

VOICE:
- ≤ 200 characters (strictly enforced). Conversational, human, never a brand \
statement. Peer-to-peer with partners (agents / architects / builders).
- Never promotional: no "check us out", "learn more", "DM for info", "link in \
bio". Never include https://myvilla.la or any variant. No hashtags. No \
emoji-spam (one only if truly additive). Don't quote the post back verbatim.

APPROVED VOCABULARY (use only when genuinely true of the subject): "reinforced \
concrete" / "ICF" / "non-combustible" / "fire-resilient" / "underwriting" / \
"insurability" / "WPH Plus" / "Zone 0". Neighborhoods: Malibu, Pacific \
Palisades, Bel Air, Brentwood, Hancock Park, Beverly Hills.

FORBIDDEN: "bunker", "fortress", "anti-fire", "fireproof", "protect your \
family", "survive the next fire", "we specialize in...", "we offer...", and \
ANY caveat / warning / risk-flag aimed at the author's own property.

GOOD replies:
- On a luxury LISTING (appreciate, never caveat): "That flat ~21k compound is \
rare for the Strip — the organic-modern massing really suits the canyon \
frontage. Beautiful listing."
- On an architecture / design post: "The way the courtyard pulls light deep \
into the plan is beautifully handled — lovely restraint in the detailing."
- On a GENERAL insurance / market thread (data point OK, aimed at no one): \
"Worth noting IBHS WPH Plus + non-combustible assemblies measurably lower \
verified loss — a real lever in the rebuild math."

BAD replies — NEVER do this:
- "Organic modern reads beautifully — only thing I'd watch is Zone 0 clearance \
and how the framing underwrites long-term." → a caveat on their listing; scares \
their buyers. SKIP or pure praise instead.
- "Honored to see thoughtful design lead the way" on a visibly WOOD-FRAMED \
rebuild, implying resilience/concrete → dishonest praise. SKIP.
"""


def generate_viral_reply_drafts(viral_items, model=_HEAVY_MODEL):
    """Generate reply drafts for viral social posts.

    Uses a different prompt/voice from the journalist-pitch drafts.
    Adds `viral_reply` dict to each item with body + optional skip flag.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [ViralReply] Skipped — no valid Anthropic API key")
        return viral_items

    if not viral_items:
        return viral_items

    client = anthropic.Anthropic(api_key=api_key)

    items_desc = []
    for i, item in enumerate(viral_items):
        eng = item.get("engagement") or {}
        items_desc.append({
            "index": i,
            "platform": ("x" if item.get("source") == "grok_x"
                         else "instagram" if item.get("source") == "instagram"
                         else "reddit"),
            "author": item.get("author", ""),
            "url": item.get("url", ""),
            "title": item.get("title", "")[:160],
            "snippet": item.get("snippet", "")[:500],
            "metrics": {
                "likes": eng.get("likes", 0) or eng.get("score", 0),
                "retweets": eng.get("retweets", 0),
                "replies": eng.get("replies", 0) or eng.get("num_comments", 0),
                "views": eng.get("views", 0),
            },
        })

    prompt = f"""You are the ENGAGEMENT FILTER + reply writer for My Villa, which builds luxury reinforced-concrete villas in Los Angeles. Goal: QUALITY traffic only — reach potential BUYERS and build relationships with useful PARTNERS. We want FEW high-value comments, not volume.

STEP 1 — QUALIFY each post. Set "lead_type":
- "buyer": commenting here plausibly reaches ultra-high-net-worth people building / buying / rebuilding luxury homes in our geographies (Malibu, Beverly Hills, Bel Air, Brentwood, Calabasas, Pacific Palisades, Hidden Hills, Montecito) — e.g. luxury listings, trophy properties, high-end LA architecture, fire-resilient/insurable luxury homes, HNW post-fire rebuilds.
- "partner": the author or audience is rich in people we'd want a business relationship with — luxury real-estate agents/brokers, residential architects, interior designers, high-end developers & custom builders, landscape architects serving LA luxury.
- "brand": clearly on-topic for our niche (luxury architecture / exposed concrete / fire-resilience) and good for visibility, even if not directly buyer/partner.
- Otherwise → "body":"SKIP". SKIP generic design-fan content with no buyer/partner overlap, off-topic, consumer/DIY/budget home content, non-LA with no relevance, drama/rage/politics, competitors, influencer fluff. When in doubt, SKIP.

STEP 2 — for QUALIFIED posts only, write the reply. Platform rules:
- "x": a reply tweet — warm, human, ≤200 chars.
- "instagram": a COMMENT under the post — warmer, conversational, ≤220 chars, NO hashtags, never salesy: add genuine, POSITIVE value (sincere appreciation of what's good, or a non-personal nuance on a shared theme). For "partner" posts, write peer-to-peer (professional respect), not as a vendor.
- "reddit": a comment in subreddit register — substantive, no marketing.

ACROSS ALL PLATFORMS (non-negotiable): if the post shows the AUTHOR'S OWN listing / property / project, the reply is APPRECIATION ONLY — never a caveat, risk note, "one thing I'd watch", or anything that could worry their buyers or imply a flaw (insurability, framing, fire risk, value). A data point / technical nuance is allowed ONLY in a general discussion / news / market thread that is not anyone's own listing. Never praise a quality the post doesn't actually have (e.g. don't call a wood-frame build resilient/concrete) — if you can't be honestly positive, set body="SKIP".

For each item, output a JSON object:
{{
  "index": 0,
  "lead_type": "buyer | partner | brand",
  "why": "≤8 words: why this is quality traffic (e.g. 'Malibu luxury buyers in audience')",
  "body": "Your reply text OR 'SKIP'",
  "skip_reason": "Only if body is SKIP — e.g. 'design-fan audience, no buyers', 'off-topic'",
  "char_count": 180,
  "tone": "one of: appreciative, peer, data-led, conversational (NOTE: 'data-led' only for general discussion threads, never on the author's own listing; never contrarian/critical about someone's own property)"
}}

Items:
{json.dumps(items_desc, indent=2)}

Return ONLY a valid JSON array of {len(items_desc)} objects, no markdown fences."""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=VIRAL_REPLY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r"^```(?:json)?\s*\n?", "", response_text)
            response_text = re.sub(r"\n?```\s*$", "", response_text)
            response_text = response_text.strip()
        if not response_text.startswith("["):
            start = response_text.find("[")
            end = response_text.rfind("]")
            if start != -1 and end != -1:
                response_text = response_text[start:end + 1]

        drafts = _robust_json_parse(response_text)
        for draft in drafts:
            idx = draft.get("index", -1)
            if 0 <= idx < len(viral_items):
                body = (draft.get("body", "") or "").strip()
                is_skip = body.upper() == "SKIP" or not body
                viral_items[idx]["viral_reply"] = {
                    "body": "" if is_skip else body,
                    "skip": is_skip,
                    "skip_reason": draft.get("skip_reason", "") if is_skip else "",
                    "char_count": len(body) if not is_skip else 0,
                    "tone": draft.get("tone", ""),
                    "lead_type": draft.get("lead_type", "") if not is_skip else "",
                    "why": draft.get("why", "") if not is_skip else "",
                }
        print(f"  [ViralReply] Generated {len(drafts)} reply drafts "
              f"({sum(1 for d in drafts if (d.get('body','') or '').upper() == 'SKIP')} skipped)")
    except json.JSONDecodeError as e:
        print(f"  [ViralReply] JSON parse error: {e}")
    except Exception as e:
        print(f"  [ViralReply] Error: {e}")

    return viral_items


# ══════════════════════════════════════════════════════════════════════
# HTML RENDERING
# ══════════════════════════════════════════════════════════════════════

def esc(text):
    """HTML-escape text."""
    return html.escape(str(text)) if text else ""


def score_class(score):
    if score >= 19:
        return "score-high"
    elif score >= 16:
        return "score-med-high"
    return "score-med"


def cluster_label(cluster):
    labels = {
        "rebuild_direct": "Rebuild",
        "materials_construction": "Materials",
        "insurance_regulation": "Insurance",
        "luxury_architecture_LA": "Luxury Arch LA",
        "luxury_real_estate_LA": "Luxury RE LA",
        "concrete_architecture": "Concrete Arch",
        "twitter": "X/Twitter",
        "reddit": "Reddit",
        "architecture_blogs": "Arch Blogs",
    }
    return labels.get(cluster, cluster or "General")


def is_new_cluster(cluster):
    return cluster in ("luxury_architecture_LA", "luxury_real_estate_LA",
                       "concrete_architecture")


def render_card(item, idx):
    """Render a single opportunity card."""
    score = item.get("ai_score", item.get("preliminary_score", 0))
    cluster = item.get("cluster", "")
    draft = item.get("draft", {})
    draft_type = draft.get("type", "email")
    title = item.get("title", "Untitled")
    url = item.get("url", "#")
    pub = item.get("publication", "Unknown")
    date = item.get("date", "")
    new_cl = is_new_cluster(cluster)
    card_id = f"card-{idx}"

    # Type badge
    type_class = {"email": "type-email", "tweet": "type-tweet",
                  "reddit_comment": "type-reddit"}.get(draft_type, "type-email")
    type_label = {"email": "Email", "tweet": "Tweet",
                  "reddit_comment": "Reddit"}.get(draft_type, "Email")

    # Cluster badge
    cl_class = "cluster-badge new" if new_cl else "cluster-badge"
    cl_label = cluster_label(cluster)

    card_class = "card new-cluster" if new_cl else "card"

    # Publication rank
    rank = lookup_publication_rank(pub, url)
    tier = rank.get("tier", "D")
    traffic = rank.get("traffic", "unknown")
    tier_meta = TIER_META.get(tier, TIER_META["D"])
    tier_badge = f'<span class="tier-badge tier-{tier}" title="{esc(tier_meta["desc"])} — {esc(traffic)}">{esc(tier_meta["label"])}</span>'

    # Build sections
    header_html = f"""
    <div class="card-header">
      <div class="score-badge {score_class(score)}">
        <span class="score-num">{score}</span>
        <span class="score-label">Score</span>
      </div>
      <div class="card-meta">
        <div class="card-meta-top">
          <span class="{type_class} type-badge">{type_label}</span>
          <span class="{cl_class}">{esc(cl_label)}</span>
          {tier_badge}
        </div>
        <div class="card-title"><a href="{esc(url)}" target="_blank">{esc(title)}</a></div>
        <div class="card-source">{esc(pub)}<span class="dot">&middot;</span><span class="traffic-label">{esc(traffic)}</span><span class="dot">&middot;</span>{esc(date)}</div>
      </div>
    </div>"""

    # Contact section (email only) — always render for email drafts
    contact_html = ""
    if draft_type == "email":
        cname = draft.get("contact_name", "") or "Newsroom / Editorial"
        crole = draft.get("contact_role", "") or esc(pub)
        cemail = draft.get("contact_email", "") or ""
        initials = "".join(w[0] for w in cname.split()[:2]).upper() or "NR"
        email_warning = "" if cemail else '<span class="email-warn" title="Email mancante — verifica manualmente">&#9888;</span>'
        contact_html = f"""
    <div class="contact-section">
      <div class="contact-avatar">{esc(initials)}</div>
      <div class="contact-info">
        <div class="contact-name">{esc(cname)}</div>
        <div class="contact-role">{esc(crole)}</div>
        <div class="to-row">
          <span class="to-prefix">To</span>
          <input type="email" class="to-input" id="to-{idx}" placeholder="email@publication.com" value="{esc(cemail)}" oninput="saveState('{card_id}')">
          {email_warning}
        </div>
      </div>
    </div>"""

    # Context data (used by Revisiona modal)
    ctx_title = esc(title).replace('"', '&quot;')
    ctx_url = esc(url).replace('"', '&quot;')
    ctx_pub = esc(pub).replace('"', '&quot;')

    # Draft section
    if draft_type == "email":
        subject = draft.get("subject", "")
        body = draft.get("body", "")
        draft_html = f"""
    <div class="draft-section">
      <div class="draft-label">Email Draft</div>
      <div class="subject-row">
        <span class="subject-prefix">Subject</span>
        <input type="text" class="subject-input" id="subj-{idx}" oninput="saveState('{card_id}')" value="{esc(subject)}">
      </div>
      <textarea class="body-textarea" id="body-{idx}" oninput="countChars({idx}); saveState('{card_id}')">{esc(body)}</textarea>
      <div class="char-count" id="cc-{idx}">{len(body)} caratteri</div>
      <div class="opus-preview" id="opus-{idx}" style="display:none;"></div>
    </div>"""
    elif draft_type == "tweet":
        body = draft.get("body", "")
        draft_html = f"""
    <div class="draft-section">
      <div class="draft-label">Tweet Draft</div>
      <textarea class="tweet-textarea" id="body-{idx}" oninput="countChars({idx}); saveState('{card_id}')">{esc(body)}</textarea>
      <div class="char-count" id="cc-{idx}">{len(body)}/280</div>
      <div class="opus-preview" id="opus-{idx}" style="display:none;"></div>
    </div>"""
    elif draft_type == "reddit_comment":
        body = draft.get("body", "")
        draft_html = f"""
    <div class="draft-section">
      <div class="draft-label">Reddit Comment Draft</div>
      <textarea class="reddit-textarea" id="body-{idx}" oninput="countChars({idx}); saveState('{card_id}')">{esc(body)}</textarea>
      <div class="char-count" id="cc-{idx}">{len(body)} caratteri</div>
      <div class="opus-preview" id="opus-{idx}" style="display:none;"></div>
    </div>"""
    else:
        draft_html = ""

    # Footer buttons
    if draft_type == "email":
        copy_btn = f"""<button class="btn btn-primary" onclick="copyDraft({idx})">&#9993; Copy Draft</button>"""
        open_btn = f"""<button class="btn btn-secondary" type="button" onclick="openMail({idx})">&#9993; Apri Mail</button>"""
        footer_btns = f"{copy_btn}{open_btn}"
    elif draft_type == "tweet":
        copy_btn = f"""<button class="btn btn-primary" onclick="copyDraft({idx})">&#128203; Copy</button>"""
        open_btn = f"""<a class="btn btn-secondary" href="https://twitter.com/intent/tweet?text=" target="_blank" id="tweetlink-{idx}" onclick="updateTweetLink({idx})">Open Twitter</a>"""
        footer_btns = f"{copy_btn}{open_btn}"
    elif draft_type == "reddit_comment":
        copy_btn = f"""<button class="btn btn-primary" onclick="copyDraft({idx})">&#128203; Copy</button>"""
        open_btn = f"""<a class="btn btn-reddit" href="{esc(url)}" target="_blank">Open Thread</a>"""
        footer_btns = f"{copy_btn}{open_btn}"
    else:
        footer_btns = ""

    # Save + Revise buttons (inline with copy/open group)
    save_btn = f"""<button class="btn btn-save" onclick="saveEdits('{card_id}', {idx})">&#128190; Salva modifiche</button>"""
    revise_btn = (
        f'<button class="btn btn-revise" data-idx="{idx}" '
        f'data-dtype="{esc(draft_type)}" data-card="{esc(card_id)}" '
        f'data-title="{esc(ctx_title)}" data-url="{esc(ctx_url)}" '
        f'data-pub="{esc(ctx_pub)}" '
        f'onclick="openReviseModal(parseInt(this.dataset.idx), this.dataset.dtype, '
        f'this.dataset.card, this.dataset.title, this.dataset.url, this.dataset.pub)"'
        f'>&#9998; Revisiona con Opus</button>')

    footer_html = f"""
    <div class="card-footer">
      <div class="action-group">{footer_btns}{save_btn}{revise_btn}</div>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <span class="copy-feedback" id="cf-{idx}">Copied!</span>
        <span class="save-feedback" id="sf-{idx}">Salvato!</span>
        <div class="status-indicator" id="status-{idx}">
          <span class="dot"></span>
          <span>Pending</span>
        </div>
        <button class="btn btn-publish" id="pub-btn-{idx}" onclick="markSent('{card_id}', {idx})">Pubblica</button>
        <span class="published-label" id="pub-label-{idx}" style="display:none;">&#10003; Pubblicato</span>
        <button class="btn btn-skip" onclick="markSkipped('{card_id}', {idx})">Scarta</button>
      </div>
    </div>"""

    return f"""<div class="{card_class}" id="{card_id}" data-draft-type="{draft_type}">{header_html}{contact_html}{draft_html}{footer_html}</div>"""


def render_watchlist_card(item, idx):
    """Render a watchlist item."""
    score = item.get("ai_score", item.get("preliminary_score", 0))
    cluster = item.get("cluster", "")
    title = item.get("title", "")
    url = item.get("url", "#")
    pub = item.get("publication", "Unknown")
    summary = item.get("summary", item.get("snippet", "")[:150])
    new_cl = is_new_cluster(cluster)
    cl_class = "watchlist-card new-cluster" if new_cl else "watchlist-card"

    rank = lookup_publication_rank(pub, url)
    tier = rank.get("tier", "D")
    traffic = rank.get("traffic", "unknown")
    tier_meta = TIER_META.get(tier, TIER_META["D"])
    tier_badge = f'<span class="tier-badge tier-{tier}" title="{esc(tier_meta["desc"])} — {esc(traffic)}">{esc(tier_meta["label"])}</span>'

    return f"""
    <div class="{cl_class}">
      <div class="watchlist-header">
        <div class="watchlist-score"><span class="wnum">{score}</span><span class="wlabel">Score</span></div>
        <div>
          <div class="watchlist-title"><a href="{esc(url)}" target="_blank">{esc(title)}</a></div>
          <div class="watchlist-source">{esc(pub)} &middot; {tier_badge} <span class="traffic-label">{esc(traffic)}</span> &middot; {esc(cluster_label(cluster))}</div>
        </div>
      </div>
      <div class="watchlist-note">{esc(summary)}</div>
    </div>"""


def render_dashboard(radar_data, date_str):
    """Render the full HTML dashboard."""
    qualified = radar_data.get("qualified", [])
    watchlist = radar_data.get("watchlist", [])
    stats = radar_data.get("stats", {})
    lookback = radar_data.get("lookback_days", 7)
    api_health = radar_data.get("api_health", {})

    n_qualified = len(qualified)
    n_watchlist = len(watchlist)
    n_total = stats.get("total_unique", n_qualified + n_watchlist)

    # Count types
    n_email = sum(1 for q in qualified if q.get("draft", {}).get("type") == "email")
    n_tweet = sum(1 for q in qualified if q.get("draft", {}).get("type") == "tweet")
    n_reddit = sum(1 for q in qualified if q.get("draft", {}).get("type") == "reddit_comment")

    # Render cards
    cards_html = "\n".join(render_card(item, i) for i, item in enumerate(qualified))
    watchlist_html = "\n".join(render_watchlist_card(item, i) for i, item in enumerate(watchlist))
    # Banner has no radar_date here — the radar dashboard already shows
    # the date in its header, so the small in-banner date pill would be
    # redundant. Empty/None api_health → empty string (helper handles it).
    api_health_html = render_api_health_banner_html(api_health)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Villa — Engagement Radar · {esc(date_str)}</title>
<style>
  :root {{
    --cream: #f5f0e8; --warm-white: #faf8f4; --dark: #1a1714; --dark-2: #2c2720;
    --brown: #4a3728; --terracotta: #C2714F; --terracotta-light: #e8855a;
    --espresso: #3E2F2B; --mv-cream: #FAF8F5;
    --gold: #b89a5e; --gold-light: #d4b87a; --green: #3d6b4f; --green-light: #5a9b72;
    --olive: #5C6B4F; --stone: #A09890;
    --blue: #2b4f7a; --blue-light: #4a7ab5; --muted: #8a7d70; --border: #e0d8cc;
    --shadow: rgba(26,23,20,0.12); --red: #c0392b; --purple: #6b4a8a; --purple-light: #8a6aaa;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Georgia', 'Times New Roman', serif; background: var(--cream); color: var(--dark); min-height: 100vh; }}

  header {{ background: var(--dark); padding: 0 40px; display: flex; align-items: center; justify-content: space-between; height: 64px; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 20px rgba(0,0,0,0.4); }}
  .header-brand {{ display: flex; align-items: center; gap: 16px; }}
  .header-logo {{ font-size: 18px; font-weight: normal; color: var(--gold-light); letter-spacing: 0.12em; text-transform: uppercase; }}
  .header-logo span {{ color: var(--muted); font-size: 13px; letter-spacing: 0.06em; text-transform: none; display: block; margin-top: 1px; }}
  .header-divider {{ width: 1px; height: 32px; background: #3a3530; }}
  .header-title {{ font-size: 13px; color: #8a7d70; letter-spacing: 0.04em; }}
  .header-stats {{ display: flex; gap: 24px; align-items: center; }}
  .stat-pill {{ font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); }}
  .stat-pill strong {{ color: var(--gold-light); font-size: 15px; }}

  /* ── API Health banner — injected from api_health_banner.CSS_DARK ─── */
  {API_HEALTH_CSS}

  main {{ max-width: 1100px; margin: 0 auto; padding: 40px 24px 80px; }}
  .page-title {{ text-align: center; margin-bottom: 40px; }}
  .page-title h1 {{ font-size: 28px; font-weight: normal; color: var(--dark); letter-spacing: 0.04em; margin-bottom: 8px; }}
  .page-title p {{ font-size: 14px; color: var(--muted); font-family: 'Helvetica Neue', Arial, sans-serif; }}

  .summary-bar {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .summary-card {{ flex: 1; min-width: 110px; background: var(--dark); border-radius: 10px; padding: 18px 20px; text-align: center; }}
  .summary-card .num {{ font-size: 32px; color: var(--gold-light); font-weight: normal; display: block; }}
  .summary-card .label {{ font-size: 11px; color: var(--muted); font-family: 'Helvetica Neue', Arial, sans-serif; letter-spacing: 0.08em; text-transform: uppercase; margin-top: 4px; display: block; }}

  .section-header {{ font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; display: flex; align-items: center; gap: 12px; }}
  .section-header::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}

  .card {{ background: var(--warm-white); border-radius: 14px; border: 1px solid var(--border); margin-bottom: 28px; overflow: hidden; box-shadow: 0 2px 16px var(--shadow); transition: box-shadow 0.2s; }}
  .card:hover {{ box-shadow: 0 6px 32px rgba(26,23,20,0.18); }}
  .card.sent {{ opacity: 0.6; border-left: 4px solid var(--olive); }}
  .card.sent .card-title, .card.sent .draft-section {{ text-decoration: none; }}
  .card.sent .body-textarea, .card.sent .tweet-textarea, .card.sent .reddit-textarea, .card.sent .subject-input {{ text-decoration: line-through; color: var(--muted); }}
  .card.skipped {{ opacity: 0.4; border-left: 4px solid var(--stone); }}
  .card.new-cluster {{ border-left: 4px solid var(--purple); }}
  .card.sent.new-cluster {{ border-left: 4px solid var(--olive); }}
  .card.skipped.new-cluster {{ border-left: 4px solid var(--stone); }}

  .card-header {{ display: flex; align-items: center; gap: 16px; padding: 20px 24px 16px; border-bottom: 1px solid var(--border); background: var(--warm-white); }}
  .score-badge {{ width: 52px; height: 52px; border-radius: 10px; display: flex; flex-direction: column; align-items: center; justify-content: center; flex-shrink: 0; font-family: 'Helvetica Neue', Arial, sans-serif; }}
  .score-badge .score-num {{ font-size: 20px; font-weight: 700; line-height: 1; }}
  .score-badge .score-label {{ font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase; opacity: 0.7; margin-top: 2px; }}
  .score-high {{ background: #1a2e1e; color: #6dcc8a; }}
  .score-med-high {{ background: #1e2a12; color: #a0d458; }}
  .score-med {{ background: #2a2210; color: var(--gold-light); }}

  .card-meta {{ flex: 1; min-width: 0; }}
  .card-meta-top {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }}
  .type-badge {{ font-size: 10px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; padding: 3px 10px; border-radius: 20px; }}
  .tier-badge {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 3px; letter-spacing: 0.05em; text-transform: uppercase; cursor: help; color: #fff; }}
  .tier-S {{ background: #1a4d2e; }}
  .tier-A {{ background: #2d5a3d; }}
  .tier-B {{ background: #8b6914; }}
  .tier-C {{ background: #7a5c3f; }}
  .tier-D {{ background: #6b6b6b; }}
  .traffic-label {{ color: var(--muted); font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; }}
  .type-email {{ background: #e8f0ff; color: #2b4f7a; }}
  .type-tweet {{ background: #e8f5ff; color: #1a6fa8; }}
  .type-reddit {{ background: #ffe8e0; color: #c1622f; }}
  .cluster-badge {{ font-size: 10px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); background: #f0ece4; padding: 3px 10px; border-radius: 20px; letter-spacing: 0.06em; }}
  .cluster-badge.new {{ background: #ede4f4; color: var(--purple); font-weight: 600; }}

  .card-title {{ font-size: 16px; font-weight: normal; color: var(--dark); line-height: 1.4; margin-bottom: 6px; }}
  .card-title a {{ color: var(--dark); text-decoration: none; border-bottom: 1px solid var(--border); transition: border-color 0.15s, color 0.15s; }}
  .card-title a:hover {{ color: var(--terracotta); border-color: var(--terracotta); }}
  .card-source {{ font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); }}
  .card-source .dot {{ margin: 0 6px; }}

  .contact-section {{ display: flex; align-items: flex-start; gap: 12px; padding: 14px 24px; background: #f7f3ec; border-bottom: 1px solid var(--border); }}
  .contact-avatar {{ width: 36px; height: 36px; border-radius: 50%; background: var(--dark); color: var(--gold-light); display: flex; align-items: center; justify-content: center; font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; font-weight: 600; flex-shrink: 0; margin-top: 2px; }}
  .contact-info {{ flex: 1; min-width: 0; }}
  .contact-name {{ font-size: 14px; color: var(--dark); font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 500; }}
  .contact-role {{ font-size: 12px; color: var(--muted); font-family: 'Helvetica Neue', Arial, sans-serif; margin-top: 2px; }}
  .to-row {{ display: flex; align-items: center; gap: 8px; margin-top: 8px; }}
  .to-prefix {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 600; flex-shrink: 0; }}
  .to-input {{ flex: 1; font-size: 13px; font-family: 'Helvetica Neue', Arial, sans-serif; padding: 6px 10px; border: 1px solid var(--border); border-radius: 4px; background: #fff; color: var(--dark); min-width: 0; }}
  .to-input:focus {{ outline: none; border-color: var(--terracotta); }}
  .email-warn {{ font-size: 16px; color: #c47b18; flex-shrink: 0; cursor: help; }}

  .draft-section {{ padding: 20px 24px 24px; }}
  .draft-label {{ font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }}
  .draft-label::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}

  .subject-row {{ display: flex; gap: 0; margin-bottom: 12px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: white; }}
  .subject-prefix {{ font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 600; color: var(--muted); background: #f0ece4; padding: 10px 14px; display: flex; align-items: center; white-space: nowrap; letter-spacing: 0.06em; text-transform: uppercase; border-right: 1px solid var(--border); }}
  input[type="text"].subject-input {{ flex: 1; border: none; outline: none; padding: 10px 14px; font-size: 13px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--dark); background: white; width: 100%; }}
  input[type="text"].subject-input:focus {{ background: #fffdf8; }}

  .body-textarea {{ width: 100%; min-height: 200px; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 13.5px; font-family: 'Georgia', serif; line-height: 1.7; color: var(--dark); background: white; resize: vertical; outline: none; transition: border-color 0.15s; }}
  .body-textarea:focus {{ border-color: var(--gold); background: #fffdf8; }}

  .tweet-textarea {{ width: 100%; min-height: 80px; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 14px; font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.5; color: var(--dark); background: white; resize: vertical; outline: none; transition: border-color 0.15s; }}
  .tweet-textarea:focus {{ border-color: var(--blue-light); background: #f8fbff; }}

  .reddit-textarea {{ width: 100%; min-height: 140px; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 13.5px; font-family: 'Georgia', serif; line-height: 1.7; color: var(--dark); background: white; resize: vertical; outline: none; transition: border-color 0.15s; }}
  .reddit-textarea:focus {{ border-color: var(--terracotta); background: #fffaf6; }}

  .char-count {{ font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); text-align: right; margin-top: 4px; }}
  .char-count.warn {{ color: var(--terracotta); }}
  .char-count.over {{ color: var(--red); font-weight: 700; }}

  .card-footer {{ display: flex; align-items: center; justify-content: space-between; padding: 14px 24px; border-top: 1px solid var(--border); background: #faf7f2; gap: 12px; flex-wrap: wrap; }}
  .action-group {{ display: flex; gap: 10px; flex-wrap: wrap; }}

  .btn {{ display: inline-flex; align-items: center; gap: 7px; padding: 9px 18px; border-radius: 8px; font-size: 13px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 500; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; text-decoration: none; white-space: nowrap; }}
  .btn-primary {{ background: var(--dark); color: var(--gold-light); border-color: var(--dark); }}
  .btn-primary:hover {{ background: var(--brown); border-color: var(--brown); color: #f0d898; }}
  .btn-secondary {{ background: white; color: var(--dark); border-color: var(--border); }}
  .btn-secondary:hover {{ background: var(--cream); border-color: var(--gold); color: var(--brown); }}
  .btn-success {{ background: var(--green); color: white; border-color: var(--green); }}
  .btn-success:hover {{ background: #2d5239; }}
  .btn-skip {{ background: transparent; color: #c0b0a0; border-color: #ddd; }}
  .btn-skip:hover {{ color: #a08070; border-color: #c0a898; background: #faf5f0; }}
  .btn-reddit {{ background: #ff4500; color: white; border-color: #ff4500; }}
  .btn-reddit:hover {{ background: #cc3700; }}
  .btn-publish {{ background: var(--terracotta); color: white; border-color: var(--terracotta); font-weight: 600; }}
  .btn-publish:hover {{ background: #a85b3e; border-color: #a85b3e; }}
  .btn-save {{ background: white; color: var(--espresso); border-color: var(--border); }}
  .btn-save:hover {{ background: var(--mv-cream); border-color: var(--terracotta); color: var(--terracotta); }}
  .btn-revise {{ background: white; color: var(--espresso); border-color: var(--terracotta); }}
  .btn-revise:hover {{ background: var(--terracotta); color: white; }}
  .published-label {{ font-size: 13px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 600; color: var(--olive); letter-spacing: 0.04em; }}
  .save-feedback {{ font-size: 11px; color: var(--olive); font-family: 'Helvetica Neue', Arial, sans-serif; opacity: 0; transition: opacity 0.3s; pointer-events: none; }}
  .save-feedback.show {{ opacity: 1; }}

  /* Opus revision preview inside a card */
  .opus-preview {{ margin-top: 14px; padding: 14px 16px; background: #fffaf6; border: 1px solid var(--terracotta); border-left: 4px solid var(--terracotta); border-radius: 8px; }}
  .opus-preview .op-label {{ font-size: 11px; font-family: 'Helvetica Neue', Arial, sans-serif; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }}
  .opus-preview .op-content {{ font-size: 13.5px; font-family: 'Georgia', serif; line-height: 1.65; color: var(--dark); background: white; border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; white-space: pre-wrap; margin-bottom: 10px; max-height: 320px; overflow-y: auto; }}
  .opus-preview .op-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .opus-preview .op-error {{ color: var(--red); font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; line-height: 1.5; }}
  .opus-spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid var(--terracotta); border-top-color: transparent; border-radius: 50%; animation: opus-spin 0.8s linear infinite; vertical-align: middle; }}
  @keyframes opus-spin {{ to {{ transform: rotate(360deg); }} }}

  /* Server status banner */
  .server-banner {{ margin-bottom: 20px; padding: 12px 18px; border-radius: 8px; font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 13px; display: flex; align-items: center; gap: 10px; }}
  .server-banner.offline {{ background: #fdf3ec; border: 1px solid #e8c4a8; color: var(--brown); }}
  .server-banner.online {{ background: transparent; color: var(--olive); font-size: 12px; padding: 4px 0; justify-content: flex-end; }}
  .server-banner .server-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
  .server-banner.offline .server-dot {{ background: #d4a96a; }}
  .server-banner.online .server-dot {{ background: var(--olive); box-shadow: 0 0 6px rgba(92,107,79,0.5); }}

  /* Modal (Revisiona con Opus) */
  .modal-backdrop {{ position: fixed; inset: 0; background: rgba(26,23,20,0.66); display: none; align-items: center; justify-content: center; z-index: 1000; padding: 20px; }}
  .modal-backdrop.open {{ display: flex; }}
  .modal-card {{ background: white; border-radius: 14px; max-width: 600px; width: 100%; padding: 28px 30px 24px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); max-height: 92vh; overflow-y: auto; font-family: 'Helvetica Neue', Arial, sans-serif; }}
  .modal-title {{ font-family: 'Georgia', 'Cormorant Garamond', serif; font-size: 24px; color: var(--espresso); font-weight: normal; margin-bottom: 6px; }}
  .modal-sub {{ font-size: 12px; color: var(--muted); margin-bottom: 18px; letter-spacing: 0.04em; }}
  .modal-card label {{ display: block; font-size: 11px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }}
  .modal-card textarea {{ width: 100%; min-height: 120px; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; font-size: 13.5px; font-family: 'Georgia', serif; line-height: 1.6; color: var(--dark); background: var(--mv-cream); resize: vertical; outline: none; }}
  .modal-card textarea:focus {{ border-color: var(--terracotta); background: white; }}
  .modal-actions {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; flex-wrap: wrap; }}
  .modal-close {{ background: transparent; border: none; font-size: 20px; color: var(--muted); cursor: pointer; float: right; line-height: 1; padding: 0; }}
  .modal-close:hover {{ color: var(--espresso); }}

  .status-indicator {{ font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; display: flex; align-items: center; gap: 6px; }}
  .status-indicator .dot {{ width: 7px; height: 7px; border-radius: 50%; background: #d4a96a; }}
  .status-indicator.sent .dot {{ background: var(--green-light); }}
  .status-indicator.skipped .dot {{ background: #c0a898; }}

  .copy-feedback {{ font-size: 11px; color: var(--green); font-family: 'Helvetica Neue', Arial, sans-serif; opacity: 0; transition: opacity 0.3s; pointer-events: none; }}
  .copy-feedback.show {{ opacity: 1; }}

  .watchlist-card {{ background: var(--warm-white); border-radius: 14px; border: 1.5px dashed var(--border); margin-bottom: 20px; padding: 20px 24px; opacity: 0.8; box-shadow: none; }}
  .watchlist-card:hover {{ opacity: 1; }}
  .watchlist-card.new-cluster {{ border-color: var(--purple-light); }}
  .watchlist-header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }}
  .watchlist-score {{ width: 44px; height: 44px; border-radius: 8px; background: #2a2416; color: #b89a5e; display: flex; flex-direction: column; align-items: center; justify-content: center; font-family: 'Helvetica Neue', Arial, sans-serif; flex-shrink: 0; }}
  .watchlist-score .wnum {{ font-size: 17px; font-weight: 700; line-height: 1; }}
  .watchlist-score .wlabel {{ font-size: 8px; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.06em; }}
  .watchlist-title {{ font-size: 14px; color: var(--dark); line-height: 1.4; }}
  .watchlist-title a {{ color: var(--dark); text-decoration: none; border-bottom: 1px solid var(--border); }}
  .watchlist-source {{ font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); margin-top: 4px; }}
  .watchlist-note {{ background: #f2ede4; border-radius: 8px; padding: 12px 16px; font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--brown); line-height: 1.6; border-left: 3px solid var(--gold); }}

  footer {{ text-align: center; padding: 32px; font-size: 12px; font-family: 'Helvetica Neue', Arial, sans-serif; color: var(--muted); border-top: 1px solid var(--border); margin-top: 20px; }}
  footer strong {{ color: var(--brown); }}

  @media (max-width: 700px) {{
    header {{ padding: 0 16px; }}
    .header-stats {{ display: none; }}
    main {{ padding: 24px 14px 60px; }}
    .card-header {{ flex-wrap: wrap; }}
    .summary-bar {{ gap: 10px; }}
    .summary-card {{ min-width: 110px; }}
  }}
</style>
</head>
<body>

{api_health_html}

<header>
  <div class="header-brand">
    <div class="header-logo">
      My Villa
      <span>Italian Soul, Californian Body</span>
    </div>
    <div class="header-divider"></div>
    <div class="header-title">Engagement Radar &middot; {esc(date_str)}</div>
  </div>
  <div class="header-stats">
    <div class="stat-pill"><strong id="pendingCount">{n_qualified}</strong> pending</div>
    <div class="stat-pill"><strong id="sentCount">0</strong> sent</div>
  </div>
</header>

<main>
  <div id="serverBanner" class="server-banner offline" style="display:none;">
    <span class="server-dot"></span>
    <span id="serverBannerText">Il review server non &egrave; attivo. Le funzioni Modifica e Revisiona Opus richiedono l'avvio di review.command.</span>
  </div>

  <div class="page-title">
    <h1>Engagement Opportunities</h1>
    <p>{esc(date_str)} &middot; Lookback: {lookback} days &middot; {n_total} scanned &middot; {n_qualified} qualified &middot; {n_watchlist} watchlist</p>
  </div>

  <div class="summary-bar">
    <div class="summary-card"><span class="num">{n_qualified}</span><span class="label">Qualified</span></div>
    <div class="summary-card"><span class="num">{n_email}</span><span class="label">Emails</span></div>
    <div class="summary-card"><span class="num">{n_tweet}</span><span class="label">Tweets</span></div>
    <div class="summary-card"><span class="num">{n_reddit}</span><span class="label">Reddit</span></div>
    <div class="summary-card"><span class="num">{n_watchlist}</span><span class="label">Watchlist</span></div>
  </div>

  <div class="section-header">Qualified Opportunities (Score &ge; 15)</div>
  {cards_html}

  <div class="section-header">Watchlist (Score 12&ndash;14)</div>
  {watchlist_html}

</main>

<footer>
  <strong>My Villa</strong> &middot; Engagement Radar &middot; Generated {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))} &middot; Automated pipeline v2.0
</footer>

<div class="modal-backdrop" id="reviseModal" onclick="if(event.target===this)closeReviseModal()">
  <div class="modal-card">
    <button class="modal-close" onclick="closeReviseModal()" aria-label="Chiudi">&times;</button>
    <div class="modal-title">Chiedi a Opus di revisionare</div>
    <div class="modal-sub" id="reviseModalSub">Fornisci un feedback per guidare la revisione.</div>
    <label for="reviseFeedback">Feedback</label>
    <textarea id="reviseFeedback" placeholder="Es: pi&ugrave; corto, tono meno formale, aggiungi un esempio concreto..."></textarea>
    <div class="modal-actions">
      <button class="btn btn-skip" onclick="closeReviseModal()">Annulla</button>
      <button class="btn btn-publish" id="reviseSubmitBtn" onclick="submitRevise()">Invia a Opus</button>
    </div>
  </div>
</div>

<script>
// ── State persistence (localStorage) ──
const STORAGE_KEY = 'myvilla_radar_{date_str.replace("-", "")}';
const REVIEW_SERVER_URL = 'http://127.0.0.1:8787';
let reviewServerOnline = false;

function loadState() {{
  try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}'); }}
  catch {{ return {{}}; }}
}}
function persistState(state) {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}}
function saveState(cardId) {{
  const state = loadState();
  const card = document.getElementById(cardId);
  if (!card) return;
  const textarea = card.querySelector('textarea');
  const subjInput = card.querySelector('.subject-input');
  const toInput = card.querySelector('.to-input');
  state[cardId] = {{
    body: textarea ? textarea.value : '',
    subject: subjInput ? subjInput.value : '',
    to: toInput ? toInput.value : '',
    status: card.classList.contains('sent') ? 'sent' : card.classList.contains('skipped') ? 'skipped' : 'pending',
  }};
  persistState(state);
}}
function saveEdits(cardId, idx) {{
  saveState(cardId);
  const sf = document.getElementById('sf-' + idx);
  if (sf) {{
    sf.classList.add('show');
    setTimeout(() => sf.classList.remove('show'), 1800);
  }}
}}
function restoreState() {{
  const state = loadState();
  for (const [cardId, data] of Object.entries(state)) {{
    const card = document.getElementById(cardId);
    if (!card) continue;
    if (data.body) {{ const ta = card.querySelector('textarea'); if (ta) ta.value = data.body; }}
    if (data.subject) {{ const si = card.querySelector('.subject-input'); if (si) si.value = data.subject; }}
    if (data.to) {{ const ti = card.querySelector('.to-input'); if (ti) ti.value = data.to; }}
    if (data.status === 'sent') applySentVisual(card);
    if (data.status === 'skipped') applySkippedVisual(card);
  }}
  // Refresh counters on all textareas
  document.querySelectorAll('.card').forEach(card => {{
    const ta = card.querySelector('textarea');
    if (ta) {{
      const m = ta.id.match(/body-(\\d+)/);
      if (m) countChars(parseInt(m[1], 10));
    }}
  }});
  updateCounts();
}}

// ── Actions ──
function copyDraft(idx) {{
  const body = document.getElementById('body-' + idx);
  if (!body) return;
  navigator.clipboard.writeText(body.value).then(() => {{
    const cf = document.getElementById('cf-' + idx);
    cf.classList.add('show');
    setTimeout(() => cf.classList.remove('show'), 2000);
  }});
}}
function openMail(idx) {{
  const subj = document.getElementById('subj-' + idx);
  const body = document.getElementById('body-' + idx);
  const to = document.getElementById('to-' + idx);
  if (!body) return;
  const recipient = to ? (to.value || '').trim() : '';
  const subject = subj ? (subj.value || '') : '';
  const bodyText = body.value || '';
  // Build mailto: URL manually (don't encode the recipient, it breaks Mail.app)
  const url = 'mailto:' + recipient
    + '?subject=' + encodeURIComponent(subject)
    + '&body=' + encodeURIComponent(bodyText);
  // Use window.location instead of window.open — mailto: must replace current nav intent
  window.location.href = url;
}}
function updateTweetLink(idx) {{
  const body = document.getElementById('body-' + idx);
  const link = document.getElementById('tweetlink-' + idx);
  if (body && link) {{
    link.href = 'https://twitter.com/intent/tweet?text=' + encodeURIComponent(body.value);
  }}
}}
function countChars(idx) {{
  const body = document.getElementById('body-' + idx);
  const cc = document.getElementById('cc-' + idx);
  if (!body || !cc) return;
  const len = body.value.length;
  const card = body.closest('.card');
  const dtype = card ? card.getAttribute('data-draft-type') : '';
  if (dtype === 'tweet') {{
    cc.textContent = len + '/280';
    cc.className = 'char-count' + (len > 280 ? ' over' : len > 250 ? ' warn' : '');
  }} else {{
    cc.textContent = len + ' caratteri';
    cc.className = 'char-count';
  }}
}}

function applySentVisual(card) {{
  card.classList.remove('skipped');
  card.classList.add('sent');
  const idxMatch = card.id.match(/card-(\\d+)/);
  if (!idxMatch) return;
  const idx = idxMatch[1];
  const si = document.getElementById('status-' + idx);
  if (si) {{
    si.className = 'status-indicator sent';
    si.querySelector('span:last-child').textContent = 'Pubblicato';
  }}
  const btn = document.getElementById('pub-btn-' + idx);
  const lbl = document.getElementById('pub-label-' + idx);
  if (btn) btn.style.display = 'none';
  if (lbl) lbl.style.display = 'inline';
}}
function applySkippedVisual(card) {{
  card.classList.remove('sent');
  card.classList.add('skipped');
  const idxMatch = card.id.match(/card-(\\d+)/);
  if (!idxMatch) return;
  const idx = idxMatch[1];
  const si = document.getElementById('status-' + idx);
  if (si) {{
    si.className = 'status-indicator skipped';
    si.querySelector('span:last-child').textContent = 'Scartato';
  }}
  const btn = document.getElementById('pub-btn-' + idx);
  const lbl = document.getElementById('pub-label-' + idx);
  if (btn) btn.style.display = 'inline-flex';
  if (lbl) lbl.style.display = 'none';
}}

function markSent(cardId, idx) {{
  const card = document.getElementById(cardId);
  applySentVisual(card);
  saveState(cardId);
  updateCounts();
}}
function markSkipped(cardId, idx) {{
  const card = document.getElementById(cardId);
  applySkippedVisual(card);
  saveState(cardId);
  updateCounts();
}}
function updateCounts() {{
  const cards = document.querySelectorAll('.card');
  let pending = 0, sent = 0;
  cards.forEach(c => {{
    if (c.classList.contains('sent')) sent++;
    else if (!c.classList.contains('skipped')) pending++;
  }});
  document.getElementById('pendingCount').textContent = pending;
  document.getElementById('sentCount').textContent = sent;
}}

// ── Revisiona con Opus: modal + API ──
let reviseCtx = null; // {{ idx, draftType, cardId, title, url, publication }}

function openReviseModal(idx, draftType, cardId, title, url, publication) {{
  reviseCtx = {{ idx, draftType, cardId, title, url, publication }};
  const modal = document.getElementById('reviseModal');
  const fb = document.getElementById('reviseFeedback');
  const sub = document.getElementById('reviseModalSub');
  if (fb) fb.value = '';
  if (sub) sub.textContent = 'Tipo: ' + draftType + ' · ' + (title || '');
  modal.classList.add('open');
  setTimeout(() => {{ if (fb) fb.focus(); }}, 50);
}}
function closeReviseModal() {{
  const modal = document.getElementById('reviseModal');
  modal.classList.remove('open');
  reviseCtx = null;
}}
async function submitRevise() {{
  if (!reviseCtx) return;
  const {{ idx, draftType, cardId, title, url, publication }} = reviseCtx;
  const fb = document.getElementById('reviseFeedback').value.trim();
  const body = document.getElementById('body-' + idx);
  const content = body ? body.value : '';
  const previewEl = document.getElementById('opus-' + idx);
  const submitBtn = document.getElementById('reviseSubmitBtn');

  // Map draft_type: backend expects email | tweet | reddit
  const dt = draftType === 'reddit_comment' ? 'reddit' : draftType;

  // Show loading state in the card preview area
  previewEl.style.display = 'block';
  previewEl.innerHTML = '<div class="op-label"><span class="opus-spinner"></span> Opus sta revisionando&hellip;</div>';
  if (submitBtn) submitBtn.disabled = true;
  closeReviseModal();

  try {{
    const res = await fetch(REVIEW_SERVER_URL + '/api/revise', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        type: 'radar_draft',
        draft_type: dt,
        content: content,
        feedback: fb,
        context: {{ title: title, url: url, publication: publication }}
      }})
    }});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const revised = data.revised || data.content || data.text || '';
    if (!revised) throw new Error('Risposta vuota dal review server');
    renderOpusPreview(idx, revised, draftType, cardId, title, url, publication);
  }} catch (err) {{
    previewEl.innerHTML = '<div class="op-label">Errore</div><div class="op-error">Il review server non &egrave; attivo. Avvialo con doppio click su review.command per usare la revisione Opus.</div><div class="op-actions" style="margin-top:10px;"><button class="btn btn-skip" onclick="discardRevision(' + idx + ')">Chiudi</button></div>';
  }} finally {{
    if (submitBtn) submitBtn.disabled = false;
  }}
}}

function renderOpusPreview(idx, revised, draftType, cardId, title, url, publication) {{
  const previewEl = document.getElementById('opus-' + idx);
  if (!previewEl) return;
  // Store revised text on a data attribute for accept
  previewEl.dataset.revised = revised;
  previewEl.dataset.draftType = draftType;
  previewEl.dataset.cardId = cardId;
  previewEl.dataset.title = title;
  previewEl.dataset.url = url;
  previewEl.dataset.publication = publication;
  const safe = revised.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  previewEl.innerHTML =
    '<div class="op-label">Proposta Opus</div>' +
    '<div class="op-content">' + safe + '</div>' +
    '<div class="op-actions">' +
      '<button class="btn btn-publish" onclick="acceptRevision(' + idx + ')">Accetta</button>' +
      '<button class="btn btn-revise" onclick="reviseAgain(' + idx + ')">Revisiona ancora</button>' +
      '<button class="btn btn-skip" onclick="discardRevision(' + idx + ')">Scarta</button>' +
    '</div>';
}}

function acceptRevision(idx) {{
  const previewEl = document.getElementById('opus-' + idx);
  const body = document.getElementById('body-' + idx);
  if (!previewEl || !body) return;
  body.value = previewEl.dataset.revised || '';
  const cardId = previewEl.dataset.cardId;
  countChars(idx);
  saveState(cardId);
  discardRevision(idx);
}}
function reviseAgain(idx) {{
  const previewEl = document.getElementById('opus-' + idx);
  if (!previewEl) return;
  openReviseModal(
    idx,
    previewEl.dataset.draftType,
    previewEl.dataset.cardId,
    previewEl.dataset.title || '',
    previewEl.dataset.url || '',
    previewEl.dataset.publication || ''
  );
}}
function discardRevision(idx) {{
  const previewEl = document.getElementById('opus-' + idx);
  if (!previewEl) return;
  previewEl.style.display = 'none';
  previewEl.innerHTML = '';
}}

// ── Review server health check ──
async function checkReviewServer() {{
  const banner = document.getElementById('serverBanner');
  const textEl = document.getElementById('serverBannerText');
  try {{
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 1500);
    const res = await fetch(REVIEW_SERVER_URL + '/', {{ method: 'GET', signal: ctrl.signal, mode: 'cors' }});
    clearTimeout(t);
    reviewServerOnline = true;
    banner.className = 'server-banner online';
    banner.style.display = 'flex';
    if (textEl) textEl.innerHTML = '&bull; Opus revisione attiva';
  }} catch (e) {{
    reviewServerOnline = false;
    banner.className = 'server-banner offline';
    banner.style.display = 'flex';
    if (textEl) textEl.textContent = "Il review server non \\u00e8 attivo. Le funzioni Modifica e Revisiona Opus richiedono l'avvio di review.command.";
  }}
}}

// Close modal on Escape
document.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') closeReviseModal();
}});

// Init
document.addEventListener('DOMContentLoaded', () => {{
  restoreState();
  checkReviewServer();
}});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def render_markdown(radar_data, date_str):
    """Produce a human-readable Markdown radar report.

    Mirrors the format previously produced by the legacy
    `Engagement_Reports/pipeline/radar_pipeline.py` so editors can
    continue to read daily radars as Markdown instead of just HTML.
    """
    qualified = radar_data.get("qualified", [])
    watchlist = radar_data.get("watchlist", [])
    api_health = radar_data.get("api_health", {})
    lookback = radar_data.get("lookback_days", 7)

    # Format date: "20 aprile 2026 · lunedì · finestra 7gg"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        months_it = [
            "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"
        ]
        days_it = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
        date_heading = f"{dt.day} {months_it[dt.month-1]} {dt.year} · {days_it[dt.weekday()]}"
    except Exception:
        date_heading = date_str

    lines = []
    lines.append("# My Villa — Engagement Radar")
    lines.append(f"## {date_heading} · finestra {lookback}gg")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Executive Summary
    lines.append("## Executive Summary")
    lines.append("")
    sources_scanned = radar_data.get("sources_scanned", [])
    if not sources_scanned:
        # Fallback: infer from items
        sources_scanned = sorted({
            item.get("source", "unknown")
            for item in qualified + watchlist
        })
    lines.append(f"- **Fonti scansionate:** {', '.join(sources_scanned) if sources_scanned else 'n/a'}")
    lines.append(f"- **Risultati analizzati:** {radar_data.get('total_analyzed', 'n/a')}")
    lines.append(f"- **Opportunità qualificate (≥15):** **{len(qualified)}**")
    lines.append(f"- **Watchlist (10–14):** **{len(watchlist)}**")
    top_score = max([it.get("ai_score", it.get("preliminary_score", 0)) for it in qualified], default=0)
    lines.append(f"- **Top score:** **{top_score}**")

    # API health snapshot (if present in JSON). The classification logic
    # lives in api_health_banner.classify_overall_status so the markdown
    # summary, the HTML banner, and any future consumer all phrase the
    # state the same way (in particular: skip is warn-level, not ok).
    if api_health:
        status_key, label = classify_overall_status(api_health)
        emoji = OVERALL_SYMBOLS.get(status_key, "ℹ️")
        lines.append(f"- **Stato API:** {emoji} {label}")
        health_md = render_api_health_banner_md(api_health)
        if health_md:
            lines.append(health_md)
    lines.append("")

    # Top 3 highlights (from qualified)
    if qualified:
        lines.append("### Top 3 highlights")
        lines.append("")
        for i, item in enumerate(qualified[:3], 1):
            pub = item.get("publication") or item.get("source", "?")
            title = item.get("title", "")[:120]
            summary = item.get("summary") or item.get("engagement_angle") or item.get("snippet", "")
            summary = summary[:200].strip()
            lines.append(f"{i}. **{pub}** — {title}. {summary}")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Priority Opportunities Table
    if qualified:
        lines.append("## Priority Opportunities Table")
        lines.append("")
        lines.append("| Rank | Score | Tipo | Fonte | Titolo | Data |")
        lines.append("|------|-------|------|-------|--------|------|")
        for i, item in enumerate(qualified, 1):
            score = item.get("ai_score", item.get("preliminary_score", 0))
            draft_type = (item.get("draft") or {}).get("type", "")
            source_label = item.get("publication") or item.get("source", "?")
            title = item.get("title", "")[:80].replace("|", "/").replace("\n", " ").replace("\r", " ").strip()
            date = item.get("date", item.get("published", ""))[:10]
            source_clean = source_label.replace("|", "/").replace("\n", " ")
            lines.append(f"| {i} | **{score}** | {draft_type} | {source_clean} | {title} | {date} |")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Draft Messages
    if qualified:
        lines.append("## Draft Messages")
        lines.append("")
        for i, item in enumerate(qualified, 1):
            lines.append("---")
            lines.append("")
            draft = item.get("draft") or {}
            dtype = draft.get("type", "email").upper()
            pub = item.get("publication", item.get("source", "?"))
            score = item.get("ai_score", item.get("preliminary_score", 0))
            lines.append(f"### Opportunità #{i} — {dtype} · {pub} · Score {score}")
            lines.append("")
            url = item.get("url", "")
            if url:
                lines.append(f"**URL:** {url}")
                lines.append("")
            title = item.get("title", "")
            if title:
                lines.append(f"**Titolo originale:** {title}")
                lines.append("")
            summary = item.get("summary") or item.get("snippet", "")
            if summary:
                lines.append(f"**Contesto:** {summary.strip()[:500]}")
                lines.append("")
            if draft.get("subject"):
                lines.append(f"**Subject:** {draft['subject']}")
                lines.append("")
            if draft.get("contact_name") or draft.get("contact_email"):
                contact = draft.get("contact_name", "")
                role = draft.get("contact_role", "")
                email = draft.get("contact_email", "")
                lines.append(f"**Contatto:** {contact}{f' ({role})' if role else ''}{f' — {email}' if email else ''}")
                lines.append("")
            body = draft.get("body", "")
            if body:
                lines.append("**Draft:**")
                lines.append("")
                lines.append("```")
                lines.append(body.strip())
                lines.append("```")
                char_count = len(body.strip())
                if dtype == "TWEET":
                    status = "✓" if char_count <= 280 else "⚠️"
                    lines.append(f"Caratteri: {char_count} {status}")
                lines.append("")

    # Watchlist
    if watchlist:
        lines.append("---")
        lines.append("")
        lines.append("## Watchlist (score 10–14)")
        lines.append("")
        for item in watchlist:
            score = item.get("ai_score", item.get("preliminary_score", 0))
            pub = item.get("publication") or item.get("source", "?")
            title = item.get("title", "")[:100]
            url = item.get("url", "")
            lines.append(f"- **{score}** · {pub} — {title}  \n  {url}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Generate Radar Dashboard HTML + Markdown")
    parser.add_argument("--radar", "-r", required=True,
                        help="Path to radar JSON output")
    parser.add_argument("--output", "-o", default=None,
                        help="Output HTML path (default: same dir as radar, .html)")
    parser.add_argument("--markdown", "-m", default=None,
                        help="Output Markdown path (default: same dir as radar, .md). "
                             "Use 'none' to skip markdown export.")
    parser.add_argument("--model", type=str, default=_HEAVY_MODEL,
                        help="Claude model for draft generation")
    parser.add_argument("--skip-drafts", action="store_true",
                        help="Skip AI draft generation")
    args = parser.parse_args()

    radar_path = Path(args.radar)
    radar_data = load_radar_json(radar_path)
    date_str = radar_data.get("date", datetime.now().strftime("%Y-%m-%d"))

    qualified = radar_data.get("qualified", [])
    watchlist = radar_data.get("watchlist", [])
    viral = radar_data.get("viral_opportunities", [])

    print(f"\nMy Villa — Radar Dashboard Generator")
    print(f"{'='*50}")
    print(f"Date: {date_str}")
    print(f"Qualified: {len(qualified)} | Watchlist: {len(watchlist)} | Viral: {len(viral)}")

    # Estimate reach for unknown publications (cached across runs).
    # Runs regardless of --skip-drafts because reach metadata is cheap
    # and the dashboard reads it even when drafts are pre-generated.
    if qualified:
        qualified = estimate_unknown_reach(qualified, model=args.model)
        radar_data["qualified"] = qualified

    # Generate drafts for qualified items
    if not args.skip_drafts and qualified:
        print(f"\nGenerating drafts (model: {args.model})...")
        qualified = generate_drafts(qualified, model=args.model)
        radar_data["qualified"] = qualified
    else:
        # Ensure draft stubs exist
        for item in qualified:
            if "draft" not in item:
                source = item.get("source", "")
                draft_type = "tweet" if source in ("grok_x", "twitter") else (
                    "reddit_comment" if source == "reddit" else "email")
                item["draft"] = {
                    "type": draft_type,
                    "subject": "",
                    "body": f"[Draft pending — {item.get('title', '')[:60]}]",
                    "contact_name": "",
                    "contact_role": "",
                }

    # Generate viral reply drafts (separate prompt, conversational voice)
    if not args.skip_drafts and viral:
        print(f"\nGenerating viral reply drafts (model: {args.model})...")
        viral = generate_viral_reply_drafts(viral, model=args.model)

        # Auto-eliminazione (richiesta Ivo 2026-06-13): se l'AI giudica
        # un post senza valore strategico (body=SKIP), proporlo è solo
        # rumore. Lo togliamo dal radar E lo marchiamo dismissed, così
        # non torna nemmeno negli scan futuri.
        kept, skipped_items = [], []
        for it in viral:
            vr = it.get("viral_reply") or {}
            body_ = (vr.get("body") or "").strip()
            is_skip = bool(vr.get("skip")) or body_.upper() == "SKIP"
            (skipped_items if is_skip else kept).append(it)
        if skipped_items:
            dedup_path = Path(__file__).resolve().parent.parent / \
                "radar" / "previously_reported.json"
            try:
                dd = json.loads(dedup_path.read_text(encoding="utf-8")) \
                    if dedup_path.exists() else {"reported": []}
            except (OSError, json.JSONDecodeError):
                dd = {"reported": []}
            for it in skipped_items:
                reason = (it.get("viral_reply") or {}).get("skip_reason", "")
                print(f"  [ViralReply] ⊘ auto-eliminato: "
                      f"{(it.get('title') or '')[:55]} ({reason[:50]})")
                dd.setdefault("reported", []).append({
                    "date_first_reported": date_str,
                    "source": "ai_skip",
                    "title": (it.get("title") or "")[:120],
                    "score": None, "cluster": it.get("cluster"),
                    "action_type": "user_dismissed",
                    "url": it.get("url", ""),
                    "note": f"auto-skip AI: {reason[:100]}",
                })
            dedup_path.write_text(
                json.dumps(dd, indent=2, ensure_ascii=False),
                encoding="utf-8")
        viral = kept
        radar_data["viral_opportunities"] = viral

    # Persist updated drafts + reach estimates back into the source JSON so
    # downstream tools (approve.py dashboard, etc.) can read the generated
    # data. Writes even when --skip-drafts is set, because reach_estimate
    # was still computed above.
    try:
        with open(radar_path, "w") as f:
            json.dump(radar_data, f, indent=2, ensure_ascii=False)
        print(f"  [Persist] Updated {radar_path.name}")
    except Exception as e:
        print(f"  [Persist] Warning: could not update {radar_path.name}: {e}")

    # Render HTML
    print("\nRendering HTML dashboard...")
    html_content = render_dashboard(radar_data, date_str)

    # Save
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = radar_path.with_suffix(".html")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html_content)
    print(f"Saved: {output_path}")
    print(f"File size: {output_path.stat().st_size / 1024:.1f} KB")

    # Also export Markdown report (human-readable daily digest)
    if args.markdown != "none":
        if args.markdown:
            md_path = Path(args.markdown)
        else:
            md_path = radar_path.with_suffix(".md")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_content = render_markdown(radar_data, date_str)
        md_path.write_text(md_content, encoding="utf-8")
        print(f"Saved: {md_path}")
        print(f"MD size: {md_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
