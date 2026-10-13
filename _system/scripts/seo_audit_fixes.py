#!/usr/bin/env python3
"""seo_audit_fixes.py — one-off (idempotent) hardening of the published Journal
articles after the 2026-10-13 SEO audit (claude-seo plugin checks run locally).

What it does on every blog/<slug>.html (index + category pages excluded):

  1. Hero image: adds intrinsic width/height (CLS), fetchpriority="high"
     (it is the LCP element) and, when assets/img/<slug>-hero.webp exists,
     wraps the <img> in <picture><source type="image/webp"> — og:image and
     the Article schema keep the JPG/PNG (social crawlers).
  2. <title>: drops the " — My Villa Journal" suffix when the tag would exceed
     60 chars (same rule as generate_journal.seo_title_tag).
  3. meta description / og:description / twitter:description / schema
     description: clipped to 160 chars at a sentence or clause boundary
     (generate_journal.clip_meta_description).
  4. Hero credit placeholder "Photo: Publication — via original article":
     "Publication" replaced by the cited source's name (match on URL) or
     the source domain.

With --webp it first converts every hero referenced by an article to WebP
(quality 82, max 1600 px wide) next to the original; originals are kept.

Usage:
  python3 _system/scripts/seo_audit_fixes.py --dry-run
  python3 _system/scripts/seo_audit_fixes.py --webp
"""
from __future__ import annotations

import argparse
import html as html_module
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
IMG_DIR = BLOG_DIR / "assets" / "img"
SKIP = {"index.html"}

# Reuse the exact helpers the generator now uses, so archive == new articles.
_spec = importlib.util.spec_from_file_location("generate_journal", SCRIPT_DIR / "generate_journal.py")
_gj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gj)
seo_title_tag = _gj.seo_title_tag
clip_meta_description = _gj.clip_meta_description
publisher_name_for_url = _gj._publisher_name_for_url

HERO_RE = re.compile(
    r'<figure class="hero-image">(?:<picture>(?:<source[^>]*>)?)?'
    r'(<img src="(assets/img/[^"]+)"([^>]*)>)(?:</picture>)?'
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
DESC_RE = re.compile(r'<meta name="description" content="([^"]*)">')


def esc(t: str) -> str:
    return html_module.escape(str(t), quote=True) if t else ""


def image_dims(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)


def make_webp(src: Path, quality: int = 82, max_w: int = 1600) -> Path | None:
    dst = src.with_suffix(".webp")
    if dst.exists():
        return dst
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
            im.save(dst, "WEBP", quality=quality, method=6)
        if dst.stat().st_size >= src.stat().st_size:   # no gain: don't ship it
            dst.unlink()
            return None
        return dst
    except Exception as e:  # noqa: BLE001
        print(f"  [webp] {src.name}: {type(e).__name__}: {e}")
        return None


def patch_article(path: Path, dry_run: bool) -> dict:
    html = path.read_text(encoding="utf-8")
    orig = html
    changes = []

    # 1. hero image -----------------------------------------------------
    m = HERO_RE.search(html)
    if m:
        img_tag, rel, attrs = m.group(1), m.group(2), m.group(3)
        fs = BLOG_DIR / rel
        a = attrs
        a = re.sub(r'\s+(width|height|fetchpriority)="[^"]*"', "", a)
        w, h = image_dims(fs)
        if w and h:
            a = a + f' width="{w}" height="{h}"'
        if 'loading="eager"' in a:
            a = a.replace('loading="eager"', 'loading="eager" fetchpriority="high"', 1)
        else:
            a = a + ' fetchpriority="high"'
        new_img = f'<img src="{rel}"{a}>'
        webp = fs.with_suffix(".webp")
        if webp.exists() and not rel.lower().endswith(".webp"):
            webp_rel = str(Path(rel).with_suffix(".webp"))
            new_block = f'<figure class="hero-image"><picture><source type="image/webp" srcset="{webp_rel}">{new_img}</picture>'
        else:
            new_block = f'<figure class="hero-image">{new_img}'
        if new_block != m.group(0):
            html = html[:m.start()] + new_block + html[m.end():]
            changes.append("hero")

    # 2. title ----------------------------------------------------------
    tm = TITLE_RE.search(html)
    if tm:
        raw = tm.group(1)
        base = html_module.unescape(re.sub(r"\s*—\s*My Villa Journal\s*$", "", raw))
        new_title = esc(seo_title_tag(base))
        if new_title != raw:
            html = html[:tm.start(1)] + new_title + html[tm.end(1):]
            changes.append(f"title {len(raw)}→{len(html_module.unescape(new_title))}")

    # 3. description ----------------------------------------------------
    dm = DESC_RE.search(html)
    if dm:
        raw = dm.group(1)
        plain = html_module.unescape(raw)
        clipped = clip_meta_description(plain)
        if clipped != plain:
            new_raw = esc(clipped)
            # same string is used for og:/twitter: description and the schema
            n = html.count(raw)
            html = html.replace(raw, new_raw)
            changes.append(f"description {len(plain)}→{len(clipped)} (x{n})")

    # 4. "Publication" credit placeholder --------------------------------
    if "Photo: Publication — via" in html or "Photo: <a" not in html and "Publication</a> — via" in html:
        pass
    cm = re.search(r'<figcaption class="hero-credit">Photo: (?:<a href="([^"]*)"[^>]*>)?Publication(?:</a>)? — via <a href="([^"]*)"', html)
    if cm:
        url = cm.group(2) or cm.group(1) or ""
        sources = []
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                sources = json.load(open(sidecar, encoding="utf-8")).get("sources") or []
            except Exception:
                sources = []
        name = publisher_name_for_url(url, sources)
        if name and name.lower() != "publication":
            seg = cm.group(0)
            new_seg = seg.replace(">Publication</a>", f">{esc(name)}</a>").replace("Photo: Publication —", f"Photo: {esc(name)} —")
            html = html.replace(seg, new_seg, 1)
            changes.append(f"credit→{name}")

    if html != orig and not dry_run:
        path.write_text(html, encoding="utf-8")
    return {"file": path.name, "changes": changes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--webp", action="store_true", help="convert referenced hero images to WebP first")
    args = ap.parse_args()

    files = sorted(p for p in BLOG_DIR.glob("*.html") if p.name not in SKIP)

    if args.webp and not args.dry_run:
        made = kept = 0
        for p in files:
            m = HERO_RE.search(p.read_text(encoding="utf-8"))
            if not m:
                continue
            src = BLOG_DIR / m.group(2)
            if src.suffix.lower() in (".jpg", ".jpeg", ".png") and src.exists():
                if src.with_suffix(".webp").exists():
                    kept += 1
                elif make_webp(src):
                    made += 1
        print(f"WebP: {made} created, {kept} already present")

    stats = {"hero": 0, "title": 0, "description": 0, "credit": 0, "files": 0}
    for p in files:
        r = patch_article(p, args.dry_run)
        if r["changes"]:
            stats["files"] += 1
            for c in r["changes"]:
                stats[c.split()[0].split("→")[0]] += 1
            print(f"  {'[dry] ' if args.dry_run else ''}{p.name}: {', '.join(r['changes'])}")
    print(f"\n{stats['files']} files touched — hero {stats['hero']}, title {stats['title']}, "
          f"description {stats['description']}, credit {stats['credit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
