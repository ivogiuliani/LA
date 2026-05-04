#!/usr/bin/env python3
"""
Apollo.io people-match wrapper for the MyVilla outreach system.

Purpose: source verified email addresses for journalists from Apollo's
People DB. Together with the editorial scraper and a curated newsroom
table, this replaces the older pattern-guessing path that produced ~50%
bounces.

Pipeline (lives in generate_radar_report.py.generate_drafts):

  1. Apollo `POST /api/v1/people/match` (this module) — verified email
     for the article author, with optional alias retry on the parent
     organization (see _ORG_ALIASES).
  2. editorial_scraper.lookup() — visits the article page + the
     publication's /contact /about /press /masthead pages and extracts
     a real newsroom email it actually published.
  3. lookup_editorial_email() — curated table of ~70 public newsroom
     addresses for the major outlets we frequently encounter.

If all three miss, contact_email stays empty. We never ship a guessed
address.

The module is **graceful**: if `APOLLO_API_KEY` is not set it logs once
and then every lookup returns None. The calling code treats this as
"Apollo had no match" and falls through to the next source.

Env vars read:
  - APOLLO_API_KEY  — required for any real lookup. If missing, the
    module is a no-op (returns None for every call).

CLI (smoke test):
    python3 apollo_lookup.py --name "Richard Lawson" --pub "HousingWire"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Config + cache
# --------------------------------------------------------------------------- #

APOLLO_API_URL = "https://api.apollo.io/api/v1/people/match"
APOLLO_TIMEOUT_S = 10

# Map Apollo's `email_status` values to our internal `email_source`
# classification. Apollo returns one of: verified, likely_to_engage,
# guessed, unavailable, pending_manual_fulfillment, unverified, extrapolated.
# We reduce them to three buckets so the dashboard can color-code:
#
#   - "apollo"         : high confidence, Send enabled by default
#   - "apollo_likely"  : medium confidence (user confirms before send)
#   - None (no match)  : caller falls through to pattern/editorial
_APOLLO_STATUS_TO_SOURCE = {
    "verified": "apollo",
    "likely_to_engage": "apollo_likely",
    "likely": "apollo_likely",
    "guessed": "apollo_likely",
    "extrapolated": "apollo_likely",
    "unverified": "apollo_likely",
    # Explicitly bad — do not use the email. Treat as no match.
    "unavailable": None,
    "pending_manual_fulfillment": None,
}


# Simple on-disk cache (avoid burning Apollo credits on repeat lookups
# within the same radar cycle). Keyed by a tuple of (name, domain).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_FILE = PROJECT_ROOT / "_system" / "outreach" / "apollo_cache.json"
_CACHE_TTL_DAYS = 30


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


def _cache_key(name: str, organization: str) -> str:
    return f"{(name or '').strip().lower()}|{(organization or '').strip().lower()}"


# Guard so we only print the "APOLLO_API_KEY missing" warning once per
# process (the generator calls lookup dozens of times per radar cycle).
_WARNED_MISSING_KEY = False


# --------------------------------------------------------------------------- #
# Publication-name detector
# --------------------------------------------------------------------------- #
#
# Sometimes radar.py extracts a byline that isn't a real person: anonymous
# blogs sign their posts with the publication name itself, and some sites
# use generic placeholders like "Editorial Team" or "Newsroom Staff".
# Apollo's people-match endpoint is guaranteed to miss in these cases —
# and we don't want to burn an API credit (or pollute the cache) finding
# that out. This guard runs before any HTTP call.
#
# Real example that triggered the addition: the 2026-04-29 radar pulled
# an article from theoutragedconsumer.com (a Substack) where the byline
# read "The Outraged Consumer". The lookup wasted a credit and returned
# None. With this guard, we skip Apollo entirely for that case.

_PUBLICATION_LIKE_PATTERNS = (
    re.compile(r"^editorial\s+(team|staff|board|desk)\b", re.IGNORECASE),
    re.compile(r"^newsroom\b", re.IGNORECASE),
    re.compile(r"^news\s+desk\b", re.IGNORECASE),
    re.compile(r"^staff\s+(writer|reporter)s?\b", re.IGNORECASE),
    re.compile(r"^press\s+(team|office)\b", re.IGNORECASE),
    re.compile(r"\beditorial\s+team\b", re.IGNORECASE),
)


def _normalize_for_compare(s: str) -> str:
    """Lowercase, strip whitespace, drop URL noise + leading 'the'.

    Designed so a byline like 'The Outraged Consumer' compares equal to
    a domain like 'theoutragedconsumer.com'. The 'the' must be removed
    AFTER alphanum normalization because in domains it's glued to the
    next word (no separating space).
    """
    s = (s or "").strip().lower()
    # Strip TLDs first — "theoutragedconsumer.com" → "theoutragedconsumer"
    s = re.sub(r"\.(com|net|org|io|co|us|news|blog)\b", "", s)
    # Drop everything that isn't a letter or digit (spaces, dots, dashes)
    s = re.sub(r"[^a-z0-9]+", "", s)
    # Now strip a leading "the" — works for both "the outragedconsumer"
    # (originally with space, now glued) and "theoutragedconsumer" (the
    # domain form, where "the" was already glued to the rest).
    if s.startswith("the") and len(s) > 3:
        s = s[3:]
    return s


def _looks_like_publication_name(
    name: str,
    organization: str = "",
    domain: str = "",
) -> bool:
    """Return True when 'name' is the publication itself, not a person.

    Catches:
      - byline equals the organization name (modulo whitespace/case/articles)
      - byline equals the domain stem (e.g. "The Outraged Consumer" vs
        "theoutragedconsumer.com")
      - byline matches a generic newsroom/editorial pattern

    Used as an early-exit before any Apollo HTTP call.
    """
    if not name:
        return False

    # Pattern match: "Editorial Team", "Newsroom", "Staff Writer", etc.
    for pat in _PUBLICATION_LIKE_PATTERNS:
        if pat.search(name):
            return True

    n = _normalize_for_compare(name)
    if not n:
        return False
    if organization and n == _normalize_for_compare(organization):
        return True
    if domain and n == _normalize_for_compare(domain):
        return True
    return False


# --------------------------------------------------------------------------- #
# Organization aliases
# --------------------------------------------------------------------------- #
#
# Some publications are tracked in Apollo under their parent company's
# name, not the article's domain. When the primary lookup misses, we
# retry once per alias listed here. Each retry costs 1 API credit only
# if the alias actually finds the person; misses are cheap.
#
# Format: { "<article-domain>": ["Alt Org 1", "Alt Org 2", ...] }
#
# Add an entry whenever a known journalist who exists on Apollo cannot
# be found via the article's domain. Verify the alias by running a
# manual `python3 apollo_lookup.py --name "..." --pub "<alias>"`.

_ORG_ALIASES: dict[str, list[str]] = {
    # Digital Insurance is published by Arizent (formerly SourceMedia).
    # Apollo indexes their reporters under the parent company.
    "dig-in.com": ["Arizent", "Digital Insurance"],

    # Property Insurance Coverage Law Blog is run by Merlin Law Group;
    # the contributors are attorneys at the firm, indexed under it.
    "propertyinsurancecoveragelaw.com": ["Merlin Law Group"],

    # Add more as new mismatches surface. Pattern: niche trade pub or
    # firm-branded blog where the journalist's employer ≠ article domain.
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def is_configured() -> bool:
    """True iff APOLLO_API_KEY is set and non-empty."""
    return bool(os.environ.get("APOLLO_API_KEY", "").strip())


# --------------------------------------------------------------------------- #
# Internal HTTP helper + sentinel exceptions
# --------------------------------------------------------------------------- #

class _ApolloAuthError(Exception):
    """Apollo returned 401/403 — auth issue. Don't retry with aliases."""


class _ApolloNetworkError(Exception):
    """Network / timeout / 429 / 5xx. Flaky — don't retry with aliases."""


def _apollo_match(
    *,
    name: str,
    organization: str,
    domain: str,
    api_key: str,
    verbose: bool,
) -> dict[str, Any] | None:
    """One Apollo /people/match call. No caching, no aliases — just the wire.

    Returns:
      - dict on a usable hit (email + recognized status)
      - None on genuine no-match or unusable hit (no email)

    Raises:
      _ApolloAuthError  on HTTP 401/403 (caller should give up entirely)
      _ApolloNetworkError on timeout / DNS / 429 / 5xx (caller may want to
        propagate None without caching, since the failure may be transient)
    """
    payload: dict[str, Any] = {
        "name": name,
        "reveal_personal_emails": True,
    }
    if organization:
        payload["organization_name"] = organization
    if domain:
        payload["domain"] = domain

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
        # Cloudflare in front of api.apollo.io rejects requests with no
        # User-Agent (error 1010). Send a plain browser-like UA.
        "User-Agent": "MyVillaOutreach/1.0 (+https://myvilla.la)",
        "Accept": "application/json",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        APOLLO_API_URL, data=data, headers=headers, method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=APOLLO_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            resp_data = json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if verbose:
            print(
                f"  [apollo_lookup] HTTP {e.code} for {name!r} "
                f"({organization or domain}): {err_body}"
            )
        if e.code in (401, 403):
            raise _ApolloAuthError(f"HTTP {e.code}") from e
        raise _ApolloNetworkError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001 — network, DNS, timeout, JSON
        if verbose:
            print(f"  [apollo_lookup] network error: {type(e).__name__}: {e}")
        raise _ApolloNetworkError(str(e)) from e

    person = resp_data.get("person")
    if not person or not isinstance(person, dict):
        if verbose:
            print(f"  [apollo_lookup] no match: {name!r} @ {organization or domain}")
        return None

    email = (person.get("email") or "").strip().lower()
    email_status = (person.get("email_status") or "").strip().lower()
    source = _APOLLO_STATUS_TO_SOURCE.get(email_status)
    # Apollo occasionally returns an email with no status field — treat
    # that as "likely" so the operator still confirms before sending.
    if email and source is None and email_status == "":
        source = "apollo_likely"

    if not email or not source:
        if verbose:
            print(
                f"  [apollo_lookup] unusable hit for {name!r}: "
                f"email={email!r} status={email_status!r}"
            )
        return None

    if verbose:
        print(
            f"  [apollo_lookup] ✓ {name!r} → {email}  "
            f"({source}, status={email_status}, org={organization or domain!r})"
        )
    return {
        "email": email,
        "email_source": source,
        "email_status": email_status,
        "apollo_id": person.get("id"),
        "title": person.get("title") or "",
        "headline": person.get("headline") or "",
        "linkedin_url": person.get("linkedin_url") or "",
        "organization_name": (
            (person.get("organization") or {}).get("name") or organization
        ),
    }


def lookup(
    *,
    name: str,
    organization: str = "",
    domain: str = "",
    verbose: bool = False,
) -> dict[str, Any] | None:
    """
    Look up a journalist in Apollo's People DB.

    Returns a dict with `{email, email_source, email_status, apollo_id,
    title, headline, linkedin_url, organization_name}` if a usable record
    is found, else None.

    Contract: callers should treat `email_source` as authoritative.
    Possible values:
      - "apollo"         → safe to send
      - "apollo_likely"  → send only after human confirm
      - None             → no Apollo hit, fall through to pattern/editorial

    Missing API key, network errors, rate limits, or absent matches all
    return None. The only visible effect is a verbose-mode log line.

    Lookup pipeline:
      1. publication-name guard — skip Apollo if the byline looks like a
         publication, not a person (early exit, no API call)
      2. cache check — 30-day TTL, keyed by (name, organization or domain)
      3. primary Apollo call with the caller's organization/domain
      4. if that misses, retry once per alias in _ORG_ALIASES[domain]
         (covers cases where Apollo indexes the journalist under a
         parent company instead of the article's domain)
      5. cache the final result (hit or genuine miss); skip caching on
         transient network errors so the next radar run can retry.
    """
    global _WARNED_MISSING_KEY

    api_key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not api_key:
        if not _WARNED_MISSING_KEY:
            print(
                "  [apollo_lookup] APOLLO_API_KEY not set — skipping all "
                "Apollo lookups (falling through to pattern/editorial "
                "fallbacks). Set it in .env to enable verified emails."
            )
            _WARNED_MISSING_KEY = True
        return None

    name = (name or "").strip()
    if not name:
        return None

    # ── Step 1: publication-name guard ────────────────────────────────
    # Skip the API call entirely when the byline is the publication
    # itself, a generic "Editorial Team", or similar. Apollo will always
    # miss in these cases and we'd just burn a credit.
    if _looks_like_publication_name(name, organization=organization, domain=domain):
        if verbose:
            print(
                f"  [apollo_lookup] skip {name!r} — looks like a "
                f"publication name, not a person (org={organization!r}, "
                f"domain={domain!r})"
            )
        return None

    # ── Step 2: cache check ───────────────────────────────────────────
    cache = _cache_load()
    key = _cache_key(name, organization or domain)
    entry = cache.get(key)
    if entry:
        ts = entry.get("cached_at", 0)
        if time.time() - ts < _CACHE_TTL_DAYS * 86400:
            if verbose:
                print(f"  [apollo_lookup] cache hit: {key}")
            return entry.get("result")

    # ── Step 3: primary Apollo call ───────────────────────────────────
    try:
        result = _apollo_match(
            name=name,
            organization=organization,
            domain=domain,
            api_key=api_key,
            verbose=verbose,
        )
    except _ApolloAuthError:
        # Permanent auth/permission error — cache negative so we don't
        # hammer Apollo with the same 401 for the rest of the run.
        cache[key] = {"cached_at": time.time(), "result": None}
        _cache_save(cache)
        return None
    except _ApolloNetworkError:
        # Transient — don't cache, let the next run retry.
        return None

    # ── Step 4: alias retry on genuine miss ───────────────────────────
    # Only retry if the primary lookup returned None (no person) AND we
    # have an alias mapping for this domain. Each alias costs ≤ 1 credit
    # but only if Apollo actually finds the person; misses are cheap.
    if result is None and domain:
        aliases = _ORG_ALIASES.get(domain.lower().strip(), [])
        for alt_org in aliases:
            if verbose:
                print(
                    f"  [apollo_lookup] retry {name!r} with alt org "
                    f"{alt_org!r} (domain {domain!r} missed)"
                )
            try:
                alt_result = _apollo_match(
                    name=name,
                    organization=alt_org,
                    domain="",  # don't send domain — alt_org IS the org
                    api_key=api_key,
                    verbose=verbose,
                )
            except (_ApolloAuthError, _ApolloNetworkError):
                # Stop retrying — infrastructure issue, not a data issue.
                break
            if alt_result and alt_result.get("email"):
                result = alt_result
                break  # first hit wins

    # ── Step 5: cache final result ────────────────────────────────────
    cache[key] = {"cached_at": time.time(), "result": result}
    _cache_save(cache)
    return result


# --------------------------------------------------------------------------- #
# CLI (smoke test)
# --------------------------------------------------------------------------- #

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--name", required=True, help="Journalist name.")
    parser.add_argument("--pub", default="", help="Publication / org name.")
    parser.add_argument("--domain", default="", help="Publication domain.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not is_configured():
        print("APOLLO_API_KEY is not set in the environment.")
        print("Add it to your .env and re-run.")
        return 2

    result = lookup(
        name=args.name,
        organization=args.pub,
        domain=args.domain,
        verbose=args.verbose,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(_main())
