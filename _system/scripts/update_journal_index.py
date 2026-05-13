#!/usr/bin/env python3
"""
My Villa — Journal Index Rebuilder
Scans all HTML articles in blog/, extracts metadata, rebuilds blog/index.html

Usage:
  python3 update_journal_index.py
  python3 update_journal_index.py --articles-dir blog/ --output blog/index.html
"""

import re
import argparse
import html as html_module
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
BLOG_DIR = SYSTEM_DIR.parent / "blog"


def esc(text):
    return html_module.escape(str(text)) if text else ""


def extract_metadata(filepath):
    """Extract article metadata from HTML head tags and schema.org JSON-LD."""
    content = filepath.read_text()

    def meta(name):
        m = re.search(rf'<meta\s+(?:name|property)="{name}"\s+content="([^"]*)"', content)
        return m.group(1) if m else ""

    title = ""
    m = re.search(r'<title>(.+?)\s*—\s*My Villa Journal</title>', content)
    if m:
        title = m.group(1)
    if not title:
        title = meta("og:title").replace(" — My Villa Journal", "")
    # The raw HTML already contains escaped entities (e.g. &#x27;); decode
    # here so esc() can re-escape exactly once when rendering the index.
    title = html_module.unescape(title)

    # Extract date from JSON-LD
    date = ""
    m = re.search(r'"datePublished":\s*"(\d{4}-\d{2}-\d{2})"', content)
    if m:
        date = m.group(1)

    # Extract section from hero tag
    section = ""
    tag_label = ""
    m = re.search(r'<div class="article-hero-tag">(.+?)</div>', content)
    if m:
        tag_label = html_module.unescape(m.group(1))

    # Map tag label to section ID
    section_map = {
        "insurance": "insurance",
        "materials": "materials",
        "concrete": "concrete_arch",
        "permits": "permits",
        "policy": "permits",
        "market": "market",
        "luxury": "market",
        "climate": "climate",
        "resilience": "climate",
    }
    tag_lower = tag_label.lower()
    for key, sec_id in section_map.items():
        if key in tag_lower:
            section = sec_id
            break
    if not section:
        section = "materials"

    # Extract excerpt/description (decode so esc() can re-escape once at render)
    excerpt = html_module.unescape(meta("description"))

    # Extract read time from hero meta
    read_time = "5 min"
    m = re.search(r'(\d+)\s*min\s*read', content)
    if m:
        read_time = f"{m.group(1)} min"

    # Source citations
    sources = []
    for m in re.finditer(r'<div class="source-citation-pub">(.+?)</div>', content):
        sources.append(html_module.unescape(m.group(1)))

    # Source strip chips
    if not sources:
        for m in re.finditer(r'class="source-chip"[^>]*>\s*(?:<[^>]+>)*\s*([^<]+)', content):
            name = html_module.unescape(m.group(1).strip())
            if name and name not in ("Sources:",):
                sources.append(name)

    return {
        "slug": filepath.stem,
        "filename": filepath.name,
        "title": title,
        "date": date,
        "section": section,
        "tag_label": tag_label,
        "excerpt": excerpt,
        "read_time": read_time,
        "sources": sources[:3],  # Max 3 for display
    }


# ══════════════════════════════════════════════════════════════════════
# LAYOUT CONFIG
# ══════════════════════════════════════════════════════════════════════

# Scalability rules
PREVIEW_LIMIT = 6               # Articles shown per section in the index (2 rows of 3)
# Generate a dedicated /blog/category/{section}.html page when a section has
# MORE than this many articles. Set to 1 so any section with 2+ articles gets
# its own topical hub (SEO: a landing destination for section queries, and
# sitting in the internal link graph as an authority node). Sections with a
# single article get surfaced only on the index.
CATEGORY_PAGE_THRESHOLD = 1
LATEST_STRIP_COUNT = 6          # Cross-section "Latest" strip after hero

# Pinned section order (strategic priority — these sections always come first,
# in the order listed, regardless of article count). Insurance is the top
# conversion topic for My Villa, so it leads.
PINNED_SECTIONS = ["insurance"]


# Featured-slot commercial-intent ranking.
# The Featured card is the biggest visual element on the Journal index and
# carries the most SEO weight. It should promote buyer-intent / commercial
# articles over pure news coverage when the publication dates tie.
#
# Ordering mirrors the priority SEO targets in
# `_system/knowledge/content_strategy.md` §4 (ranked 1-15 for the coming
# 6-12 months). Tier-1 geographies (Malibu, Beverly Hills) + commercial
# intent come first; generic fire-resistant / concrete / villa typology
# come next; news-framing cues ("hidden cost") come last so they only win
# when no commercial candidate exists.
#
# Match is case-insensitive substring. First match wins. News-only
# articles (e.g. "34 Homes in 15 Months", "Brush Clearance Notices") do
# not match anything here and get rank 999 → they never take the Featured
# slot as long as any commercial article is available.
FEATURED_COMMERCIAL_KEYWORDS = [
    # ── Pillar-target queries (strategy §4 #1, #2, #12, #13) ─────────────
    # These match FUTURE pillar pages (not yet written). As soon as a
    # pillar article exists, it automatically wins the Featured slot over
    # any current cluster article. Keep these at the top of the list.
    "luxury home builder malibu",          # §4 #1 — top strategic target
    "custom home beverly hills",           # §4 #2
    "custom home bel air",                 # §4 #12
    "architect luxury home malibu",        # §4 #13
    "luxury home builder",                 # fallback (any luxury builder piece)
    "custom home builder",                 # fallback

    # ── Highest-SEO current articles: cost + buyer intent ────────────────
    # Why these beat insurance-framed titles: (1) long-tail low-competition
    # queries, (2) concrete numbers in titles drive better CTR, (3) this is
    # bottom-of-funnel — searcher is comparing costs, closer to hiring.
    "fire-resistant home construction cost",   # §4 #14 (matches "3% Premium" piece)
    "concrete home cost",                      # §4 #14 variant
    "how to build fire-resistant luxury home", # §4 #15

    # ── Fire-resistant / fire-resilient construction (§4 #3, #5, #10, #11) ──
    "fire-resistant home construction",
    "fire-resilient home construction",
    "fire-resistant home california",
    "concrete homes in los angeles",       # §4 #5
    "concrete home builder los angeles",   # §4 #5
    "icf home builder california",         # §4 #10
    "fireproof home california",           # §4 #11
    "reinforced concrete home",

    # ── Insurance pain-point (§4 #4, #8, #9) ─────────────────────────────
    # Still commercial, but higher-competition + more top-of-funnel than
    # cost queries. Rank below construction-cost pieces.
    "california fire insurance solution",  # §4 #8
    "fire insurance in california",        # §4 #8 partial
    "insurable home california",           # §4 #4
    "fair plan alternative",               # §4 #9
    "insurable home",

    # ── Italian / Mediterranean villa typology (§4 #6, #7) ───────────────
    "italian villa california",            # §4 #6
    "mediterranean villa california",      # §4 #7
    "italian villa",
    "mediterranean villa",

    # ── Last resort: cost-framed news (still beats pure cronaca) ─────────
    "hidden cost",
]


def featured_commercial_score(article):
    """Return a rank (lower = better) for featured-slot priority.

    Articles whose title matches a commercial keyword get a rank equal to
    the keyword's index. Non-matching (news-style) titles get rank 999 so
    they only win when there is no commercial candidate.
    """
    title_lower = (article.get("title") or "").lower()
    for i, kw in enumerate(FEATURED_COMMERCIAL_KEYWORDS):
        if kw in title_lower:
            return i
    return 999


def pick_featured(articles):
    """Choose the Featured article.

    Preference:
      1. Lowest commercial-intent rank (i.e. best buyer-intent match).
      2. Within the same rank, most recent publication date.
    If no article has a commercial match, falls back to the most-recent one.
    """
    if not articles:
        return None
    ranked = sorted(
        articles,
        key=lambda a: (featured_commercial_score(a), -_date_ordinal(a.get("date", ""))),
    )
    return ranked[0]


def _date_ordinal(date_str):
    """Return an integer ordinal for a YYYY-MM-DD string, 0 if missing."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").toordinal()
    except (ValueError, TypeError):
        return 0


# ══════════════════════════════════════════════════════════════════════
# SECTION CONFIG
# ══════════════════════════════════════════════════════════════════════

SECTIONS = {
    "insurance": {
        "name": "Insurance & Insurability",
        "accent": "#C2714F",
        "accent_class": "accent-insurance",
        "tag_class": "tag-insurance",
        "gradient": "linear-gradient(135deg, #3E2F2B, #C2714F)",
    },
    "materials": {
        "name": "Materials & Construction",
        "accent": "#6B8F9E",
        "accent_class": "accent-construction",
        "tag_class": "tag-construction",
        "gradient": "linear-gradient(135deg, #3E4F5B, #6B8F9E)",
    },
    "concrete_arch": {
        "name": "Concrete Architecture",
        "accent": "#6B8F9E",
        "accent_class": "accent-construction",
        "tag_class": "tag-construction",
        "gradient": "linear-gradient(135deg, #4a4a4a, #6B8F9E)",
    },
    "permits": {
        "name": "Permits & Policy",
        "accent": "#5C6B4F",
        "accent_class": "accent-permits",
        "tag_class": "tag-permits",
        "gradient": "linear-gradient(135deg, #3E4F3B, #5C6B4F)",
    },
    "market": {
        "name": "LA Luxury Market",
        "accent": "#C4A265",
        "accent_class": "accent-market",
        "tag_class": "tag-market",
        "gradient": "linear-gradient(135deg, #5C4A2E, #C4A265)",
    },
    "climate": {
        "name": "Climate & Resilience",
        "accent": "#A8B5A8",
        "accent_class": "accent-climate",
        "tag_class": "tag-climate",
        "gradient": "linear-gradient(135deg, #4F5C4F, #A8B5A8)",
    },
}


def find_hero_image(slug, blog_dir=None):
    """Return the relative path to the hero image for this slug, or None.

    Looks for `<blog>/assets/img/<slug>-hero.{jpg,jpeg,png,webp}` in that
    order. Returns a URL-relative path suitable for <img src="..."> inside
    `blog/index.html`.
    """
    base = blog_dir or BLOG_DIR
    img_dir = base / "assets" / "img"
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidate = img_dir / f"{slug}-hero.{ext}"
        if candidate.exists():
            return f"assets/img/{slug}-hero.{ext}"
    return None


def render_article_card(article, delay_idx=0):
    """Render an article card for the index grid."""
    sec = SECTIONS.get(article["section"], SECTIONS["materials"])
    delay_class = f" reveal-d{delay_idx + 1}" if delay_idx < 4 else ""
    hero = find_hero_image(article.get("slug", ""))
    img_block = (
        f'<img src="{esc(hero)}" alt="{esc(article["title"])}" loading="lazy" '
        f'style="width:100%;height:100%;object-fit:cover;display:block;">'
        if hero else
        f'<div class="card-img-placeholder" style="background:{sec["gradient"]};">'
        f'<span>article image</span></div>'
    )

    source_html = ""
    if article["sources"]:
        sources_str = ", ".join(f"<strong>{esc(s)}</strong>" for s in article["sources"])
        source_html = f"""
      <div class="card-source">
        <div class="card-source-icon src-article">
          <svg viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.2"/><path d="M6 7h4M6 9.5h4M6 12h2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>
        </div>
        <span class="card-source-text">Citing {sources_str}</span>
      </div>"""

    # Format date
    try:
        dt = datetime.strptime(article["date"], "%Y-%m-%d")
        date_display = dt.strftime("%B %d, %Y")
    except (ValueError, KeyError):
        date_display = article.get("date", "")

    article_year = (article.get("date", "") or "")[:4]
    return f"""
    <article class="card reveal{delay_class}" data-year="{article_year}" data-section="{esc(article['section'])}">
      <a href="{esc(article['filename'])}" style="text-decoration:none;color:inherit;">
      <div class="card-accent {sec['accent_class']}"></div>
      <div class="card-img">
        {img_block}
      </div>
      <div class="card-body">
        <span class="card-tag {sec['tag_class']}">{esc(article.get('tag_label', sec['name']))}</span>
        <h3 class="card-title">{esc(article['title'])}</h3>
        <p class="card-excerpt">{esc(article['excerpt'])}</p>
        <div class="card-meta">{date_display} &middot; {article['read_time']} read</div>
        {source_html}
      </div>
      </a>
    </article>"""


def render_featured(article):
    """Render the featured (latest) article."""
    sec = SECTIONS.get(article["section"], SECTIONS["materials"])
    hero = find_hero_image(article.get("slug", ""))
    featured_img_block = (
        f'<img src="{esc(hero)}" alt="{esc(article["title"])}" loading="eager" '
        f'style="width:100%;height:100%;object-fit:cover;display:block;">'
        if hero else
        f'<div class="featured-img-placeholder" style="background:{sec["gradient"]};">'
        f'<span>article image</span></div>'
    )

    try:
        dt = datetime.strptime(article["date"], "%Y-%m-%d")
        date_display = dt.strftime("%B %d, %Y")
    except (ValueError, KeyError):
        date_display = article.get("date", "")

    source_badges = ""
    for s in article.get("sources", [])[:3]:
        source_badges += f"""
          <span class="source-badge sb-article">
            <svg viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.2"/><path d="M6 7h4M6 9.5h4M6 12h2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>
            {esc(s)}
          </span>"""

    return f"""
  <div class="featured-label reveal">Featured</div>
  <a href="{esc(article['filename'])}" style="text-decoration:none;color:inherit;">
  <div class="featured-card reveal reveal-d1">
    <div class="featured-img">
      {featured_img_block}
    </div>
    <div class="featured-body">
      <div class="featured-tag">{esc(article.get('tag_label', sec['name']))}</div>
      <h2 class="featured-title">{esc(article['title'])}</h2>
      <p class="featured-excerpt">{esc(article['excerpt'])}</p>
      <div class="featured-meta">{date_display} &middot; {article['read_time']} read</div>
      <div class="featured-sources">{source_badges}</div>
      <div class="read-link">Read Article <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
    </div>
  </div>
  </a>"""


def render_section_block(section_id, articles, preview_only=True, heading_anchor=""):
    """Render a section with its article grid.

    preview_only=True: shows only PREVIEW_LIMIT cards + "See all" link to
    category page if more exist. Used on the main index.
    preview_only=False: shows all articles (used on category pages).
    """
    sec = SECTIONS.get(section_id, SECTIONS["materials"])
    total = len(articles)
    if preview_only:
        display_arts = articles[:PREVIEW_LIMIT]
    else:
        display_arts = articles
    cards = "\n".join(render_article_card(a, i) for i, a in enumerate(display_arts))

    see_all_html = ""
    if preview_only and total > PREVIEW_LIMIT:
        see_all_html = (
            f'<a href="category/{section_id}.html" class="see-all-link">'
            f'See all {total} articles '
            f'<svg width="12" height="12" viewBox="0 0 16 16" fill="none" style="vertical-align:middle">'
            f'<path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
            f'</svg></a>'
        )

    anchor = f' id="{heading_anchor}"' if heading_anchor else ""
    count_label = f"{total} article{'s' if total != 1 else ''}"

    return f"""
<div class="section-block" data-section="{section_id}"{anchor}>
  <div class="section-header reveal">
    <div>
      <div class="section-accent {sec['accent_class']}"></div>
      <h2 class="section-name">{esc(sec['name'])}</h2>
    </div>
    <div class="section-meta">
      <span class="section-count">{count_label}</span>
      {see_all_html}
    </div>
  </div>
  <div class="article-grid">
    {cards}
  </div>
</div>"""


# ══════════════════════════════════════════════════════════════════════
# LATEST STRIP + ARCHIVE + DYNAMIC SECTION ORDER
# ══════════════════════════════════════════════════════════════════════

def render_latest_card(article):
    """Compact card for the cross-section Latest strip."""
    sec = SECTIONS.get(article["section"], SECTIONS["materials"])
    try:
        dt = datetime.strptime(article["date"], "%Y-%m-%d")
        date_display = dt.strftime("%b %d")
    except (ValueError, KeyError):
        date_display = article.get("date", "")

    article_year = (article.get("date", "") or "")[:4]
    return f"""
    <a href="{esc(article['filename'])}" class="latest-card" data-section="{article['section']}" data-year="{article_year}">
      <div class="latest-card-accent" style="background:{sec['accent']};"></div>
      <div class="latest-card-body">
        <span class="latest-card-tag {sec['tag_class']}">{esc(article.get('tag_label', sec['name']))}</span>
        <div class="latest-card-title">{esc(article['title'])}</div>
        <div class="latest-card-meta">{date_display} &middot; {article['read_time']} read</div>
      </div>
    </a>"""


def render_latest_strip(articles):
    """Render the cross-section Latest strip.

    Caller is responsible for filtering out the Featured article — this
    function renders the top LATEST_STRIP_COUNT items from `articles` as-is.
    """
    if not articles:
        return ""
    latest = articles[:LATEST_STRIP_COUNT]
    if not latest:
        return ""
    cards = "\n".join(render_latest_card(a) for a in latest)
    return f"""
<section class="latest-strip-section reveal">
  <div class="latest-strip-header">
    <div class="latest-strip-label">Latest Updates</div>
    <div class="latest-strip-sub">New perspectives across all categories</div>
  </div>
  <div class="latest-strip">
    {cards}
  </div>
</section>"""


def render_archive_strip(articles):
    """Render the archive strip (years with article counts)."""
    years = {}
    for a in articles:
        date = a.get("date", "")
        if date and len(date) >= 4:
            y = date[:4]
            years[y] = years.get(y, 0) + 1
    if not years:
        return ""
    years_sorted = sorted(years.keys(), reverse=True)
    all_chip = (
        '<button type="button" class="archive-chip archive-chip-all active" '
        'onclick="filterYear(\'all\')">'
        '<strong>All</strong></button>'
    )
    year_chips = " ".join(
        f'<button type="button" class="archive-chip" data-year="{y}" '
        f'onclick="filterYear(\'{y}\')">'
        f'<strong>{y}</strong><span class="archive-chip-count">{years[y]}</span>'
        f'</button>'
        for y in years_sorted
    )
    chips = all_chip + " " + year_chips
    return f"""
<section class="archive-strip-section reveal">
  <div class="archive-strip-inner">
    <span class="archive-label">Archive</span>
    <div class="archive-chips">{chips}</div>
  </div>
</section>"""


def order_sections(by_section):
    """Order sections for display.

    Rules:
      1. Pinned sections first, in PINNED_SECTIONS order.
      2. Then by article count descending.
      3. Then by most-recent article date descending.
    """
    pinned = [s for s in PINNED_SECTIONS if s in by_section]
    others = [s for s in by_section if s not in PINNED_SECTIONS]
    # Stable sort: first by date desc, then by count desc (count wins as it's last)
    others.sort(
        key=lambda s: max((a.get("date", "") for a in by_section[s]), default=""),
        reverse=True,
    )
    others.sort(key=lambda s: len(by_section[s]), reverse=True)
    return pinned + others


def render_index_html(articles):
    """Render the full journal index page."""
    latest_strip_html = ""
    archive_html = ""

    if not articles:
        featured_html = '<p style="text-align:center;color:#A09890;padding:60px;">No articles yet.</p>'
        sections_html = ""
        present_sections = []
    else:
        # Featured = best commercial-intent match (see pick_featured).
        # Falls back to most-recent if no commercial match exists.
        featured = pick_featured(articles)
        featured_html = render_featured(featured)

        # Cross-section Latest strip (skips the Featured card, whichever it is).
        non_featured = [a for a in articles if a is not featured]
        latest_strip_html = render_latest_strip(non_featured)

        # Group ALL articles by section (including featured, so counts reflect reality
        # and the pinned section always appears below with its full content)
        by_section = {}
        for a in articles:
            sec = a.get("section", "materials")
            by_section.setdefault(sec, []).append(a)

        # Dynamic ordering: pinned first, then by article count desc, then latest date
        ordered = order_sections(by_section)
        present_sections = ordered
        sections_html = "\n".join(
            render_section_block(sec_id, by_section[sec_id], preview_only=True)
            for sec_id in ordered
        )

        # Archive strip by year
        archive_html = render_archive_strip(articles)

    # Category pills — only show pills for sections that actually have articles
    cat_pills = '<button class="cat-pill active" onclick="filterCat(\'all\')">All</button>\n'
    for sec_id in present_sections:
        sec = SECTIONS.get(sec_id, SECTIONS["materials"])
        cat_pills += f'  <button class="cat-pill" onclick="filterCat(\'{sec_id}\')">{esc(sec["name"])}</button>\n'

    # Read the index template CSS from the template file
    template_path = SYSTEM_DIR / "templates" / "journal-index.html"
    if template_path.exists():
        template_content = template_path.read_text()
        # Extract the <style> block
        style_match = re.search(r'<style>(.*?)</style>', template_content, re.DOTALL)
        style_block = style_match.group(1) if style_match else ""
    else:
        style_block = ""

    # Additional styles for new scalable components (latest strip, see all link,
    # archive strip, refined section meta). Appended after the template CSS so
    # they win on specificity ties.
    extra_css = """
    /* ── Latest cross-section strip ──────────────────────────── */
    .latest-strip-section { max-width: 1280px; margin: 40px auto 60px; padding: 0 40px; }
    .latest-strip-header { margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid rgba(192, 180, 165, 0.3); display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
    .latest-strip-label { font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--espresso); font-weight: 600; }
    .latest-strip-sub { font-family: var(--sans); font-size: 11px; color: var(--stone-grey); letter-spacing: 0.04em; }
    .latest-strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 18px; }
    .latest-card { display: flex; background: var(--white); border: 1px solid rgba(192, 180, 165, 0.25); border-radius: 2px; text-decoration: none; color: inherit; transition: all 0.2s ease; overflow: hidden; }
    .latest-card:hover { border-color: var(--terracotta); box-shadow: 0 4px 14px rgba(62, 47, 43, 0.08); transform: translateY(-1px); }
    .latest-card-accent { width: 3px; flex-shrink: 0; }
    .latest-card-body { padding: 14px 18px; flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
    .latest-card-tag { display: inline-block; font-family: var(--sans); font-size: 9px; text-transform: uppercase; letter-spacing: 0.12em; padding: 3px 8px; align-self: flex-start; font-weight: 500; }
    .latest-card-title { font-family: var(--serif); font-size: 17px; line-height: 1.3; color: var(--offblack); font-weight: 500; }
    .latest-card-meta { font-family: var(--sans); font-size: 10px; color: var(--stone-grey); letter-spacing: 0.05em; margin-top: 2px; }

    /* ── Section meta: count + see all link ──────────────────── */
    .section-meta { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
    .see-all-link { font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--terracotta); text-decoration: none; font-weight: 600; transition: color 0.2s ease; }
    .see-all-link:hover { color: var(--espresso); }
    .see-all-link svg { margin-left: 2px; transition: transform 0.2s ease; }
    .see-all-link:hover svg { transform: translateX(3px); }

    /* ── Archive strip ───────────────────────────────────────── */
    .archive-strip-section { max-width: 1280px; margin: 60px auto 40px; padding: 0 40px; }
    .archive-strip-inner { padding: 20px 0; border-top: 1px solid rgba(192, 180, 165, 0.3); border-bottom: 1px solid rgba(192, 180, 165, 0.3); display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
    .archive-label { font-family: var(--sans); font-size: 10px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--espresso); font-weight: 600; }
    .archive-chips { display: flex; gap: 10px; flex-wrap: wrap; }
    .archive-chip { font-family: var(--sans); font-size: 12px; color: var(--charcoal); padding: 6px 14px; background: rgba(192, 180, 165, 0.15); border: none; border-radius: 2px; display: inline-flex; align-items: center; gap: 8px; cursor: pointer; transition: background 0.18s ease, color 0.18s ease; }
    .archive-chip:hover { background: rgba(192, 180, 165, 0.35); color: var(--espresso); }
    .archive-chip.active { background: var(--terracotta); color: var(--white); }
    .archive-chip.active .archive-chip-count { color: rgba(255,255,255,0.8); }
    .archive-chip strong { font-weight: 600; letter-spacing: 0.05em; }
    .archive-chip-count { color: var(--stone-grey); font-size: 10px; }

    /* ── Responsive ──────────────────────────────────────────── */
    @media (max-width: 768px) {
      .latest-strip-section, .archive-strip-section { padding: 0 20px; }
      .latest-strip { grid-template-columns: 1fr; }
      .section-meta { flex-direction: column; align-items: flex-start; gap: 6px; }
    }
    """
    style_block = style_block + extra_css

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-D6HJX7BNZN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-D6HJX7BNZN');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Journal — My Villa</title>
<meta name="description" content="Perspectives on building in Los Angeles. Insurance, materials, regulation, and resilience — grounded in data, informed by daily monitoring.">
<meta name="author" content="My Villa">
<link rel="canonical" href="https://myvilla.la/blog/">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>{style_block}</style>
</head>
<body>

<nav class="nav" id="nav" role="navigation" aria-label="Main navigation">
  <a href="https://myvilla.la" class="nav-logo-wrap">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul &middot; Californian Body</span>
  </a>
  <div class="nav-links">
    <a href="https://myvilla.la/#thesis">Vision</a>
    <a href="https://myvilla.la/#collection">Collection</a>
    <a href="https://myvilla.la/#ethical">Approach</a>
    <a href="https://myvilla.la/#resilience">Resilience</a>
    <a href="#" class="active">Journal</a>
    <a href="https://myvilla.la/#contact" class="nav-cta">Request Briefing</a>
  </div>
  <div class="nav-burger" id="burger" aria-label="Menu" onclick="document.getElementById('mobileMenu').classList.toggle('open')">
    <span></span><span></span><span></span>
  </div>
</nav>

<div class="mobile-menu" id="mobileMenu">
  <a href="https://myvilla.la/#thesis" onclick="closeMobile()">Vision</a>
  <a href="https://myvilla.la/#collection" onclick="closeMobile()">Collection</a>
  <a href="https://myvilla.la/#ethical" onclick="closeMobile()">Approach</a>
  <a href="https://myvilla.la/#resilience" onclick="closeMobile()">Resilience</a>
  <a href="#" onclick="closeMobile()">Journal</a>
  <a href="https://myvilla.la/#contact" onclick="closeMobile()">Request Briefing</a>
</div>

<section class="hero">
  <div class="hero-label reveal">Journal</div>
  <h1 class="hero-title reveal reveal-d1">Perspectives on <em>Building</em><br>in Los Angeles</h1>
  <div class="hero-divider reveal reveal-d2"></div>
  <p class="hero-subtitle reveal reveal-d3">We monitor daily what's shaping the future of building in Los Angeles &mdash; insurance, materials, regulation, and resilience. Grounded in data, informed by our research.</p>
  <a href="https://myvilla.la" class="hero-subscribe reveal reveal-d4">Discover My Villa &rarr;</a>
</section>

<div class="cat-nav">
  {cat_pills}
</div>

<section class="featured-section">
  <div class="featured-wrapper">
    {featured_html}
  </div>
</section>

{latest_strip_html}

{sections_html}

{archive_html}

<section class="journal-cta">
  <div class="journal-cta-inner">
    <div class="journal-cta-label">Curated by My Villa</div>
    <div class="journal-cta-divider"></div>
    <h2 class="journal-cta-title">Building <em>Permanence</em> in<br>Los Angeles</h2>
    <p class="journal-cta-text">We design and coordinate luxury reinforced concrete villas for the Los Angeles market. European engineering. Californian lifestyle.</p>
    <div class="journal-cta-buttons">
      <a href="https://myvilla.la" class="cta-btn cta-btn-primary">Explore My Villa</a>
      <a href="https://myvilla.la/#contact" class="cta-btn cta-btn-secondary">Request a Briefing</a>
    </div>
  </div>
</section>

<footer class="footer">
  <div class="footer-brand-wrap">
    <span class="footer-brand">MY VILLA</span>
    <span class="footer-brand-payoff">Italian Soul &middot; Californian Body</span>
  </div>
  <span class="footer-copy">&copy; 2026 My Villa. All rights reserved.</span>
  <div class="footer-links">
    <a href="https://myvilla.la/team.html">Studio</a>
    <a href="https://myvilla.la/privacy.html">Privacy</a>
  </div>
</footer>

<script>
// Scroll nav
window.addEventListener('scroll', function() {{
  document.getElementById('nav').classList.toggle('scrolled', window.scrollY > 40);
}});
// Mobile menu
function closeMobile() {{ document.getElementById('mobileMenu').classList.remove('open'); }}
// Reveal on scroll
const obs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{ if (e.isIntersecting) {{ e.target.classList.add('visible'); obs.unobserve(e.target); }} }});
}}, {{ threshold: 0.1 }});
document.querySelectorAll('.reveal').forEach(el => obs.observe(el));

// Category filter (scroll-to-section + hide others)
function filterCat(cat) {{
  document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
  if (event && event.target) event.target.classList.add('active');

  // Any category click resets the Archive year filter back to "All"
  document.querySelectorAll('.archive-chip').forEach(c => c.classList.remove('active'));
  const allYearChip = document.querySelector('.archive-chip-all');
  if (allYearChip) allYearChip.classList.add('active');
  // Show all cards hidden by a prior year filter
  document.querySelectorAll('.card').forEach(c => {{ c.removeAttribute('data-hidden-by-year'); }});

  const latestStrip = document.querySelector('.latest-strip-section');
  const archiveStrip = document.querySelector('.archive-strip-section');

  if (cat === 'all') {{
    document.querySelectorAll('.section-block').forEach(b => {{ b.style.display = ''; }});
    // Also filter the latest strip cards back to all
    document.querySelectorAll('.latest-card').forEach(c => {{ c.style.display = ''; }});
    document.querySelectorAll('.card').forEach(c => {{ c.style.display = ''; }});
    if (latestStrip) latestStrip.style.display = '';
    if (archiveStrip) archiveStrip.style.display = '';
    return;
  }}

  // Hide non-matching sections
  let firstMatch = null;
  document.querySelectorAll('.section-block').forEach(block => {{
    if (block.getAttribute('data-section') === cat) {{
      block.style.display = '';
      if (!firstMatch) firstMatch = block;
    }} else {{
      block.style.display = 'none';
    }}
  }});

  // Also filter the latest strip to only show matching category cards
  let visibleLatest = 0;
  document.querySelectorAll('.latest-card').forEach(c => {{
    if (c.getAttribute('data-section') === cat) {{
      c.style.display = '';
      visibleLatest++;
    }} else {{
      c.style.display = 'none';
    }}
  }});
  if (latestStrip) latestStrip.style.display = visibleLatest > 0 ? '' : 'none';
  // Hide archive strip when filtered (it spans all categories)
  if (archiveStrip) archiveStrip.style.display = 'none';

  // Scroll to first match
  if (firstMatch) {{
    firstMatch.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }}
}}

// Archive year filter: shows only cards from the chosen year, across every
// section + the latest strip. Resets the category pills to "All".
function filterYear(year) {{
  // Reset category pills to All
  document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
  const allCatPill = document.querySelector('.cat-pill');
  if (allCatPill) allCatPill.classList.add('active');

  // Update archive-chip active state
  document.querySelectorAll('.archive-chip').forEach(c => c.classList.remove('active'));
  if (event && event.target) {{
    const btn = event.target.closest('.archive-chip');
    if (btn) btn.classList.add('active');
  }}

  const latestStrip = document.querySelector('.latest-strip-section');

  if (year === 'all') {{
    document.querySelectorAll('.section-block').forEach(b => {{ b.style.display = ''; }});
    document.querySelectorAll('.card').forEach(c => {{ c.style.display = ''; }});
    document.querySelectorAll('.latest-card').forEach(c => {{ c.style.display = ''; }});
    if (latestStrip) latestStrip.style.display = '';
    return;
  }}

  // Filter cards inside each section-block; hide blocks with no visible cards
  let firstMatch = null;
  document.querySelectorAll('.section-block').forEach(block => {{
    let visible = 0;
    block.querySelectorAll('.card').forEach(card => {{
      if (card.getAttribute('data-year') === year) {{
        card.style.display = '';
        visible++;
      }} else {{
        card.style.display = 'none';
      }}
    }});
    if (visible > 0) {{
      block.style.display = '';
      if (!firstMatch) firstMatch = block;
    }} else {{
      block.style.display = 'none';
    }}
  }});

  // Filter the latest strip too
  let visibleLatest = 0;
  document.querySelectorAll('.latest-card').forEach(c => {{
    if (c.getAttribute('data-year') === year) {{
      c.style.display = '';
      visibleLatest++;
    }} else {{
      c.style.display = 'none';
    }}
  }});
  if (latestStrip) latestStrip.style.display = visibleLatest > 0 ? '' : 'none';

  if (firstMatch) {{
    firstMatch.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }}
}}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
# CATEGORY PAGES (generated when a section exceeds PREVIEW_LIMIT)
# ══════════════════════════════════════════════════════════════════════

def group_by_month(articles):
    """Group articles by YYYY-MM; return ordered list of (label, articles)."""
    groups = {}
    for a in articles:
        date = a.get("date", "")
        ym = date[:7] if date and len(date) >= 7 else "unknown"
        groups.setdefault(ym, []).append(a)
    ordered_keys = sorted(groups.keys(), reverse=True)
    result = []
    for ym in ordered_keys:
        if ym == "unknown":
            label = "Undated"
        else:
            try:
                dt = datetime.strptime(ym, "%Y-%m")
                label = dt.strftime("%B %Y")
            except ValueError:
                label = ym
        result.append((label, groups[ym]))
    return result


def render_category_page(section_id, articles, style_block):
    """Render a dedicated category page with articles grouped by month."""
    sec = SECTIONS.get(section_id, SECTIONS["materials"])
    total = len(articles)

    # Group by month
    month_groups = group_by_month(articles)

    # Build month sections
    months_html = ""
    for month_label, month_articles in month_groups:
        cards = "\n".join(render_article_card(a, i) for i, a in enumerate(month_articles))
        months_html += f"""
<div class="month-group">
  <div class="month-header reveal">
    <h3 class="month-label">{esc(month_label)}</h3>
    <span class="month-count">{len(month_articles)} article{'s' if len(month_articles) != 1 else ''}</span>
  </div>
  <div class="article-grid">
    {cards}
  </div>
</div>"""

    # Category-specific hero gradient
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-D6HJX7BNZN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-D6HJX7BNZN');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{esc(sec['name'])} — My Villa Journal</title>
<meta name="description" content="{esc(sec['name'])} articles from the My Villa Journal. Perspectives on building reinforced concrete villas in Los Angeles.">
<link rel="canonical" href="https://myvilla.la/blog/category/{section_id}.html">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>{style_block}
/* ── Category page extras ─────────────────────────────────── */
.category-hero {{ padding: 100px 40px 60px; text-align: center; max-width: 900px; margin: 0 auto; }}
.category-breadcrumb {{ font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.15em; color: var(--stone-grey); margin-bottom: 20px; }}
.category-breadcrumb a {{ color: var(--stone-grey); text-decoration: none; transition: color 0.2s ease; }}
.category-breadcrumb a:hover {{ color: var(--terracotta); }}
.category-title {{ font-family: var(--serif); font-size: clamp(44px, 6vw, 68px); font-weight: 300; color: var(--offblack); margin-bottom: 16px; letter-spacing: -0.01em; }}
.category-sub {{ font-family: var(--sans); font-size: 14px; color: var(--stone-grey); letter-spacing: 0.04em; }}
.category-accent-bar {{ width: 60px; height: 3px; background: {sec['accent']}; margin: 28px auto; }}
.month-group {{ max-width: 1280px; margin: 0 auto 60px; padding: 0 40px; }}
.month-header {{ display: flex; justify-content: space-between; align-items: baseline; padding-bottom: 14px; border-bottom: 1px solid rgba(192, 180, 165, 0.3); margin-bottom: 28px; }}
.month-label {{ font-family: var(--serif); font-size: 26px; font-weight: 400; color: var(--espresso); letter-spacing: 0.02em; }}
.month-count {{ font-family: var(--sans); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--stone-grey); }}
.back-link {{ display: inline-flex; align-items: center; gap: 6px; font-family: var(--sans); font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--terracotta); text-decoration: none; padding: 40px; }}
.back-link:hover {{ color: var(--espresso); }}
@media (max-width: 768px) {{ .category-hero {{ padding: 60px 20px 40px; }} .month-group {{ padding: 0 20px; }} }}
</style>
</head>
<body>

<nav class="nav" id="nav" role="navigation" aria-label="Main navigation">
  <a href="https://myvilla.la" class="nav-logo-wrap">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul &middot; Californian Body</span>
  </a>
  <div class="nav-links">
    <a href="https://myvilla.la/#thesis">Vision</a>
    <a href="https://myvilla.la/#collection">Collection</a>
    <a href="https://myvilla.la/#ethical">Approach</a>
    <a href="https://myvilla.la/#resilience">Resilience</a>
    <a href="../index.html" class="active">Journal</a>
    <a href="https://myvilla.la/#contact" class="nav-cta">Request Briefing</a>
  </div>
  <div class="nav-burger" id="burger" aria-label="Menu" onclick="document.getElementById('mobileMenu').classList.toggle('open')">
    <span></span><span></span><span></span>
  </div>
</nav>
<div class="mobile-menu" id="mobileMenu">
  <a href="https://myvilla.la/#thesis" onclick="closeMobile()">Vision</a>
  <a href="https://myvilla.la/#collection" onclick="closeMobile()">Collection</a>
  <a href="https://myvilla.la/#ethical" onclick="closeMobile()">Approach</a>
  <a href="https://myvilla.la/#resilience" onclick="closeMobile()">Resilience</a>
  <a href="../index.html" onclick="closeMobile()">Journal</a>
  <a href="https://myvilla.la/#contact" onclick="closeMobile()">Request Briefing</a>
</div>

<section class="category-hero">
  <div class="category-breadcrumb">
    <a href="https://myvilla.la">Home</a> &rsaquo; <a href="../index.html">Journal</a> &rsaquo; {esc(sec['name'])}
  </div>
  <h1 class="category-title">{esc(sec['name'])}</h1>
  <div class="category-accent-bar"></div>
  <div class="category-sub">{total} article{'s' if total != 1 else ''} &middot; Updated perspectives on this topic</div>
</section>

{months_html}

<a href="../index.html" class="back-link">
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M13 8H3M7 4L3 8l4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
  Back to Journal
</a>

<section class="journal-cta">
  <div class="journal-cta-inner">
    <div class="journal-cta-label">Curated by My Villa</div>
    <div class="journal-cta-divider"></div>
    <h2 class="journal-cta-title">Building <em>Permanence</em> in<br>Los Angeles</h2>
    <p class="journal-cta-text">We design and coordinate luxury reinforced concrete villas for the Los Angeles market. European engineering. Californian lifestyle.</p>
    <div class="journal-cta-buttons">
      <a href="https://myvilla.la" class="cta-btn cta-btn-primary">Explore My Villa</a>
      <a href="https://myvilla.la/#contact" class="cta-btn cta-btn-secondary">Request a Briefing</a>
    </div>
  </div>
</section>

<footer class="footer">
  <div class="footer-brand-wrap">
    <span class="footer-brand">MY VILLA</span>
    <span class="footer-brand-payoff">Italian Soul &middot; Californian Body</span>
  </div>
  <span class="footer-copy">&copy; 2026 My Villa. All rights reserved.</span>
  <div class="footer-links">
    <a href="https://myvilla.la/team.html">Studio</a>
    <a href="https://myvilla.la/privacy.html">Privacy</a>
  </div>
</footer>

<script>
const obs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{ if (e.isIntersecting) {{ e.target.classList.add('visible'); obs.unobserve(e.target); }} }});
}}, {{ threshold: 0.1 }});
document.querySelectorAll('.reveal').forEach(el => obs.observe(el));
</script>
</body>
</html>"""


def get_style_block():
    """Load the CSS <style> block from the index template."""
    template_path = SYSTEM_DIR / "templates" / "journal-index.html"
    if template_path.exists():
        template_content = template_path.read_text()
        style_match = re.search(r'<style>(.*?)</style>', template_content, re.DOTALL)
        if style_match:
            return style_match.group(1)
    return ""


def generate_category_pages(articles, output_dir):
    """Generate category pages for sections with > PREVIEW_LIMIT articles.

    Returns the number of pages generated.
    """
    by_section = {}
    for a in articles:
        sec = a.get("section", "materials")
        by_section.setdefault(sec, []).append(a)

    category_dir = output_dir / "category"
    category_dir.mkdir(parents=True, exist_ok=True)
    style_block = get_style_block()

    # Clean up stale category pages: remove pages for sections that no longer
    # exceed the threshold (or no longer exist).
    valid_section_ids = {s for s in by_section if len(by_section[s]) > CATEGORY_PAGE_THRESHOLD}
    for stale in category_dir.glob("*.html"):
        if stale.stem not in valid_section_ids:
            try:
                stale.unlink()
                print(f"  Removed stale category page: {stale.name}")
            except Exception:
                pass

    count = 0
    for sec_id in valid_section_ids:
        arts = sorted(by_section[sec_id], key=lambda a: a.get("date", ""), reverse=True)
        html_out = render_category_page(sec_id, arts, style_block)
        out_path = category_dir / f"{sec_id}.html"
        out_path.write_text(html_out)
        print(f"  Category page: {out_path.relative_to(output_dir.parent)} ({len(arts)} articles)")
        count += 1
    return count


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Journal Index Rebuilder")
    parser.add_argument("--articles-dir", default=None,
                        help="Directory containing article HTMLs (default: blog/)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path for index.html (default: blog/index.html)")
    args = parser.parse_args()

    articles_dir = Path(args.articles_dir) if args.articles_dir else BLOG_DIR
    output_path = Path(args.output) if args.output else (articles_dir / "index.html")

    print(f"\nMy Villa — Journal Index Rebuilder")
    print(f"{'='*50}")
    print(f"Scanning: {articles_dir}")

    # Find all HTML files (exclude index.html)
    html_files = [f for f in articles_dir.glob("*.html") if f.name != "index.html"]
    print(f"Found {len(html_files)} article files")

    # Extract metadata
    articles = []
    for f in html_files:
        try:
            meta = extract_metadata(f)
            if meta["title"]:
                articles.append(meta)
                print(f"  [{meta['section']}] {meta['title'][:50]}")
        except Exception as e:
            print(f"  Error reading {f.name}: {e}")

    # Sort by date descending
    articles.sort(key=lambda a: a.get("date", ""), reverse=True)

    # Render index
    index_html = render_index_html(articles)

    with open(output_path, "w") as f:
        f.write(index_html)
    print(f"\nSaved: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
    print(f"Total articles indexed: {len(articles)}")

    # Generate dedicated category pages for sections exceeding the preview threshold
    try:
        cat_count = generate_category_pages(articles, output_path.parent)
        if cat_count:
            print(f"Generated {cat_count} category page(s)")
    except Exception as e:
        print(f"Warning: category page generation failed: {e}")


if __name__ == "__main__":
    main()
