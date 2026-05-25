#!/usr/bin/env python3
"""
author_lookup.py — find a direct journalist contact when we only have
a newsroom alias.

Used by followup_engine.py at Touch 3 ("author rescue"): if the cold
pitch and the data-bump went to a generic alias (info@, newsroom@,
editorial@, etc.) and didn't get a reply, we try to upgrade the
channel by scraping the original article for a direct byline contact
and (carefully) verifying the email before sending.

Output shape — same dict shape the radar drafts produce, so the
follow-up engine can hand it back to send_email/send_draft uniformly:

  {
    "email": "first.last@publication.com",
    "name":  "First Last",
    "source": "byline_mailto" | "json_ld_author" | "author_profile"
              | "staff_page" | "pattern_guess",
    "confidence": "high" | "low",
    "evidence_url": "<URL where we found it>",
  }

"high" confidence = email was literally on a page (mailto:, JSON-LD
                    field, or rendered text). Auto-send.
"low"  confidence = we extracted a name only, then guessed the email
                    from common patterns. Goes to manual review.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent

# Headers that look like a normal browser. Several editorial sites
# block urllib's default User-Agent entirely.
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Newsroom alias prefixes — used both to identify "generic" recipients
# AND to filter out a generic mailto found on the page when we're
# trying to upgrade to a direct contact.
GENERIC_ALIAS_PREFIXES = {
    "info", "newsroom", "editorial", "tips", "media", "mediarelations",
    "events", "contact", "hello", "press", "submit", "pitches",
    "newsdesk", "metro", "dnmetro", "cdipress", "webassist", "admin",
    "office", "general", "letters", "comments", "noreply", "no-reply",
    "support", "help", "subscribe", "subscriptions",
}

# Email patterns to try when we have a name but no email. Most US
# editorial publications use one of the first three.
_EMAIL_PATTERNS = [
    "{first}.{last}@{domain}",   # megan.munce@sfchronicle.com
    "{first}{last}@{domain}",    # meganmunce@sfchronicle.com
    "{f}{last}@{domain}",        # mmunce@sfchronicle.com
    "{first}@{domain}",          # megan@sfchronicle.com
    "{last}@{domain}",           # munce@sfchronicle.com
]

# Recognized fields in JSON-LD "author" objects
_JSONLD_AUTHOR_KEYS = ("author", "creator", "contributor")


def is_generic_alias(email: str) -> bool:
    """True if the email is a newsroom/team alias rather than a
    person. Used by followup_engine to decide whether T3 should be
    a standard close OR an author-rescue attempt.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    local = email.split("@", 1)[0]
    # Exact match
    if local in GENERIC_ALIAS_PREFIXES:
        return True
    # Compound prefixes ("news", "info" inside) — guard against false
    # positives like "ginfo@..." by requiring word boundary
    for kw in ("news", "info", "edit", "contact", "press", "media", "tips"):
        if local == kw or local.startswith(kw + "_") or local.startswith(kw + "-"):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# HTTP fetch (with timeout & soft error handling)
# ─────────────────────────────────────────────────────────────────────

def _fetch(url: str, *, timeout: float = 8.0) -> str | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(2_000_000)  # cap at 2MB
            return raw.decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, TimeoutError, ValueError, OSError) as e:
        # Don't spam logs for normal blocks (403/429); just return None
        return None


# ─────────────────────────────────────────────────────────────────────
# HTML scraping — JSON-LD, meta tags, mailto, byline classes
# ─────────────────────────────────────────────────────────────────────

_EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Last-segment "TLDs" that are actually filename extensions or other
# junk that the regex would otherwise accept. Anything ending in these
# (e.g. "edificio@1x.jpg") is rejected.
_BAD_TLDS = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "tiff",
    "css", "js", "json", "xml", "html", "htm", "php", "asp", "aspx",
    "mp4", "webm", "mov", "mp3", "wav", "pdf", "doc", "docx",
    "zip", "rar", "tar", "gz",
    "ttf", "otf", "woff", "woff2", "eot",
    "min", "map",
}

# Public-suffix-ish list of valid email TLDs we'll accept. Conservative
# — better to miss a rare ".museum" than to send to a junk address.
_GOOD_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "io", "co", "us", "uk",
    "ca", "au", "de", "fr", "it", "es", "nl", "se", "ch", "be", "jp",
    "info", "biz", "name", "pro", "news", "press", "media",
    "tv", "fm", "ai", "app", "dev",
}

# Domains used by analytics / error tracking / CDN — these emails
# (random hash@sentry.io, etc.) end up embedded in JavaScript on
# editorial sites. Never a journalist contact.
_INFRA_DOMAINS = {
    "sentry.io", "bugsnag.com", "newrelic.com", "raygun.com",
    "datadog.com", "datadoghq.com", "honeybadger.io",
    "mixpanel.com", "amplitude.com", "segment.io", "segment.com",
    "google-analytics.com", "googleadservices.com", "googletagmanager.com",
    "doubleclick.net", "facebook.com", "fb.com", "twitter.com",
    "wordpress.com", "wpengine.com", "cloudflare.com", "akamai.com",
    "cloudfront.net", "github.io", "githubusercontent.com",
    "example.com", "example.org", "test.com", "localhost",
}
_JSON_LD_RX = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_META_AUTHOR_RX = re.compile(
    r'<meta\s+[^>]*(?:name|property)\s*=\s*["\'](?:article:author|author|og:author)["\']'
    r'\s+content\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_BYLINE_RX = re.compile(
    r'<(?:span|a|p|div)[^>]*class\s*=\s*["\'][^"\']*\b(?:byline|author(?:-name)?|writer)\b[^"\']*["\'][^>]*>'
    r'(.*?)</(?:span|a|p|div)>',
    re.DOTALL | re.IGNORECASE,
)
_AUTHOR_HREF_RX = re.compile(
    r'<a[^>]+(?:rel\s*=\s*["\'](?:author)["\']|class\s*=\s*["\'][^"\']*\bauthor[^"\']*["\'])'
    r'[^>]+href\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_TAG_RX = re.compile(r'<[^>]+>')


def _strip_html(s: str) -> str:
    """Remove HTML tags and decode the most common entities."""
    s = _HTML_TAG_RX.sub('', s)
    s = (s.replace('&amp;', '&').replace('&nbsp;', ' ')
          .replace('&#39;', "'").replace('&quot;', '"')
          .replace('&lt;', '<').replace('&gt;', '>'))
    return re.sub(r'\s+', ' ', s).strip()


def _find_jsonld_authors(html: str) -> list[dict]:
    """Extract author objects from JSON-LD <script> blocks."""
    authors = []
    for m in _JSON_LD_RX.finditer(html):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites wrap arrays — try harder
            try:
                data = json.loads(raw.rstrip(',') + ']')
            except Exception:
                continue
        # Normalize: data can be dict, list of dicts, or @graph
        nodes = []
        if isinstance(data, dict):
            if "@graph" in data:
                nodes.extend(data["@graph"])
            else:
                nodes.append(data)
        elif isinstance(data, list):
            nodes.extend(data)
        for n in nodes:
            if not isinstance(n, dict):
                continue
            for k in _JSONLD_AUTHOR_KEYS:
                v = n.get(k)
                if not v:
                    continue
                if isinstance(v, dict):
                    authors.append(v)
                elif isinstance(v, list):
                    authors.extend(a for a in v if isinstance(a, dict))
                elif isinstance(v, str):
                    authors.append({"name": v})
    return authors


def _parse_byline_text(html: str) -> str | None:
    """Extract author name from byline-class spans/divs. Returns the
    first reasonable-looking name found.
    """
    for m in _BYLINE_RX.finditer(html):
        text = _strip_html(m.group(1))
        # Drop "By " prefix
        text = re.sub(r'^(?:by\s+|written by\s+)', '', text, flags=re.IGNORECASE)
        # Take only what's before "and" / "," / "|" / "/"
        text = re.split(r'\s+(?:and|&|,|\||\/)\s+', text)[0].strip()
        if 4 <= len(text) <= 60 and ' ' in text and not text.lower().startswith('the '):
            return text
    return None


def _is_plausible_email(em: str) -> bool:
    """Reject strings the regex accepts but a human would call obvious
    junk: filename extensions in the TLD slot, unknown TLDs, etc.
    """
    em = em.lower()
    if "@" not in em or em.count("@") != 1:
        return False
    local, domain = em.split("@", 1)
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    if tld in _BAD_TLDS:
        return False
    # Strict allow-list for the TLD. Conservative on purpose.
    if tld not in _GOOD_TLDS:
        # Allow 2-letter country codes we didn't enumerate
        if not (len(tld) == 2 and tld.isalpha()):
            return False
    # Reject obvious image filenames: "edificio@1x.jpg" → "1x" is the
    # subdomain. Catches a common scraping false positive.
    if re.search(r'\b\d+x\.', domain):
        return False
    # Reject analytics / error-tracking / CDN domains. These emit
    # autogenerated emails embedded in JS bundles on editorial sites,
    # never a real journalist contact.
    domain_no_www = domain.replace("www.", "")
    if domain_no_www in _INFRA_DOMAINS:
        return False
    for infra in _INFRA_DOMAINS:
        if domain_no_www.endswith("." + infra):
            return False
    # Hashy local-part (32+ hex chars) is almost always a tracking ID,
    # not a person — catches "1937ab71c8804b2b8438178dfdd6468f@..."
    if re.fullmatch(r'[a-f0-9]{16,}', local):
        return False
    # JSON-escape leakage: HTML/JS sources sometimes encode '>' as
    # '>'. When the regex slurps that into a local part we end
    # up with "u003esubscriptions@..." or similar. Reject anything
    # starting with a Unicode-escape fragment.
    if re.match(r'^u00[0-9a-f]{2}', local):
        return False
    return True


# Tokens that, when found as a "name" part, indicate the extraction
# picked up an organization / job title rather than a person.
_NON_PERSON_TOKENS = {
    "the", "of", "and", "for", "with",
    "department", "dept", "state", "county", "city", "office",
    "bureau", "agency", "committee", "commission", "council",
    "association", "team", "staff", "press", "media", "newsroom",
    "editorial", "reporter", "writer", "correspondent", "contributor",
    "editor", "desk", "wire", "service", "news",
    "california", "wildfire", "subscriptions", "feedback",
}


def _looks_like_person_name(name: str) -> bool:
    """True if the extracted string looks like a real person's name
    (two distinct word-tokens that aren't on the org/title blacklist).
    """
    if not name:
        return False
    tokens = [t for t in re.split(r'\s+', name.strip()) if t.isalpha()]
    if len(tokens) < 2:
        return False
    lc = [t.lower() for t in tokens]
    if any(t in _NON_PERSON_TOKENS for t in lc):
        return False
    # All-lowercase tokens are suspicious — real bylines are
    # typically capitalized
    if all(t == t.lower() for t in tokens):
        return False
    return True


def _emails_on_page(html: str, *, exclude_generic: bool = True) -> list[str]:
    """All email addresses on the page, optionally filtered to skip
    the same generic-alias prefixes we're trying to escape.
    """
    seen = set()
    out = []
    for em in _EMAIL_RX.findall(html):
        em = em.lower()
        if em in seen:
            continue
        seen.add(em)
        if not _is_plausible_email(em):
            continue
        if exclude_generic and is_generic_alias(em):
            continue
        out.append(em)
    return out


# ─────────────────────────────────────────────────────────────────────
# Strategies
# ─────────────────────────────────────────────────────────────────────

def _try_jsonld(html: str, source_url: str) -> dict | None:
    """Strategy 1: JSON-LD author object. Often contains both name AND
    email or a URL to a profile page that contains the email.
    """
    for author in _find_jsonld_authors(html):
        name = author.get("name") or ""
        email = (author.get("email") or "").strip().lower()
        if email and not is_generic_alias(email):
            return {
                "email": email,
                "name": name if isinstance(name, str) else "",
                "source": "json_ld_author",
                "confidence": "high",
                "evidence_url": source_url,
            }
        # Found a name + author profile URL — we'll come back to it
        # in the author-profile strategy
    return None


def _try_mailto(html: str, source_url: str,
                publication_domain: str | None) -> dict | None:
    """Strategy 2: a mailto: link on the page, biased to non-generic
    and (if we know the publication) to the same domain as the
    publication.
    """
    candidates = _emails_on_page(html, exclude_generic=True)
    if not candidates:
        return None
    if publication_domain:
        same_domain = [
            e for e in candidates
            if e.split("@", 1)[1] == publication_domain
        ]
        if same_domain:
            candidates = same_domain
    if not candidates:
        return None
    return {
        "email": candidates[0],
        "name": "",
        "source": "byline_mailto",
        "confidence": "high",
        "evidence_url": source_url,
    }


def _try_author_profile(html: str, source_url: str) -> dict | None:
    """Strategy 3: follow the <a rel='author'> link to the author's
    profile page, then look for an email there.
    """
    m = _AUTHOR_HREF_RX.search(html)
    if not m:
        return None
    href = m.group(1)
    profile_url = urllib.parse.urljoin(source_url, href)
    if profile_url == source_url:
        return None
    page = _fetch(profile_url, timeout=6.0)
    if not page:
        return None
    emails = _emails_on_page(page, exclude_generic=True)
    if not emails:
        return None
    # Prefer email on same domain as profile page
    domain = urllib.parse.urlparse(profile_url).netloc.replace("www.", "")
    same = [e for e in emails if e.endswith("@" + domain)]
    chosen = same[0] if same else emails[0]
    return {
        "email": chosen,
        "name": "",
        "source": "author_profile",
        "confidence": "high",
        "evidence_url": profile_url,
    }


def _name_to_pattern_emails(name: str, domain: str) -> list[str]:
    """Produce common email patterns from a name + domain."""
    name = re.sub(r'\s+', ' ', name.strip().lower())
    parts = [p for p in name.split(' ') if p and p.isalpha()]
    if len(parts) < 2:
        return []
    first = parts[0]
    last = parts[-1]
    f = first[0]
    out = []
    for pat in _EMAIL_PATTERNS:
        try:
            out.append(pat.format(
                first=first, last=last, f=f, domain=domain,
            ))
        except KeyError:
            continue
    return out


def _try_pattern_guess(html: str, source_url: str,
                       publication_domain: str | None) -> dict | None:
    """Strategy 4: extract a name (JSON-LD, meta, or byline class) and
    construct an email from common patterns. Returned with low
    confidence — the caller is expected to route this to manual review.
    """
    if not publication_domain:
        return None

    # Pull names from anywhere we can
    name = None
    for author in _find_jsonld_authors(html):
        n = author.get("name")
        if isinstance(n, str) and ' ' in n:
            name = n
            break
    if not name:
        m = _META_AUTHOR_RX.search(html)
        if m:
            name = m.group(1)
    if not name:
        name = _parse_byline_text(html)

    if not name:
        return None
    name = re.sub(r'^(?:by\s+|written by\s+)', '', name, flags=re.IGNORECASE).strip()
    # Filter out org/title strings before we waste a pattern-guess
    # email on something that isn't a person.
    if not _looks_like_person_name(name):
        return None
    patterns = _name_to_pattern_emails(name, publication_domain)
    if not patterns:
        return None
    # Return the first (most common) pattern. The follow-up engine
    # will queue this for manual review since confidence is low.
    return {
        "email": patterns[0],
        "name": name,
        "source": "pattern_guess",
        "confidence": "low",
        "evidence_url": source_url,
        "alternatives": patterns[1:],
    }


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────

def find_direct_contact(source_url: str,
                        publication_domain: str | None = None) -> dict | None:
    """Try to upgrade a generic-alias outreach to a direct journalist
    contact by scraping the source article.

    Returns the contact dict (see module docstring) or None.

    publication_domain: optional hint, used to bias same-domain results
    when multiple emails are on the page. If omitted, derived from
    source_url.
    """
    if not source_url:
        return None
    if not publication_domain:
        publication_domain = urllib.parse.urlparse(
            source_url).netloc.replace("www.", "").lower()

    html = _fetch(source_url)
    if not html:
        return None

    # Try strategies in order of confidence.
    for strategy in (
        _try_jsonld,
        _try_mailto,
        _try_author_profile,
    ):
        try:
            if strategy is _try_mailto or strategy is _try_author_profile:
                # These need the domain hint
                result = strategy(html, source_url, publication_domain) \
                    if strategy is _try_mailto else strategy(html, source_url)
            else:
                result = strategy(html, source_url)
        except Exception:
            continue
        if result and result.get("email"):
            return result

    # Last resort: name + pattern guess (low confidence)
    try:
        return _try_pattern_guess(html, source_url, publication_domain)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# CLI for quick manual testing
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: author_lookup.py <article_url>")
        sys.exit(1)
    url = sys.argv[1]
    result = find_direct_contact(url)
    if result:
        print(json.dumps(result, indent=2))
    else:
        print("(no contact found)")
