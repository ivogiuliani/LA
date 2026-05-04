#!/usr/bin/env python3
"""
Unsplash hero image picker for MyVilla journal articles.

Usage as library:
    from image_picker import fetch_hero_image
    result = fetch_hero_image(
        query="Hancock Park mansion architecture",
        slug="hancock-park-century-old-mansions",
        out_dir=Path("blog/assets/img"),
    )

Usage as CLI (POC / debug):
    python3 image_picker.py "Hancock Park mansion architecture" my-slug

Returns a dict with:
    local_path         — Path to downloaded image under out_dir
    web_path           — Relative web path ("assets/img/my-slug-hero.jpg")
    author_name        — Photographer display name
    author_url         — Unsplash profile URL (with UTM params per ToS)
    unsplash_url       — Photo's canonical Unsplash URL (with UTM params)
    alt_description    — Alt text from Unsplash metadata
    color              — Dominant color hex
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
import urllib.request
import urllib.error

# Unsplash API configuration
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
UTM_SUFFIX = "?utm_source=myvilla&utm_medium=referral"


def _load_access_key() -> str:
    """Load Unsplash access key from environment or .env file."""
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if key:
        return key
    # Fallback: parse .env directly so the script works without dotenv
    repo = Path(__file__).resolve().parent.parent.parent
    env_path = repo / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("UNSPLASH_ACCESS_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(url: str, headers: Optional[dict] = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _trigger_download(download_location: str, access_key: str) -> None:
    """Per Unsplash ToS: notify download endpoint when serving a photo.
    https://help.unsplash.com/en/articles/2511258-guideline-triggering-a-download
    """
    try:
        req = urllib.request.Request(
            download_location,
            headers={"Authorization": f"Client-ID {access_key}"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass  # Fire-and-forget; not blocking


def _normalize_image_url(url: str) -> tuple[str, str]:
    """Return (full_url, thumb_url) for a raw image URL.

    For WordPress-style URLs (wp-content/uploads), the full version strips
    resize query params and the thumb uses ?w=400 for fast grid loading.
    For unknown CDNs, full and thumb are identical.
    """
    if "?" in url:
        bare = url.split("?", 1)[0]
    else:
        bare = url
    is_wp = "/wp-content/uploads/" in bare
    if is_wp:
        return bare, f"{bare}?w=400"
    return url, url


def _extract_body_images(html: str, base_url: str, max_per_page: int = 8) -> list[dict]:
    """Extract in-body <img> URLs from an article page, filtered to likely
    content photos (same-domain CDN, real size, excluding ads/nav/sidebar).

    Returns list of {"url", "alt", "width"} sorted by estimated relevance.
    """
    import re as _re
    from html import unescape as _unescape
    from urllib.parse import urljoin, urlparse

    # Locate article body — try structural markers used by most CMS templates.
    article_start = -1
    for marker in (
        r'<article\b',
        r'class="[^"]*(?:entry-content|post-content|article-body|article__body|'
        r'story-content|story-body|c-entry-content|single-content|post__body)[^"]*"',
    ):
        m = _re.search(marker, html, _re.IGNORECASE)
        if m:
            article_start = m.start()
            break

    if article_start < 0:
        body = html
    else:
        # Cut off at </article>, </main>, footer, related-posts, or comments
        slice_ = html[article_start:]
        end_rel = len(slice_)
        for end_marker in (
            r'</article>',
            r'class="[^"]*(?:related-posts|related-articles|more-from|'
            r'comments-area|site-footer|footer-)[^"]*"',
            r'<footer\b',
        ):
            em = _re.search(end_marker, slice_, _re.IGNORECASE)
            if em:
                end_rel = min(end_rel, em.start())
        body = slice_[:end_rel]

    # Block patterns — obvious non-content / UI / ads / tracking
    skip_re = _re.compile(
        r'(?:'
        r'logo|avatar|gravatar|icon[-_/]|sprite|spacer|pixel|tracking|'
        r'/ads?[-/]|_ad[._]|adsby|google_|taboola|outbrain|doubleclick|'
        r'sidebar|nav[-_]|navigation|social[-_]|share[-_]|emoji|favicon|'
        r'newsletter|subscribe|badge|button|author[-_]photo'
        r')',
        _re.IGNORECASE,
    )

    same_domain = urlparse(base_url).netloc
    seen: set[str] = set()
    results: list[dict] = []

    for m in _re.finditer(r'<img[^>]+>', body, _re.IGNORECASE):
        tag = m.group(0)

        # Prefer lazy-load attributes over plain src (WP / Gatsby / Next)
        url = ""
        for attr in ('data-lazy-src', 'data-src', 'data-original', 'data-hi-res-src', 'src'):
            am = _re.search(rf'{attr}=["\']([^"\']+)["\']', tag, _re.IGNORECASE)
            if am:
                u = _unescape(am.group(1)).strip()
                if u and not u.startswith('data:'):
                    url = u
                    break

        if not url:
            # Try srcset — pick the largest descriptor
            sm = _re.search(r'srcset=["\']([^"\']+)["\']', tag, _re.IGNORECASE)
            if sm:
                best = ("", 0)
                for entry in sm.group(1).split(','):
                    parts = entry.strip().split()
                    if not parts:
                        continue
                    u = parts[0]
                    w = 0
                    if len(parts) > 1 and parts[1].endswith('w'):
                        try:
                            w = int(parts[1][:-1])
                        except ValueError:
                            pass
                    if w >= best[1]:
                        best = (u, w)
                url = _unescape(best[0]).strip()

        if not url:
            continue

        # Resolve relative URL
        if url.startswith('//'):
            url = 'https:' + url
        elif url.startswith('/'):
            url = urljoin(base_url, url)

        parsed = urlparse(url)
        if not parsed.netloc:
            continue
        # Same domain only — avoids ad networks and trackers
        if parsed.netloc != same_domain and not parsed.netloc.endswith('.' + same_domain):
            continue

        # Must be a real image extension
        if not _re.search(r'\.(?:jpe?g|png|webp)(?:[?#]|$)', url, _re.IGNORECASE):
            continue

        # Skip patterns
        if skip_re.search(url):
            continue

        key = url.split('?')[0].split('#')[0]
        if key in seen:
            continue
        seen.add(key)

        alt_m = _re.search(r'\balt=["\']([^"\']*)["\']', tag, _re.IGNORECASE)
        alt = _unescape(alt_m.group(1)).strip() if alt_m else ''

        width = 0
        wm = _re.search(r'\bwidth=["\']?(\d+)', tag)
        if wm:
            try:
                width = int(wm.group(1))
            except ValueError:
                pass

        # Skip obvious tiny icons (< 200px if declared)
        if 0 < width < 200:
            continue

        results.append({"url": url, "alt": alt, "width": width})
        if len(results) >= max_per_page:
            break

    return results


def fetch_source_images(sources: list[dict], max_sources: int = 6, per_source: int = 8) -> list[dict]:
    """For each cited source URL, extract both the og:image AND in-body article
    images. Returns a list of candidates compatible with fetch_candidates()
    format but with origin='source'.

    We intentionally HOTLINK these images (we do not download them) so the
    publisher continues to host them from their own CDN — this keeps the
    feature well inside the bounds of fair-use editorial commentary and
    avoids any rehosting of copyrighted press photos.
    """
    import re as _re
    from html import unescape as _unescape
    from urllib.parse import urlparse as _up

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    OG_IMAGE_RE = _re.compile(
        r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        _re.IGNORECASE,
    )
    OG_IMAGE_RE_ALT = _re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
        _re.IGNORECASE,
    )
    TW_IMAGE_RE = _re.compile(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        _re.IGNORECASE,
    )
    OG_TITLE_RE = _re.compile(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        _re.IGNORECASE,
    )
    OG_SITE_RE = _re.compile(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
        _re.IGNORECASE,
    )

    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for src in sources[:max_sources]:
        url = (src.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        if "x.com" in url or "twitter.com" in url or "reddit.com" in url:
            continue

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                # Read up to 1.2 MB — gallery-heavy articles push the <img>
                # payload far past 800 KB once inline CSS and ads are parsed.
                html = resp.read(1_200_000).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [image/source] fetch failed for {url[:60]}: {e}")
            continue

        # Extract site metadata once per page
        tm = OG_TITLE_RE.search(html)
        sm = OG_SITE_RE.search(html)
        page_title = _unescape(tm.group(1)).strip() if tm else (src.get("title") or "")
        site_name = (
            _unescape(sm.group(1)).strip() if sm else (src.get("name") or "")
        )

        page_candidates: list[tuple[str, str, int]] = []  # (url, alt, rank_hint)

        # 1) og:image first (usually the hero)
        m = OG_IMAGE_RE.search(html) or OG_IMAGE_RE_ALT.search(html)
        og_url = _unescape(m.group(1)).strip() if m else ""
        if not og_url:
            tw = TW_IMAGE_RE.search(html)
            if tw:
                og_url = _unescape(tw.group(1)).strip()
        if og_url:
            if og_url.startswith("//"):
                og_url = "https:" + og_url
            elif og_url.startswith("/"):
                p = _up(url)
                og_url = f"{p.scheme}://{p.netloc}{og_url}"
            page_candidates.append((og_url, page_title[:120], 0))

        # 2) In-body images from the article content
        body_imgs = _extract_body_images(html, url, max_per_page=per_source)
        for bi in body_imgs:
            page_candidates.append((bi["url"], bi["alt"] or page_title[:120], 1))

        added = 0
        for img_url, alt, _rank in page_candidates:
            full_url, thumb_url = _normalize_image_url(img_url)
            key = full_url.split('#')[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)

            candidates.append({
                "origin": "source",
                "photo_id": f"src-{len(candidates)}",
                "thumb_url": thumb_url,
                "full_url": full_url,
                "author_name": site_name or "Publication",
                "author_profile_url": url,
                "unsplash_url": url,
                "download_location": "",
                "alt_description": alt,
                "color": "#3e2f2b",
                "width": 0,
                "height": 0,
                "query_used": "cited source",
                "source_page_url": url,
                "source_publication": site_name or src.get("name") or "",
                "source_title": page_title,
            })
            added += 1

        print(
            f"  [image/source] {site_name or url[:40]}: "
            f"og={'yes' if og_url else 'no'} body={len(body_imgs)} → {added} candidates"
        )

    return candidates


def fetch_candidates(
    query: str,
    count: int = 8,
    orientation: str = "landscape",
    fallback_queries: Optional[list[str]] = None,
) -> list[dict]:
    """Return a list of lightweight candidate metadata dicts (no downloads).

    Each candidate:
        photo_id, thumb_url, full_url, author_name, author_profile_url,
        unsplash_url, alt_description, color, width, height, download_location
    """
    access_key = _load_access_key()
    if not access_key:
        print("  [image] No UNSPLASH_ACCESS_KEY — skipping")
        return []

    headers = {
        "Authorization": f"Client-ID {access_key}",
        "Accept-Version": "v1",
    }

    queries = [query] + list(fallback_queries or [])
    photos: list[dict] = []
    used_query = ""
    for q in queries:
        if not q:
            continue
        params = urlencode({
            "query": q,
            "orientation": orientation,
            "content_filter": "high",
            "per_page": max(1, min(count, 30)),
            "order_by": "relevant",
        })
        url = f"{UNSPLASH_SEARCH_URL}?{params}"
        try:
            data = _http_get_json(url, headers)
        except urllib.error.HTTPError as e:
            print(f"  [image] Unsplash HTTP {e.code} for query: {q}")
            continue
        except Exception as e:
            print(f"  [image] Unsplash error for query '{q}': {e}")
            continue
        photos = data.get("results") or []
        if photos:
            used_query = q
            break

    if not photos:
        return []

    candidates: list[dict] = []
    for p in photos[:count]:
        urls = p.get("urls", {}) or {}
        links = p.get("links", {}) or {}
        user = p.get("user", {}) or {}
        user_links = user.get("links", {}) or {}
        candidates.append({
            "origin": "unsplash",
            "photo_id": p.get("id", ""),
            "thumb_url": urls.get("small") or urls.get("thumb") or "",
            "full_url": urls.get("regular") or urls.get("full") or "",
            "author_name": user.get("name") or user.get("username") or "Unknown",
            "author_profile_url": user_links.get("html") or "",
            "unsplash_url": links.get("html") or "",
            "download_location": links.get("download_location") or "",
            "alt_description": p.get("alt_description") or p.get("description") or "",
            "color": p.get("color") or "#3e2f2b",
            "width": p.get("width") or 0,
            "height": p.get("height") or 0,
            "query_used": used_query,
        })
    return candidates


def download_candidate(
    candidate: dict,
    slug: str,
    out_dir: Path,
) -> Optional[dict]:
    """Download a specific candidate (chosen from fetch_candidates results)."""
    access_key = _load_access_key()
    if not access_key:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_url = candidate.get("full_url", "")
    if not image_url:
        return None

    # Per ToS: trigger download endpoint before serving
    dl = candidate.get("download_location", "")
    if dl:
        _trigger_download(dl, access_key)

    try:
        img_bytes = _http_get_bytes(image_url)
    except Exception as e:
        print(f"  [image] Download failed: {e}")
        return None

    local_path = out_dir / f"{slug}-hero.jpg"
    local_path.write_bytes(img_bytes)
    size_kb = len(img_bytes) / 1024
    print(f"  [image] Saved: {local_path.name} ({size_kb:.1f} KB)")

    author_profile = candidate.get("author_profile_url", "")
    photo_page = candidate.get("unsplash_url", "")

    return {
        "local_path": str(local_path),
        "web_path": f"assets/img/{slug}-hero.jpg",
        "photo_id": candidate.get("photo_id", ""),
        "author_name": candidate.get("author_name", "Unknown"),
        "author_url": f"{author_profile}{UTM_SUFFIX}" if author_profile else "",
        "unsplash_url": f"{photo_page}{UTM_SUFFIX}" if photo_page else "",
        "alt_description": candidate.get("alt_description", ""),
        "color": candidate.get("color", "#3e2f2b"),
        "source": "unsplash",
    }


def download_source_image(
    candidate: dict,
    slug: str,
    out_dir: Path,
) -> Optional[dict]:
    """Download an image from a cited source (Robb Report, LA Times, etc.)
    and save it locally — same pattern as download_candidate but without
    Unsplash ToS requirements.

    Returns a metadata dict on success, None on failure.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_url = candidate.get("full_url", "")
    if not image_url:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "image/*,*/*",
        "Referer": candidate.get("source_page_url", ""),
    }

    try:
        img_bytes = _http_get_bytes(image_url, headers=headers)
    except Exception as e:
        print(f"  [image/source] Download failed: {e}")
        return None

    # Detect extension from URL or default to .jpg
    ext = ".jpg"
    lower = image_url.lower().split("?")[0]
    if lower.endswith(".png"):
        ext = ".png"
    elif lower.endswith(".webp"):
        ext = ".webp"

    local_path = out_dir / f"{slug}-hero{ext}"
    local_path.write_bytes(img_bytes)
    size_kb = len(img_bytes) / 1024
    print(f"  [image/source] Saved: {local_path.name} ({size_kb:.1f} KB)")

    publication = (
        candidate.get("source_publication")
        or candidate.get("author_name")
        or "Publication"
    )
    page_url = candidate.get("source_page_url", "")

    return {
        "local_path": str(local_path),
        "web_path": f"assets/img/{slug}-hero{ext}",
        "photo_id": candidate.get("photo_id", ""),
        "author_name": publication,
        "author_url": page_url,
        "unsplash_url": page_url,
        "alt_description": candidate.get("alt_description", ""),
        "color": candidate.get("color", "#3e2f2b"),
        "source": "cited_source",
        "origin": "source",
    }


def fetch_hero_image(
    query: str,
    slug: str,
    out_dir: Path,
    orientation: str = "landscape",
    fallback_queries: Optional[list[str]] = None,
) -> Optional[dict]:
    """Search Unsplash for the best matching photo and download it.

    Returns a metadata dict on success, None on failure.
    """
    access_key = _load_access_key()
    if not access_key:
        print("  [image] No UNSPLASH_ACCESS_KEY — skipping")
        return None

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "Authorization": f"Client-ID {access_key}",
        "Accept-Version": "v1",
    }

    tried: list[str] = []
    queries = [query] + list(fallback_queries or [])
    result = None
    for q in queries:
        if not q:
            continue
        tried.append(q)
        params = urlencode({
            "query": q,
            "orientation": orientation,
            "content_filter": "high",
            "per_page": 10,
            "order_by": "relevant",
        })
        url = f"{UNSPLASH_SEARCH_URL}?{params}"
        try:
            data = _http_get_json(url, headers)
        except urllib.error.HTTPError as e:
            print(f"  [image] Unsplash HTTP {e.code} for query: {q}")
            continue
        except Exception as e:
            print(f"  [image] Unsplash error for query '{q}': {e}")
            continue
        photos = data.get("results") or []
        if photos:
            result = photos[0]
            print(f"  [image] Matched '{q}' → {len(photos)} results, picking top")
            break
        print(f"  [image] No results for query: {q}")

    if not result:
        print(f"  [image] No match found after {len(tried)} query attempts")
        return None

    # Extract metadata
    photo_id = result.get("id", "")
    urls = result.get("urls", {}) or {}
    links = result.get("links", {}) or {}
    user = result.get("user", {}) or {}
    user_links = user.get("links", {}) or {}

    download_location = (links.get("download_location") or "").strip()
    image_url = (urls.get("regular") or urls.get("full") or "").strip()
    if not image_url:
        print("  [image] No image URL in response")
        return None

    # Per ToS: ping download_location before serving the image
    if download_location:
        _trigger_download(download_location, access_key)

    # Download the image
    try:
        img_bytes = _http_get_bytes(image_url)
    except Exception as e:
        print(f"  [image] Download failed: {e}")
        return None

    local_path = out_dir / f"{slug}-hero.jpg"
    local_path.write_bytes(img_bytes)
    size_kb = len(img_bytes) / 1024
    print(f"  [image] Saved: {local_path.name} ({size_kb:.1f} KB)")

    author_name = user.get("name") or user.get("username") or "Unknown"
    author_profile = (user_links.get("html") or "").strip()
    photo_page = (links.get("html") or "").strip()

    return {
        "local_path": str(local_path),
        "web_path": f"assets/img/{slug}-hero.jpg",
        "photo_id": photo_id,
        "author_name": author_name,
        "author_url": f"{author_profile}{UTM_SUFFIX}" if author_profile else "",
        "unsplash_url": f"{photo_page}{UTM_SUFFIX}" if photo_page else "",
        "alt_description": result.get("alt_description") or result.get("description") or "",
        "color": result.get("color") or "#3e2f2b",
    }


def _main_cli() -> None:
    if len(sys.argv) < 3:
        print("Usage: image_picker.py <query> <slug> [out_dir]")
        sys.exit(1)
    query = sys.argv[1]
    slug = sys.argv[2]
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("blog/assets/img")
    result = fetch_hero_image(query, slug, out_dir)
    if result:
        print(json.dumps(result, indent=2))
    else:
        print("No result", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _main_cli()
