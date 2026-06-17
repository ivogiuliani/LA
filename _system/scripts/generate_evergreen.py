#!/usr/bin/env python3
"""
generate_evergreen.py — flusso EVERGREEN di brand-awareness per Instagram,
derivato dai topic del sito (myvilla.la), separato dai post del journal.

Ogni giorno propone N post (default 3) ruotando sui topic di
_system/config/evergreen-topics.yml con anti-ripetizione, abbinando
un'immagine ORIGINALE del sito e una caption in voce My Villa (regole di
social_guidelines.py). Sono PROPOSTE (status: draft) — non pubblica nulla:
compaiono nel pannello nella sezione "Evergreen dal sito" per l'approvazione.

LLM: Anthropic (tier balanced) con fallback Gemini se Anthropic è giù.

CLI:
  python3 generate_evergreen.py            # genera le proposte di oggi
  python3 generate_evergreen.py --count 3
  python3 generate_evergreen.py --dry-run  # stampa senza scrivere file
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CONFIG = SYSTEM_DIR / "config" / "evergreen-topics.yml"
OUT_DIR = ROOT_DIR / "_drafts" / "social"
IMG_DIR = ROOT_DIR / "img"
ARCHIVE = ROOT_DIR / "_archive" / "social"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from model_resolver import resolve as _resolve_model
except Exception:  # noqa: BLE001
    def _resolve_model(tier):
        return "claude-sonnet-4-6"
try:
    from social_guidelines import VOICE_RULES
except Exception:  # noqa: BLE001
    VOICE_RULES = ""


def _load_dotenv():
    env = ROOT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── LLM con fallback Gemini ──────────────────────────────────────────
def _gemini_complete(prompt: str, model: str = "gemini-2.5-flash") -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return ""
    import urllib.request as _u
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 600},
    }).encode("utf-8")
    req = _u.Request(url, data=body,
                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _u.urlopen(req, timeout=60) as r:
            d = json.load(r)
        parts = (d.get("candidates", [{}])[0]
                 .get("content", {}).get("parts", []))
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as e:  # noqa: BLE001
        print(f"  [evergreen] Gemini error: {type(e).__name__}")
        return ""


def _complete(system: str, user: str) -> str:
    """Anthropic (balanced) → fallback Gemini. '' se entrambi giù."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key and not key.startswith("sk-ant-PLACEHOLDER"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            r = client.messages.create(
                model=_resolve_model("balanced"), max_tokens=600,
                system=system, messages=[{"role": "user", "content": user}])
            return "".join(b.text for b in r.content
                           if getattr(b, "type", "") == "text").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  [evergreen] Anthropic non disponibile "
                  f"({type(e).__name__}) → fallback Gemini")
    return _gemini_complete(system + "\n\n" + user)


_SYSTEM = """You write Instagram captions for My Villa (myvilla.la),
Italian-designed reinforced-concrete luxury villas in Los Angeles. These are
EVERGREEN brand posts (not news), each built around a recurring theme from
our site and paired with a golden-hour image.

%s

- 150-220 characters in the caption body. Lead with the IDEA or feeling, not
  promotion. Make the villa a symbol of a desirable, slower, greener LA life.
- Conversational and sharp. Never salesy, never self-celebratory. No
  "discover", "learn more", "DM", "link in bio".
- No prices/costs. Numbers only when they are real supporting data.
- Then a NEW LINE, then 5-7 CamelCase hashtags: always #MyVilla #MyVillaLA,
  plus theme + #LosAngeles when fitting.

OUTPUT (strict): only the caption body + the hashtag line, separated by ONE
blank line. No preamble, no quotes, no explanation.""" % VOICE_RULES


def _parse_fm(path: Path):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", raw, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for ln in m.group(1).splitlines():
        kv = ln.split(":", 1)
        if len(kv) == 2:
            fm[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
    return fm


def _recent(days: int):
    """→ (topic_set, image_set) usati negli ultimi `days` giorni."""
    cutoff = datetime.now() - timedelta(days=days)
    topics, images = set(), set()
    for d in (OUT_DIR, ARCHIVE, SYSTEM_DIR / "social" / "posts" / "approved",
              SYSTEM_DIR / "social" / "posts" / "published"):
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            fm = _parse_fm(f)
            if fm.get("type") != "evergreen":
                continue
            ds = (fm.get("date") or "")[:10]
            try:
                if ds and datetime.strptime(ds, "%Y-%m-%d") < cutoff:
                    continue
            except ValueError:
                pass
            if fm.get("topic"):
                topics.add(fm["topic"])
            if fm.get("image"):
                images.add(fm["image"])
    return topics, images


def _find_image(stems, used_images):
    """Primo stem con un file esistente in img/ non ancora usato di recente."""
    for stem in stems:
        for ext in ("webp", "jpg", "png"):
            rel = f"img/{stem}.{ext}"
            if (ROOT_DIR / rel).exists() and rel not in used_images:
                return rel
    # tutti usati di recente: ripiega sul primo esistente
    for stem in stems:
        for ext in ("webp", "jpg", "png"):
            rel = f"img/{stem}.{ext}"
            if (ROOT_DIR / rel).exists():
                return rel
    return ""


def _split_caption(text):
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.match(r"^(\s*#[A-Za-z0-9_]+\s*)+$", ln):
            return "\n".join(lines[:i]).strip(), " ".join(lines[i:]).strip()
    return text.strip(), ""


def _slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48]


def generate(count=None, dry_run=False):
    _load_dotenv()
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    topics = cfg.get("topics", []) or []
    count = count or int(cfg.get("posts_per_day", 3))
    cooldown = int(cfg.get("topic_cooldown_days", 5))
    today = datetime.now().strftime("%Y-%m-%d")

    # Idempotenza: se le proposte evergreen di oggi esistono già, stop.
    existing_today = [f for f in OUT_DIR.glob("*evergreen*.md")
                      if (_parse_fm(f).get("date") or "")[:10] == today] \
        if OUT_DIR.exists() else []
    if existing_today and not dry_run:
        print(f"  [evergreen] {len(existing_today)} proposte di oggi già "
              f"presenti — skip")
        return existing_today

    recent_topics, recent_images = _recent(cooldown)
    # Ordina i topic: prima quelli NON usati di recente (varietà).
    fresh = [t for t in topics if t.get("key") not in recent_topics]
    stale = [t for t in topics if t.get("key") in recent_topics]
    ordered = fresh + stale
    chosen = ordered[:count] if ordered else topics[:count]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    # angolo a rotazione deterministica (giorno dell'anno) → varia nel tempo
    day_idx = datetime.now().timetuple().tm_yday
    for t in chosen:
        angles = t.get("angles") or [t.get("brief", "")]
        angle = angles[day_idx % len(angles)]
        user = (f"Theme: {t.get('name')}\n"
                f"What it's about: {t.get('brief')}\n"
                f"Angle for THIS post: {angle}\n\nWrite the caption.")
        text = _complete(_SYSTEM, user)
        if not text:
            print(f"  [evergreen] '{t.get('key')}': LLM non disponibile — skip")
            continue
        body, tags = _split_caption(text)
        img = _find_image(t.get("images") or [], recent_images)
        recent_images.add(img)
        hashtags = re.findall(r"#([A-Za-z0-9_]+)", tags)
        slug = _slugify(angle) or t.get("key")
        fname = f"{today}-ig-evergreen-{t.get('key')}.md"
        if dry_run:
            print(f"\n  ── [{t.get('key')}] {angle}")
            print(f"     img: {img}")
            print(f"     {body[:160]}")
            print(f"     {tags[:80]}")
            out.append(fname)
            continue
        fm = ["---", "channel: ig", "type: evergreen",
              f"topic: {t.get('key')}", f"angle: {angle}",
              f"image: {img}" if img else "image:",
              f"date: {today}", f"char_count: {len(body)}"]
        if hashtags:
            fm.append("hashtags:")
            fm += [f"  - {h}" for h in hashtags]
        fm += ["status: draft", "---"]
        body_out = f"{body}\n\n{tags}\n" if tags else f"{body}\n"
        (OUT_DIR / fname).write_text("\n".join(fm) + "\n\n" + body_out,
                                     encoding="utf-8")
        print(f"  [evergreen] ✓ {fname}  (img: {img or '—'})")
        out.append(fname)
    print(f"  [evergreen] {len(out)} proposte generate "
          f"(topic: {', '.join(t.get('key') for t in chosen)})")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Evergreen IG proposals dal sito")
    p.add_argument("--count", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    generate(count=args.count, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
