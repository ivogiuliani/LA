#!/usr/bin/env python3
"""
My Villa — Instagram Viral Comment Radar

Discovers high-engagement Instagram posts in My Villa's niche (luxury LA real
estate, architecture, fire-resilient / concrete construction, Italian villa
design), scores them for relevance, and drafts a brand-aligned comment for
each so the team can copy-paste and engage in seconds.

Mirrors the X/Reddit "Viral Opportunities" flow but for Instagram — where the
Graph API does NOT allow commenting on arbitrary third-party posts, so the
workflow is: discover → draft comment → human copies + pastes on instagram.com.

Pipeline:
  1. Scrape configured hashtags via Apify (apify~instagram-hashtag-scraper)
  2. Heuristic pre-filter (engagement threshold + on-niche keywords) — cheap,
     keeps Claude spend low
  3. Claude Haiku (VISION) scores each survivor 0-10 for relevance AND, in the
     same call, drafts a comment + classifies the engagement angle. The post
     IMAGE is sent with the caption so judgement is on what's actually built,
     not the seller's pitch. Two hard brand rules: we comment on others' posts
     ONLY as a positive peer — never critical/cautionary (don't scare a
     seller's buyers), and never praise a quality the post doesn't have (don't
     congratulate a visibly wood-framed build as fire-resilient/concrete). When
     we can't be honestly positive AND on-brand → skip.
  4. Download thumbnails for the dashboard
  5. Dedup against history (already-surfaced shortcodes never reappear)
  6. Write _system/social/viral/ig_viral_<YYYY-MM-DD>.json + update the
     "latest" pointer the dashboard reads

Config: _system/config/radar-keywords.yml → instagram:
  hashtags, results_per_hashtag, min_likes, min_comments

Usage:
  python3 ig_viral_radar.py                         # full run, all hashtags
  python3 ig_viral_radar.py --hashtag malibu        # one hashtag
  python3 ig_viral_radar.py --limit 10              # cap posts/hashtag
  python3 ig_viral_radar.py --min-relevance 7       # keep only 7+/10
  python3 ig_viral_radar.py --offline               # reuse cache, just re-score
  python3 ig_viral_radar.py --dry-run               # no writes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
HISTORY_DIR = SYSTEM_DIR / "history"
VIRAL_DIR = SYSTEM_DIR / "social" / "viral"
THUMBS_DIR = VIRAL_DIR / "thumbs"

CONFIG_FILE = CONFIG_DIR / "radar-keywords.yml"
SEEN_FILE = HISTORY_DIR / "ig_viral_seen.json"

APIFY_ACTOR = "apify~instagram-hashtag-scraper"
APIFY_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"

DEFAULT_MAX_AGE_DAYS = 21
DEFAULT_MIN_RELEVANCE = 6
SCORING_MODEL = "claude-haiku-4-5"

# Niche keyword anchors for the cheap pre-filter (any hit → send to Claude).
# Broadened 2026-06-14 to cover all 6 conversation areas the target reads:
# real estate, design/architecture, fire/insurance/rebuild, villa typology,
# lifestyle/Malibu, coastal living.
NICHE_KEYWORDS = (
    # architecture / construction / materials
    "concrete", "calcestruzzo", "cemento", "reinforced", "architect",
    "architettura", "architecture", "villa", "modernist", "modern home",
    "custom home", "new build", "new construction", "build", "builder",
    "construction", "renovation", "remodel", "courtyard", "facade", "stucco",
    "tadao", "ando", "brutalist", "minimalist", "design", "designer",
    "interior", "interiors", "structural", "steel", "glass", "timber",
    # luxury real estate / market
    "luxury home", "luxury real estate", "mansion", "estate", "compound",
    "listing", "for sale", "just sold", "real estate", "property", "realtor",
    "price per", "median", "market", "investment", "asset",
    # fire / insurance / rebuild
    "fire", "wildfire", "ember", "insurance", "insurable", "fair plan",
    "resilient", "resilience", "rebuild", "rebuilding", "hardening",
    "defensible", "non-combustible", "fire-resistant", "fire safe",
    "palisades", "altadena", "eaton",
    # villa typology / italian-mediterranean
    "italian", "mediterranean", "tuscan", "courtyard", "pergola", "loggia",
    # geography
    "malibu", "beverly hills", "bel air", "pacific palisades", "brentwood",
    "calabasas", "montecito", "los angeles", "westside", "trousdale",
    "hollywood hills", "santa monica",
    # lifestyle / coastal living (target audience signals)
    "lifestyle", "coastal", "beachfront", "oceanfront", "canyon view",
    "indoor outdoor", "wellness", "sustainable living", "dream home",
    "home tour", "where i live", "california living", "luxury living",
)


# ── .env ─────────────────────────────────────────────────────────────

def load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v and (k not in os.environ or not os.environ[k]):
            os.environ[k] = v


load_dotenv()


# ── Apify ────────────────────────────────────────────────────────────

def apify_scrape_hashtag(tag: str, limit: int, timeout: int = 240) -> list:
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_API_TOKEN not set")
    url = f"{APIFY_URL}?token={token}"
    payload = json.dumps({
        "hashtags": [tag.lstrip("#")],
        "resultsLimit": limit,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Apify HTTP {e.code}: {e.read().decode()[:200]}")
    return data if isinstance(data, list) else []


# ── Shaping + pre-filter ─────────────────────────────────────────────

def _parse_ts(post: dict):
    ts = post.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def shape(post: dict, source_tag: str) -> dict | None:
    sc = post.get("shortCode")
    if not sc:
        return None
    likes = post.get("likesCount")
    likes = None if likes in (None, -1) else int(likes)
    comments = int(post.get("commentsCount") or 0)
    ts = _parse_ts(post)
    age = (datetime.now(timezone.utc) - ts).days if ts else None
    caption = (post.get("caption") or "").strip()
    img = post.get("displayUrl") or ""
    return {
        "shortcode": sc,
        "url": post.get("url") or f"https://www.instagram.com/p/{sc}/",
        "owner": post.get("ownerUsername") or "",
        "owner_name": post.get("ownerFullName") or "",
        "likes": likes,
        "comments": comments,
        "caption": caption,
        "caption_excerpt": (caption[:240] + "…") if len(caption) > 240 else caption,
        "image_url": img,
        "type": post.get("type", ""),
        "timestamp_iso": ts.isoformat() if ts else None,
        "age_days": age,
        "source_hashtag": source_tag,
        "location": post.get("locationName") or "",
        # filled later:
        "relevance_score": None,
        "angle": None,
        "comment": None,
        "skip_reason": None,
        "local_thumbnail": None,
    }


def passes_prefilter(p: dict, min_likes: int, min_comments: int,
                     max_age_days: int) -> bool:
    if p["age_days"] is not None and p["age_days"] > max_age_days:
        return False
    # Engagement: likes may be hidden (None) → require comments threshold then
    likes_ok = (p["likes"] is None) or (p["likes"] >= min_likes)
    eng_ok = (p["likes"] is not None and p["likes"] >= min_likes) or \
             (p["comments"] >= min_comments)
    if not eng_ok and not likes_ok:
        return False
    # Niche keyword presence in caption or hashtag
    blob = (p["caption"] + " " + p["source_hashtag"] + " " + p["location"]).lower()
    return any(kw in blob for kw in NICHE_KEYWORDS)


# ── Claude: relevance + comment in one call ──────────────────────────

SYSTEM_PROMPT = """\
You find Instagram engagement opportunities for My Villa (@myvilla.la), a
luxury reinforced-CONCRETE villa company in Los Angeles. We build and stand
for fire-resilient, insurable, concrete construction and serious design.

You are shown each post's IMAGE and its caption. Judge BOTH — the image tells
you what is actually being built or sold; the caption is just the seller's pitch.

WHO WE WANT TO REACH: ultra-high-net-worth people planning to BUILD, REBUILD,
or BUY a luxury home on the LA Westside (Malibu, Beverly Hills, Bel Air,
Pacific Palisades, Brentwood, Calabasas). They follow listings, architecture &
design, the fire/insurance/rebuild conversation, and the coastal lifestyle.

HOW WE COMMENT — read this twice. We comment on someone else's post ONLY as a
GENEROUS, POSITIVE PEER, and ONLY when we can say something TRUE and aligned
with what My Villa stands for. Two HARD rules. Breaking either makes us look
bad and can damage the other account — that is far worse than staying silent:

  RULE 1 — NEVER critical, cautionary, or doubt-casting. Especially on a
  seller's / agent's / builder's post. Do NOT raise risks, concerns, "the one
  thing I'd watch", Zone-0 / defensible-space / clearance / framing /
  insurability / underwriting caveats — nothing that could scare their buyers
  or imply their property is deficient in ANY way (insurability, build quality,
  fire risk, value). We never create doubt about someone else's home. If the
  only substantive thing we could add is a caveat or a "but" → SKIP.

  RULE 2 — NEVER praise a quality the post does not actually have. Affirm ONLY
  what you can VERIFY in the image or caption. If the building is visibly
  WOOD-FRAMED (timber studs, plywood/OSB sheathing, stick framing) do NOT call
  it fire-resilient, insurable, resilient, or concrete — that is dishonest and
  makes us look foolish. Praising a combustible, hard-to-insure build for
  "resilience" mocks our own message → SKIP. Same for generic / low-quality
  work: if there is nothing genuinely admirable, do not invent it → SKIP.

WE HAVE A REAL OPPORTUNITY ONLY IF we can honestly affirm something ALIGNED
with My Villa — at least one of:
  • genuine architectural / design quality that is actually visible in the image
  • concrete / masonry / ICF / steel / non-combustible construction that is
    actually evident in the image or explicitly stated in the caption
  • a project that genuinely prioritizes insurability / fire resilience
  • OR a warm, NON-critical note on a shared theme (great design, the coastal
    living experience, the rebuild as a collective effort) that praises the
    post without judging anyone's specific property.
If none of these is honestly available, it is NOT an opportunity → SKIP.

For each post output:
1) relevance 0-10 = how well can My Villa leave an HONEST, POSITIVE, on-brand
   comment here? A post can be perfectly on-topic and still score LOW if the
   only honest comment would be a caveat, or if praising it would mean claiming
   qualities it does not have (e.g. a visibly wood-framed rebuild).
   HIGH (7-10): we can sincerely admire real design quality, or recognize real
     concrete / non-combustible / insurable construction, and add an informed,
     welcome, positive note.
   MEDIUM (5-6): a genuine, positive, non-critical comment is possible on a
     shared theme without over-claiming.
   LOW (0-4): we'd have to criticize or caveat; OR praise something we cannot
     verify / that contradicts our values (wood-frame sold as resilient); OR
     it's off-niche / spam / giveaway / generic.
   DEFAULT TO LOW WHEN UNSURE — a skipped post costs nothing; a tone-deaf or
   dishonest comment costs reputation and can hurt the other account.
2) angle — one of: "architecture_appreciation", "material_insight",
   "fire_insurance_insight", "rebuild_recovery", "market_observation",
   "design_dialogue", "lifestyle_resonance", "skip".
3) comment — a SHORT Instagram comment (max ~150 chars) in My Villa's voice:
   - warm, specific, peer-to-peer; praise ONLY what is genuinely there
   - match the post's register (lifestyle post → the living experience, not sales)
   - NEVER a caveat, warning, risk note, or comparison; nothing that diminishes
     the post or worries the reader
   - NEVER salesy, no links, no "DM us", no @myvilla.la self-tag
   - no forbidden words: bunker, fortress, fireproof, dream home, or fear
     language ("protect your family", "survive the next fire")
   - English. A gracious, knowledgeable peer — not an ad, not a critic.
   If relevance < 5 or angle is "skip", set comment to "" and give skip_reason.

skip_reason MUST be ONE short clause, max 12 words. Do not elaborate.

Output ONLY the JSON object — no prose before or after, no code fences, one line:
{"relevance": <int>, "angle": "<...>", "comment": "<...>", "skip_reason": "<short or empty>"}
"""


def score_and_comment(post: dict, model: str = SCORING_MODEL,
                      img_bytes: bytes | None = None,
                      img_media_type: str = "image/jpeg") -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return None
    cap = (post.get("caption") or "").strip()
    lines = [
        f"Hashtag found under: #{post.get('source_hashtag','')}",
        f"Author: @{post.get('owner','')}",
        f"Likes: {post.get('likes')}  Comments: {post.get('comments')}",
        f"Location: {post.get('location','')}",
        f"Caption:\n\"\"\"\n{cap[:700]}\n\"\"\"",
        "",
    ]
    if img_bytes:
        # The image is the decisive signal for RULE 2 (don't praise wood as
        # concrete/fire-resilient) — tell the model to read it, not the pitch.
        lines.append(
            "The attached image is THIS post's actual photo. Judge the real "
            "construction and design quality from it — e.g. visible wood/timber "
            "framing & plywood sheathing vs concrete/masonry/steel. The caption "
            "is the seller's pitch; trust your eyes over the words.")
    lines.append("Evaluate as a My Villa comment opportunity.")
    user_text = "\n".join(lines)

    content = []
    if img_bytes:
        import base64
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": img_media_type,
            "data": base64.standard_b64encode(img_bytes).decode()}})
    content.append({"type": "text", "text": user_text})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model, max_tokens=400, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}], timeout=45)
        text = resp.content[0].text.strip()
        # Vision responses sometimes fence the JSON and/or append prose
        # ("**Why:** …") despite the single-line instruction. Extract the
        # outermost {...} object before parsing so that never trips us up.
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            text = text[a:b + 1]
        r = json.loads(text)
        return {
            "relevance": max(0, min(10, int(r.get("relevance", 0)))),
            "angle": str(r.get("angle", "skip")),
            "comment": str(r.get("comment", "")).strip(),
            "skip_reason": str(r.get("skip_reason", "")).strip(),
        }
    except Exception as e:
        return {"relevance": 0, "angle": "skip", "comment": "",
                "skip_reason": f"scorer error: {type(e).__name__}"}


# ── Images (vision scoring + dashboard thumbnails) ───────────────────

def _sniff_media_type(data: bytes) -> str:
    """Anthropic accepts jpeg/png/webp/gif. IG is almost always jpeg."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def fetch_image_bytes(url: str | None, timeout: int = 20):
    """→ (bytes, media_type) | (None, None). Fetched ONCE per scored post and
    reused for both the vision call and the keeper's dashboard thumbnail."""
    if not url:
        return None, None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (MyVilla viral radar)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data or len(data) < 512:
            return None, None
        return data, _sniff_media_type(data)
    except Exception:
        return None, None


def save_thumb_bytes(shortcode: str, data: bytes) -> str | None:
    try:
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        dst = THUMBS_DIR / f"{shortcode}.jpg"
        dst.write_bytes(data)
        return str(dst.relative_to(ROOT_DIR))
    except Exception:
        return None


# ── Seen ledger ──────────────────────────────────────────────────────

def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="My Villa — IG Viral Comment Radar")
    ap.add_argument("--hashtag", help="Single hashtag (default: all in config)")
    ap.add_argument("--limit", type=int, help="Cap posts per hashtag")
    ap.add_argument("--min-relevance", type=int, default=DEFAULT_MIN_RELEVANCE)
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_DAYS)
    ap.add_argument("--offline", action="store_true",
                    help="Reuse last raw scrape, just re-score")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_FILE.read_text()).get("instagram", {})
    hashtags = [args.hashtag] if args.hashtag else cfg.get("hashtags", [])
    per_tag = args.limit or cfg.get("results_per_hashtag", 25)
    min_likes = cfg.get("min_likes", 30)
    min_comments = cfg.get("min_comments", 5)
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\nMy Villa — IG Viral Comment Radar")
    print(f"{'='*52}")
    print(f"  Hashtags: {', '.join('#'+h for h in hashtags)}")
    print(f"  Per-tag: {per_tag} · min_likes {min_likes} · min_comments {min_comments}")
    print(f"  Max age: {args.max_age}d · min relevance {args.min_relevance}\n")

    seen = load_seen()
    raw_cache = VIRAL_DIR / "_raw_latest.json"

    # 1. Scrape
    all_raw = []
    if args.offline and raw_cache.exists():
        all_raw = json.loads(raw_cache.read_text())
        print(f"  [offline] reusing {len(all_raw)} cached raw posts")
    else:
        for tag in hashtags:
            try:
                posts = apify_scrape_hashtag(tag, per_tag)
                print(f"  #{tag}: {len(posts)} posts")
                for p in posts:
                    p["_source_tag"] = tag
                all_raw.extend(posts)
            except RuntimeError as e:
                print(f"  #{tag}: ✗ {e}")
            time.sleep(1)
        if not args.dry_run:
            VIRAL_DIR.mkdir(parents=True, exist_ok=True)
            raw_cache.write_text(json.dumps(all_raw, ensure_ascii=False))

    # 2. Shape + dedup + pre-filter
    shaped, seen_now = [], set()
    for p in all_raw:
        s = shape(p, p.get("_source_tag", p.get("inputUrl", "")))
        if not s or s["shortcode"] in seen_now:
            continue
        seen_now.add(s["shortcode"])
        if s["shortcode"] in seen:
            continue  # already surfaced in a previous run
        if passes_prefilter(s, min_likes, min_comments, args.max_age):
            shaped.append(s)
    print(f"\n  {len(shaped)} posts pass pre-filter "
          f"({len(seen_now)} unique scraped, {len(seen)} already seen)")

    # 3. Score + comment (VISION). The post image is fetched once and reused
    #    for the keeper's thumbnail, so the honesty rule "don't praise a
    #    wood-frame build as concrete/fire-resilient" is judged on what's
    #    actually built, not on the seller's caption.
    print(f"  Scoring with {SCORING_MODEL} (vision)…")
    kept = []
    for s in shaped:
        img_bytes, media_type = fetch_image_bytes(s.get("image_url"))
        r = score_and_comment(s, img_bytes=img_bytes,
                              img_media_type=media_type or "image/jpeg")
        if not r:
            continue
        s.update({"relevance_score": r["relevance"], "angle": r["angle"],
                  "comment": r["comment"], "skip_reason": r["skip_reason"]})
        if r["relevance"] >= args.min_relevance and r["comment"]:
            if not args.dry_run and img_bytes:
                s["local_thumbnail"] = save_thumb_bytes(s["shortcode"], img_bytes)
            kept.append(s)
            vflag = "" if img_bytes else "  ⚠no-img(text-only)"
            print(f"    {r['relevance']:2d}/10 [{r['angle']:22}] @{s['owner'][:18]:18} "
                  f"{s['caption_excerpt'][:40]}{vflag}")
        time.sleep(0.1)

    kept.sort(key=lambda x: (-(x["relevance_score"] or 0),
                             -(x["likes"] or 0), -(x["comments"] or 0)))

    # 5. Persist + update seen
    out = {
        "date": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hashtags": hashtags,
        "count": len(kept),
        "opportunities": kept,
    }
    if args.dry_run:
        print(f"\n  [dry-run] {len(kept)} opportunities (not written)")
        for s in kept[:5]:
            print(f"\n  @{s['owner']} ({s['relevance_score']}/10, {s['angle']})")
            print(f"    {s['url']}")
            print(f"    💬 {s['comment']}")
        return

    VIRAL_DIR.mkdir(parents=True, exist_ok=True)
    dated = VIRAL_DIR / f"ig_viral_{today}.json"
    dated.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    (VIRAL_DIR / "ig_viral_latest.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False))

    for s in kept:
        seen[s["shortcode"]] = {"date": today, "relevance": s["relevance_score"]}
    save_seen(seen)

    print(f"\n  ✓ {len(kept)} comment opportunities → {dated.relative_to(ROOT_DIR)}")
    print(f"  Dashboard reads: ig_viral_latest.json")


if __name__ == "__main__":
    main()
