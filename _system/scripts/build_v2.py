#!/usr/bin/env python3
"""
My Villa — v2 Journal builder ("Quiet Permanence" design system)

Reads the canonical article sidecars (blog/*.json) and generates the full
v2 Journal into v2/blog/:

    v2/blog/<slug>.html            one page per published article
    v2/blog/index.html             journal front page (featured + latest + sections)
    v2/blog/category/<section>.html  one hub page per section

Only slugs that ALSO have a published HTML at blog/<slug>.html are built
(a JSON without its HTML is treated as unpublished).

PREVIEW mode (default True): every page gets <meta name="robots" content="noindex">
and canonicals pointing at the FINAL root URLs (https://myvilla.la/blog/…).
At promotion: run with --live to drop the noindex.

Usage:
  python3 _system/scripts/build_v2.py
  python3 _system/scripts/build_v2.py --live
"""
from __future__ import annotations

import argparse
import html as H
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
BLOG_DIR = PROJECT_ROOT / "blog"
OUT_DIR = PROJECT_ROOT / "v2" / "blog"
CAT_DIR = OUT_DIR / "category"

SITE = "https://myvilla.la"

GTAG = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-D6HJX7BNZN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-D6HJX7BNZN');
</script>"""

FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">"""

# Canonical section registry (order = editorial priority on the index page)
SECTIONS = {
    "insurance":     {"name": "Insurance & Insurability", "accent": "#C2714F"},
    "permits":       {"name": "Permits & Policy",          "accent": "#8C9C8A"},
    "materials":     {"name": "Materials & Construction",  "accent": "#C4A265"},
    "market":        {"name": "LA Luxury Market",          "accent": "#6B8F9E"},
    "climate":       {"name": "Climate & Resilience",      "accent": "#7A8B99"},
    "concrete_arch": {"name": "Concrete Architecture",     "accent": "#A89080"},
}
SECTION_PREVIEW = 6   # cards per section on the front page

# Featured-slot commercial-intent ranking (ported from the legacy index
# renderer, strategy ref: _system/knowledge/content_strategy.md §4).
# The Featured card should promote buyer-intent articles over pure news.
# Case-insensitive substring match on title+slug; first match wins; ties
# broken by date. Articles matching nothing rank 999 (news) and only take
# the slot when no commercial candidate exists in the freshest pool.
FEATURED_COMMERCIAL_KEYWORDS = [
    "luxury home builder malibu",
    "custom home beverly hills",
    "custom home bel air",
    "architect luxury home malibu",
    "luxury home builder",
    "custom home builder",
    "fire-resistant home construction cost",
    "concrete home cost",
    "how to build fire-resistant luxury home",
    "fire-resistant home construction",
    "fire-resilient home construction",
    "fire-resistant home california",
    "concrete homes in los angeles",
    "concrete home builder los angeles",
    "icf home builder california",
    "fireproof home california",
    "reinforced concrete home",
    "california fire insurance solution",
    "fire insurance in california",
    "insurable home california",
    "fair plan alternative",
    "insurable home",
    "italian villa california",
    "mediterranean villa california",
    "italian villa",
    "mediterranean villa",
]
FEATURED_POOL = 12  # only the N freshest articles compete for the slot


def commercial_rank(art):
    hay = (art.get("title", "") + " " + art.get("slug", "")).lower()
    for i, kw in enumerate(FEATURED_COMMERCIAL_KEYWORDS):
        if kw in hay:
            return i
    return 999


def pick_featured(arts_sorted):
    """Best commercial-intent article among the freshest FEATURED_POOL;
    ties go to the most recent (pool is date-desc and min() is stable).
    Falls back to the most recent article when nothing matches."""
    pool = arts_sorted[:FEATURED_POOL]
    return min(pool, key=commercial_rank) if pool else None

# ────────────────────────────────────────────────────────────────────
# shared CSS (design tokens + components used by all v2 blog pages)
# ────────────────────────────────────────────────────────────────────
BASE_CSS = """
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --ink: #16130F; --espresso: #2A211C; --cream: #FAF7F2; --linen: #F1EBE1; --white: #fff;
  --sand: #D4B896; --gold: #C4A265; --terracotta: #C2714F; --terra-deep: #A85A3B;
  --ink-60: rgba(22,19,15,.6); --ink-40: rgba(22,19,15,.4);
  --cream-80: rgba(250,247,242,.8); --cream-55: rgba(250,247,242,.55);
  --line-d: rgba(212,184,150,.16); --line-l: rgba(42,33,28,.14);
  --serif: 'Cormorant Garamond', Georgia, serif;
  --sans: 'Montserrat', 'Helvetica Neue', Arial, sans-serif;
  --pad-x: clamp(24px, 6vw, 120px);
  --ease: cubic-bezier(.22, 1, .36, 1);
}
html { scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }
body { font-family: var(--sans); font-weight: 300; background: var(--cream); color: var(--espresso); line-height: 1.78; font-size: 16px; overflow-x: hidden; }
::selection { background: var(--terracotta); color: var(--cream); }
img { display: block; max-width: 100%; }
:focus-visible { outline: 2px solid var(--terracotta); outline-offset: 3px; }
[data-rv] { opacity: 0; transform: translateY(28px); transition: opacity 1s var(--ease), transform 1s var(--ease); transition-delay: calc(var(--d, 0) * 80ms); }
[data-rv].on { opacity: 1; transform: none; }
.nav {
  position: fixed; top: 0; left: 0; right: 0; z-index: 200;
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px var(--pad-x);
  background: rgba(22,19,15,.92);
  -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px);
}
.nav-logo-wrap { display: flex; flex-direction: column; gap: 2px; text-decoration: none; }
.nav-logo { font-family: var(--serif); font-size: 17px; font-weight: 500; letter-spacing: .3em; color: var(--cream); }
.nav-payoff { font-size: 8px; letter-spacing: .26em; font-weight: 500; text-transform: uppercase; color: var(--cream-55); }
.nav-right { display: flex; align-items: center; gap: 24px; }
.nav-right a { font-size: 11px; letter-spacing: .15em; font-weight: 500; text-transform: uppercase; color: var(--cream-80); text-decoration: none; transition: color .3s; white-space: nowrap; }
.nav-right a:hover { color: var(--sand); }
.nav-cta { padding: 10px 20px; background: var(--terracotta); color: #fff !important; font-weight: 600; transition: background .3s; }
.nav-cta:hover { background: var(--terra-deep); }
.card {
  display: flex; flex-direction: column;
  background: var(--white); border: 1px solid var(--line-l);
  text-decoration: none; color: inherit; overflow: hidden; position: relative;
  transition: transform .5s var(--ease), box-shadow .5s;
}
.card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: var(--accent, var(--terracotta)); transform: scaleX(0); transform-origin: left; transition: transform .45s var(--ease); z-index: 2; }
.card:hover { transform: translateY(-4px); box-shadow: 0 20px 44px rgba(42,33,28,.1); }
.card:hover::before { transform: scaleX(1); }
.card-img { width: 100%; height: auto; aspect-ratio: 16/9; object-fit: cover; filter: saturate(.92); transition: filter .5s, transform .8s var(--ease); background: var(--linen); }
.card:hover .card-img { filter: saturate(1.04); transform: scale(1.02); }
.card-body { padding: 18px 20px 20px; display: flex; flex-direction: column; gap: 9px; flex: 1; }
.card-tag { font-size: 9.5px; letter-spacing: .2em; font-weight: 700; text-transform: uppercase; color: var(--accent, var(--terracotta)); }
.card-title { font-family: var(--serif); font-weight: 500; font-size: 18px; line-height: 1.3; color: var(--ink); }
.card-excerpt { font-size: 12.5px; line-height: 1.6; color: var(--ink-60); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; flex: 1; }
.card-meta { font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-40); font-weight: 500; display: flex; align-items: center; gap: 10px; }
.card-meta::before { content: ''; width: 18px; height: 1px; background: var(--accent, var(--terracotta)); }
.btn {
  display: inline-flex; align-items: center; gap: 12px;
  padding: 16px 32px; background: var(--terracotta); color: var(--cream);
  font-size: 12px; letter-spacing: .22em; font-weight: 600; text-transform: uppercase;
  text-decoration: none; border: none; cursor: pointer; transition: background .35s;
}
.btn:hover { background: var(--terra-deep); }
.link-line {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 12px; letter-spacing: .2em; font-weight: 600; text-transform: uppercase;
  text-decoration: none; color: var(--terracotta); padding-bottom: 5px; position: relative;
}
.link-line::after { content: ''; position: absolute; left: 0; bottom: 0; width: 100%; height: 1px; background: currentColor; transform-origin: right; transition: transform .45s var(--ease); }
.link-line:hover::after { transform: scaleX(.35); }
.cta-band { background: var(--ink); text-align: center; padding: clamp(70px, 11vh, 120px) var(--pad-x); }
.cta-band h2 { font-family: var(--serif); font-weight: 400; font-size: clamp(28px, 4vw, 48px); line-height: 1.12; color: var(--cream); }
.cta-band h2 em { font-style: italic; color: var(--sand); }
.cta-band p { margin: 16px auto 30px; max-width: 540px; font-size: 14.5px; line-height: 1.8; color: var(--cream-55); }
.footer { background: var(--ink); border-top: 1px solid var(--line-d); padding: 32px var(--pad-x); }
.footer-inner { max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; font-size: 11.5px; color: var(--cream-55); }
.footer-inner a { color: inherit; text-decoration: none; }
.footer-inner a:hover { color: var(--cream); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
  [data-rv] { opacity: 1 !important; transform: none !important; }
}
"""

ARTICLE_CSS = BASE_CSS + """
.a-hero { padding: clamp(120px, 17vh, 180px) var(--pad-x) 0; background: var(--cream); }
.a-hero-inner { max-width: 860px; margin: 0 auto; }
.breadcrumb { display: flex; flex-wrap: wrap; gap: 8px; font-size: 10.5px; letter-spacing: .16em; font-weight: 600; text-transform: uppercase; color: var(--ink-40); }
.breadcrumb a { color: var(--terracotta); text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.a-title { margin-top: 22px; font-family: var(--serif); font-weight: 400; font-size: clamp(32px, 4.6vw, 56px); line-height: 1.08; color: var(--ink); }
.a-subtitle { margin-top: 16px; font-family: var(--serif); font-style: italic; font-weight: 300; font-size: clamp(18px, 2.2vw, 24px); line-height: 1.45; color: var(--ink-60); }
.a-meta { margin-top: 22px; display: flex; align-items: center; gap: 14px; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; font-weight: 600; color: var(--ink-40); }
.a-meta .sep { width: 22px; height: 1px; background: var(--terracotta); }
.a-figure { max-width: 1100px; margin: clamp(36px, 6vh, 56px) auto 0; padding: 0 var(--pad-x); }
.a-figure img { width: 100%; height: auto; aspect-ratio: 16/8.5; object-fit: cover; }
.keydata { max-width: 860px; margin: clamp(36px, 5vh, 52px) auto 0; padding: 0 var(--pad-x); display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.kd { padding: 22px 20px; background: var(--linen); border-top: 2px solid var(--terracotta); }
.kd-no { font-family: var(--serif); font-weight: 300; font-size: clamp(26px, 3vw, 38px); line-height: 1; color: var(--ink); }
.kd-label { margin-top: 8px; font-size: 10px; letter-spacing: .14em; font-weight: 600; text-transform: uppercase; color: var(--ink-60); line-height: 1.6; white-space: pre-line; }
.a-body { max-width: 760px; margin: clamp(40px, 6vh, 64px) auto 0; padding: 0 var(--pad-x); }
.a-body p { margin-bottom: 22px; font-size: 16.5px; line-height: 1.85; color: var(--espresso); }
.a-body h2 { font-family: var(--serif); font-weight: 500; font-size: clamp(24px, 2.8vw, 32px); line-height: 1.2; color: var(--ink); margin: 40px 0 18px; }
.a-body a { color: var(--terracotta); text-decoration: none; border-bottom: 1px solid rgba(194,113,79,.35); transition: border-color .25s; }
.a-body a:hover { border-color: var(--terracotta); }
.a-body .source-citation { display: block; margin-top: 8px; font-size: 12.5px; color: var(--ink-40); }
.a-body .key-data { margin: 30px 0; padding: 22px 26px; background: var(--linen); border-left: 2px solid var(--terracotta); }
.a-body .key-data p { margin: 0; font-size: 14.5px; line-height: 1.7; color: var(--espresso); }
.a-body .pullquote { margin: 36px 0; padding: 0 0 0 26px; border-left: 2px solid var(--terracotta); font-family: var(--serif); font-style: italic; font-weight: 400; font-size: clamp(20px, 2.4vw, 26px); line-height: 1.5; color: var(--ink); }
.perspective { max-width: 860px; margin: clamp(48px, 7vh, 72px) auto 0; padding: 0 var(--pad-x); }
.perspective-inner { background: var(--ink); color: var(--cream); padding: clamp(30px, 4vw, 48px); position: relative; }
.perspective-inner::before { content: '\\201C'; position: absolute; top: 6px; left: 22px; font-family: var(--serif); font-size: 90px; line-height: 1; color: var(--sand); opacity: .3; }
.perspective-label { display: flex; align-items: center; gap: 14px; font-size: 10px; letter-spacing: .3em; font-weight: 700; text-transform: uppercase; color: var(--sand); margin-bottom: 18px; }
.perspective-label::after { content: ''; flex: 1; height: 1px; background: var(--line-d); }
.perspective-inner p { font-family: var(--serif); font-weight: 400; font-size: clamp(17px, 2vw, 21px); line-height: 1.65; color: var(--cream-80); }
.sources { max-width: 860px; margin: clamp(44px, 6vh, 64px) auto 0; padding: 0 var(--pad-x); }
.sources h3, .faq-h, .related h3 { display: flex; align-items: center; gap: 14px; font-size: 10.5px; letter-spacing: .28em; font-weight: 700; text-transform: uppercase; color: var(--ink-40); margin-bottom: 18px; }
.sources h3::after, .faq-h::after, .related h3::after { content: ''; flex: 1; height: 1px; background: var(--line-l); }
.source { display: block; padding: 16px 0; border-bottom: 1px solid var(--line-l); text-decoration: none; }
.source:first-of-type { border-top: 1px solid var(--line-l); }
.source .s-pub { font-size: 10px; letter-spacing: .2em; font-weight: 700; text-transform: uppercase; color: var(--terracotta); }
.source .s-title { margin-top: 4px; font-family: var(--serif); font-weight: 500; font-size: 17px; color: var(--ink); line-height: 1.35; transition: color .3s; }
.source:hover .s-title { color: var(--terracotta); }
.faq-block { max-width: 860px; margin: clamp(44px, 6vh, 64px) auto 0; padding: 0 var(--pad-x); }
.faq-item { border-top: 1px solid var(--line-l); padding: 20px 0; }
.faq-item:last-child { border-bottom: 1px solid var(--line-l); }
.faq-item h4 { font-family: var(--serif); font-weight: 500; font-size: 19px; color: var(--ink); }
.faq-item p { margin-top: 8px; font-size: 14px; line-height: 1.75; color: var(--ink-60); }
.related { max-width: 860px; margin: clamp(48px, 7vh, 72px) auto 0; padding: 0 var(--pad-x); }
.related-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.a-cta { margin-top: clamp(64px, 9vh, 100px); }
@media (max-width: 880px) {
  .keydata, .related-grid { grid-template-columns: 1fr; }
  .nav-right a:not(.nav-cta) { display: none; }
}
"""

INDEX_CSS = BASE_CSS + """
.j-hero { padding: clamp(130px, 18vh, 200px) var(--pad-x) clamp(48px, 7vh, 80px); background: var(--ink); }
.j-hero-inner { max-width: 1240px; margin: 0 auto; }
.j-hero .eyebrow { display: flex; align-items: center; gap: 14px; font-size: 11px; letter-spacing: .32em; font-weight: 600; text-transform: uppercase; color: var(--sand); margin-bottom: 22px; }
.j-hero .eyebrow::before { content: ''; width: 50px; height: 1px; background: var(--sand); opacity: .7; }
.j-hero h1 { font-family: var(--serif); font-weight: 400; font-size: clamp(38px, 6vw, 80px); line-height: 1.02; color: var(--cream); max-width: 880px; }
.j-hero h1 em { font-style: italic; font-weight: 300; color: var(--sand); }
.j-hero p { margin-top: 22px; font-size: clamp(15px, 1.5vw, 17px); line-height: 1.8; color: var(--cream-55); max-width: 640px; }
.j-stamp { display: inline-flex; align-items: center; gap: 8px; margin-top: 26px; padding: 6px 14px; border: 1px solid rgba(212,184,150,.4); font-size: 10px; letter-spacing: .2em; font-weight: 700; text-transform: uppercase; color: var(--sand); }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(212,184,150,.6); } 70% { box-shadow: 0 0 0 5px rgba(212,184,150,0); } 100% { box-shadow: 0 0 0 0 rgba(212,184,150,0); } }
.j-stamp .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--sand); animation: pulse 2.4s infinite; }
.wrap { max-width: 1240px; margin: 0 auto; padding: 0 var(--pad-x); }
.featured { padding-top: clamp(48px, 7vh, 80px); }
.featured-card { display: grid; grid-template-columns: minmax(0, 7fr) minmax(0, 5fr); border: 1px solid var(--line-l); background: var(--white); text-decoration: none; color: inherit; overflow: hidden; transition: box-shadow .5s; }
.featured-card:hover { box-shadow: 0 28px 60px rgba(42,33,28,.12); }
.featured-card .f-img { position: relative; overflow: hidden; min-height: 340px; }
.featured-card .f-img img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transition: transform 1.4s var(--ease); }
.featured-card:hover .f-img img { transform: scale(1.04); }
.featured-card .f-body { padding: clamp(28px, 3.4vw, 48px); display: flex; flex-direction: column; gap: 14px; }
.f-kicker { display: inline-flex; align-items: center; gap: 10px; font-size: 10px; letter-spacing: .24em; font-weight: 700; text-transform: uppercase; color: var(--terracotta); }
.f-kicker .lead-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--terracotta); }
.featured-card h2 { font-family: var(--serif); font-weight: 400; font-size: clamp(24px, 2.8vw, 38px); line-height: 1.14; color: var(--ink); }
.featured-card .f-excerpt { font-size: 14px; line-height: 1.75; color: var(--ink-60); }
.featured-card .f-meta { margin-top: auto; font-size: 10.5px; letter-spacing: .12em; text-transform: uppercase; font-weight: 600; color: var(--ink-40); }
.latest { padding-top: clamp(44px, 6vh, 64px); }
.latest-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.latest-item { display: block; padding: 18px 20px; background: var(--white); border: 1px solid var(--line-l); border-left: 2px solid var(--accent, var(--terracotta)); text-decoration: none; color: inherit; transition: transform .4s var(--ease), box-shadow .4s; }
.latest-item:hover { transform: translateY(-3px); box-shadow: 0 14px 30px rgba(42,33,28,.08); }
.latest-item .l-tag { font-size: 9px; letter-spacing: .2em; font-weight: 700; text-transform: uppercase; color: var(--accent, var(--terracotta)); }
.latest-item .l-title { margin-top: 7px; font-family: var(--serif); font-weight: 500; font-size: 16px; line-height: 1.3; color: var(--ink); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.latest-item .l-meta { margin-top: 9px; font-size: 9.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-40); font-weight: 500; }
.sec-block { padding-top: clamp(64px, 9vh, 96px); }
.sec-head { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; flex-wrap: wrap; border-bottom: 1px solid var(--line-l); padding-bottom: 16px; margin-bottom: 26px; }
.sec-head h2 { font-family: var(--serif); font-weight: 400; font-size: clamp(24px, 3vw, 36px); color: var(--ink); display: flex; align-items: center; gap: 16px; }
.sec-head h2::before { content: ''; width: 10px; height: 10px; background: var(--accent, var(--terracotta)); }
.sec-head .sec-meta { display: flex; align-items: baseline; gap: 18px; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; font-weight: 600; color: var(--ink-40); }
.sec-head .sec-meta a { color: var(--terracotta); text-decoration: none; }
.sec-head .sec-meta a:hover { text-decoration: underline; }
.sec-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
.bottom-pad { padding-bottom: clamp(72px, 11vh, 130px); }
@media (max-width: 1000px) {
  .featured-card { grid-template-columns: 1fr; }
  .sec-grid, .latest-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 640px) {
  .sec-grid, .latest-grid { grid-template-columns: 1fr; }
  .nav-right a:not(.nav-cta) { display: none; }
}
"""

REVEAL_JS = """<script>
(function () {
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var els = document.querySelectorAll('[data-rv], .a-body .reveal');
  if ('IntersectionObserver' in window && !reduced) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('on'); io.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -4% 0px' });
    els.forEach(function (el) { el.classList.add('rv-ready'); io.observe(el); });
  } else {
    els.forEach(function (el) { el.classList.add('on'); });
  }
})();
</script>"""

# body_html embeds class="reveal" — map it onto the same reveal behaviour
BODY_REVEAL_CSS = """
.a-body .reveal.rv-ready { opacity: 0; transform: translateY(24px); transition: opacity .9s var(--ease), transform .9s var(--ease); }
.a-body .reveal.on { opacity: 1; transform: none; }
"""


def esc(t):
    return H.escape(str(t)) if t else ""


def fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        return iso or ""


def fmt_date_short(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return iso or ""


def robots_tag(preview):
    if preview:
        return '<!-- PREVIEW FLAG: rimuovere alla promozione -->\n<meta name="robots" content="noindex, nofollow">'
    return '<meta name="robots" content="index, follow">'


def hero_web_path(art):
    wp = (art.get("hero_image") or {}).get("web_path", "")
    if not wp:
        return None
    # web_path is relative to blog/ (some sidecars already include the
    # "blog/" prefix) → normalize, then make root-absolute so it works
    # at /v2/blog/ in preview and at /blog/ after promotion
    wp = wp.lstrip("/")
    if wp.startswith("blog/"):
        wp = wp[len("blog/"):]
    return "/blog/" + wp


def nav_html(depth=0):
    """depth 0 = v2/blog/, depth 1 = v2/blog/category/"""
    up = "../" * (depth + 1)   # to v2/
    idx = "../" * depth        # to v2/blog/
    return f"""<nav class="nav" aria-label="Main navigation">
  <a href="{up}index.html" class="nav-logo-wrap" aria-label="My Villa — home">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul &middot; Californian Body</span>
  </a>
  <div class="nav-right">
    <a href="{up}index.html">Home</a>
    <a href="{idx}index.html">Journal</a>
    <a href="{up}team.html">Studio</a>
    <a href="{up}index.html#briefing" class="nav-cta">Request Briefing</a>
  </div>
</nav>"""


def footer_html(depth=0):
    up = "../" * (depth + 1)
    idx = "../" * depth
    return f"""<footer class="footer">
  <div class="footer-inner">
    <span>&copy; 2026 My Villa &middot; a practice of <a href="https://its.vision" target="_blank" rel="noopener">IT'S Architecture</a></span>
    <span><a href="{up}index.html">Home</a> &nbsp;&middot;&nbsp; <a href="{idx}index.html">Journal</a> &nbsp;&middot;&nbsp; <a href="/privacy.html">Privacy</a></span>
  </div>
</footer>"""


def cta_band_html(depth=0):
    up = "../" * (depth + 1)
    return f"""<section class="cta-band a-cta" aria-label="Request a private briefing">
  <h2 data-rv>Reading the risk is our job.<br><em>Building past it is our craft.</em></h2>
  <p data-rv style="--d:1">If you are considering a luxury build or rebuild in Malibu, Beverly Hills, or the broader Westside — request a private briefing with the design team.</p>
  <a class="btn" href="{up}index.html#briefing" data-rv style="--d:2"><span>Request a Private Briefing</span><span>&rarr;</span></a>
</section>"""


def card_html(art, depth=0):
    """Article card used on index + category pages. depth = path depth from v2/blog/."""
    rel = ("../" * depth) + art["slug"] + ".html"
    sec = SECTIONS.get(art["section"], SECTIONS["materials"])
    hero = hero_web_path(art)
    img = (f'<img class="card-img" src="{esc(hero)}" alt="" loading="lazy" width="800" height="450">'
           if hero else "")
    return f"""<a class="card" href="{esc(rel)}" style="--accent:{sec['accent']}" aria-label="Read: {esc(art['title'])}">
  {img}
  <div class="card-body">
    <span class="card-tag">{esc(art.get('tag_label') or sec['name'])}</span>
    <h3 class="card-title">{esc(art['title'])}</h3>
    <p class="card-excerpt">{esc(art.get('excerpt', ''))}</p>
    <div class="card-meta">{esc(fmt_date_short(art['date']))} &middot; {art.get('read_time', 5)} min read</div>
  </div>
</a>"""


# ────────────────────────────────────────────────────────────────────
# ARTICLE PAGE
# ────────────────────────────────────────────────────────────────────
def render_article(art, all_arts, preview=True):
    slug = art["slug"]
    sec = SECTIONS.get(art["section"], SECTIONS["materials"])
    url = f"{SITE}/blog/{slug}.html"
    hero = hero_web_path(art)
    title = art.get("seo_title") or art["title"]
    desc = art.get("meta_description") or art.get("excerpt", "")
    date_iso = art["date"]

    # related: same section, newest first, exclude self
    related = [a for a in all_arts if a["section"] == art["section"] and a["slug"] != slug]
    related = sorted(related, key=lambda a: a["date"], reverse=True)[:3]
    related_html = ""
    if related:
        cards = "\n".join(card_html(r) for r in related)
        related_html = f"""<aside class="related" aria-label="Related articles">
  <h3>Related from the Desk</h3>
  <div class="related-grid">
{cards}
  </div>
</aside>"""

    # key data strip
    kd_html = ""
    kds = art.get("key_data") or []
    if kds:
        items = "\n".join(
            f'<div class="kd" data-rv style="--d:{i}"><div class="kd-no">{esc(k.get("number",""))}</div>'
            f'<div class="kd-label">{esc(k.get("label",""))}</div></div>'
            for i, k in enumerate(kds[:3])
        )
        kd_html = f'<div class="keydata">{items}</div>'

    # sources
    src_html = ""
    sources = art.get("sources") or []
    if sources:
        rows = []
        for s in sources:
            href = s.get("url", "#")
            rows.append(
                f'<a class="source" href="{esc(href)}" target="_blank" rel="noopener">'
                f'<span class="s-pub">{esc(s.get("name",""))}</span>'
                f'<div class="s-title">{esc(s.get("title",""))}</div></a>'
            )
        src_html = f"""<aside class="sources" aria-label="Sources">
  <h3>Sources</h3>
  {''.join(rows)}
</aside>"""

    # FAQ (rare)
    faq_html, faq_schema = "", ""
    faqs = art.get("faq") or []
    if faqs:
        items = "\n".join(
            f'<div class="faq-item"><h4>{esc(q.get("q") or q.get("question",""))}</h4>'
            f'<p>{esc(q.get("a") or q.get("answer",""))}</p></div>'
            for q in faqs
        )
        faq_html = f'<div class="faq-block"><h3 class="faq-h">Questions, answered</h3>{items}</div>'
        faq_entities = ",\n        ".join(
            json.dumps({
                "@type": "Question",
                "name": (q.get("q") or q.get("question", "")),
                "acceptedAnswer": {"@type": "Answer", "text": (q.get("a") or q.get("answer", ""))}
            }, ensure_ascii=False)
            for q in faqs
        )
        faq_schema = f""",
    {{
      "@type": "FAQPage",
      "mainEntity": [
        {faq_entities}
      ]
    }}"""

    hero_fig = (f'<figure class="a-figure" data-rv><img src="{esc(hero)}" alt="{esc(art["title"])}" '
                f'fetchpriority="high" width="1280" height="680"></figure>' if hero else "")

    schema = f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@graph": [
    {{
      "@type": "Article",
      "@id": "{url}#article",
      "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{url}" }},
      "headline": {json.dumps(art['title'], ensure_ascii=False)},
      "description": {json.dumps(desc, ensure_ascii=False)},
      {f'"image": "{SITE}{hero}",' if hero else ''}
      "datePublished": "{date_iso}",
      "dateModified": "{date_iso}",
      "author": {{ "@type": "Organization", "name": "My Villa", "url": "{SITE}" }},
      "publisher": {{
        "@type": "Organization", "name": "My Villa",
        "logo": {{ "@type": "ImageObject", "url": "{SITE}/img/logos/favicon.svg" }}
      }},
      "articleSection": {json.dumps(sec['name'], ensure_ascii=False)},
      "inLanguage": "en-US"
    }},
    {{
      "@type": "BreadcrumbList",
      "itemListElement": [
        {{ "@type": "ListItem", "position": 1, "name": "Home", "item": "{SITE}/" }},
        {{ "@type": "ListItem", "position": 2, "name": "Journal", "item": "{SITE}/blog/" }},
        {{ "@type": "ListItem", "position": 3, "name": {json.dumps(sec['name'], ensure_ascii=False)}, "item": "{SITE}/blog/category/{art['section']}.html" }},
        {{ "@type": "ListItem", "position": 4, "name": {json.dumps(art['title'], ensure_ascii=False)}, "item": "{url}" }}
      ]
    }}{faq_schema}
  ]
}}
</script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
{GTAG}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">

{robots_tag(preview)}

<title>{esc(title)} — My Villa Journal</title>
<meta name="description" content="{esc(desc)}">
<meta name="keywords" content="{esc(art.get('meta_keywords',''))}">
<meta name="author" content="My Villa">
<link rel="canonical" href="{url}">

<meta property="og:type" content="article">
<meta property="og:url" content="{url}">
<meta property="og:title" content="{esc(art['title'])}">
<meta property="og:description" content="{esc(desc)}">
{f'<meta property="og:image" content="{SITE}{esc(hero)}">' if hero else ''}
<meta property="og:site_name" content="My Villa">
<meta property="article:published_time" content="{date_iso}">
<meta property="article:section" content="{esc(sec['name'])}">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(art['title'])}">
<meta name="twitter:description" content="{esc(desc)}">
{f'<meta name="twitter:image" content="{SITE}{esc(hero)}">' if hero else ''}

<link rel="icon" type="image/svg+xml" href="/img/logos/favicon.svg">
{FONTS}
{schema}
<style>{ARTICLE_CSS}{BODY_REVEAL_CSS}</style>
</head>
<body>

{nav_html(depth=0)}

<main>
<article>
  <header class="a-hero">
    <div class="a-hero-inner">
      <div class="breadcrumb" data-rv>
        <a href="../index.html">Home</a><span>/</span>
        <a href="index.html">Journal</a><span>/</span>
        <a href="category/{art['section']}.html">{esc(sec['name'])}</a>
      </div>
      <h1 class="a-title" data-rv style="--d:1">{esc(art['title'])}</h1>
      <p class="a-subtitle" data-rv style="--d:2">{esc(art.get('subtitle',''))}</p>
      <div class="a-meta" data-rv style="--d:3">
        <span>{esc(fmt_date(date_iso))}</span><span class="sep" aria-hidden="true"></span>
        <span>{art.get('read_time', 5)} min read</span><span class="sep" aria-hidden="true"></span>
        <span>{esc(art.get('tag_label') or sec['name'])}</span>
      </div>
    </div>
  </header>

  {hero_fig}
  {kd_html}

  <div class="a-body">
{art.get('body_html','')}
  </div>

  <aside class="perspective" aria-label="Our perspective">
    <div class="perspective-inner" data-rv>
      <div class="perspective-label">Our Perspective</div>
      <p>{esc(art.get('our_perspective',''))}</p>
    </div>
  </aside>

  {faq_html}
  {src_html}
  {related_html}
</article>

{cta_band_html(depth=0)}
</main>

{footer_html(depth=0)}
{REVEAL_JS}
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
# INDEX PAGE
# ────────────────────────────────────────────────────────────────────
def render_index(arts, preview=True):
    arts_sorted = sorted(arts, key=lambda a: a["date"], reverse=True)
    featured = pick_featured(arts_sorted) or arts_sorted[0]
    latest = [a for a in arts_sorted if a["slug"] != featured["slug"]][:6]
    total = len(arts)
    url = f"{SITE}/blog/"

    fhero = hero_web_path(featured)
    fsec = SECTIONS.get(featured["section"], SECTIONS["materials"])
    featured_html = f"""<section class="featured wrap" aria-label="Featured story">
  <a class="featured-card" href="{esc(featured['slug'])}.html" data-rv>
    <div class="f-img">{f'<img src="{esc(fhero)}" alt="" fetchpriority="high" width="1100" height="640">' if fhero else ''}</div>
    <div class="f-body">
      <span class="f-kicker"><span class="lead-dot" aria-hidden="true"></span>Featured &middot; {esc(featured.get('tag_label') or fsec['name'])}</span>
      <h2>{esc(featured['title'])}</h2>
      <p class="f-excerpt">{esc(featured.get('excerpt',''))}</p>
      <div class="f-meta">{esc(fmt_date_short(featured['date']))} &middot; {featured.get('read_time',5)} min read</div>
    </div>
  </a>
</section>"""

    latest_items = "\n".join(
        f"""<a class="latest-item" href="{esc(a['slug'])}.html" style="--accent:{SECTIONS.get(a['section'], SECTIONS['materials'])['accent']}" data-rv style="--d:{i%3}">
  <span class="l-tag">{esc(a.get('tag_label') or SECTIONS.get(a['section'], SECTIONS['materials'])['name'])}</span>
  <div class="l-title">{esc(a['title'])}</div>
  <div class="l-meta">{esc(fmt_date_short(a['date']))} &middot; {a.get('read_time',5)} min</div>
</a>""" for i, a in enumerate(latest)
    )
    latest_html = f"""<section class="latest wrap" aria-label="Latest articles">
  <div class="latest-grid">
{latest_items}
  </div>
</section>"""

    # section blocks (skip featured? no — sections show their own latest 6 regardless)
    sec_blocks = []
    for sec_id, sec in SECTIONS.items():
        sec_arts = [a for a in arts_sorted if a["section"] == sec_id]
        if not sec_arts:
            continue
        shown = sec_arts[:SECTION_PREVIEW]
        n = len(sec_arts)
        see_all = (f'<a href="category/{sec_id}.html">See all {n} articles &rarr;</a>'
                   if n > SECTION_PREVIEW else "")
        cards = "\n".join(card_html(a) for a in shown)
        sec_blocks.append(f"""<section class="sec-block wrap" aria-label="{esc(sec['name'])}" style="--accent:{sec['accent']}">
  <div class="sec-head" data-rv>
    <h2>{esc(sec['name'])}</h2>
    <div class="sec-meta"><span>{n} article{'s' if n != 1 else ''}</span>{see_all}</div>
  </div>
  <div class="sec-grid">
{cards}
  </div>
</section>""")

    items_schema = ",\n        ".join(
        json.dumps({
            "@type": "ListItem", "position": i + 1,
            "url": f"{SITE}/blog/{a['slug']}.html", "name": a["title"]
        }, ensure_ascii=False)
        for i, a in enumerate(arts_sorted[:10])
    )

    schema = f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@graph": [
    {{
      "@type": "CollectionPage",
      "@id": "{url}",
      "url": "{url}",
      "name": "The My Villa Journal — The Monitoring Desk",
      "description": "Daily monitoring of California's wildfire, insurance and building-code landscape for luxury construction in Los Angeles.",
      "inLanguage": "en-US",
      "isPartOf": {{ "@type": "WebSite", "url": "{SITE}" }},
      "breadcrumb": {{
        "@type": "BreadcrumbList",
        "itemListElement": [
          {{ "@type": "ListItem", "position": 1, "name": "Home", "item": "{SITE}/" }},
          {{ "@type": "ListItem", "position": 2, "name": "Journal", "item": "{url}" }}
        ]
      }}
    }},
    {{
      "@type": "ItemList",
      "itemListElement": [
        {items_schema}
      ]
    }}
  ]
}}
</script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
{GTAG}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">

{robots_tag(preview)}

<title>The Journal — The Monitoring Desk | My Villa, Los Angeles</title>
<meta name="description" content="Editorial intelligence on California's risk reality: daily monitoring of the wildfire, insurance and building-code landscape that shapes luxury construction in Los Angeles. {total} articles and counting.">
<meta name="keywords" content="California wildfire insurance news, luxury construction Los Angeles, WUI code 2026, IBHS wildfire prepared home, insurable home California, My Villa journal, LA rebuild">
<meta name="author" content="My Villa">
<link rel="canonical" href="{url}">

<meta property="og:type" content="website">
<meta property="og:url" content="{url}">
<meta property="og:title" content="The Journal — The Monitoring Desk | My Villa">
<meta property="og:description" content="Daily monitoring of California's wildfire, insurance and building-code landscape for luxury construction in Los Angeles.">
<meta property="og:image" content="{SITE}/img/hero.png">
<meta property="og:site_name" content="My Villa">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="The Journal — The Monitoring Desk | My Villa">
<meta name="twitter:description" content="Daily monitoring of California's wildfire, insurance and building-code landscape for luxury construction in Los Angeles.">
<meta name="twitter:image" content="{SITE}/img/hero.png">

<link rel="icon" type="image/svg+xml" href="/img/logos/favicon.svg">
{FONTS}
{schema}
<style>{INDEX_CSS}</style>
</head>
<body>

{nav_html(depth=0)}

<main>
<header class="j-hero">
  <div class="j-hero-inner">
    <div class="eyebrow" data-rv>The Journal</div>
    <h1 data-rv style="--d:1">The Monitoring <em>Desk.</em></h1>
    <p data-rv style="--d:2">Editorial intelligence on California's risk reality — daily monitoring of the construction, insurance and code economics that decide what a luxury home in Southern California actually is. We track the signal, and publish only what changes the math.</p>
    <div class="j-stamp" data-rv style="--d:3"><span class="dot" aria-hidden="true"></span>{total} articles &middot; monitored daily</div>
  </div>
</header>

{featured_html}
{latest_html}
{''.join(sec_blocks)}

<div class="bottom-pad"></div>

{cta_band_html(depth=0).replace('class="cta-band a-cta"', 'class="cta-band"')}
</main>

{footer_html(depth=0)}
{REVEAL_JS}
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
# CATEGORY PAGE
# ────────────────────────────────────────────────────────────────────
def render_category(sec_id, arts, preview=True):
    sec = SECTIONS[sec_id]
    sec_arts = sorted([a for a in arts if a["section"] == sec_id],
                      key=lambda a: a["date"], reverse=True)
    n = len(sec_arts)
    url = f"{SITE}/blog/category/{sec_id}.html"
    cards = "\n".join(card_html(a, depth=1) for a in sec_arts)

    schema = f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "CollectionPage",
  "@id": "{url}",
  "url": "{url}",
  "name": "{esc(sec['name'])} — My Villa Journal",
  "inLanguage": "en-US",
  "breadcrumb": {{
    "@type": "BreadcrumbList",
    "itemListElement": [
      {{ "@type": "ListItem", "position": 1, "name": "Home", "item": "{SITE}/" }},
      {{ "@type": "ListItem", "position": 2, "name": "Journal", "item": "{SITE}/blog/" }},
      {{ "@type": "ListItem", "position": 3, "name": "{esc(sec['name'])}", "item": "{url}" }}
    ]
  }}
}}
</script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
{GTAG}
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">

{robots_tag(preview)}

<title>{esc(sec['name'])} — My Villa Journal</title>
<meta name="description" content="All My Villa Journal coverage in {esc(sec['name'])}: {n} articles monitoring California's risk, insurance and construction landscape for luxury homes in Los Angeles.">
<meta name="author" content="My Villa">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:url" content="{url}">
<meta property="og:title" content="{esc(sec['name'])} — My Villa Journal">
<meta property="og:image" content="{SITE}/img/hero.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="/img/logos/favicon.svg">
{FONTS}
{schema}
<style>{INDEX_CSS}</style>
</head>
<body>

{nav_html(depth=1)}

<main>
<header class="j-hero">
  <div class="j-hero-inner">
    <div class="eyebrow" data-rv>The Journal &middot; Section</div>
    <h1 data-rv style="--d:1">{esc(sec['name'])}<em>.</em></h1>
    <p data-rv style="--d:2">{n} article{'s' if n != 1 else ''} from the Monitoring Desk.</p>
  </div>
</header>

<section class="sec-block wrap" style="--accent:{sec['accent']}">
  <div class="sec-head" data-rv>
    <h2>All coverage</h2>
    <div class="sec-meta"><a href="../index.html">&larr; Back to the Journal</a></div>
  </div>
  <div class="sec-grid">
{cards}
  </div>
</section>

<div class="bottom-pad"></div>
{cta_band_html(depth=1).replace('class="cta-band a-cta"', 'class="cta-band"')}
</main>

{footer_html(depth=1)}
{REVEAL_JS}
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
def load_articles():
    arts = []
    for jf in sorted(BLOG_DIR.glob("*.json")):
        if not (BLOG_DIR / (jf.stem + ".html")).exists():
            continue  # unpublished
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [WARN] bad json {jf.name}: {e}", file=sys.stderr)
            continue
        d["slug"] = d.get("slug") or jf.stem
        d["date"] = d.get("_date") or datetime.fromtimestamp(jf.stat().st_mtime).strftime("%Y-%m-%d")
        d["section"] = d.get("_section_id") or d.get("section") or "materials"
        if d["section"] not in SECTIONS:
            d["section"] = "materials"
        d["read_time"] = d.get("read_time_min") or 5
        arts.append(d)
    return arts


def run(root=False, live=False):
    """Build the Journal. root=True writes straight into blog/ (production);
    otherwise into v2/blog/ (staging). live=True drops the noindex flag
    (always implied by root mode). Generated HTML uses relative internal
    links + root-absolute assets, so the same markup works in both trees."""
    preview = not (live or root)
    out_dir = (PROJECT_ROOT / "blog") if root else OUT_DIR
    cat_dir = out_dir / "category"

    arts = load_articles()
    if not arts:
        print("No published articles found — nothing to build.")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    cat_dir.mkdir(parents=True, exist_ok=True)

    # articles
    for a in arts:
        out = out_dir / f"{a['slug']}.html"
        out.write_text(render_article(a, arts, preview=preview), encoding="utf-8")
    print(f"Articles built: {len(arts)} → {out_dir}")

    # index
    (out_dir / "index.html").write_text(render_index(arts, preview=preview), encoding="utf-8")
    print(f"Index built: {out_dir / 'index.html'}")

    # categories (drop stale hubs, then write current ones)
    n_cat = 0
    for stale in cat_dir.glob("*.html"):
        if stale.stem not in SECTIONS:
            stale.unlink()
    for sec_id in SECTIONS:
        if any(a["section"] == sec_id for a in arts):
            (cat_dir / f"{sec_id}.html").write_text(
                render_category(sec_id, arts, preview=preview), encoding="utf-8")
            n_cat += 1
    print(f"Category pages built: {n_cat} → {cat_dir}")

    mode = "PREVIEW (noindex)" if preview else "LIVE (indexable)"
    print(f"Done — mode: {mode}, target: {'blog/ (root)' if root else 'v2/blog/'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Build the v2 Journal from blog/*.json")
    ap.add_argument("--live", action="store_true", help="drop the noindex preview flag")
    ap.add_argument("--root", action="store_true",
                    help="write into blog/ (production target; implies --live)")
    args = ap.parse_args()
    return run(root=args.root, live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
