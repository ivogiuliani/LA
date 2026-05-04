#!/usr/bin/env python3
"""
My Villa — Homepage "From the Journal" auto-updater

Scans `blog/*.html` (excluding `blog/index.html` and any category index page),
takes the 3 most-recent articles by `datePublished`, and rewrites the card
grid inside `index.html` between the markers:

    <!-- JOURNAL:AUTO:START -->
    ...
    <!-- JOURNAL:AUTO:END -->

Safe to re-run. Idempotent. Zero-arg invocation is the expected shape — called
after every approve/promote cycle.

Usage:
  python3 update_homepage_journal.py
  python3 update_homepage_journal.py --count 3
  python3 update_homepage_journal.py --dry-run
"""
from __future__ import annotations

import argparse
import html as html_module
import re
import sys
from datetime import datetime
from pathlib import Path

# Reuse the parser from the journal index rebuilder so both surfaces
# read the same metadata the same way.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from update_journal_index import extract_metadata  # noqa: E402

SYSTEM_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SYSTEM_DIR.parent
BLOG_DIR = PROJECT_ROOT / "blog"
HOMEPAGE = PROJECT_ROOT / "index.html"

MARK_START = "<!-- JOURNAL:AUTO:START -->"
MARK_END = "<!-- JOURNAL:AUTO:END -->"

# Articles to display on the homepage
DEFAULT_COUNT = 3

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


def collect_articles(count: int) -> list[dict]:
    """Return the N most-recent published articles (oldest first filtered out)."""
    candidates = []
    for f in BLOG_DIR.glob("*.html"):
        if f.stem in CATEGORY_STEMS:
            continue
        try:
            meta = extract_metadata(f)
        except Exception as e:
            print(f"  [WARN] could not parse {f.name}: {e}", file=sys.stderr)
            continue
        if not meta.get("date"):
            # Fall back to file mtime if the article omits datePublished
            meta["date"] = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        candidates.append(meta)

    candidates.sort(key=lambda m: m["date"], reverse=True)
    return candidates[:count]


def render_card(meta: dict) -> str:
    """Render a single .journal-card anchor.

    Uses a RELATIVE href (``blog/foo.html`` — no leading slash) so the
    homepage links work whether opened via ``file://``, a subdirectory
    dev server, or the production root at ``myvilla.la``.
    """
    href = f"blog/{meta['filename']}"
    tag_label = meta.get("tag_label") or meta.get("section", "Journal").title()
    date_str = format_date(meta.get("date", ""))
    read_time = (meta.get("read_time") or "5 min").strip()
    if read_time and not read_time.lower().endswith("read"):
        read_time = f"{read_time} read"
    meta_line_parts = [p for p in [date_str, read_time] if p]
    meta_line = " · ".join(meta_line_parts)

    excerpt = meta.get("excerpt", "").strip()
    if len(excerpt) > 220:
        # Clip at last sentence-end <= 220 chars, else hard cut + ellipsis
        clip = excerpt[:220]
        last_stop = max(clip.rfind(". "), clip.rfind("! "), clip.rfind("? "))
        if last_stop > 120:
            excerpt = clip[: last_stop + 1]
        else:
            excerpt = clip.rstrip() + "…"

    return (
        f'      <a class="journal-card" href="{esc(href)}" aria-label="Read: {esc(meta["title"])}">\n'
        f'        <div class="journal-card-body">\n'
        f'          <span class="journal-card-tag">{esc(tag_label)}</span>\n'
        f'          <h3 class="journal-card-title">{esc(meta["title"])}</h3>\n'
        f'          <p class="journal-card-excerpt">{esc(excerpt)}</p>\n'
        f'          <div class="journal-card-meta">{esc(meta_line)}</div>\n'
        f'        </div>\n'
        f'      </a>'
    )


def render_block(articles: list[dict]) -> str:
    """Build the full replacement block (markers included)."""
    if not articles:
        inner = "      <!-- no published articles yet -->"
    else:
        inner = "\n".join(render_card(a) for a in articles)
    return f"{MARK_START}\n{inner}\n      {MARK_END}"


BLOCK_RE = re.compile(
    re.escape(MARK_START) + r".*?" + re.escape(MARK_END),
    re.DOTALL,
)


def update_homepage(new_block: str, dry_run: bool = False) -> bool:
    if not HOMEPAGE.exists():
        print(f"ERROR: homepage not found: {HOMEPAGE}", file=sys.stderr)
        return False
    src = HOMEPAGE.read_text(encoding="utf-8")
    if MARK_START not in src or MARK_END not in src:
        print(
            f"ERROR: homepage is missing the JOURNAL:AUTO markers. "
            f"Add them inside the .journal-grid container.",
            file=sys.stderr,
        )
        return False
    new_src = BLOCK_RE.sub(lambda _m: new_block, src, count=1)
    if new_src == src:
        print("  Homepage already up-to-date")
        return True
    if dry_run:
        print("  [dry-run] would rewrite JOURNAL:AUTO block")
        return True
    HOMEPAGE.write_text(new_src, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite the 'From the Journal' grid on the homepage."
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help=f"Number of articles to feature (default {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview the block without writing index.html",
    )
    args = parser.parse_args()

    articles = collect_articles(args.count)
    if not articles:
        print("No published articles found in blog/ — homepage not touched.")
        return 0

    print(f"Selected {len(articles)} article(s) for homepage:")
    for a in articles:
        print(f"  - {a['date']} | {a['filename']} | {a['title'][:80]}")

    block = render_block(articles)
    if args.dry_run:
        print("\n--- block preview ---")
        print(block)
        print("--- end preview ---")

    ok = update_homepage(block, dry_run=args.dry_run)
    if not ok:
        return 1
    if not args.dry_run:
        print(f"  Homepage updated: {HOMEPAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
