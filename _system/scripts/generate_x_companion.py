#!/usr/bin/env python3
"""
generate_x_companion.py — produce an X (Twitter) post announcing a
just-published journal article. Mirror of generate_ig_companion.py.

Differenze rispetto all'IG companion:
- X consente i LINK cliccabili → il tweet INCLUDE l'URL dell'articolo
  (myvilla.la/blog/<slug>.html), non "Link in bio".
- ≤ 280 caratteri TOTALI (testo + link + hashtag).
- 2-3 hashtag in coda (non 5-7 come IG).

Scrive il companion accanto all'articolo come
  <slug>.x.md            (channel: x, type: journal_companion)
così approve.py / x_publisher.py lo trattano come gli altri post X.

Usage:
    python3 generate_x_companion.py --article blog/<slug>.json
    python3 generate_x_companion.py --article ... --print
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Auto-model (tier balanced) via model_resolver; fallback hardcoded.
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


def _anthropic_client():
    try:
        import anthropic  # noqa: WPS433
    except ImportError as e:
        print(f"  [x-companion] anthropic SDK not installed: {e}", file=sys.stderr)
        sys.exit(1)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("  [x-companion] ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    return anthropic.Anthropic(api_key=key)


_SYSTEM_PROMPT = """You write X (Twitter) posts for My Villa, a Los \
Angeles luxury home builder using reinforced concrete in Italian villa \
typology.

Voice rules:
- Conversational, sharp, not promotional. Sound like an architect-curator.
- ONE single insight — pick the strongest angle from the article and \
lead with it. No "check out our new article" framing.
- The WHOLE post (text + link + hashtags) must be ≤ 280 characters.
- Include the article link exactly as given, on its own or after the text.
- No exclamation marks, no buzzwords ("revolutionary", "game-changer"), \
no emojis.
- End with 2-3 CamelCase hashtags. Defaults to pick from based on topic: \
#ReinforcedConcrete #ItalianVilla #FireResilient #LosAngeles \
#LuxuryHomes. Never use #fireproof / #bunker / #investment.

OUTPUT FORMAT (strict): return ONLY the tweet text (with the link and \
the hashtags inline), nothing else. No preamble, no quotes."""


def _build_user_prompt(meta, article_url):
    title = meta.get("title", "")
    subtitle = meta.get("subtitle", "") or ""
    section = meta.get("tag_label") or meta.get("section", "")
    description = meta.get("meta_description") or ""
    sources = meta.get("sources") or []
    angle_hint = (sources[0].get("excerpt") or "")[:300] if sources else ""
    return f"""Write the X post for this just-published article.

TITLE:        {title}
SUBTITLE:     {subtitle}
SECTION:      {section}
META:         {description}
ANGLE HINT:   {angle_hint}
ARTICLE LINK: {article_url}

Recap: ≤280 chars TOTAL incl. link and hashtags; include the link; \
2-3 CamelCase hashtags at the end."""


_URL_RE = re.compile(r"https?://\S+")


def _fit_280(text: str) -> str:
    """Garantisce ≤280 caratteri VISIBILI preservando link e hashtag.
    Necessario perché il leg X di ig_publisher tronca con text[:280]:
    un tweet più lungo verrebbe tagliato a metà link. Accorcia solo la
    FRASE iniziale (prima del link), al confine di parola."""
    text = text.strip()
    if len(text) <= 280:
        return text
    m = _URL_RE.search(text)
    if not m:  # nessun link: taglio netto al confine di parola
        return text[:279].rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"
    head = text[:m.start()].strip()          # la frase
    tail = text[m.start():].strip()          # link (+ hashtag dopo)
    budget = 280 - len(tail) - 1             # -1 per il newline frase/link
    if budget < 20:
        return text[:280]                    # caso limite improbabile
    if len(head) > budget:
        head = head[:budget].rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"
    return f"{head}\n{tail}"


def generate_post(meta, article_url, model=_BALANCED_MODEL, max_tokens=300):
    client = _anthropic_client()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user",
                   "content": _build_user_prompt(meta, article_url)}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return _fit_280("".join(parts).strip().strip('"').strip())


def companion_markdown(post_text, article_slug):
    article_url = f"https://myvilla.la/blog/{article_slug}.html"
    hashtags = re.findall(r"#([A-Za-z0-9_]+)", post_text)
    fm_lines = [
        "---",
        "channel: x",
        "type: journal_companion",
        f"journal_slug: {article_slug}",
        f"article_url: {article_url}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        f"char_count: {len(post_text)}",
    ]
    if hashtags:
        fm_lines.append("hashtags:")
        for h in hashtags:
            fm_lines.append(f"  - {h}")
    fm_lines.append("status: draft")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n" + post_text.strip() + "\n"


def write_companion(post_text, slug, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(companion_markdown(post_text, slug), encoding="utf-8")


def _main(argv=None):
    p = argparse.ArgumentParser(description="X companion per articoli journal")
    p.add_argument("--article", required=True,
                   help="Path al .json dell'articolo (sidecar)")
    p.add_argument("--output", help="Path .x.md (default: sibling .x.md)")
    p.add_argument("--model", default=_BALANCED_MODEL)
    p.add_argument("--print", action="store_true",
                   help="Stampa il tweet senza scrivere il file")
    args = p.parse_args(argv)

    _load_dotenv()
    article_path = Path(args.article)
    if not article_path.exists():
        print(f"  [x-companion] file not found: {article_path}", file=sys.stderr)
        return 1
    try:
        meta = json.loads(article_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  [x-companion] malformed sidecar: {e}", file=sys.stderr)
        return 1

    slug = meta.get("slug") or article_path.stem
    article_url = f"https://myvilla.la/blog/{slug}.html"
    try:
        post_text = generate_post(meta, article_url, model=args.model)
    except Exception as e:  # noqa: BLE001
        print(f"  [x-companion] Anthropic call failed: {e}", file=sys.stderr)
        return 2

    if args.print:
        print(post_text)
        return 0

    out = Path(args.output) if args.output else article_path.with_suffix(".x.md")
    write_companion(post_text, slug, out)
    print(f"  [x-companion] wrote {out} ({len(post_text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
