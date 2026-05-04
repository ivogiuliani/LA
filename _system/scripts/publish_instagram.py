#!/usr/bin/env python3
"""
My Villa — Instagram Publisher (STUB MODE)

Phase 1 of the editorial pipeline. Until the Meta Graph API integration is
wired (depends on IG↔FB Page link being live), this script does NOT publish
to Instagram. Instead it builds a "publish-ready package" — a per-post folder
under _system/social/posts/editorial/_publish_ready/ containing:

  caption.txt       — final caption + hashtag line, ready to paste into the IG app
  image.<ext>       — the picked image (single-image post)
  carousel/01.<ext> — per-slide images (carousel post)
  slides.txt        — per-slide headlines + body text (for designer / canva)
  metadata.json     — full slot metadata + hashtags + scheduled time

The package lives in iCloud Drive, so it shows up on the iPhone where the
user can copy/paste into the Instagram app.

When Meta Graph API is enabled later, this script will be extended with a
real publish() function. The stub stays useful as a fallback / preview.

Usage:
  python3 publish_instagram.py --draft 2026-05-04-ig-editorial-two-millennia-...md
  python3 publish_instagram.py --month 2026-05 --status draft   # all drafts in May
  python3 publish_instagram.py --month 2026-05 --status approved
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
DRAFTS_DIR = ROOT_DIR / "_drafts" / "social_editorial"
SOCIAL_DIR = SYSTEM_DIR / "social"
EDITORIAL_DIR = SOCIAL_DIR / "posts" / "editorial"
PUBLISH_READY_DIR = EDITORIAL_DIR / "_publish_ready"
IMG_DIR = ROOT_DIR / "img"
CALENDAR_DIR = SOCIAL_DIR / "calendar"


# ══════════════════════════════════════════════════════════════════════
# Draft parsing
# ══════════════════════════════════════════════════════════════════════

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def parse_draft(path: Path):
    """Parse a draft .md file. Returns (frontmatter_dict, body_str)."""
    raw = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return None, raw
    fm = yaml.safe_load(m.group(1))
    body = m.group(2).strip()
    return fm, body


def split_caption_and_hashtags(body: str):
    """Split body into (caption_text, hashtag_line). Hashtag line is the
    last line that starts with # (after a blank line)."""
    parts = body.rstrip().split("\n\n")
    if len(parts) >= 2 and parts[-1].lstrip().startswith("#"):
        return "\n\n".join(parts[:-1]).strip(), parts[-1].strip()
    return body.strip(), ""


# ══════════════════════════════════════════════════════════════════════
# Package building
# ══════════════════════════════════════════════════════════════════════

def build_package(draft_path: Path, dry_run: bool = False):
    """Build a publish-ready package for a single draft.
    Returns the package path or None on error."""
    fm, body = parse_draft(draft_path)
    if not fm:
        print(f"  ✗ {draft_path.name}: no frontmatter")
        return None

    slug = fm.get("slug") or draft_path.stem
    date_str = fm.get("date", "")
    fmt = fm.get("format", "single_image")
    pkg_name = f"{date_str}-{slug}"
    pkg_dir = PUBLISH_READY_DIR / pkg_name

    if dry_run:
        print(f"  [dry-run] Package → {pkg_dir.relative_to(ROOT_DIR)}")
        return pkg_dir

    pkg_dir.mkdir(parents=True, exist_ok=True)

    # 1. caption.txt
    caption, hashtag_line = split_caption_and_hashtags(body)
    caption_txt = caption + ("\n\n" + hashtag_line if hashtag_line else "")
    (pkg_dir / "caption.txt").write_text(caption_txt + "\n")

    # 2. image — resolve via image_web_path first (works for partner-cache
    #    images outside /img/), fall back to IMG_DIR / image_filename for
    #    legacy drafts.
    img_filename = fm.get("image_filename", "")
    img_web_path = (fm.get("image_web_path") or "").lstrip("/")
    image_copied = False
    image_warning = None

    src_candidates = []
    if img_web_path:
        src_candidates.append(ROOT_DIR / img_web_path)
    if img_filename:
        src_candidates.append(IMG_DIR / img_filename)

    src = next((p for p in src_candidates if p.exists()), None)
    if src:
        ext = src.suffix or ".jpg"
        dst = pkg_dir / f"image{ext}"
        shutil.copy2(src, dst)
        image_copied = True
    else:
        # Hard failure signal — package is built, but the image is missing
        # and the user must know before they try to publish.
        image_warning = (
            f"IMAGE NOT FOUND. Looked at: " +
            ", ".join(str(p) for p in src_candidates) +
            ". Caption + slides are still usable; supply image manually."
        )
        print(f"  ⚠ {image_warning}")

    # 3. slides.txt + per-slide images (carousel format)
    is_carousel = fmt == "carousel" or fm.get("slides")
    if is_carousel and fm.get("slides"):
        slides = fm["slides"]
        carousel_dir = pkg_dir / "carousel"
        carousel_dir.mkdir(exist_ok=True)

        slides_text = []
        for i, s in enumerate(slides, start=1):
            num = f"{i:02d}"
            slides_text.append(f"--- SLIDE {num}: {s.get('headline', '')} ---")
            slides_text.append(s.get("body", "").strip())
            slides_text.append("")

            # Slide 1 reuses the resolved cover image (whether from /img/
            # or partner cache); middles stay placeholders for now.
            if i == 1 and image_copied and src is not None:
                shutil.copy2(src, carousel_dir / f"{num}{src.suffix or '.jpg'}")
            else:
                (carousel_dir / f"{num}-PLACEHOLDER.txt").write_text(
                    f"Slide {i} — {s.get('headline', '')}\n\n"
                    f"BODY: {s.get('body', '')}\n\n"
                    f"VISUAL TODO: render or pick an image for this slide.\n"
                )

        (pkg_dir / "slides.txt").write_text("\n".join(slides_text))

    # 4. metadata.json
    metadata = {
        "draft_file": draft_path.name,
        "package_built_at": datetime.now().isoformat(timespec="seconds"),
        "publish_target": {
            "platform": "instagram",
            "account": "@myvilla.la",
            "scheduled_date": fm.get("date"),
            "scheduled_time": fm.get("scheduled_time", "09:00"),
            "timezone": fm.get("timezone", "America/Los_Angeles"),
        },
        "post": {
            "pillar": fm.get("pillar"),
            "sub_topic": fm.get("sub_topic"),
            "format": fmt,
            "char_count": fm.get("char_count"),
            "hashtags": fm.get("hashtags", []),
            "image_filename": img_filename,
            "image_alt": fm.get("image_alt", ""),
            "topic_tags": fm.get("topic_tags", []),
        },
        "publishing_mode": "STUB",
        "instructions": [
            "Open the package folder in Files app on iPhone (iCloud Drive).",
            "Copy caption.txt content (long-press → Copy All).",
            "Open Instagram app → New post → upload image.<ext> (or carousel/* for carousel).",
            "Paste caption. Verify hashtags. Tap Share.",
            "After publishing, run: python3 _system/scripts/publish_instagram.py --mark-published <draft>",
        ],
    }

    if fm.get("partner_handle"):
        metadata["post"]["partner_handle"] = fm["partner_handle"]
        metadata["instructions"].insert(
            3, "Verify partner handle is mentioned with @ in caption."
        )

    if image_warning:
        metadata["image_warning"] = image_warning
        metadata["instructions"].insert(
            0, "⚠ IMAGE MISSING — supply manually before publishing"
        )

    (pkg_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False)
    )

    status = "⚠" if image_warning else "✓"
    print(f"  {status} Package → {pkg_dir.relative_to(ROOT_DIR)}")
    return pkg_dir


# ══════════════════════════════════════════════════════════════════════
# State transition: mark-published
# ══════════════════════════════════════════════════════════════════════

def mark_published(draft_filename: str, ig_post_url: str = "", dry_run: bool = False):
    """Move a draft to _system/social/posts/editorial/published/ and update
    the calendar slot's status to 'published'."""
    src = DRAFTS_DIR / draft_filename
    if not src.exists():
        print(f"  ✗ Draft not found: {src}")
        return False

    fm, body = parse_draft(src)
    if not fm:
        print(f"  ✗ Draft has no frontmatter: {src}")
        return False

    fm["status"] = "published"
    fm["published_at"] = datetime.now().isoformat(timespec="seconds")
    if ig_post_url:
        fm["ig_post_url"] = ig_post_url

    if dry_run:
        print(f"  [dry-run] Would mark published: {draft_filename}")
        return True

    # Write to published/
    dst_dir = EDITORIAL_DIR / "published"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / draft_filename

    new_md = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body + "\n"
    dst.write_text(new_md)

    # Remove from drafts
    src.unlink()

    # Update calendar slot
    date_str = fm.get("date", "")
    if date_str:
        month = date_str[:7]
        cal_path = CALENDAR_DIR / f"{month}.yml"
        if cal_path.exists():
            cal = yaml.safe_load(cal_path.read_text())
            for s in cal.get("slots", []):
                if s.get("date") == date_str and s.get("slug") == fm.get("slug"):
                    s["status"] = "published"
                    s["published_at"] = fm["published_at"]
                    if ig_post_url:
                        s["ig_post_url"] = ig_post_url
            cal_path.write_text(yaml.safe_dump(cal, sort_keys=False, allow_unicode=True))

    print(f"  ✓ Marked published: {draft_filename}")
    print(f"    Moved to: {dst.relative_to(ROOT_DIR)}")
    return True


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="My Villa — IG Publisher (stub)")
    parser.add_argument("--draft", help="Specific draft filename in _drafts/social_editorial/")
    parser.add_argument("--month", help="YYYY-MM — process all drafts for that month")
    parser.add_argument("--status", default="draft",
                        help="Filter by status when --month is set (default: draft)")
    parser.add_argument("--mark-published", help="Mark a draft as published (filename)")
    parser.add_argument("--ig-post-url", default="",
                        help="Instagram post URL (used with --mark-published)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"\nMy Villa — IG Publisher (STUB MODE)")
    print(f"{'=' * 50}")

    # Mode A: mark a draft as published (state transition only)
    if args.mark_published:
        ok = mark_published(args.mark_published, args.ig_post_url, args.dry_run)
        sys.exit(0 if ok else 1)

    # Mode B: build package(s)
    if args.draft:
        draft_path = DRAFTS_DIR / args.draft
        if not draft_path.exists():
            print(f"  ✗ Draft not found: {draft_path}")
            sys.exit(1)
        targets = [draft_path]
    elif args.month:
        # All drafts for that month with matching status
        targets = []
        for f in sorted(DRAFTS_DIR.glob(f"{args.month}-*.md")):
            fm, _ = parse_draft(f)
            if fm and fm.get("status") == args.status:
                targets.append(f)
        print(f"  Filter: month={args.month} status={args.status}")
    else:
        parser.error("Specify --draft, --month, or --mark-published")

    if not targets:
        print(f"  No matching drafts found.")
        return

    print(f"  Drafts to package: {len(targets)}\n")

    success = 0
    for d in targets:
        if build_package(d, dry_run=args.dry_run):
            success += 1

    print(f"\n  ✓ {success}/{len(targets)} packages built")
    print(f"  Open: {PUBLISH_READY_DIR.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
