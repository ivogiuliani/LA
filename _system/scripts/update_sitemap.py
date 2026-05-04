#!/usr/bin/env python3
"""
My Villa — Sitemap Generator
Scans blog/*.html articles, extracts metadata, and rebuilds sitemap.xml

Usage:
  python3 update_sitemap.py
  python3 update_sitemap.py --output sitemap.xml --blog-dir blog/
"""

from __future__ import annotations

import re
import argparse
from datetime import date
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
BLOG_DIR = ROOT_DIR / "blog"

BASE_URL = "https://myvilla.la"

# ── Static entries (always included) ─────────────────────────────────
STATIC_ENTRIES = [
    {
        "loc": f"{BASE_URL}/",
        "lastmod": date.today().isoformat(),
        "changefreq": "weekly",
        "priority": "1.0",
    },
    {
        "loc": f"{BASE_URL}/team.html",
        "lastmod": "2026-04-19",
        "changefreq": "monthly",
        "priority": "0.8",
    },
    {
        "loc": f"{BASE_URL}/malibu-custom-home-builder.html",
        "lastmod": "2026-04-19",
        "changefreq": "monthly",
        "priority": "0.9",
    },
    {
        "loc": f"{BASE_URL}/privacy.html",
        "lastmod": "2026-03-03",
        "changefreq": "yearly",
        "priority": "0.3",
    },
]


def extract_article_metadata(filepath: Path) -> dict | None:
    """Extract canonical URL and datePublished from a blog article HTML file.

    Returns a dict with loc, lastmod, changefreq, priority — or None if
    the required metadata cannot be found.
    """
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  WARN: could not read {filepath.name}: {exc}")
        return None

    # datePublished from JSON-LD
    date_match = re.search(r'"datePublished":\s*"(\d{4}-\d{2}-\d{2})"', content)
    if not date_match:
        print(f"  WARN: no datePublished in {filepath.name}, skipping")
        return None
    date_published = date_match.group(1)

    # Canonical URL from <link rel="canonical">
    canon_match = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', content)
    if canon_match:
        loc = canon_match.group(1)
    else:
        # Fallback: construct from filename
        loc = f"{BASE_URL}/blog/{filepath.name}"

    return {
        "loc": loc,
        "lastmod": date_published,
        "changefreq": "monthly",
        "priority": "0.7",
    }


def build_sitemap_xml(entries: list[dict]) -> str:
    """Render a list of URL entries into a complete sitemap.xml string."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{xml_escape(entry['loc'])}</loc>")
        lines.append(f"    <lastmod>{xml_escape(entry['lastmod'])}</lastmod>")
        lines.append(f"    <changefreq>{xml_escape(entry['changefreq'])}</changefreq>")
        lines.append(f"    <priority>{xml_escape(entry['priority'])}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate/update sitemap.xml for myvilla.la",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "sitemap.xml",
        help="Path to write sitemap.xml (default: project root)",
    )
    parser.add_argument(
        "--blog-dir",
        type=Path,
        default=BLOG_DIR,
        help="Path to the blog directory (default: ROOT_DIR/blog)",
    )
    args = parser.parse_args()

    blog_dir: Path = args.blog_dir.resolve()
    output_path: Path = args.output.resolve()

    if not blog_dir.is_dir():
        print(f"ERROR: blog directory not found: {blog_dir}")
        raise SystemExit(1)

    # ── Collect blog articles ────────────────────────────────────────
    html_files = sorted(blog_dir.glob("*.html"))
    articles: list[dict] = []

    print(f"Scanning {blog_dir} for articles...")
    for filepath in html_files:
        if filepath.name == "index.html":
            continue
        meta = extract_article_metadata(filepath)
        if meta:
            articles.append(meta)
            print(f"  + {meta['loc']}  ({meta['lastmod']})")

    # Sort articles by date descending
    articles.sort(key=lambda a: a["lastmod"], reverse=True)

    # ── Blog index entry ─────────────────────────────────────────────
    # Use the most recent article date as lastmod, or today
    blog_index_lastmod = articles[0]["lastmod"] if articles else date.today().isoformat()
    blog_index_entry = {
        "loc": f"{BASE_URL}/blog/index.html",
        "lastmod": blog_index_lastmod,
        "changefreq": "weekly",
        "priority": "0.8",
    }

    # ── Category hub pages (blog/category/*.html) ───────────────────
    # Generated by update_journal_index.py; we surface them in sitemap
    # for SEO (category hubs = topical authority signal).
    category_dir = blog_dir / "category"
    category_entries: list[dict] = []
    if category_dir.is_dir():
        for cat_path in sorted(category_dir.glob("*.html")):
            category_entries.append({
                "loc": f"{BASE_URL}/blog/category/{cat_path.name}",
                "lastmod": blog_index_lastmod,
                "changefreq": "weekly",
                "priority": "0.7",
            })

    # ── Assemble all entries ─────────────────────────────────────────
    all_entries: list[dict] = []
    all_entries.extend(STATIC_ENTRIES)
    all_entries.append(blog_index_entry)
    all_entries.extend(category_entries)
    all_entries.extend(articles)

    # ── Write sitemap ────────────────────────────────────────────────
    xml_content = build_sitemap_xml(all_entries)
    output_path.write_text(xml_content, encoding="utf-8")

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\nSitemap written to {output_path}")
    print(f"  Static entries:  {len(STATIC_ENTRIES)}")
    print(f"  Blog index:      1")
    print(f"  Category pages:  {len(category_entries)}")
    print(f"  Blog articles:   {len(articles)}")
    print(f"  Total URLs:      {len(all_entries)}")


if __name__ == "__main__":
    main()
