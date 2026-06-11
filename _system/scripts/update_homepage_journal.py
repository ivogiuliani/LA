#!/usr/bin/env python3
"""
My Villa — Homepage "The Monitoring Desk" auto-updater (4 topical desks)

Scans `blog/*.html` (excluding category index pages), maps each article to one
of four topics (Insurance / Fire Code / Rebuild / Market), and rewrites the
four topical cards inside `index.html` between the markers:

    <!-- DESK:INSURANCE:START --> ... <!-- DESK:INSURANCE:END -->
    <!-- DESK:FIRE_CODE:START --> ... <!-- DESK:FIRE_CODE:END -->
    <!-- DESK:REBUILD:START -->   ... <!-- DESK:REBUILD:END -->
    <!-- DESK:MARKET:START -->    ... <!-- DESK:MARKET:END -->

For each topic the most-recent matching article (by datePublished, falling
back to file mtime) becomes the featured card.

The four topical eyebrows (`<div class="topical-col-eyebrow">…</div>`) are
NOT inside the markers — they are static, so the topic labels never change.

Safe to re-run. Idempotent. Zero-arg invocation is the expected shape — called
after every approve/promote cycle.

Usage:
  python3 update_homepage_journal.py
  python3 update_homepage_journal.py --dry-run
"""
from __future__ import annotations

import argparse
import html as html_module
import re
import sys
from datetime import datetime
from pathlib import Path

# Reuse the parser + hero locator from the journal index rebuilder so the
# homepage and the /blog/ index read the same metadata the same way.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from update_journal_index import extract_metadata, find_hero_image  # noqa: E402

SYSTEM_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SYSTEM_DIR.parent
BLOG_DIR = PROJECT_ROOT / "blog"
HOMEPAGE = PROJECT_ROOT / "index.html"

# Topic definitions. Each topic has:
#   sections:      `section` IDs (from extract_metadata) that match this topic
#   slug_keywords: optional substrings — any article whose slug contains one
#                  of these is also a candidate (used for "rebuild", which is
#                  a thematic cut, not a section)
#   marker:        the HTML comment pair to replace
TOPICS = [
    {
        "key": "INSURANCE",
        "sections": {"insurance"},
        "slug_keywords": (),
        "marker_start": "<!-- DESK:INSURANCE:START -->",
        "marker_end":   "<!-- DESK:INSURANCE:END -->",
    },
    {
        "key": "FIRE_CODE",
        "sections": {"permits", "climate"},
        "slug_keywords": (),
        "marker_start": "<!-- DESK:FIRE_CODE:START -->",
        "marker_end":   "<!-- DESK:FIRE_CODE:END -->",
    },
    {
        "key": "REBUILD",
        # `materials` / `concrete_arch` are the closest existing sections;
        # any article with "rebuild" in the slug also qualifies (gives this
        # topic a thematic, not just structural, signal).
        "sections": {"materials", "concrete_arch"},
        "slug_keywords": ("rebuild",),
        "marker_start": "<!-- DESK:REBUILD:START -->",
        "marker_end":   "<!-- DESK:REBUILD:END -->",
    },
    {
        "key": "MARKET",
        "sections": {"market"},
        "slug_keywords": (),
        "marker_start": "<!-- DESK:MARKET:START -->",
        "marker_end":   "<!-- DESK:MARKET:END -->",
    },
]

# Category pages we should skip even if they land in blog/ later
CATEGORY_STEMS = {
    "index",
    "insurance",
    "market",
    "materials",
    "permits",
    "climate",
    "concrete_arch",
    "concrete-architecture",
}


def esc(text: str) -> str:
    return html_module.escape(str(text)) if text else ""


def format_date(iso: str) -> str:
    """YYYY-MM-DD -> 'Apr 19, 2026'. Falls back to the input on parse error."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return iso


def collect_articles() -> list[dict]:
    """Return all published articles with metadata (date guaranteed)."""
    out = []
    for f in BLOG_DIR.glob("*.html"):
        if f.stem in CATEGORY_STEMS:
            continue
        try:
            meta = extract_metadata(f)
        except Exception as e:
            print(f"  [WARN] could not parse {f.name}: {e}", file=sys.stderr)
            continue
        if not meta.get("date"):
            meta["date"] = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        out.append(meta)
    return out


def pick_best_for_topic(topic: dict, articles: list[dict]) -> dict | None:
    """Return the most-recent article matching the topic, or None."""
    matches = []
    for a in articles:
        if a.get("section") in topic["sections"]:
            matches.append(a)
            continue
        slug = a.get("slug", "")
        if any(kw in slug for kw in topic["slug_keywords"]):
            matches.append(a)
    if not matches:
        return None
    matches.sort(key=lambda m: m["date"], reverse=True)
    return matches[0]


def hero_path_for_homepage(slug: str) -> str | None:
    """find_hero_image returns a path relative to blog/. Prepend 'blog/' so
    it works from the homepage at the project root."""
    rel = find_hero_image(slug)
    if not rel:
        return None
    return f"blog/{rel}"


def clip_excerpt(text: str, limit: int = 280) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    clip = text[:limit]
    last_stop = max(clip.rfind(". "), clip.rfind("! "), clip.rfind("? "))
    if last_stop > 140:
        return clip[: last_stop + 1]
    return clip.rstrip() + "…"


def render_topical_card(article: dict | None) -> str:
    """Build the inner HTML for one DESK marker pair (the <a> element)."""
    if article is None:
        return "        <!-- no published article matched this topic yet -->"

    href = f"blog/{article['filename']}"
    title = article.get("title", "")
    excerpt = clip_excerpt(article.get("excerpt", ""), 280)
    read_time = (article.get("read_time") or "5 min").strip()
    if read_time and not read_time.lower().endswith("read"):
        read_time = f"{read_time} read"
    meta_parts = [p for p in [format_date(article.get("date", "")), read_time] if p]
    meta_line = " · ".join(meta_parts)

    hero = hero_path_for_homepage(article.get("slug", ""))
    img_block = (
        f'          <img class="journal-card-hero" src="{esc(hero)}" '
        f'alt="" loading="lazy" width="800" height="500">\n'
        if hero else ""
    )

    return (
        f'        <a class="journal-card" href="{esc(href)}" aria-label="Read: {esc(title)}">\n'
        f'{img_block}'
        f'          <div class="journal-card-body">\n'
        f'            <h3 class="journal-card-title">{esc(title)}</h3>\n'
        f'            <p class="journal-card-excerpt">{esc(excerpt)}</p>\n'
        f'            <div class="journal-card-meta">{esc(meta_line)}</div>\n'
        f'          </div>\n'
        f'        </a>'
    )


def replace_marker_block(src: str, marker_start: str, marker_end: str, inner: str) -> str:
    """Replace whatever sits between marker_start..marker_end with `inner`,
    keeping the markers themselves in place. Newline-padded so the result
    stays readable."""
    pattern = re.compile(
        re.escape(marker_start) + r".*?" + re.escape(marker_end),
        re.DOTALL,
    )
    replacement = f"{marker_start}\n{inner}\n{marker_end}"
    return pattern.sub(lambda _m: replacement, src, count=1)


def update_homepage(dry_run: bool = False) -> int:
    if not HOMEPAGE.exists():
        print(f"ERROR: homepage not found: {HOMEPAGE}", file=sys.stderr)
        return 1

    src = HOMEPAGE.read_text(encoding="utf-8")
    missing = [t["key"] for t in TOPICS if t["marker_start"] not in src or t["marker_end"] not in src]
    if missing:
        print(
            f"ERROR: homepage is missing DESK markers for: {', '.join(missing)}. "
            f"Add them inside the .topical-col blocks.",
            file=sys.stderr,
        )
        return 1

    articles = collect_articles()
    if not articles:
        print("No published articles found in blog/ — homepage not touched.")
        return 0

    new_src = src
    selected = []
    for topic in TOPICS:
        best = pick_best_for_topic(topic, articles)
        selected.append((topic["key"], best))
        inner = render_topical_card(best)
        new_src = replace_marker_block(
            new_src, topic["marker_start"], topic["marker_end"], inner
        )

    print("Selected articles for The Monitoring Desk:")
    for key, art in selected:
        if art:
            print(f"  {key:9s} | {art['date']} | {art['filename']}")
        else:
            print(f"  {key:9s} | (no match)")

    if new_src == src:
        print("  Homepage already up-to-date")
        return 0

    if dry_run:
        print("  [dry-run] would rewrite the four DESK blocks")
        return 0

    HOMEPAGE.write_text(new_src, encoding="utf-8")
    print(f"  Homepage updated: {HOMEPAGE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite the four topical cards on the homepage's Monitoring Desk."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview which articles would land in each topic without writing index.html",
    )
    args = parser.parse_args()
    return update_homepage(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
