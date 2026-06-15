#!/usr/bin/env python3
"""
My Villa — Instagram Publisher

Two modes:

STUB MODE (default, no flags): builds a "publish-ready package" — a per-post
folder under _system/social/posts/editorial/_publish_ready/ containing
caption.txt + image + slides.txt + metadata.json, for manual copy-paste
publishing from the Instagram app. Always available as fallback.

LIVE MODE (--publish-live): publishes directly to @myvilla.la via the
Instagram API (graph.instagram.com). Enabled 2026-06-13 after the
IG↔FB-Page link was fixed and the Meta app "MyVilla Publisher"
(id 1338124771778359) was wired with an Instagram-login token.

  Requirements handled automatically:
  - Instagram accepts ONLY JPEG → webp/png get converted via Pillow
  - Aspect ratio must be within 4:5 … 1.91:1 → center-crop if outside
  - image_url must be PUBLIC → images already under /img/ use the live
    site URL (https://myvilla.la/img/…); partner-cache images get copied
    to img/social/, committed, pushed, and polled until the CDN serves
    them (GitHub Pages deploy)

  Env (.env at repo root):
    IG_ACCESS_TOKEN          Instagram-login user token (IGAA…, ~60d)
    IG_BUSINESS_ACCOUNT_ID   17841437849933313 (@myvilla.la)

Usage:
  # Stub package (unchanged):
  python3 publish_instagram.py --draft <file>.md
  python3 publish_instagram.py --month 2026-06 --status approved

  # Real publishing:
  python3 publish_instagram.py --publish-live <file>.md          # publish one draft
  python3 publish_instagram.py --publish-live <file>.md --dry-run  # prepare only, no API call

  # Token health:
  python3 publish_instagram.py --check-token
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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
SOCIAL_IMG_DIR = IMG_DIR / "social"        # public staging for API images
CALENDAR_DIR = SOCIAL_DIR / "calendar"

# ── Instagram API config ─────────────────────────────────────────────
IG_GRAPH = "https://graph.instagram.com/v23.0"
SITE_BASE_URL = "https://myvilla.la"

# Aspect-ratio hard limits for IG image posts
AR_MIN = 0.8        # 4:5 portrait
AR_MAX = 1.91       # 1.91:1 landscape


def load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v


load_dotenv()


# ══════════════════════════════════════════════════════════════════════
# Instagram API client (graph.instagram.com, Instagram-login token)
# ══════════════════════════════════════════════════════════════════════

def _ig_credentials():
    # Opportunistic token refresh: every API-touching call first checks
    # whether the token is >7 days since last refresh and renews it
    # (rewrites .env + os.environ). Never raises — see refresh_ig_token.py.
    try:
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        from refresh_ig_token import maybe_refresh
        maybe_refresh(quiet=True)
    except Exception:
        pass

    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    ig_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "").strip()
    if not token or token.startswith("PLACEHOLDER"):
        raise RuntimeError("IG_ACCESS_TOKEN missing in .env")
    if not ig_id:
        raise RuntimeError("IG_BUSINESS_ACCOUNT_ID missing in .env")
    return token, ig_id


def _api_get(path: str, params: dict) -> dict:
    url = f"{IG_GRAPH}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def _api_post(path: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{IG_GRAPH}/{path}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def check_token(verbose: bool = True) -> bool:
    """Validate token + publishing permission. Returns True if healthy."""
    try:
        token, ig_id = _ig_credentials()
    except RuntimeError as e:
        if verbose:
            print(f"  ✗ {e}")
        return False
    try:
        me = _api_get("me", {"fields": "user_id,username,account_type",
                             "access_token": token})
        quota = _api_get(f"{ig_id}/content_publishing_limit",
                         {"access_token": token})
        usage = quota.get("data", [{}])[0].get("quota_usage", "?")
        if verbose:
            print(f"  ✓ token valid — @{me.get('username')} "
                  f"({me.get('account_type')}) · quota 24h: {usage}/100")
        return True
    except urllib.error.HTTPError as e:
        if verbose:
            body = e.read().decode(errors="replace")[:300]
            print(f"  ✗ API error HTTP {e.code}: {body}")
        return False


def _strip_unverified_handles(text):
    """Remove @mentions that aren't ours or operator-verified (the generators
    sometimes invent source handles like @eaaorg). Best-effort."""
    try:
        from verify_handles import sanitize
        clean, removed = sanitize(text)
        if removed:
            print(f"  [handles] stripped unverified: "
                  f"{', '.join('@' + r for r in removed)}")
        return clean
    except Exception:  # noqa: BLE001
        return text


def api_publish_image(image_url: str, caption: str,
                      timeout_s: int = 120) -> dict:
    """Create a media container for image_url + caption, wait for it to be
    ready, then publish. Returns {'media_id':…, 'permalink':…}."""
    token, ig_id = _ig_credentials()
    caption = _strip_unverified_handles(caption)

    # 1. Create container
    container = _api_post(f"{ig_id}/media", {
        "image_url": image_url,
        "caption": caption,
        "access_token": token,
    })
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError(f"container creation failed: {container}")

    # 2. Poll container status until FINISHED (image fetch is usually fast)
    deadline = time.time() + timeout_s
    status = ""
    while time.time() < deadline:
        info = _api_get(creation_id, {"fields": "status_code",
                                      "access_token": token})
        status = info.get("status_code", "")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError(f"container {creation_id} entered ERROR state "
                               f"(bad image_url / format / aspect ratio?)")
        time.sleep(3)
    if status != "FINISHED":
        raise RuntimeError(f"container {creation_id} not ready after "
                           f"{timeout_s}s (status={status!r})")

    # 3. Publish
    pub = _api_post(f"{ig_id}/media_publish", {
        "creation_id": creation_id,
        "access_token": token,
    })
    media_id = pub.get("id")
    if not media_id:
        raise RuntimeError(f"media_publish failed: {pub}")

    # 4. Fetch permalink for the dashboard / draft record
    permalink = ""
    try:
        meta = _api_get(media_id, {"fields": "permalink",
                                   "access_token": token})
        permalink = meta.get("permalink", "")
    except Exception:
        pass

    return {"media_id": media_id, "permalink": permalink}


# ══════════════════════════════════════════════════════════════════════
# Image preparation — JPEG + aspect-ratio + public URL
# ══════════════════════════════════════════════════════════════════════

def prepare_image_file(src: Path, slug: str) -> Path:
    """Convert to JPEG (sRGB) and center-crop into IG's allowed aspect-ratio
    window if needed. Writes to img/social/<slug>.jpg and returns the path."""
    from PIL import Image

    SOCIAL_IMG_DIR.mkdir(parents=True, exist_ok=True)
    dst = SOCIAL_IMG_DIR / f"{slug}.jpg"

    img = Image.open(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    w, h = img.size
    ar = w / h
    if ar < AR_MIN:           # too tall → crop height
        new_h = int(w / AR_MIN)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ar > AR_MAX:         # too wide → crop width
        new_w = int(h * AR_MAX)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))

    # IG max 8 MB / recommended max width 1440px
    if img.width > 1440:
        ratio = 1440 / img.width
        img = img.resize((1440, int(img.height * ratio)), Image.LANCZOS)

    img.save(dst, "JPEG", quality=90, optimize=True)
    return dst


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT_DIR),
                          capture_output=True, text=True, timeout=120)


def ensure_public_url(local_jpg: Path, *, push: bool = True,
                      wait_timeout_s: int = 300) -> str:
    """Make img/social/<file>.jpg publicly reachable on the live site.
    Commits + pushes the file, then polls the URL until HTTP 200."""
    rel = local_jpg.relative_to(ROOT_DIR)
    public_url = f"{SITE_BASE_URL}/{rel.as_posix()}"

    # Already live? (e.g. re-publish attempt)
    if _url_is_live(public_url):
        return public_url

    if push:
        _git("add", str(rel))
        commit = _git("commit", "-m",
                      f"social: publish image {local_jpg.name} for IG API")
        # commit may fail if nothing to commit — that's fine if file is
        # already committed but not yet deployed
        push_res = _git("push")
        if push_res.returncode != 0:
            raise RuntimeError(f"git push failed: {push_res.stderr[:300]}")

    deadline = time.time() + wait_timeout_s
    while time.time() < deadline:
        if _url_is_live(public_url):
            return public_url
        time.sleep(10)
    raise RuntimeError(f"{public_url} not live after {wait_timeout_s}s "
                       f"(GitHub Pages deploy slow?)")


def _url_is_live(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


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
        caption, tail = "\n\n".join(parts[:-1]).strip(), parts[-1].strip()
    else:
        caption, tail = body.strip(), ""
    return _strip_unverified_handles(caption), tail


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
# LIVE publishing flow
# ══════════════════════════════════════════════════════════════════════

def publish_live(draft_filename: str, *, dry_run: bool = False) -> bool:
    """Publish one draft to @myvilla.la via the Instagram API.

    Steps: parse draft → resolve local image → JPEG+AR prep → ensure the
    image is publicly reachable (site URL or commit+push to img/social/) →
    container → publish → mark_published with the real permalink.
    """
    src = DRAFTS_DIR / draft_filename
    if not src.exists():
        print(f"  ✗ Draft not found: {src}")
        return False
    fm, body = parse_draft(src)
    if not fm:
        print(f"  ✗ Draft has no frontmatter")
        return False

    if fm.get("format") == "carousel" or fm.get("slides"):
        print(f"  ✗ Carousel publishing not implemented yet — use the stub "
              f"package + manual publish for carousels.")
        return False

    caption, hashtag_line = split_caption_and_hashtags(body)
    full_caption = caption + ("\n\n" + hashtag_line if hashtag_line else "")

    # Resolve local image path (same logic as build_package)
    img_filename = fm.get("image_filename", "")
    img_web_path = (fm.get("image_web_path") or "").lstrip("/")
    candidates = []
    if img_web_path:
        candidates.append(ROOT_DIR / img_web_path)
    if img_filename:
        candidates.append(IMG_DIR / img_filename)
    local_src = next((p for p in candidates if p.exists()), None)
    if local_src is None:
        print(f"  ✗ Image not found locally: {candidates}")
        return False

    slug = fm.get("slug") or src.stem
    print(f"  → image: {local_src.relative_to(ROOT_DIR)}")

    # JPEG + aspect-ratio prep into img/social/
    jpg = prepare_image_file(local_src, f"{fm.get('date','')}-{slug}")
    print(f"  → prepared: {jpg.relative_to(ROOT_DIR)}")

    if dry_run:
        print(f"  [dry-run] caption ({len(full_caption)} chars):")
        print("  " + full_caption.replace("\n", "\n  ")[:400])
        print(f"  [dry-run] would push + publish via API")
        return True

    # Public URL (commit+push if not yet live)
    public_url = ensure_public_url(jpg)
    print(f"  → public: {public_url}")

    # API publish
    result = api_publish_image(public_url, full_caption)
    permalink = result.get("permalink") or f"media_id:{result['media_id']}"
    print(f"  ✓ PUBLISHED → {permalink}")

    # State transition (moves draft, updates calendar)
    mark_published(draft_filename, ig_post_url=permalink)
    return True


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="My Villa — IG Publisher")
    parser.add_argument("--draft", help="Specific draft filename in _drafts/social_editorial/")
    parser.add_argument("--month", help="YYYY-MM — process all drafts for that month")
    parser.add_argument("--status", default="draft",
                        help="Filter by status when --month is set (default: draft)")
    parser.add_argument("--mark-published", help="Mark a draft as published (filename)")
    parser.add_argument("--ig-post-url", default="",
                        help="Instagram post URL (used with --mark-published)")
    parser.add_argument("--publish-live",
                        help="Publish a draft directly to Instagram via API (filename)")
    parser.add_argument("--check-token", action="store_true",
                        help="Validate IG_ACCESS_TOKEN + publishing quota")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    live = bool(args.publish_live or args.check_token)
    print(f"\nMy Villa — IG Publisher ({'LIVE' if live else 'STUB'} MODE)")
    print(f"{'=' * 50}")

    # Mode 0: token health check
    if args.check_token:
        ok = check_token()
        sys.exit(0 if ok else 1)

    # Mode L: live publish via API
    if args.publish_live:
        ok = publish_live(args.publish_live, dry_run=args.dry_run)
        sys.exit(0 if ok else 1)

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
