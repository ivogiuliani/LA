#!/usr/bin/env python3
"""
My Villa — Sitemap, feed & news-sitemap generator

Scans blog/*.html articles (+ JSON sidecars), extracts metadata and rebuilds:

  sitemap.xml        every indexable URL with a REAL <lastmod>
                       - articles: sidecar `dateModified` > sidecar `_date`
                         > HTML datePublished > file mtime
                       - static pages: file mtime
  feed.xml           Atom feed, latest 30 articles (title, summary, link,
                     updated, hero image as enclosure)
  news-sitemap.xml   Google News sitemap, only articles published in the
                     last 48 hours (publication "My Villa Journal", en)

IndexNow: only the DELTA (new or changed URLs vs. _system/history/indexnow_last.json)
is submitted; state is saved on success. Disable with MYVILLA_INDEXNOW=0.

Usage:
  python3 update_sitemap.py
  python3 update_sitemap.py --output sitemap.xml --blog-dir blog/
  python3 update_sitemap.py --dry-run          # print, write nothing, no IndexNow

Exit code is always 0 unless the blog directory is missing (pipeline safety).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
BLOG_DIR = ROOT_DIR / "blog"
HISTORY_DIR = SYSTEM_DIR / "history"
INDEXNOW_STATE = HISTORY_DIR / "indexnow_last.json"

BASE_URL = "https://myvilla.la"
PUBLICATION_NAME = "My Villa Journal"
FEED_MAX = 30
NEWS_WINDOW_HOURS = 48

# ── Static entries (always included) ─────────────────────────────────
# `file`: path relative to ROOT_DIR, used for the real lastmod (mtime).
# The homepage keeps date.today() (its Journal block changes daily).
STATIC_ENTRIES = [
    {"loc": f"{BASE_URL}/", "file": "index.html", "lastmod": None, "changefreq": "weekly", "priority": "1.0"},
    {"loc": f"{BASE_URL}/team.html", "file": "team.html", "lastmod": None, "changefreq": "monthly", "priority": "0.8"},
    {"loc": f"{BASE_URL}/malibu-custom-home-builder.html", "file": "malibu-custom-home-builder.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    {"loc": f"{BASE_URL}/italian-villa-california-builder.html", "file": "italian-villa-california-builder.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    {"loc": f"{BASE_URL}/icf-concrete-home-builder-los-angeles.html", "file": "icf-concrete-home-builder-los-angeles.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    {"loc": f"{BASE_URL}/beverly-hills-custom-home.html", "file": "beverly-hills-custom-home.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    {"loc": f"{BASE_URL}/pacific-palisades-rebuild.html", "file": "pacific-palisades-rebuild.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    # Fase 2 (2026-09-16): landing unica del briefing (thank-you resta fuori: noindex)
    {"loc": f"{BASE_URL}/private-briefing.html", "file": "private-briefing.html", "lastmod": None, "changefreq": "monthly", "priority": "0.9"},
    # Fase 2 (2026-09-16): answer page + linkable data asset
    {"loc": f"{BASE_URL}/insurable-home-california.html", "file": "insurable-home-california.html", "lastmod": None, "changefreq": "weekly", "priority": "0.9"},
    {"loc": f"{BASE_URL}/research/westside-rebuild-tracker.html", "file": "research/westside-rebuild-tracker.html", "lastmod": None, "changefreq": "weekly", "priority": "0.8"},
    {"loc": f"{BASE_URL}/privacy.html", "file": "privacy.html", "lastmod": None, "changefreq": "yearly", "priority": "0.3"},
]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def file_mtime_date(path: Path) -> str:
    """Real last-modified date of a tracked file.

    Order: uncommitted changes → today; last git commit date of the file
    (reliable on the Mac AND on a GitHub runner, where a fresh checkout sets
    every mtime to "now"); untracked / no git → filesystem mtime."""
    try:
        import subprocess
        rel = str(path.resolve().relative_to(ROOT_DIR))
        dirty = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=str(ROOT_DIR),
                               capture_output=True, text=True, timeout=10).stdout.strip()
        if dirty:
            return date.today().isoformat()
        committed = subprocess.run(["git", "log", "-1", "--format=%cs", "--", rel], cwd=str(ROOT_DIR),
                                   capture_output=True, text=True, timeout=10).stdout.strip()
        if DATE_RE.match(committed):
            return committed[:10]
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()
    except OSError:
        return date.today().isoformat()


def _clean_date(value: Optional[str]) -> Optional[str]:
    if value and DATE_RE.match(value.strip()):
        return value.strip()[:10]
    return None


def resolve_static_entries() -> list[dict]:
    """Fill lastmod for static pages from the real file mtime. Missing files are
    skipped (the answer page / tracker may not exist yet on a fresh checkout)."""
    out = []
    for e in STATIC_ENTRIES:
        entry = dict(e)
        path = ROOT_DIR / entry.pop("file")
        if not path.exists():
            print(f"  WARN: static page missing, skipped: {path.name}")
            continue
        if entry["loc"] == f"{BASE_URL}/":
            entry["lastmod"] = date.today().isoformat()
        else:
            entry["lastmod"] = entry["lastmod"] or file_mtime_date(path)
        out.append(entry)
    return out


def _meta(content: str, name: str) -> str:
    m = re.search(r'<meta\s+(?:name|property)="%s"\s+content="([^"]*)"' % re.escape(name), content)
    return m.group(1) if m else ""


def extract_article_metadata(filepath: Path) -> dict | None:
    """Extract canonical URL, dates, title, summary and hero from a blog article
    (HTML + optional JSON sidecar). Returns None if not indexable/complete."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"  WARN: could not read {filepath.name}: {exc}")
        return None

    # POTATURA SEO (2026-08-26): le pagine in noindex restano sul sito ma
    # NON vanno in sitemap — il loro canonical punta al canonico del
    # cluster, che è già in sitemap col proprio file (evita duplicati).
    if re.search(r'name="robots"\s+content="noindex', content):
        return None

    date_match = re.search(r'"datePublished":\s*"(\d{4}-\d{2}-\d{2})"', content)
    if not date_match:
        print(f"  WARN: no datePublished in {filepath.name}, skipping")
        return None
    date_published = date_match.group(1)

    canon_match = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', content)
    loc = canon_match.group(1) if canon_match else f"{BASE_URL}/blog/{filepath.name}"

    # Sidecar JSON = source of truth for dates / summary when present
    sidecar: dict = {}
    jp = filepath.with_suffix(".json")
    if jp.exists():
        try:
            sidecar = json.loads(jp.read_text(encoding="utf-8")) or {}
        except Exception:
            sidecar = {}

    lastmod = (
        _clean_date(sidecar.get("dateModified"))
        or _clean_date(sidecar.get("_date"))
        or date_published
        or file_mtime_date(filepath)
    )
    # Never let lastmod precede the publication date.
    if lastmod < date_published:
        lastmod = date_published

    title = sidecar.get("title") or ""
    if not title:
        m = re.search(r"<title>(.*?)</title>", content, re.S)
        title = re.sub(r"\s*[—|-]\s*My Villa Journal\s*$", "", m.group(1).strip()) if m else filepath.stem
    summary = sidecar.get("meta_description") or sidecar.get("excerpt") or _meta(content, "description")
    hero = _meta(content, "og:image")
    if not hero:
        hm = re.search(r'"image":\s*"(https?://[^"]+)"', content)
        hero = hm.group(1) if hm else ""

    return {
        "loc": loc,
        "lastmod": lastmod,
        "changefreq": "monthly",
        "priority": "0.7",
        "published": date_published,
        "title": title,
        "summary": summary,
        "hero": hero,
    }


# ── Renderers ────────────────────────────────────────────────────────
def build_sitemap_xml(entries: list[dict]) -> str:
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
    lines.append("")
    return "\n".join(lines)


def _mime_for(url: str) -> str:
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
            "gif": "image/gif", "avif": "image/avif"}.get(ext, "image/jpeg")


def build_feed_xml(articles: list[dict]) -> str:
    """Atom 1.0 feed with the latest FEED_MAX articles."""
    items = articles[:FEED_MAX]
    feed_updated = (items[0]["lastmod"] if items else date.today().isoformat()) + "T00:00:00Z"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{xml_escape(PUBLICATION_NAME)}</title>",
        "  <subtitle>Notes on insurable, fire-resilient reinforced concrete homes in Los Angeles: insurance, materials, market, permits.</subtitle>",
        f'  <link href="{BASE_URL}/blog/" rel="alternate" type="text/html"/>',
        f'  <link href="{BASE_URL}/feed.xml" rel="self" type="application/atom+xml"/>',
        f"  <id>{BASE_URL}/blog/</id>",
        f"  <updated>{feed_updated}</updated>",
        f"  <author><name>My Villa</name><uri>{BASE_URL}</uri></author>",
        f'  <icon>{BASE_URL}/img/logos/apple-touch-icon.png</icon>',
        "  <rights>© My Villa</rights>",
    ]
    for a in items:
        lines.append("  <entry>")
        lines.append(f"    <title>{xml_escape(a['title'])}</title>")
        lines.append(f'    <link href="{xml_escape(a["loc"])}" rel="alternate" type="text/html"/>')
        lines.append(f"    <id>{xml_escape(a['loc'])}</id>")
        lines.append(f"    <published>{a['published']}T00:00:00Z</published>")
        lines.append(f"    <updated>{a['lastmod']}T00:00:00Z</updated>")
        if a.get("summary"):
            lines.append(f"    <summary>{xml_escape(a['summary'])}</summary>")
        if a.get("hero"):
            lines.append(f'    <link rel="enclosure" type="{_mime_for(a["hero"])}" href="{xml_escape(a["hero"])}"/>')
        lines.append("  </entry>")
    lines.append("</feed>")
    lines.append("")
    return "\n".join(lines)


def build_news_sitemap_xml(articles: list[dict], now: Optional[datetime] = None) -> tuple[str, int]:
    """Google News sitemap: only articles published within NEWS_WINDOW_HOURS."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=NEWS_WINDOW_HOURS)).date()
    recent = [a for a in articles if a["published"] >= cutoff.isoformat()]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">',
    ]
    for a in recent:
        lines.append("  <url>")
        lines.append(f"    <loc>{xml_escape(a['loc'])}</loc>")
        lines.append("    <news:news>")
        lines.append("      <news:publication>")
        lines.append(f"        <news:name>{xml_escape(PUBLICATION_NAME)}</news:name>")
        lines.append("        <news:language>en</news:language>")
        lines.append("      </news:publication>")
        lines.append(f"      <news:publication_date>{a['published']}</news:publication_date>")
        lines.append(f"      <news:title>{xml_escape(a['title'])}</news:title>")
        lines.append("    </news:news>")
        lines.append("  </url>")
    lines.append("</urlset>")
    lines.append("")
    return "\n".join(lines), len(recent)


# ── IndexNow (delta only) ────────────────────────────────────────────
# IndexNow key — the matching key file `<KEY>.txt` lives at the site root and
# must contain exactly this string (that is how IndexNow verifies ownership).
INDEXNOW_KEY = "7f1fb51f07e43e0f66ff0ff5a261826d"


def _load_indexnow_state() -> dict:
    try:
        return json.loads(INDEXNOW_STATE.read_text(encoding="utf-8")).get("urls", {})
    except Exception:
        return {}


def _save_indexnow_state(urls: dict) -> None:
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        INDEXNOW_STATE.write_text(json.dumps({
            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "urls": urls,
        }, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"  IndexNow: state not saved ({exc})")


def submit_indexnow(entries: list[dict]) -> None:
    """POST only new/changed URLs (loc → lastmod delta vs. the last successful
    submission) to IndexNow. Fully graceful: never raises, never blocks the
    publish pipeline. Disable with env MYVILLA_INDEXNOW=0."""
    if os.environ.get("MYVILLA_INDEXNOW", "1") == "0":
        print("  IndexNow: skipped (MYVILLA_INDEXNOW=0)")
        return
    current = {e["loc"]: e["lastmod"] for e in entries}
    previous = _load_indexnow_state()
    delta = [loc for loc, lm in current.items() if previous.get(loc) != lm]
    if not delta:
        print("  IndexNow: no new or changed URLs — nothing submitted")
        return
    import urllib.request
    host = BASE_URL.replace("https://", "").replace("http://", "").rstrip("/")
    payload = {
        "host": host,
        "key": INDEXNOW_KEY,
        "keyLocation": f"{BASE_URL}/{INDEXNOW_KEY}.txt",
        "urlList": delta[:10000],
    }
    try:
        req = urllib.request.Request(
            "https://api.indexnow.org/indexnow",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  IndexNow: submitted {len(payload['urlList'])} changed URL(s) (HTTP {resp.status})")
            if resp.status in (200, 202):
                merged = dict(previous)
                merged.update({loc: current[loc] for loc in delta})
                # drop URLs no longer in the sitemap
                merged = {k: v for k, v in merged.items() if k in current}
                _save_indexnow_state(merged)
    except Exception as e:  # network/DNS/HTTP — never fatal
        print(f"  IndexNow: skipped ({type(e).__name__}: {e})")


# ── Main ─────────────────────────────────────────────────────────────
def collect(blog_dir: Path) -> tuple[list[dict], list[dict], list[dict]]:
    html_files = sorted(blog_dir.glob("*.html"))
    articles: list[dict] = []
    print(f"Scanning {blog_dir} for articles...")
    for filepath in html_files:
        if filepath.name == "index.html":
            continue
        meta = extract_article_metadata(filepath)
        if meta:
            articles.append(meta)
    articles.sort(key=lambda a: (a["published"], a["lastmod"]), reverse=True)
    for a in articles:
        print(f"  + {a['loc']}  (pub {a['published']}, mod {a['lastmod']})")

    blog_index_lastmod = max((a["lastmod"] for a in articles), default=date.today().isoformat())
    blog_index_entry = {
        "loc": f"{BASE_URL}/blog/index.html",
        "lastmod": blog_index_lastmod,
        "changefreq": "weekly",
        "priority": "0.8",
    }
    category_entries: list[dict] = []
    category_dir = blog_dir / "category"
    if category_dir.is_dir():
        for cat_path in sorted(category_dir.glob("*.html")):
            category_entries.append({
                "loc": f"{BASE_URL}/blog/category/{cat_path.name}",
                "lastmod": file_mtime_date(cat_path),
                "changefreq": "weekly",
                "priority": "0.7",
            })
    statics = resolve_static_entries()
    all_entries: list[dict] = []
    all_entries.extend(statics)
    all_entries.append(blog_index_entry)
    all_entries.extend(category_entries)
    all_entries.extend({k: a[k] for k in ("loc", "lastmod", "changefreq", "priority")} for a in articles)
    return all_entries, articles, category_entries


def main():
    parser = argparse.ArgumentParser(description="Generate sitemap.xml, feed.xml and news-sitemap.xml for myvilla.la")
    parser.add_argument("--output", type=Path, default=ROOT_DIR / "sitemap.xml", help="Path to write sitemap.xml")
    parser.add_argument("--feed-output", type=Path, default=ROOT_DIR / "feed.xml", help="Path to write feed.xml")
    parser.add_argument("--news-output", type=Path, default=ROOT_DIR / "news-sitemap.xml", help="Path to write news-sitemap.xml")
    parser.add_argument("--blog-dir", type=Path, default=BLOG_DIR, help="Path to the blog directory")
    parser.add_argument("--dry-run", action="store_true", help="print summary, write nothing, no IndexNow")
    args = parser.parse_args()

    blog_dir: Path = args.blog_dir.resolve()
    if not blog_dir.is_dir():
        print(f"ERROR: blog directory not found: {blog_dir}")
        raise SystemExit(1)

    all_entries, articles, category_entries = collect(blog_dir)
    sitemap_xml = build_sitemap_xml(all_entries)
    feed_xml = build_feed_xml(articles)
    news_xml, news_count = build_news_sitemap_xml(articles)

    if args.dry_run:
        print("\nDry-run: nothing written.")
    else:
        args.output.resolve().write_text(sitemap_xml, encoding="utf-8")
        args.feed_output.resolve().write_text(feed_xml, encoding="utf-8")
        args.news_output.resolve().write_text(news_xml, encoding="utf-8")
        print(f"\nSitemap written to {args.output.resolve()}")
        print(f"Feed written to    {args.feed_output.resolve()}  ({min(len(articles), FEED_MAX)} entries)")
        print(f"News sitemap to    {args.news_output.resolve()}  ({news_count} article(s) in last {NEWS_WINDOW_HOURS}h)")

    print(f"  Static entries:  {len(all_entries) - 1 - len(category_entries) - len(articles)}")
    print(f"  Blog index:      1")
    print(f"  Category pages:  {len(category_entries)}")
    print(f"  Blog articles:   {len(articles)}")
    print(f"  Total URLs:      {len(all_entries)}")

    if not args.dry_run:
        # IndexNow: notify Bing/Yandex/Seznam of the delta (Google does not
        # support IndexNow — submit sitemap.xml + news-sitemap.xml in Search Console).
        submit_indexnow(all_entries)


if __name__ == "__main__":
    main()
