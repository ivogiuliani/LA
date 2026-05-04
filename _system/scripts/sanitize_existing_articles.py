#!/usr/bin/env python3
"""
One-shot: clean up source links in already-published journal articles.

Rules (mirror sanitize_sources() in generate_journal.py):
- An anchor is "broken" if its URL is a bare root domain (e.g.
  https://www.latimes.com) or has no meaningful path segment.
- If we can recover the article's PRIMARY URL (from the _drafts/journal/*.json
  sidecar or from the radar signal JSON), redirect all broken anchors to it.
- Otherwise, unwrap the broken anchor into plain text so the reader is not
  sent to an irrelevant homepage.
- Also removes the "Our Perspective" decorative CTA links if they point at
  root domains.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent.parent
BLOG = REPO / "blog"
DRAFTS = REPO / "_drafts" / "journal"
RADAR_DIR = REPO / "_system" / "radar" / "reports"


def is_deep_link(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if "vertexaisearch.cloud.google.com" in url:
        return False  # grounding redirects are effectively dead
    try:
        p = urlparse(url)
    except Exception:
        return False
    if not p.netloc:
        return False
    path = (p.path or "").strip("/")
    if not path:
        return False
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    if all(len(s) < 3 for s in segs):
        return False
    return True


VERTEX_REDIRECT = "vertexaisearch.cloud.google.com"


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().lstrip(".")
    except Exception:
        return ""


def load_primary_urls() -> dict[str, str]:
    """Build slug → primary URL map from draft sidecars and radar signals.

    Priority:
      1) Sidecar _source_item.url if it is already a valid deep link and
         NOT a Vertex AI grounding redirect.
      2) Otherwise, find a radar signal whose host matches the sidecar
         publication (e.g. publication="nationaltoday.com" →
         radar URL on nationaltoday.com).
    """
    # Collect radar signals by host
    radar_by_host: dict[str, str] = {}
    for rj in RADAR_DIR.glob("radar_*.json"):
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sig in data.get("qualified", []):
            url = (sig.get("url") or "").strip()
            if not is_deep_link(url) or VERTEX_REDIRECT in url:
                continue
            host = _host(url)
            if host and host not in radar_by_host:
                radar_by_host[host] = url
            # also stash the bare second-level for fuzzy matching
            parts = host.split(".")
            if len(parts) >= 2:
                base = ".".join(parts[-2:])
                radar_by_host.setdefault(base, url)

    out: dict[str, str] = {}
    for j in DRAFTS.glob("*.json"):
        try:
            data = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        src = data.get("_source_item") or {}
        url = (src.get("url") or "").strip()

        if is_deep_link(url) and VERTEX_REDIRECT not in url:
            out[j.stem] = url
            continue

        # Fallback: match by publication → radar host
        pub = (src.get("publication") or "").strip().lower()
        pub_host = pub if "." in pub else ""
        if pub_host and pub_host in radar_by_host:
            out[j.stem] = radar_by_host[pub_host]
            continue
        # second-level fallback
        if pub_host:
            parts = pub_host.split(".")
            if len(parts) >= 2:
                base = ".".join(parts[-2:])
                if base in radar_by_host:
                    out[j.stem] = radar_by_host[base]

    return out


ANCHOR_RE = re.compile(r'<a\b([^>]*?)href="([^"]*)"([^>]*)>(.*?)</a>', re.DOTALL | re.IGNORECASE)


def sanitize_html(html: str, primary_url: str | None) -> tuple[str, int, int]:
    """Return (new_html, redirected_count, unwrapped_count)."""
    redirected = 0
    unwrapped = 0

    def _fix(m: re.Match) -> str:
        nonlocal redirected, unwrapped
        pre = m.group(1)
        href = m.group(2)
        post = m.group(3)
        inner = m.group(4)
        if is_deep_link(href):
            return m.group(0)
        if not href.startswith(("http://", "https://")):
            return m.group(0)  # leave mailto / anchors / relative paths alone
        if primary_url:
            redirected += 1
            return f'<a{pre}href="{primary_url}"{post}>{inner}</a>'
        unwrapped += 1
        return inner  # strip the anchor, keep visible text

    new_html = ANCHOR_RE.sub(_fix, html)
    return new_html, redirected, unwrapped


def main() -> None:
    primary_map = load_primary_urls()
    print(f"Loaded {len(primary_map)} primary URL(s) from draft sidecars")

    changed = 0
    total = 0
    for html_path in sorted(BLOG.glob("*.html")):
        if html_path.name == "index.html":
            continue
        total += 1
        original = html_path.read_text(encoding="utf-8")
        slug = html_path.stem
        primary = primary_map.get(slug)
        new_html, red, unw = sanitize_html(original, primary)
        if new_html != original:
            html_path.write_text(new_html, encoding="utf-8")
            changed += 1
            marker = f"primary={primary[:60]}…" if primary else "no primary — unwrapped"
            print(f"  [fix] {html_path.name}  redirected={red} unwrapped={unw}  ({marker})")
        else:
            print(f"  [ok]  {html_path.name}")

    print(f"\nScanned {total} article(s); updated {changed}.")


if __name__ == "__main__":
    main()
