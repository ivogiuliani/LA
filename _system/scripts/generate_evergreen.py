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
try:
    import image_picker  # Unsplash on-brand per gli evergreen (no foto del sito)
except Exception:  # noqa: BLE001
    image_picker = None
try:
    import brand_grade  # grade caldo golden-hour on-brand (adatta lo stock)
except Exception:  # noqa: BLE001
    brand_grade = None


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
    """→ (topic_set, image_set, photo_id_set) usati negli ultimi `days` giorni.
    photo_id serve a non riproporre la STESSA foto Unsplash a giorni vicini."""
    cutoff = datetime.now() - timedelta(days=days)
    topics, images, photo_ids = set(), set(), set()
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
            if fm.get("image_photo_id"):
                photo_ids.add(fm["image_photo_id"])
    return topics, images, photo_ids


def _pick_unsplash_options(topic, slug, used_ids, day_idx, out_dir, n=3,
                           download=True):
    """Fino a `n` immagini Unsplash ON-BRAND per il topic (alta qualità, NON
    dal sito), SCARICATE e GRADATE in stile golden-hour (brand_grade) così lo
    stock parla il linguaggio visivo My Villa. La PRIMA è la preselezionata;
    le altre sono alternative che l'utente può scegliere dal pannello.
    Ruota le query per giorno e preferisce foto non usate di recente
    (photo_id), riempiendo con photo_id DISTINTI. → lista di dict
    {"p": web_path_rel, "id": photo_id, "by": credit}. In dry-run
    (download=False) non scarica: ritorna [{"p": "unsplash? «query»"}]."""
    if image_picker is None:
        return []
    queries = topic.get("image_queries") or []
    if not queries:
        return []
    primary = queries[day_idx % len(queries)]
    if not download:
        return [{"p": f"unsplash? «{primary}»", "id": "", "by": ""}]
    fallbacks = [q for q in queries if q != primary]
    # fetch_candidates si ferma alla PRIMA query non vuota → una query stretta
    # (es. «Mediterranean courtyard water») può rendere 1 solo risultato e
    # lasciarci senza alternative. Qui aggreghiamo su primary + fallback,
    # dedup per photo_id, finché abbiamo abbastanza candidati "freschi".
    cands, seen_pid = [], set()
    for q in [primary] + fallbacks:
        if not q:
            continue
        try:
            batch = image_picker.fetch_candidates(
                q, count=12, orientation="squarish")
        except Exception as e:  # noqa: BLE001
            print(f"  [evergreen] Unsplash fetch error ({q!r}): "
                  f"{type(e).__name__}")
            batch = []
        for c in batch:
            pid = c.get("photo_id") or ""
            if pid and pid in seen_pid:
                continue
            if pid:
                seen_pid.add(pid)
            cands.append(c)
        # Basta appena abbiamo n candidati non usati di recente.
        if sum(1 for c in cands if c.get("photo_id") not in used_ids) >= n:
            break
    if not cands:
        return []
    # Prima le foto non usate di recente (varietà), poi le altre per riempire
    # fino a n mantenendo i photo_id DISTINTI (mai 2 opzioni uguali).
    fresh = [c for c in cands if c.get("photo_id") not in used_ids]
    rest = [c for c in cands if c.get("photo_id") in used_ids]
    ordered = fresh + rest
    # suffissi di file distinti per le opzioni: -hero, -b-hero, -c-hero…
    suffixes = ["", "-b", "-c", "-d", "-e"]
    options, seen = [], set()
    for c in ordered:
        pid = c.get("photo_id") or ""
        if pid and pid in seen:
            continue
        idx = len(options)
        sub_slug = f"{slug}{suffixes[idx]}" if idx < len(suffixes) \
            else f"{slug}-{idx}"
        meta = image_picker.download_candidate(c, sub_slug, ROOT_DIR / out_dir)
        if not meta or not meta.get("local_path"):
            continue
        local = meta["local_path"]
        if brand_grade is not None:
            brand_grade.grade_file(local)  # golden-hour on-brand, in place
        try:
            rel = Path(local).resolve().relative_to(ROOT_DIR).as_posix()
        except ValueError:
            rel = f"{out_dir.rstrip('/')}/{sub_slug}-hero.jpg"
        if pid:
            seen.add(pid)
        options.append({"p": rel, "id": pid,
                        "by": meta.get("author_name", "")})
        if len(options) >= n:
            break
    return options


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


def generate(count=None, dry_run=False, no_unsplash=False):
    _load_dotenv()
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    topics = cfg.get("topics", []) or []
    count = count or int(cfg.get("posts_per_day", 3))
    cooldown = int(cfg.get("topic_cooldown_days", 5))
    image_out_dir = cfg.get("image_out_dir", "img/social/evergreen")
    today = datetime.now().strftime("%Y-%m-%d")

    # Le opzioni immagine viaggiano in git (pannello condiviso sul server):
    # per non gonfiare il repo si tengono solo gli ultimi 7 giorni. Le più
    # vecchie sono già state pubblicate (l'immagine scelta è copiata in
    # img/social/ dal publisher) o scadute col draft; le cancellazioni le
    # committa il daily_publish con git add -A.
    _img_dir = ROOT_DIR / image_out_dir
    if _img_dir.exists() and not dry_run:
        for _old in _img_dir.glob("*.jpg"):
            _m = re.match(r"^(\d{4}-\d{2}-\d{2})-", _old.name)
            if not _m:
                continue
            try:
                if datetime.now() - datetime.strptime(
                        _m.group(1), "%Y-%m-%d") > timedelta(days=7):
                    _old.unlink()
            except (ValueError, OSError):
                pass

    # Idempotenza: se le proposte evergreen di oggi esistono già, stop.
    existing_today = [f for f in OUT_DIR.glob("*evergreen*.md")
                      if (_parse_fm(f).get("date") or "")[:10] == today] \
        if OUT_DIR.exists() else []
    if existing_today and not dry_run:
        print(f"  [evergreen] {len(existing_today)} proposte di oggi già "
              f"presenti — skip")
        return existing_today

    recent_topics, recent_images, recent_photo_ids = _recent(cooldown)
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
        hashtags = re.findall(r"#([A-Za-z0-9_]+)", tags)
        slug = _slugify(angle) or t.get("key")
        img_slug = f"{today}-ig-evergreen-{t.get('key')}"
        fname = f"{img_slug}.md"
        # Immagine: PRIMA Unsplash on-brand (alta qualità, niente foto del
        # sito ormai esaurite); se non disponibile, ripiega sugli stem del sito.
        photo_id, credit = "", ""
        img = ""
        options = []
        if not no_unsplash:
            options = _pick_unsplash_options(
                t, img_slug, recent_photo_ids, day_idx, image_out_dir,
                download=not dry_run)
        if options:
            img = options[0]["p"]
            photo_id = options[0].get("id", "")
            credit = options[0].get("by", "")
        if not img or img.startswith("unsplash? "):
            stem_img = _find_image(t.get("images") or [], recent_images)
            if img.startswith("unsplash? "):  # dry-run: mostra entrambe
                img = f"{img}  (fallback: {stem_img or '—'})"
            else:
                img = stem_img
        if img and not img.startswith("unsplash? "):
            recent_images.add(img)
        # Tutte le foto scelte (non solo la preselezionata) non vanno
        # riproposte nei prossimi giorni.
        for _o in options:
            if _o.get("id"):
                recent_photo_ids.add(_o["id"])
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
        if photo_id:
            fm.append(f"image_photo_id: {photo_id}")
        if credit:
            fm.append(f"image_credit: {credit}")
        # 3 opzioni immagine (preselezionata + alternative) per il selettore
        # del pannello. JSON su UNA riga: è YAML valido e resta leggibile dal
        # parser naïve del pannello (che fa json.loads del valore).
        real_opts = [o for o in options
                     if not str(o.get("p", "")).startswith("unsplash? ")]
        if len(real_opts) >= 2:
            fm.append("image_options: " + json.dumps(
                [{"p": o["p"], "id": o.get("id", ""), "by": o.get("by", "")}
                 for o in real_opts],
                ensure_ascii=False, separators=(",", ":")))
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
    p.add_argument("--no-unsplash", action="store_true",
                   help="non scaricare da Unsplash (usa solo gli stem del sito)")
    args = p.parse_args(argv)
    generate(count=args.count, dry_run=args.dry_run,
             no_unsplash=args.no_unsplash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
