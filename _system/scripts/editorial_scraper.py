#!/usr/bin/env python3
"""
Editorial email scraper for the MyVilla outreach system.

Purpose: find a real, publicly-listed newsroom/editorial email for a
given article by fetching the article URL (and a couple of fallback
paths like /contact and /about) and extracting `mailto:` links and
plain-text email addresses that live on the same domain as the
article.

Why: pattern-guessing journalist emails caused ~50% bounce rate. The
hardcoded EDITORIAL_EMAILS table in generate_radar_report.py only
covers ~30 publications. A scraper fills the gap between these two —
for publications where the editorial address is publicly disclosed but
not yet in the hardcoded table, we find it at radar time.

Usage::

    from editorial_scraper import lookup
    hit = lookup(url="https://example.com/article", domain="example.com")
    # {'email': 'tips@example.com', 'source': 'article_scraped', 'path': '/article'}

Return values::

    {'email': str, 'source': 'article_scraped' | 'contact_page' | 'about_page',
     'path': <relative path where the address was found>}
    or None if no reasonable editorial address could be found.

Cache: on-disk JSON at _system/outreach/editorial_scraper_cache.json,
30-day TTL, keyed by domain (one editorial address per domain is plenty
— we never want to scrape the same publication twice). Negative
results are cached with a shorter TTL (7 days) so a publication that
eventually adds a contact page gets rediscovered.

CLI smoke test::

    python3 editorial_scraper.py --url https://www.latimes.com/article/xyz
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── Config ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_FILE = PROJECT_ROOT / "_system" / "outreach" / "editorial_scraper_cache.json"
_CACHE_TTL_HIT_DAYS = 30
_CACHE_TTL_MISS_DAYS = 7
_HTTP_TIMEOUT_S = 8
_USER_AGENT = "MyVillaOutreachBot/1.0 (+https://myvilla.la)"

# Email prefixes that STRONGLY suggest editorial / newsroom intent.
# Score +10 each. Ordered roughly by how likely they are to reach a
# human who handles pitches.
_EDITORIAL_PREFIXES = {
    "tips": 15, "news": 14, "newsroom": 14, "editor": 13, "editorial": 13,
    "press": 12, "media": 11, "story": 11, "pitch": 11, "stories": 11,
    "letters": 10, "contact": 9, "hello": 9, "feedback": 8, "comments": 7,
    "info": 6, "help": 4, "general": 4,
}

# Prefixes we explicitly DON'T want (technical, legal, marketing).
_NEGATIVE_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "webmaster",
    "admin", "hostmaster", "postmaster", "abuse", "spam",
    "unsubscribe", "optout", "bounce", "mailer-daemon",
    "privacy", "legal", "copyright", "dmca", "compliance",
    "careers", "jobs", "hr", "recruiting", "recruit",
    "sales", "advertising", "ads", "sponsor", "partnerships",
    "billing", "accounts", "accounting", "finance",
    "marketing", "promo", "promotions",
    "subscribe", "subscription", "subs", "newsletter",
    "technical", "tech", "support", "helpdesk",
}

# Quick regex for harvesting email addresses from raw HTML text. Kept
# liberal; we filter by domain + prefix heuristics afterwards.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}"
)

# Match mailto: links specifically — we score these higher because the
# publisher chose to make them clickable.
_MAILTO_RE = re.compile(
    r'(?:href|HREF)\s*=\s*["\']?mailto:([^"\'?\s>]+)',
)

# Fallback paths to try if the article itself has no editorial email.
# Order matters: try the most author-specific first, then newsroom,
# then corporate. Each URL is resolved relative to the article's
# scheme+host.
_FALLBACK_PATHS = (
    "/contact", "/contact-us", "/contact.html",
    "/about", "/about-us", "/about.html",
    "/press", "/pressroom", "/media",
    "/masthead", "/team", "/staff",
    "/tips", "/newsroom",
)


# ── Cache helpers ─────────────────────────────────────────────────────

def _cache_load() -> dict[str, Any]:
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _cache_save(data: dict[str, Any]) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(_CACHE_FILE)


def _cache_valid(entry: dict[str, Any]) -> bool:
    """Is the cached entry still fresh (hit: 30d, miss: 7d)?"""
    if not isinstance(entry, dict):
        return False
    ts = entry.get("cached_at", 0)
    ttl_days = _CACHE_TTL_HIT_DAYS if entry.get("result") else _CACHE_TTL_MISS_DAYS
    return (time.time() - ts) < ttl_days * 86400


# ── HTTP + parsing ────────────────────────────────────────────────────

def _fetch(url: str, *, verbose: bool = False) -> str | None:
    """Fetch URL, return HTML body (or None on any error)."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                # Avoid downloading PDFs/videos by accident
                if verbose:
                    print(f"  [scraper] skip non-html content: {url} ({ctype})")
                return None
            raw = resp.read(1024 * 1024)  # 1 MB cap — plenty for any article page
            # Best-effort decoding
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                return raw.decode(charset, errors="replace")
            except LookupError:
                return raw.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — network, DNS, HTTP error
        if verbose:
            print(f"  [scraper] fetch failed for {url}: {type(e).__name__}: {e}")
        return None


def _same_site(email: str, domain: str) -> bool:
    """True iff the email's domain is the same as (or a sibling of) the site domain.

    Siblings are allowed because many publications use a sub-brand domain
    for email (e.g. `example.com` uses `@example.net` for editorial).
    """
    email_dom = email.split("@", 1)[1].lower() if "@" in email else ""
    if not email_dom:
        return False
    site = (domain or "").lower().lstrip("www.").split("/")[0]
    if not site:
        return True  # no site hint → accept any
    # Strip leading "www." from email side for symmetry
    email_dom = email_dom.lstrip("www.")
    # Exact match, or either is a suffix of the other (covers news.ex.com /
    # ex.com, plus mail.ex.com / ex.com)
    site_root = _second_level(site)
    email_root = _second_level(email_dom)
    return site_root == email_root and bool(site_root)


def _second_level(dom: str) -> str:
    parts = [p for p in dom.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def _score(email: str, *, in_mailto: bool, author_email: str | None) -> int:
    """Score an email address for editorial intent. Higher is better.
    Returns a negative number for rejects."""
    email = email.lower().strip().rstrip(".,;:")
    if author_email and email == author_email.lower():
        return -100  # the author's own address — we want editorial, not personal
    if "@" not in email:
        return -100
    prefix = email.split("@", 1)[0].split(".")[0]  # first token before dot
    if prefix in _NEGATIVE_PREFIXES:
        return -50
    score = _EDITORIAL_PREFIXES.get(prefix, 0)
    # Some publications use "firstname" as a personal editor email.
    # If the prefix has no dot/underscore and isn't in our allow list,
    # treat it as a person (could be a single editor) — weakly neutral.
    if score == 0 and "." not in email.split("@", 1)[0] and "_" not in email.split("@", 1)[0]:
        score = 2  # mild interest but not strong
    if in_mailto:
        score += 5  # author chose to publish it as a clickable link
    return score


def _extract_candidates(html: str, domain: str, *, author_email: str | None = None) -> list[tuple[int, str, bool]]:
    """Parse HTML, return list of (score, email, in_mailto) tuples,
    sorted descending by score."""
    # Mailto links first — we remember these were clickable
    mailtos = set()
    for m in _MAILTO_RE.finditer(html):
        addr = m.group(1).split("?")[0].strip().lower()
        if addr:
            mailtos.add(addr)
    # All plain-text emails
    all_emails = {m.group(0).lower() for m in _EMAIL_RE.finditer(html)}
    # Union
    all_emails |= mailtos
    candidates = []
    seen = set()
    for email in all_emails:
        if email in seen:
            continue
        seen.add(email)
        if not _same_site(email, domain):
            continue
        sc = _score(email, in_mailto=(email in mailtos), author_email=author_email)
        if sc <= 0:
            continue
        candidates.append((sc, email, email in mailtos))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates


# ── Public API ────────────────────────────────────────────────────────

def lookup(
    *,
    url: str,
    domain: str = "",
    author_email: str | None = None,
    verbose: bool = False,
) -> dict[str, Any] | None:
    """
    Try to find an editorial/newsroom email for the publication hosting
    `url`. Order of attempts:

    1. The article URL itself.
    2. A handful of conventional paths on the same host
       (/contact, /about, /press, /masthead, ...).

    Returns the best candidate as a dict, or None.

    `domain` is the publication's second-level domain. If empty, it's
    derived from the URL. Used to filter out cross-site emails (ads,
    affiliates, embedded widgets).

    `author_email` is the journalist's own address if known — we skip
    it since we're specifically looking for something OTHER than the
    author.
    """
    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    if not domain:
        domain = parsed.netloc.lstrip("www.")
    cache_key = _second_level(domain) or domain

    # Cache check
    cache = _cache_load()
    entry = cache.get(cache_key)
    if entry and _cache_valid(entry):
        if verbose:
            print(f"  [scraper] cache hit: {cache_key} → {entry.get('result')}")
        return entry.get("result")

    # Try the article URL first, then fallback paths on the same host.
    base = f"{parsed.scheme}://{parsed.netloc}"
    attempts: list[tuple[str, str]] = [(url, parsed.path or "/")]
    for path in _FALLBACK_PATHS:
        attempts.append((base + path, path))

    best_candidate = None
    for attempt_url, tag in attempts:
        html = _fetch(attempt_url, verbose=verbose)
        if not html:
            continue
        cands = _extract_candidates(html, domain=cache_key, author_email=author_email)
        if not cands:
            continue
        score, email, in_mailto = cands[0]
        source = "article_scraped" if attempt_url == url else (
            "contact_page" if "contact" in tag else
            "about_page" if "about" in tag or "masthead" in tag or "team" in tag or "staff" in tag else
            "article_scraped"
        )
        candidate = {"email": email, "source": source, "path": tag, "score": score}
        if verbose:
            print(f"  [scraper] {tag} → {email} (score {score})")
        # If we got a STRONG hit from the article itself, stop here.
        if attempt_url == url and score >= 10:
            best_candidate = candidate
            break
        # Otherwise keep the best across attempts.
        if best_candidate is None or score > best_candidate.get("score", 0):
            best_candidate = candidate
        # A high-confidence contact/about hit also ends the loop early.
        if score >= 15:
            break

    # Save to cache (positive or negative)
    cache[cache_key] = {
        "cached_at": time.time(),
        "result": best_candidate,
    }
    _cache_save(cache)

    return best_candidate


# ── CLI smoke test ────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--url", required=True, help="Article URL to scrape.")
    ap.add_argument("--domain", default="", help="Publication domain hint.")
    ap.add_argument("--author-email", default="", help="Skip this address.")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="Ignore + skip cache.")
    args = ap.parse_args(argv)

    if args.no_cache and _CACHE_FILE.exists():
        # Soft-ignore: rename cache aside during this call
        backup = _CACHE_FILE.with_suffix(".json.bak")
        _CACHE_FILE.rename(backup)
        try:
            result = lookup(
                url=args.url, domain=args.domain,
                author_email=args.author_email or None,
                verbose=args.verbose,
            )
        finally:
            backup.rename(_CACHE_FILE)
    else:
        result = lookup(
            url=args.url, domain=args.domain,
            author_email=args.author_email or None,
            verbose=args.verbose,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(_main())
