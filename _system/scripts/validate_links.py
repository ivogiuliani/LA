#!/usr/bin/env python3
"""
My Villa — External Link Validator

Scans an HTML article (or directory of articles) and validates every external
<a href="http..."> link with a HEAD request (falling back to GET on 405).

Why: The article-generation AI occasionally fabricates plausible-looking source
URLs for secondary citations (e.g. a CDI page that doesn't exist at that path).
This tool catches those and, in --fix mode, unwraps the broken <a> tags so the
reader gets plain text instead of a dead link.

Usage:
  # Report only (no changes)
  python3 validate_links.py _drafts/journal/
  python3 validate_links.py blog/article.html

  # Auto-fix: strip <a> wrapper on broken links, preserve inner text
  python3 validate_links.py _drafts/journal/ --fix

  # JSON report for pipeline integration
  python3 validate_links.py path --json

Exit codes:
  0 — all links OK
  1 — at least one broken link found (even in --fix mode)
  2 — usage / I/O error
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

# ── Config ────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 myvilla-link-validator/1.0"
)
TIMEOUT = 12          # seconds per request
MAX_WORKERS = 8       # parallel HEAD requests
RETRY = 1             # one retry on transient network error

# Internal / share / social URLs we never validate — they're either authored
# templates or hash-based and HEAD-check is meaningless.
SKIP_HOST_SUFFIXES = (
    "myvilla.la",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "googletagmanager.com",
    # Social share endpoints (return 200 for any URL)
    "linkedin.com/sharing",
    "twitter.com/intent",
    "x.com/intent",
    "facebook.com/sharer",
    "api.whatsapp.com/send",
)

# Known-safe anti-bot / CDN-fronted / hotlink-locked hosts: accept 401/403/429
# as "probably up — the URL resolves, the server just gates bots or requires auth".
TOLERATE_403_HOSTS = (
    "bloomberg.com",
    "wsj.com",
    "nytimes.com",
    "ft.com",
    "cloudflare.com",
    "insurancejournal.com",
    "architecturaldigest.com",
    "therealdeal.com",
    "latimes.com",
    "washingtonpost.com",
    "fastcompany.com",
    "fire.ca.gov",
    "unsplash.com",
    "homesandgardens.com",
    "businessoffashion.com",
    "bizjournals.com",
    "foxbusiness.com",
    "bbc.com",
    "reuters.com",
    "cnbc.com",
    "forbes.com",
    # Peer-reviewed publishers / academic hosts — real URLs, anti-bot HEAD blocks
    "mdpi.com",
    "sciencedirect.com",
    "springer.com",
    "tandfonline.com",
    "wiley.com",
    "sagepub.com",
    "cambridge.org",
    "oup.com",
    "jstor.org",
)

# Regex to extract every href/src that starts with http(s)://
HREF_RE = re.compile(r'(?P<attr>href|src)\s*=\s*(?P<q>["\'])(?P<url>https?://[^"\']+)(?P=q)', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────
def should_skip(url: str) -> bool:
    """URLs we never validate (internal, social share endpoints, fonts, etc.)."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
    except Exception:
        return True
    if not host:
        return True
    # Normalize: drop leading "www." so the skip list matches regardless of subdomain prefix
    host_norm = host[4:] if host.startswith("www.") else host
    host_path = f"{host_norm}{parsed.path}"
    for suffix in SKIP_HOST_SUFFIXES:
        if "/" in suffix:
            # Substring match on host+path — social share URLs embed the user URL as query
            if suffix in host_path:
                return True
        elif host_norm == suffix or host_norm.endswith("." + suffix):
            return True
    return False


def tolerates_403(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    for suffix in TOLERATE_403_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


# Soft-404 markers — the page returns 200 but the body announces "not found".
# Checked case-insensitively inside first ~8 KB of body.
SOFT_404_MARKERS = (
    "document not found",
    "page not found",
    "page cannot be found",
    "the page you are looking for",
    "404 error",
    "<title>404",
    "<title>not found",
    "error 404",
    "this page doesn't exist",
    "that page no longer exists",
)


def _request(url: str, method: str, read_body: bool = False) -> tuple[int, str, bytes]:
    """Return (status_code, final_url, body_prefix). body_prefix is up to 8KB if read_body else b''.
    Raises urllib.error.URLError on network failure.
    """
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(8192) if read_body else b""
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, url, b""


def _is_soft_404(body: bytes) -> bool:
    if not body:
        return False
    try:
        snippet = body.decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    # Only look at <head> + first slice of <body> to minimize false positives
    head_region = snippet[:4096]
    for marker in SOFT_404_MARKERS:
        if marker in head_region:
            return True
    return False


def check_url(url: str) -> dict:
    """Return {url, ok, status, reason, method_used}."""
    result = {"url": url, "ok": False, "status": 0, "reason": "", "method": ""}

    if should_skip(url):
        result.update(ok=True, status=0, reason="skipped (internal/social/font)", method="skip")
        return result

    last_err = ""
    for attempt in range(RETRY + 1):
        # HEAD first (cheap) — catches hard 404s without downloading body
        try:
            status, _final, _ = _request(url, "HEAD", read_body=False)
            result["method"] = "HEAD"
            result["status"] = status
            # 405/403/other → fall through to GET (some sites block HEAD)
            if 200 <= status < 400:
                # Soft-404 verification: GET first 8KB and scan for "Document Not Found" etc.
                try:
                    gstatus, _gfinal, body = _request(url, "GET", read_body=True)
                    if 200 <= gstatus < 400 and _is_soft_404(body):
                        result["ok"] = False
                        result["status"] = gstatus
                        result["reason"] = f"{gstatus} soft-404 (body says 'not found')"
                        return result
                except (urllib.error.URLError, socket.timeout):
                    # Can't verify body — trust the HEAD 200
                    pass
                result["ok"] = True
                result["reason"] = f"{status} OK"
                return result
            if status in (401, 403, 429) and tolerates_403(url):
                result["ok"] = True
                result["reason"] = f"{status} (tolerated)"
                return result
            # Not OK on HEAD: try GET (maybe HEAD was blocked)
            if status in (405, 400, 401, 403):
                gstatus, _gfinal, body = _request(url, "GET", read_body=True)
                result["method"] = "GET"
                result["status"] = gstatus
                if 200 <= gstatus < 400:
                    if _is_soft_404(body):
                        result["ok"] = False
                        result["reason"] = f"{gstatus} soft-404 (body says 'not found')"
                        return result
                    result["ok"] = True
                    result["reason"] = f"{gstatus} OK (via GET)"
                    return result
                if gstatus in (403, 429) and tolerates_403(url):
                    result["ok"] = True
                    result["reason"] = f"{gstatus} (tolerated)"
                    return result
                result["reason"] = f"{gstatus}"
                return result
            # Hard error status (e.g. 404, 410, 500)
            result["reason"] = f"{status}"
            return result
        except (urllib.error.URLError, socket.timeout) as e:
            last_err = str(e)
            continue
    result["ok"] = False
    result["status"] = 0
    result["reason"] = f"network error: {last_err}"
    return result


def extract_links(html: str) -> list[str]:
    """Return every unique http(s) URL found in href/src attributes."""
    seen = set()
    out = []
    for m in HREF_RE.finditer(html):
        url = m.group("url").strip()
        # Strip trailing backslash-escape artifacts (common in inline JSON)
        url = url.rstrip(",;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def validate_links(urls: list[str]) -> list[dict]:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_url, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    # Preserve original order
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["url"], 1e9))
    return results


# Anchors with these class tokens are "container" chips/buttons — removing
# the <a> would leave orphan icons/SVGs behind. We remove the whole anchor
# (including its inner markup) instead of unwrapping.
CONTAINER_ANCHOR_CLASSES = (
    "source-chip",
    "cta-btn",
    "share-btn",
    "back-link",
)


def strip_broken_anchors(html: str, broken_urls: set[str]) -> tuple[str, int]:
    """For each broken URL, either:
    - Remove the entire <a>…</a> (if it's a container chip/button), OR
    - Unwrap the <a>…</a> preserving inner text (normal inline citation).

    Also cleans up orphan <li>...</li> items in sources-list that end up empty
    after their single broken anchor is unwrapped.

    Returns (new_html, num_anchors_affected).
    """
    removed = 0
    if not broken_urls:
        return html, 0

    for url in sorted(broken_urls, key=len, reverse=True):
        anchor_re = re.compile(
            r'<a\b([^>]*?)\bhref\s*=\s*["\']' + re.escape(url) + r'["\']([^>]*)>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )

        def _replace(m):
            nonlocal removed
            attrs_before = m.group(1) or ""
            attrs_after = m.group(2) or ""
            attrs = (attrs_before + " " + attrs_after).lower()
            inner = m.group(3)
            removed += 1
            # If it's a container anchor (chip/button), strip it entirely
            for cls in CONTAINER_ANCHOR_CLASSES:
                if f'class="{cls}' in attrs or f"class='{cls}" in attrs or f'class="' in attrs and cls in attrs:
                    # Stricter check: look for exact class token match
                    cls_match = re.search(r'class\s*=\s*["\']([^"\']*)["\']', attrs)
                    if cls_match and cls in cls_match.group(1).split():
                        return ""
            # Otherwise: unwrap, keep inner text
            return inner

        html = anchor_re.sub(_replace, html)

    # Clean up empty sources-list <li>…</li> items (they become empty when their
    # sole anchor was unwrapped and contained "Title" text that's now gone or
    # left as just "<strong>Pub</strong> — " dangling)
    html = re.sub(
        r'<li[^>]*>\s*<strong>[^<]*</strong>\s*[—-]\s*</li>\s*',
        '',
        html,
        flags=re.IGNORECASE,
    )

    return html, removed


def clean_source_strip(html: str) -> tuple[str, int]:
    """Re-build <div class="source-strip-inner">…</div> to contain ONLY
    the label span + proper <a class="source-chip">…</a> anchors. Removes
    orphan icon-spans that were left behind by earlier anchor unwrapping.

    Returns (new_html, num_inner_divs_cleaned).
    """
    cleaned_count = [0]

    def process_inner(match):
        inner = match.group(1)
        # Keep label and full source-chip anchors only
        keepers = re.findall(
            r'<span class="source-strip-label"[^>]*>.*?</span>'
            r'|<a\b[^>]*class="[^"]*\bsource-chip\b[^"]*"[^>]*>.*?</a>',
            inner,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Detect if we had orphans (anything other than keepers inside)
        without_keepers = inner
        for k in keepers:
            without_keepers = without_keepers.replace(k, "", 1)
        if re.search(r'<span\s+class="source-chip-icon', without_keepers, re.IGNORECASE):
            cleaned_count[0] += 1
        return '\n    ' + '\n    '.join(keepers) + '\n  '

    new_html = re.sub(
        r'<div class="source-strip-inner"[^>]*>(.*?)</div>',
        lambda m: f'<div class="source-strip-inner">{process_inner(m)}</div>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return new_html, cleaned_count[0]


# ── Main ──────────────────────────────────────────────────────────────
def iter_html_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() == ".html":
            yield path
        return
    if path.is_dir():
        for p in sorted(path.glob("**/*.html")):
            yield p


def process_file(filepath: Path, fix: bool) -> dict:
    text = filepath.read_text(encoding="utf-8", errors="replace")
    urls = extract_links(text)
    results = validate_links(urls)
    broken = [r for r in results if not r["ok"]]
    orphans_cleaned = 0
    if fix:
        new_text = text
        if broken:
            broken_urls = {r["url"] for r in broken}
            new_text, _ = strip_broken_anchors(new_text, broken_urls)
        # Always run source-strip cleanup (catches orphans from previous runs)
        new_text, orphans_cleaned = clean_source_strip(new_text)
        if new_text != text:
            filepath.write_text(new_text, encoding="utf-8")
    return {
        "file": str(filepath),
        "total": len(urls),
        "ok": len(urls) - len(broken),
        "broken": broken,
        "orphans_cleaned": orphans_cleaned,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate external links in HTML articles.")
    parser.add_argument("path", type=Path, help="HTML file or directory")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-strip <a> wrapper on broken links (inner text preserved)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON report only (no human-readable output)")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"ERROR: path not found: {args.path}", file=sys.stderr)
        sys.exit(2)

    reports = [process_file(f, args.fix) for f in iter_html_files(args.path)]

    if args.json:
        print(json.dumps(reports, indent=2))
        any_broken = any(r["broken"] for r in reports)
        sys.exit(1 if any_broken else 0)

    # Human-readable
    total_files = len(reports)
    total_links = sum(r["total"] for r in reports)
    total_broken = sum(len(r["broken"]) for r in reports)

    print(f"Validated {total_links} external link(s) across {total_files} file(s)")
    print(f"  OK:     {total_links - total_broken}")
    print(f"  Broken: {total_broken}")
    print()
    if total_broken:
        for r in reports:
            if not r["broken"]:
                continue
            name = Path(r["file"]).name
            print(f"  [{name}]")
            for b in r["broken"]:
                print(f"    ✗ {b['status'] or '---'}  {b['url']}")
                if b["reason"] and b["reason"] != str(b["status"]):
                    print(f"        {b['reason']}")
            if args.fix:
                # Count how many were actually stripped (re-read file)
                # For brevity we trust process_file already did the work
                print(f"    → fixed (anchors unwrapped, text preserved)")
            print()
    sys.exit(1 if total_broken else 0)


if __name__ == "__main__":
    main()
