#!/usr/bin/env python3
"""
My Villa — Social Post Generator
Generates tweets + Instagram captions from radar signals and Journal articles

Usage:
  python3 generate_social.py --radar radar.json --output _system/social/posts/reactive/
  python3 generate_social.py --radar radar.json --dry-run
"""

import json
import os
import re
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import yaml

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Auto-model: tier risolto via model_resolver — upgrade automatico
# ai modelli più recenti appena compaiono su /v1/models (policy Ivo
# 2026-06-10). Fallback hardcoded se il resolver non è importabile:
# il modello non deve MAI bloccare la pipeline.
try:
    import sys as _sys
    if str(SCRIPT_DIR) not in _sys.path:
        _sys.path.insert(0, str(SCRIPT_DIR))
    from model_resolver import resolve as _resolve_model
except Exception:  # noqa: BLE001
    def _resolve_model(tier, _fb={"writer": "claude-opus-4-8",
                                  "heavy": "claude-opus-4-8",
                                  "balanced": "claude-sonnet-4-6",
                                  "cheap": "claude-haiku-4-5"}):
        return _fb.get(tier, "claude-sonnet-4-6")
# Linee guida social ufficiali (tono di voce + obiettivo) — fonte unica.
try:
    from social_guidelines import VOICE_RULES, IMAGE_STYLE_HINT, X_SCOPE
except Exception:  # noqa: BLE001
    VOICE_RULES, IMAGE_STYLE_HINT, X_SCOPE = "", "", ""
_HEAVY_MODEL = _resolve_model("heavy")
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
KNOWLEDGE_DIR = SYSTEM_DIR / "knowledge"
HISTORY_DIR = SYSTEM_DIR / "history"
SOCIAL_DIR = SYSTEM_DIR / "social"


def load_dotenv():
    env_file = SYSTEM_DIR.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v

load_dotenv()


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
# CONTENT LEDGER (dedup)
# ══════════════════════════════════════════════════════════════════════

def load_content_ledger():
    path = HISTORY_DIR / "content_ledger.json"
    if not path.exists():
        return {"published_posts": [], "topic_cooldowns": {}, "data_point_cooldowns": {}}
    return load_json(path)


def save_content_ledger(ledger):
    save_json(HISTORY_DIR / "content_ledger.json", ledger)


def get_blocked_topics(ledger, today_str):
    """Return topics in cooldown (14 days for social)."""
    today = datetime.strptime(today_str, "%Y-%m-%d")
    blocked = []
    for topic, date_str in ledger.get("topic_cooldowns", {}).items():
        cooldown_end = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=14)
        if today < cooldown_end:
            blocked.append(topic)
    return blocked


_SIG_STOPWORDS = {
    "california", "home", "homes", "house", "luxury", "angeles", "los",
    "real", "estate", "2026", "2025", "with", "from", "that", "this",
    "what", "your", "their", "about", "after", "more", "into", "have",
}


def _topic_sig(text):
    """Firma fuzzy di un titolo/tema: token >3 char meno stopwords.
    Stesso approccio che ha eliminato i duplicati sul journal — i
    topic_tags LLM variano a ogni run e il cooldown li manca."""
    import re as _re
    toks = _re.findall(r"[a-z0-9]{4,}", (text or "").lower())
    return {t for t in toks if t not in _SIG_STOPWORDS}


def _recent_social_sigs(days=14):
    """Firme di TUTTO ciò che è uscito/in coda negli ultimi N giorni:
    published, approved, draft, reactive, archivio recente. È lo
    storico anti-ripetizione: un nuovo candidato che si sovrappone
    (≥3 token) a qualcosa di recente viene scartato PRIMA della
    chiamata LLM (zero costo)."""
    import time as _t
    cutoff = _t.time() - days * 86400
    dirs = [
        SYSTEM_DIR / "social" / "posts" / "published",
        SYSTEM_DIR / "social" / "posts" / "approved",
        SYSTEM_DIR / "social" / "posts" / "reactive",
        ROOT_DIR / "_drafts" / "social",
        ROOT_DIR / "_archive" / "social",
    ]
    sigs = []
    for d in dirs:
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            try:
                if f.stat().st_mtime < cutoff:
                    continue
                raw = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # slug + prima riga del body come "tema"
            import re as _re
            m = _re.search(r"slug: (\S+)", raw)
            slug_txt = (m.group(1).replace("-", " ") if m else "")
            body = raw.split("---", 2)[-1].strip()
            first_line = body.splitlines()[0] if body else ""
            sig = _topic_sig(slug_txt + " " + first_line)
            if sig:
                sigs.append(sig)
    return sigs


def filter_redundant_candidates(candidates, days=14, overlap=3):
    """Scarta i candidati radar il cui tema è già stato trattato di
    recente sui social. Ritorna (tenuti, scartati)."""
    recent = _recent_social_sigs(days)
    kept, dropped = [], []
    for c in candidates:
        sig = _topic_sig(c.get("title", ""))
        if any(len(sig & r) >= overlap for r in recent):
            dropped.append(c)
        else:
            kept.append(c)
    return kept, dropped


# ══════════════════════════════════════════════════════════════════════
# GENERATION (Claude Opus)
# ══════════════════════════════════════════════════════════════════════

SOCIAL_SYSTEM_PROMPT = """\
You are the social media content writer for My Villa, a luxury reinforced \
concrete villa company in Los Angeles (myvilla.la).

LANGUAGE: ALL posts MUST be written in English. Never write in Italian or \
any other language.

BRAND VOICE:
- Value before product — lead with data or insight, not promotion
- Use "reinforced concrete" / "fire-resilient" — NEVER "bunker" or fear language
- The thought leader is Paolo Mezzalama (Founder, @paolomezzalama on IG/X)
- My Villa has NOT built any homes yet — never claim otherwise

HASHTAG RULES:
- X/Twitter: 2-3 hashtags at the END of the tweet, after the main text.
- Instagram: 8-12 hashtags at the end, mixing:
  • Brand: #MyVilla #MyVillaLA
  • Topic: relevant to the content (#ReinforcedConcrete, #LuxuryHomes, etc.)
  • Location: #LosAngeles #LA #HancockPark (when applicable)

MENTION RULES — STRICT. A wrong @tag links to a stranger or a dead account \
and embarrasses us, so we never guess:
- NEVER invent, guess, or approximate an @username. Do not turn a publication \
  name into a handle on your own (e.g. do NOT write "@eaaorg").
- Credit the source by its PLAIN NAME (e.g. "per Robb Report", "a Los Angeles \
  Times analysis") — NOT an @handle — UNLESS a verified handle is provided for \
  that item in its `source_handle` field.
- If `source_handle` is non-empty, you MAY @mention it, exactly as given. \
  If it is empty, use the plain publication name and NO @handle.
- The only always-safe handle is @paolomezzalama (our founder), and only when \
  directly attributing a quote to him.
- Never @mention a named person unless their handle is explicitly given to you.

FORBIDDEN: bunker, fortress, dream home, anti-fire, "protect your family", \
"survive the next fire"

TONE: Informed, editorial, sharp. Like a market analyst who happens to \
build houses. Never salesy, never breathless.

CHANNEL SPLIT — IMPORTANT:
- The X/Twitter post (the `x_post` field) MUST obey the "X/TWITTER — SCOPE" \
rules below: ONLY the build / certify / permit / insure expertise angle.
- The Instagram caption (the `ig_caption` field) is NOT bound by that scope — \
it stays broad brand-awareness / lifestyle per the SOCIAL GUIDELINES above.

""" + VOICE_RULES + "\n\n" + X_SCOPE


REACTIVE_PROMPT = """\
Generate social posts for these radar opportunities. ALL posts MUST be in English.

For each item, create:
1. An X/Twitter post (max 280 chars):
   - Lead with the most striking data point or finding
   - Credit the source by NAME (e.g. "per Robb Report, ..."); use its @handle
     ONLY if the item's `source_handle` is non-empty — never invent one
   - End with 2-3 relevant hashtags (#FireResilient #LosAngeles etc.)
   - If a Journal article exists, include the link
   - Tone: sharp, editorial, data-led. Never salesy.

2. An Instagram caption:
   - First 125 chars must hook (visible before "more")
   - Credit the source by NAME in the body; use its @handle ONLY if the item's
     `source_handle` is non-empty — never invent or guess a handle
   - End with 8-12 hashtags mixing:
     • Brand: #MyVilla #MyVillaLA
     • Topic: #ReinforcedConcrete #FireResilient #LuxuryHomes etc.
     • Location: #LosAngeles #LA plus neighborhood if applicable
     • Do NOT repeat the source handle as a hashtag
   - "Link in bio" if linking to Journal

Items:
{items_json}

Return a JSON array:
[
  {{
    "index": 0,
    "x_post": "Tweet text (max 280 chars); credit the source by name, or its verified @handle ONLY if source_handle is provided — never invent one; end with #hashtags",
    "ig_caption": "Instagram caption; credit the source by name (verified @handle only if source_handle is provided), 8-12 #hashtags at end",
    "topic_tags": ["tag1", "tag2"]
  }}
]

Return ONLY valid JSON, no markdown fences."""


COMPANION_PROMPT = """\
Generate social companion posts for these Journal articles. ALL posts MUST be in English.

Each post should drive traffic to the article, highlighting the most \
striking data point or finding. Do NOT summarize the whole article.

Articles:
{articles_json}

Return a JSON array:
[
  {{
    "index": 0,
    "slug": "article-slug",
    "x_post": "Tweet (max 280 chars). Include link: myvilla.la/blog/{{slug}}.html. Credit any source by name (verified @handle only if provided; never invent). End with 2-3 hashtags.",
    "ig_caption": "Instagram caption. Credit any source by name (verified @handle only if provided; never invent). Link in bio. End with 8-12 hashtags mixing #MyVilla #MyVillaLA + topic + location tags.",
    "topic_tags": ["tag1", "tag2"]
  }}
]

Return ONLY valid JSON, no markdown fences."""


REDDIT_SUBMISSION_PROMPT = """\
Draft a Reddit SELF-POST for each Journal article below, to share it in a \
relevant subreddit. ALL text in English.

Reddit is hostile to brand self-promotion. Each post MUST read like a \
knowledgeable person contributing to a discussion — NOT marketing. Rules:
- Pick the single best-fit subreddit for each article from THIS allowlist \
ONLY: {allowlist}. If none fits well, set "subreddit": "" (it gets skipped).
- TITLE: specific, discussion- or curiosity-driven, no clickbait, no \
hashtags, no emoji, max 280 chars. Subs like fatFIRE / RealEstate / \
homebuilding reward "here's what I learned / what I'd do" framing or a \
genuine question.
- BODY (self-post, markdown allowed): 120-250 words. LEAD with real \
value/insight tied to the article's key data — do NOT open with the brand. \
Write in first person plural as My Villa ("we", "at My Villa we…") only where \
natural. Mention the article link ONCE, contextually, near the end: \
"full write-up: https://myvilla.la/blog/{{slug}}.html". No hashtags. No salesy \
CTA. Close with a question that invites discussion.
- NEVER name any individual founder or person.
- If the article is purely visual eye-candy with no discussion hook, prefer \
"subreddit": "" — that belongs on Instagram, not Reddit.

Articles:
{articles_json}

Return a JSON array (one object per GOOD-FIT article; omit poor fits entirely):
[
  {{
    "index": 0,
    "slug": "article-slug",
    "subreddit": "fatFIRE",
    "title": "Title without hashtags or emoji",
    "body": "Value-first self-post body with the link contextual near the end."
  }}
]
Return ONLY valid JSON, no markdown fences."""


def _verified_source_handle(publication):
    """Verified @handle for a known publication, else '' — so the model can
    credit it correctly instead of guessing (and gets nothing to guess with
    when the source is unknown)."""
    try:
        from verify_handles import resolve_source
        return resolve_source(publication) or ""
    except Exception:  # noqa: BLE001
        return ""


def _sanitize_posts(posts):
    """Strip any @handle the model invented despite instructions (last net
    before these drafts reach the panel/queue)."""
    try:
        from verify_handles import sanitize
        for p in posts:
            for k in ("x_post", "ig_caption"):
                if p.get(k):
                    p[k], _ = sanitize(p[k])
    except Exception:  # noqa: BLE001
        pass
    return posts


def generate_reactive_posts(items, model=_HEAVY_MODEL):
    """Generate reactive social posts from radar signals."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [Social] No valid API key — skipping generation")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    items_json = json.dumps([{
        "index": i,
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "publication": item.get("publication", ""),
        # Verified handle (or "" → the model must use the plain name, never guess)
        "source_handle": _verified_source_handle(item.get("publication", "")),
        "snippet": item.get("snippet", "")[:200],
        "summary": item.get("summary", ""),
        "score": item.get("ai_score", item.get("preliminary_score", 0)),
        "cluster": item.get("cluster", ""),
    } for i, item in enumerate(items)], indent=2)

    prompt = REACTIVE_PROMPT.format(items_json=items_json)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SOCIAL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```json?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        posts = _sanitize_posts(json.loads(text))
        print(f"  [Social] Generated {len(posts)} reactive posts")
        return posts
    except Exception as e:
        print(f"  [Social] Error: {e}")
        return []


def generate_companion_posts(articles, model=_HEAVY_MODEL):
    """Generate social companion posts for Journal articles."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return []

    client = anthropic.Anthropic(api_key=api_key)

    articles_json = json.dumps([{
        "index": i,
        "slug": a.get("slug", ""),
        "title": a.get("title", ""),
        "excerpt": a.get("excerpt", ""),
        "section": a.get("section", ""),
        "key_data": a.get("key_data", []),
    } for i, a in enumerate(articles)], indent=2)

    prompt = COMPANION_PROMPT.format(articles_json=articles_json)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=SOCIAL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```json?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        posts = json.loads(text)
        print(f"  [Social] Generated {len(posts)} companion posts")
        return posts
    except Exception as e:
        print(f"  [Social] Error: {e}")
        return []


def _norm_sr(name):
    """'r/fatFIRE' / ' FatFIRE ' → 'fatfire' (per il match con l'allowlist)."""
    return (name or "").strip().removeprefix("r/").removeprefix("/r/").lower()


def generate_reddit_posts(articles, allowlist, model=_HEAVY_MODEL):
    """Genera self-post Reddit per gli articoli Journal, mirati all'allowlist
    submission. → lista di {index, slug, subreddit, title, body}.

    Solo i fit con subreddit IN allowlist e con titolo+corpo non vuoti
    sopravvivono (il modello può restituire subreddit:"" per scartare i
    pezzi puramente visivi, che su Reddit non hanno appiglio)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [Reddit] No valid API key — skipping generation")
        return []
    if not allowlist:
        print("  [Reddit] Nessun submission_subreddits in config — skip")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    articles_json = json.dumps([{
        "index": i,
        "slug": a.get("slug", ""),
        "title": a.get("title", ""),
        "excerpt": a.get("excerpt", ""),
        "section": a.get("section", ""),
        "key_data": a.get("key_data", []),
    } for i, a in enumerate(articles)], indent=2)

    prompt = REDDIT_SUBMISSION_PROMPT.format(
        allowlist=", ".join(allowlist), articles_json=articles_json)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=SOCIAL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```json?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        posts = json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"  [Reddit] Error: {e}")
        return []

    allow = {_norm_sr(s) for s in allowlist}
    kept = [p for p in posts
            if _norm_sr(p.get("subreddit")) in allow
            and (p.get("title") or "").strip()
            and (p.get("body") or "").strip()]
    skipped = len(posts) - len(kept)
    print(f"  [Reddit] Generated {len(kept)} submission draft(s)"
          + (f" ({skipped} scartati/poco-fit)" if skipped else ""))
    return kept


# ══════════════════════════════════════════════════════════════════════
# OUTPUT (Markdown with YAML frontmatter)
# ══════════════════════════════════════════════════════════════════════

IMG_SOCIAL_DIR = ROOT_DIR / "img" / "social"


def attach_ig_image(item, slug):
    """Immagine automatica per i post IG reattivi — stessa cascata a
    3 livelli degli articoli journal:
      1. og:image / immagini della fonte citata (item["url"])
      2. Unsplash sul topic
      3. brand fallback img/hero.png (mai un post IG senza visual)
    Ritorna il path repo-relative da scrivere nel frontmatter `image:`
    (il repo È il sito → l'URL pubblico esiste dopo il push)."""
    try:
        from image_picker import (fetch_source_images,
                                  download_source_image, fetch_hero_image)
    except ImportError:
        return "img/hero.png"
    IMG_SOCIAL_DIR.mkdir(parents=True, exist_ok=True)

    def _found():
        for ext in ("jpg", "jpeg", "png", "webp"):
            f = IMG_SOCIAL_DIR / f"{slug}-hero.{ext}"
            if f.exists():
                return f"img/social/{f.name}"
        return None

    already = _found()
    if already:
        return already

    url = (item or {}).get("url") or ""
    if url:
        try:
            cands = fetch_source_images(
                [{"url": url,
                  "publication": (item or {}).get("publication", "")}],
                max_sources=1, per_source=6)
            if cands and download_source_image(cands[0], slug, IMG_SOCIAL_DIR):
                got = _found()
                if got:
                    print(f"  [img] ✓ source: {got}")
                    return got
        except Exception as e:  # noqa: BLE001
            print(f"  [img] source fail ({type(e).__name__})")

    try:
        q = ((item or {}).get("title")
             or " ".join((item or {}).get("topic_tags") or [])
             or "italian villa architecture los angeles")
        # Orienta lo stock verso il linguaggio fotografico delle guidelines
        # (golden hour, minimal, simmetrico). Controllo vero = umano.
        if IMAGE_STYLE_HINT:
            q = f"{q} {IMAGE_STYLE_HINT}"
        if fetch_hero_image(query=q, slug=slug, out_dir=IMG_SOCIAL_DIR):
            got = _found()
            if got:
                print(f"  [img] ✓ unsplash: {got}")
                return got
    except Exception as e:  # noqa: BLE001
        print(f"  [img] unsplash fail ({type(e).__name__})")

    print("  [img] → brand fallback img/hero.png")
    return "img/hero.png"


def save_post(post, post_type, date_str, output_dir, index,
              image=None, source_url=None, radar_score=None,
              channels=("x", "instagram")):
    """Save a social post as Markdown with YAML frontmatter.

    `channels` filtra quali canali scrivere (default X+IG = comportamento
    storico, invariato). Reddit ha un formato proprio (titolo + subreddit)
    → vedi save_reddit_draft."""
    output_dir.mkdir(parents=True, exist_ok=True)

    slug = post.get("slug", f"post-{index}")

    # X/Twitter post
    x_content = post.get("x_post", "")
    if x_content and "x" in channels:
        x_path = output_dir / f"{date_str}-x-{slug}.md"
        score_line = f"radar_score: {radar_score}\n" if radar_score else ""
        x_md = f"""---
channel: x
type: {post_type}
date: {date_str}
slug: {slug}
status: draft
{score_line}topic_tags: {json.dumps(post.get('topic_tags', []))}
char_count: {len(x_content)}
---

{x_content}
"""
        with open(x_path, "w") as f:
            f.write(x_md)

    # Instagram post
    ig_content = post.get("ig_caption", "")
    if ig_content and ("instagram" in channels or "ig" in channels):
        ig_path = output_dir / f"{date_str}-ig-{slug}.md"
        img_line = f"image: {image}\n" if image else ""
        url_line = f"url: {source_url}\n" if source_url else ""
        sc_line = f"radar_score: {radar_score}\n" if radar_score else ""
        ig_md = f"""---
channel: instagram
type: {post_type}
date: {date_str}
slug: {slug}
status: draft
{img_line}{url_line}{sc_line}topic_tags: {json.dumps(post.get('topic_tags', []))}
---

{ig_content}
"""
        with open(ig_path, "w") as f:
            f.write(ig_md)

    return x_content, ig_content


def save_reddit_draft(post, date_str, output_dir, radar_score=None):
    """Scrive un draft self-post Reddit: _drafts/social/{date}-reddit-{slug}.md.
    Frontmatter: channel/subreddit/title/status; il body è il corpo del post.
    Il titolo è JSON-encoded (può contenere ':' → scalare YAML quotato)."""
    slug = post.get("slug") or "post"
    subreddit = (post.get("subreddit") or "").strip()
    title = (post.get("title") or "").strip()
    body = (post.get("body") or "").strip()
    if not subreddit or not title or not body:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{date_str}-reddit-{slug}.md"
    sc_line = f"radar_score: {radar_score}\n" if radar_score else ""
    md = (
        "---\n"
        "channel: reddit\n"
        "type: submission\n"
        f"date: {date_str}\n"
        f"slug: {slug}\n"
        f"subreddit: {subreddit}\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "status: draft\n"
        f"{sc_line}"
        f"char_count: {len(body)}\n"
        "---\n\n"
        f"{body}\n"
    )
    path.write_text(md, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def load_submission_allowlist():
    """Allowlist subreddit per le submission (radar-keywords.yml →
    reddit.submission_subreddits). Vuota = nessuna submission generata."""
    try:
        cfg = load_yaml(CONFIG_DIR / "radar-keywords.yml")
        return (cfg.get("reddit", {}) or {}).get("submission_subreddits", []) or []
    except Exception:  # noqa: BLE001
        return []


def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Social Post Generator")
    parser.add_argument("--radar", "-r", default=None,
                        help="Path to radar JSON (for reactive posts)")
    parser.add_argument("--articles", nargs="*",
                        help="Journal article JSON files (for companion posts)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: _system/social/posts/reactive/)")
    parser.add_argument("--model", default=_HEAVY_MODEL,
                        help="Claude model for generation")
    parser.add_argument("--min-score", type=int, default=15,
                        help="Min radar score for reactive posts (default: 15)")
    parser.add_argument("--max-posts", type=int, default=5,
                        help="Max reactive posts per run (default: 5)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ledger", default=None,
                        help="Path to content_ledger.json")
    parser.add_argument("--channels", default="x,instagram",
                        help="Canali da generare, csv (default: x,instagram). "
                             "Aggiungi 'reddit' per i self-post degli articoli "
                             "(richiede --articles).")
    args = parser.parse_args()

    channels = {c.strip().lower() for c in (args.channels or "").split(",")
                if c.strip()} or {"x", "instagram"}

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\nMy Villa — Social Post Generator")
    print(f"{'='*50}")

    # Default: _drafts/social/ (review before publish)
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = SYSTEM_DIR.parent / "_drafts" / "social"
    ledger = load_content_ledger()
    blocked = get_blocked_topics(ledger, today)

    all_posts = []

    # 1. Reactive posts from radar
    if args.radar:
        radar_data = load_json(Path(args.radar))
        qualified = radar_data.get("qualified", [])
        candidates = [q for q in qualified
                      if q.get("ai_score", q.get("preliminary_score", 0)) >= args.min_score]
        candidates = candidates[:args.max_posts]

        if candidates:
            candidates, dropped = filter_redundant_candidates(candidates)
            if dropped:
                print(f"  [Dedup] {len(dropped)} candidati scartati "
                      f"(tema già trattato negli ultimi 14gg):")
                for d_ in dropped[:5]:
                    print(f"    ⊘ {d_.get('title','')[:60]}")
        if candidates:
            print(f"\nGenerating {len(candidates)} reactive posts...")
            posts = generate_reactive_posts(candidates, model=args.model)

            for post in posts:
                idx = post.get("index", 0)
                if 0 <= idx < len(candidates):
                    post["slug"] = re.sub(
                        r'[^a-z0-9]+', '-',
                        candidates[idx].get("title", "post")[:40].lower()
                    ).strip("-")

                if not args.dry_run:
                    item = candidates[idx] if 0 <= idx < len(candidates) else {}
                    ig_img = (attach_ig_image(item, post.get("slug", f"post-{idx}"))
                              if post.get("ig_caption") else None)
                    save_post(post, "reactive", today, output_dir, idx,
                              image=ig_img, source_url=item.get("url"),
                              radar_score=(item.get("ai_score")
                                           or item.get("preliminary_score")),
                              channels=channels)
                else:
                    x = post.get("x_post", "")
                    print(f"  [X] ({len(x)} chars) {x[:80]}...")

                all_posts.append(post)

    # 2. Companion posts from Journal articles
    if args.articles:
        articles = []
        for af in args.articles:
            data = load_json(Path(af))
            if isinstance(data, dict):
                articles.append(data)
            elif isinstance(data, list):
                articles.extend(data)

        if articles:
            print(f"\nGenerating {len(articles)} companion posts...")
            posts = generate_companion_posts(articles, model=args.model)

            for i, post in enumerate(posts):
                if not args.dry_run:
                    save_post(post, "companion", today, output_dir, i,
                              channels=channels)
                all_posts.append(post)

            # Reddit self-posts (submission) — additivo, solo se 'reddit' è
            # tra i canali. Su account nuovo aspettarsi rimozioni: prima si
            # costruisce karma coi commenti (vedi reddit_setup.md).
            if "reddit" in channels:
                allowlist = load_submission_allowlist()
                reddit_posts = generate_reddit_posts(articles, allowlist,
                                                     model=args.model)
                for rp in reddit_posts:
                    idx = rp.get("index", 0)
                    if not rp.get("slug") and 0 <= idx < len(articles):
                        rp["slug"] = articles[idx].get("slug", f"post-{idx}")
                    if args.dry_run:
                        print(f"  [Reddit] r/{rp.get('subreddit')} — "
                              f"{(rp.get('title') or '')[:70]}")
                    else:
                        p = save_reddit_draft(rp, today, output_dir)
                        if p:
                            print(f"  [Reddit] draft → {p.name}")

    # Update ledger
    if not args.dry_run and all_posts:
        for post in all_posts:
            for tag in post.get("topic_tags", []):
                ledger.setdefault("topic_cooldowns", {})[tag] = today
            ledger.setdefault("published_posts", []).append({
                "date": today,
                "slug": post.get("slug", ""),
                "channels": ["x", "instagram"],
            })
        save_content_ledger(ledger)
        print(f"\nContent ledger updated")

    print(f"\n{'='*50}")
    print(f"Generated {len(all_posts)} post sets ({len(all_posts) * 2} total: X + IG)")


if __name__ == "__main__":
    main()
