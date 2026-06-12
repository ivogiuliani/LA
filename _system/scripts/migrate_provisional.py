#!/usr/bin/env python3
"""
My Villa — one-off migration: @myvilla__ (provisional SMM page) → @myvilla.la

Reads the Apify scrape at _system/social/migration/myvilla__-raw.json,
downloads every media file, re-hosts them on the live site (img/social/
migration/ via commit+push), then republishes every post on @myvilla.la
in the ORIGINAL chronological order (oldest first) with the original
captions.

Supports:
  - single Image posts        → image container
  - Sidecar (carousel) posts  → child containers + CAROUSEL container
  - Video slides in carousels → media_type=VIDEO child (with poll)

Dedup: _system/social/migration/migrated.json maps shortcode → permalink;
already-migrated posts are skipped, so the script is safe to re-run.

Usage:
  python3 migrate_provisional.py --download        # fetch media only
  python3 migrate_provisional.py --publish         # full run (download+host+publish)
  python3 migrate_provisional.py --publish --only DYwd24YC6Oe   # one post
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from publish_instagram import (  # noqa: E402
    ROOT_DIR, IMG_DIR, _api_get, _api_post, _ig_credentials,
    _git, _url_is_live, SITE_BASE_URL, AR_MIN, AR_MAX,
)

MIGRATION_DIR = ROOT_DIR / "_system" / "social" / "migration"
RAW_FILE = MIGRATION_DIR / "myvilla__-raw.json"
MEDIA_DIR = MIGRATION_DIR / "media"
PUBLIC_DIR = IMG_DIR / "social" / "migration"     # re-host location (public)
MIGRATED_FILE = MIGRATION_DIR / "migrated.json"

PAUSE_BETWEEN_POSTS_S = 12
VIDEO_POLL_TIMEOUT_S = 300


# ── helpers ──────────────────────────────────────────────────────────

def _download(url: str, dst: Path) -> bool:
    if dst.exists() and dst.stat().st_size > 1024:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (My Villa migration)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            dst.write_bytes(r.read())
        return True
    except Exception as e:
        print(f"    ⚠ download failed: {type(e).__name__}: {e}")
        return False


def prep_image(src: Path, dst: Path):
    """JPEG + center-crop to IG aspect window (same logic as publisher)."""
    from PIL import Image
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    ar = w / h
    if ar < AR_MIN:
        new_h = int(w / AR_MIN)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ar > AR_MAX:
        new_w = int(h * AR_MAX)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    if img.width > 1440:
        ratio = 1440 / img.width
        img = img.resize((1440, int(img.height * ratio)), Image.LANCZOS)
    img.save(dst, "JPEG", quality=90, optimize=True)


def load_migrated() -> dict:
    if MIGRATED_FILE.exists():
        return json.loads(MIGRATED_FILE.read_text())
    return {}


def save_migrated(state: dict):
    MIGRATED_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _poll_container(creation_id: str, token: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    status = ""
    while time.time() < deadline:
        info = _api_get(creation_id, {"fields": "status_code",
                                      "access_token": token})
        status = info.get("status_code", "")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"container {creation_id} → ERROR")
        time.sleep(4)
    raise RuntimeError(f"container {creation_id} not FINISHED after "
                       f"{timeout_s}s (status={status!r})")


# ── per-post pipeline ────────────────────────────────────────────────

def collect_media(post: dict) -> list:
    """Return [{kind, url, local, public_name}, …] for a post."""
    sc = post["shortCode"]
    items = []
    if post.get("type") == "Sidecar":
        for i, c in enumerate(post.get("childPosts", []) or [], 1):
            if c.get("type") == "Video" and c.get("videoUrl"):
                items.append({
                    "kind": "video",
                    "url": c["videoUrl"],
                    "local": MEDIA_DIR / f"{sc}-{i}.mp4",
                    "public_name": f"{sc}-{i}.mp4",
                })
            else:
                items.append({
                    "kind": "image",
                    "url": c.get("displayUrl", ""),
                    "local": MEDIA_DIR / f"{sc}-{i}.jpg",
                    "public_name": f"{sc}-{i}.jpg",
                })
    else:
        items.append({
            "kind": "image",
            "url": post.get("displayUrl", ""),
            "local": MEDIA_DIR / f"{sc}.jpg",
            "public_name": f"{sc}.jpg",
        })
    return items


def publish_post(post: dict, token: str, ig_id: str) -> dict:
    """Publish one post (image or carousel). Media must already be live.
    Returns {'media_id':…, 'permalink':…}."""
    caption = post.get("caption") or ""
    media = collect_media(post)

    if post.get("type") != "Sidecar":
        url = f"{SITE_BASE_URL}/img/social/migration/{media[0]['public_name']}"
        container = _api_post(f"{ig_id}/media", {
            "image_url": url, "caption": caption, "access_token": token,
        })
        cid = container.get("id")
        if not cid:
            raise RuntimeError(f"container failed: {container}")
        _poll_container(cid, token, 120)
    else:
        # 1. child containers
        child_ids = []
        for m in media:
            url = f"{SITE_BASE_URL}/img/social/migration/{m['public_name']}"
            if m["kind"] == "video":
                c = _api_post(f"{ig_id}/media", {
                    "media_type": "VIDEO", "video_url": url,
                    "is_carousel_item": "true", "access_token": token,
                })
            else:
                c = _api_post(f"{ig_id}/media", {
                    "image_url": url, "is_carousel_item": "true",
                    "access_token": token,
                })
            cid = c.get("id")
            if not cid:
                raise RuntimeError(f"child container failed: {c}")
            _poll_container(cid, token,
                            VIDEO_POLL_TIMEOUT_S if m["kind"] == "video" else 120)
            child_ids.append(cid)
        # 2. carousel container
        container = _api_post(f"{ig_id}/media", {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": token,
        })
        cid = container.get("id")
        if not cid:
            raise RuntimeError(f"carousel container failed: {container}")
        _poll_container(cid, token, 120)

    pub = _api_post(f"{ig_id}/media_publish", {
        "creation_id": cid, "access_token": token,
    })
    media_id = pub.get("id")
    if not media_id:
        raise RuntimeError(f"publish failed: {pub}")
    permalink = ""
    try:
        meta = _api_get(media_id, {"fields": "permalink", "access_token": token})
        permalink = meta.get("permalink", "")
    except Exception:
        pass
    return {"media_id": media_id, "permalink": permalink}


# ── main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Migrate @myvilla__ → @myvilla.la")
    ap.add_argument("--download", action="store_true", help="Download media only")
    ap.add_argument("--publish", action="store_true", help="Full migration")
    ap.add_argument("--only", help="Restrict to one shortcode")
    args = ap.parse_args()
    if not (args.download or args.publish):
        ap.error("specify --download or --publish")

    raw = json.loads(RAW_FILE.read_text())
    posts = sorted(raw, key=lambda p: p.get("timestamp", ""))
    if args.only:
        posts = [p for p in posts if p.get("shortCode") == args.only]

    migrated = load_migrated()

    # 1. download all media
    print(f"\n[1/4] Download media ({len(posts)} posts)")
    for p in posts:
        for m in collect_media(p):
            ok = _download(m["url"], m["local"])
            print(f"  {'✓' if ok else '✗'} {m['local'].name} ({m['kind']})")
            if not ok:
                sys.exit(1)

    if args.download:
        print("\nDownload-only run complete.")
        return

    # 2. prep + stage public copies
    print(f"\n[2/4] Prepare + stage to img/social/migration/")
    staged = []
    for p in posts:
        if p["shortCode"] in migrated:
            continue
        for m in collect_media(p):
            dst = PUBLIC_DIR / m["public_name"]
            if m["kind"] == "image":
                prep_image(m["local"], dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(m["local"].read_bytes())
            staged.append(dst)
            print(f"  → {dst.relative_to(ROOT_DIR)}")

    if not staged and all(p["shortCode"] in migrated for p in posts):
        print("  (everything already migrated — nothing to do)")
        return

    # 3. commit + push + wait live
    print(f"\n[3/4] Publish media files to the live site")
    _git("add", "img/social/migration")
    _git("commit", "-m", "social: stage media for @myvilla__ migration")
    push = _git("push", "origin", "main")
    if push.returncode != 0 and "rejected" in (push.stderr or ""):
        _git("pull", "--rebase", "origin", "main")
        push = _git("push", "origin", "main")
    if push.returncode != 0:
        print(f"  ✗ git push failed: {push.stderr[:300]}")
        sys.exit(1)
    first_url = f"{SITE_BASE_URL}/img/social/migration/{staged[0].name}" if staged else ""
    if first_url:
        print(f"  waiting for deploy… ({first_url})")
        deadline = time.time() + 300
        while time.time() < deadline and not _url_is_live(first_url):
            time.sleep(10)
        if not _url_is_live(first_url):
            print("  ✗ deploy not live after 300s")
            sys.exit(1)
        print("  ✓ live")

    # 4. publish in chronological order
    print(f"\n[4/4] Republish on @myvilla.la (oldest first)")
    token, ig_id = _ig_credentials()
    for p in posts:
        sc = p["shortCode"]
        if sc in migrated:
            print(f"  · {sc} already migrated → {migrated[sc]['permalink']}")
            continue
        cap_preview = (p.get("caption") or "")[:50].replace("\n", " ")
        print(f"  → {sc} ({p.get('type')}) — {cap_preview}…")
        try:
            result = publish_post(p, token, ig_id)
        except Exception as e:
            print(f"    ✗ FAILED: {e}")
            print(f"    (migrated so far saved; re-run to resume)")
            save_migrated(migrated)
            sys.exit(1)
        migrated[sc] = {
            "media_id": result["media_id"],
            "permalink": result["permalink"],
            "original_timestamp": p.get("timestamp"),
            "type": p.get("type"),
        }
        save_migrated(migrated)
        print(f"    ✓ {result['permalink'] or result['media_id']}")
        time.sleep(PAUSE_BETWEEN_POSTS_S)

    print(f"\n✓ Migration complete — {len(migrated)} posts on @myvilla.la")


if __name__ == "__main__":
    main()
