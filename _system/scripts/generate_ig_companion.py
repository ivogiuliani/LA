#!/usr/bin/env python3
"""
generate_ig_companion.py — produce an Instagram caption announcing a
just-drafted journal article.

Pipeline role
-------------
Triggered from the dashboard "📷 Generate IG companion" button (or
from this CLI for manual runs). Reads the journal article's .json
sidecar, asks Anthropic Sonnet for an IG caption that announces the
piece, and writes the result alongside the article as
  _drafts/journal/<slug>.ig.md

The companion stays next to the article so the reviewer can edit it
inline on the dashboard. When the operator clicks "Pubblica" on the
journal card, approve.py moves the companion to
  _system/social/posts/approved/<date>-ig-journal-<slug>.md
so the IG publish layer can pick it up.

Caption rules (tuned to MyVilla brand voice)
--------------------------------------------
- ≤220 chars before the hashtag line (fits inside IG's "see more" fold).
- Conversational, single insight, no marketing slang.
- Closes with "Link in bio" — IG strips URLs from captions for accounts
  <10k followers, so we send readers to the linktree / pinned URL.
- 5-8 hashtags, MyVilla-house style (CamelCase, no spam).
- Reuses copy/voice constraints from _system/knowledge/outreach_voice.md.

Usage
-----
    python3 generate_ig_companion.py \\
        --article _drafts/journal/<slug>.json \\
        --output  _drafts/journal/<slug>.ig.md      # default = sibling of --article
    python3 generate_ig_companion.py --article ... --print     # stdout only
    python3 generate_ig_companion.py --article ... --model claude-haiku-4-5

Exit codes
----------
    0  ok
    1  bad input (no article, no API key, malformed sidecar)
    2  Anthropic call failed
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ── Paths + .env loader ──────────────────────────────────────────────
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
try:
    from social_guidelines import VOICE_RULES
except Exception:  # noqa: BLE001
    VOICE_RULES = ""
_BALANCED_MODEL = _resolve_model("balanced")
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent


def _load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── Anthropic client (lazy import; we want a clean error if missing) ──
def _anthropic_client():
    try:
        import anthropic  # noqa: WPS433 — intentional lazy import
    except ImportError as e:
        print(
            f"  [ig-companion] anthropic SDK not installed: {e}\n"
            "  Install with: pip install anthropic",
            file=sys.stderr,
        )
        sys.exit(1)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("  [ig-companion] ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    return anthropic.Anthropic(api_key=key)


# ── Caption synthesis ─────────────────────────────────────────────────

_SYSTEM_PROMPT = """You write Instagram captions for My Villa, a Los \
Angeles luxury home builder using reinforced concrete in Italian villa \
typology.

Voice rules:
- Conversational, not promotional. Sound like an architect-curator, \
not like marketing.
- ONE single insight per caption. Pick the strongest angle from the \
article and lead with it.
- 180-220 characters in the caption body (NOT counting hashtags). \
Stay under IG's "see more" fold.
- No exclamation marks. No buzzwords ("revolutionary", "game-changer"). \
No emojis in the body.
- Close the body with: Link in bio.
- Then a NEW LINE, then 5-7 hashtags in CamelCase. Use these as \
defaults and pick ones that match the article topic: #MyVilla \
#MyVillaLA #ReinforcedConcrete #ItalianVilla #LosAngelesArchitecture \
#FireResistantHome #LuxuryHomeLA. Avoid duplicates.

OUTPUT FORMAT (strict):
Return only the caption + hashtag line, separated by ONE blank line. \
No preamble, no explanation, no quotes around the text.

""" + VOICE_RULES


def _build_user_prompt(article_meta):
    title = article_meta.get("title", "")
    subtitle = article_meta.get("subtitle", "") or ""
    section = article_meta.get("tag_label") or article_meta.get("section", "")
    description = article_meta.get("meta_description") or ""
    # Pick the first 1-2 source excerpts as the "angle hint" so the
    # caption stays anchored to what the article actually says.
    sources = article_meta.get("sources") or []
    angle_hint = ""
    if sources:
        first = sources[0]
        angle_hint = (first.get("excerpt") or "")[:300]

    return f"""Write the IG caption for this just-drafted article.

TITLE:        {title}
SUBTITLE:     {subtitle}
SECTION:      {section}
META:         {description}
ANGLE HINT:   {angle_hint}

Constraints (recap): 180-220 char body, ends with "Link in bio.", \
blank line, then 5-7 CamelCase hashtags."""


def generate_caption(article_meta, model=_BALANCED_MODEL, max_tokens=400):
    client = _anthropic_client()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(article_meta)}],
    )
    parts = []
    for block in msg.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "".join(parts).strip()


# ── Companion file writer ─────────────────────────────────────────────

def _split_body_and_hashtags(caption):
    """Best-effort: separate the caption body from the trailing hashtag line.

    Handles both "body\\n\\n#tags" and "body\\n#tags" outputs. Returns
    (body, hashtags) where each is a stripped string.
    """
    # Try to find the last block that starts with #
    lines = [ln.rstrip() for ln in caption.strip().splitlines() if ln.strip()]
    if not lines:
        return caption, ""
    # Find the first line that's entirely hashtags
    for i, ln in enumerate(lines):
        if re.match(r"^(\s*#[A-Za-z0-9_]+\s*)+$", ln):
            body = "\n".join(lines[:i]).strip()
            tags = " ".join(lines[i:]).strip()
            return body, tags
    return caption.strip(), ""


def companion_markdown(article_meta, caption, article_slug):
    """Wrap the caption in a markdown frontmatter the dashboard knows.

    Frontmatter mirrors the format used by social drafts (channel,
    journal_slug, char_count, hashtags) so existing helpers in
    approve.py / scan_social_drafts can read it without special-casing.
    """
    body, tags = _split_body_and_hashtags(caption)
    char_count = len(body)
    hashtag_list = re.findall(r"#[A-Za-z0-9_]+", tags)
    # Pre-compute the public article URL for downstream tools.
    article_url = f"https://myvilla.la/blog/{article_slug}.html"

    fm_lines = [
        "---",
        f"channel: ig",
        f"type: journal_companion",
        f"journal_slug: {article_slug}",
        f"article_url: {article_url}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        f"char_count: {char_count}",
        "hashtags:",
    ]
    for h in hashtag_list:
        fm_lines.append(f"  - {h.lstrip('#')}")
    fm_lines.append("status: draft")
    fm_lines.append("---")

    if tags:
        body_out = f"{body}\n\n{tags}\n"
    else:
        body_out = f"{body}\n"

    return "\n".join(fm_lines) + "\n\n" + body_out


def write_companion(article_meta, caption, output_path):
    slug = article_meta.get("slug") or output_path.stem.replace(".ig", "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        companion_markdown(article_meta, caption, slug),
        encoding="utf-8",
    )


# ── CLI ───────────────────────────────────────────────────────────────

def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--article", required=True,
        help="Path to the journal article's .json sidecar in _drafts/journal/",
    )
    parser.add_argument(
        "--output",
        help="Output .ig.md path. Default: sibling of --article with .ig.md suffix.",
    )
    parser.add_argument(
        "--model", default=_BALANCED_MODEL,
        help="Anthropic model id (default: auto, tier balanced).",
    )
    parser.add_argument(
        "--print", action="store_true",
        help="Print the generated caption to stdout and exit (no file write).",
    )
    args = parser.parse_args(argv)

    _load_dotenv()

    article_path = Path(args.article)
    if not article_path.exists():
        print(f"  [ig-companion] file not found: {article_path}", file=sys.stderr)
        return 1
    try:
        meta = json.loads(article_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  [ig-companion] malformed sidecar: {e}", file=sys.stderr)
        return 1

    try:
        caption = generate_caption(meta, model=args.model)
    except Exception as e:  # noqa: BLE001 — surface the real error to the operator
        print(f"  [ig-companion] Anthropic call failed: {e}", file=sys.stderr)
        return 2

    if args.print:
        print(caption)
        return 0

    out = Path(args.output) if args.output else article_path.with_suffix(".ig.md")
    # Slug used in the URL is derived from the sidecar (preferred) or
    # the article filename if the sidecar didn't include it.
    if not meta.get("slug"):
        # Strip the .json sidecar's stem of any extension noise.
        meta["slug"] = article_path.stem
    write_companion(meta, caption, out)
    print(f"  [ig-companion] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
