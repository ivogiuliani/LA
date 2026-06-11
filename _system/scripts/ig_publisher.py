#!/usr/bin/env python3
"""
ig_publisher.py — pubblicazione giornaliera social: Instagram + X (Fase 1).

Flusso concordato con Ivo (2026-06-10):
  radar/generatori propongono → Ivo APPROVA dal pannello → questo
  script pubblica gli approvati in automatico, max IG_DAILY_CAP al
  giorno (default 1 — ramp-up account nuovo).

Coda:
  _system/social/posts/approved/   → post .md con frontmatter
       channel: instagram|ig|x  (X: testo puro via publish_to_x,
       cap separato X_DAILY_CAP, niente immagine richiesta)
  _system/social/posts/published/  → spostati qui dopo il publish,
       con media_id + permalink + published_at nel frontmatter

Selezione immagine (obbligatoria per i feed post):
  1. frontmatter `image:` (path relativo al repo o URL assoluto)
  2. journal companion (`journal_slug:`) → hero dell'articolo
  3. nessuna → il post viene SALTATO con nota nel report (niente
     publish senza visual: l'approvazione dal pannello deve
     includere l'immagine)
  I path locali diventano URL myvilla.la (il repo È il sito) e
  vengono verificati con HEAD prima del publish (CDN warm-up).

Caption: body del .md + riga hashtag (frontmatter `hashtags:` se
presente, altrimenti derivati dai topic_tags + set brand).

Idempotenza/cap: conta i post già in published/ con published_at di
oggi (file in git → il conteggio vale cross-rail, come il marker
della digest).

Senza credenziali (IG_ACCESS_TOKEN/IG_BUSINESS_ACCOUNT_ID): esce
pulito con notice — la pipeline non si rompe (fase pre-setup Meta).

CLI:
  python3 ig_publisher.py                # pubblica fino al cap di oggi
  python3 ig_publisher.py --dry-run      # mostra cosa pubblicherebbe
  python3 ig_publisher.py --whoami       # valida token, stampa account
  python3 ig_publisher.py --cap 2        # override del cap giornaliero
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
SOCIAL_DIR = SYSTEM_DIR / "social"
APPROVED_DIR = SOCIAL_DIR / "posts" / "approved"
PUBLISHED_DIR = SOCIAL_DIR / "posts" / "published"
SITE_BASE = "https://myvilla.la"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_DAILY_CAP = 1  # ramp-up account nuovo (deciso 2026-06-10)

BRAND_HASHTAGS = ["MyVilla", "MyVillaLA", "CementoAVista",
                  "ItalianDesign", "LosAngelesArchitecture"]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _load_dotenv():
    env = ROOT_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


def _parse_post(path: Path) -> tuple[dict, str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return fm, m.group(2).strip()


def _channel(fm: dict) -> str:
    """'ig' | 'x' | '' — canale normalizzato del post."""
    ch = str(fm.get("channel", "")).lower()
    if ch in ("instagram", "ig"):
        return "ig"
    if ch in ("x", "twitter"):
        return "x"
    return ""


def _is_ig(fm: dict) -> bool:
    return _channel(fm) == "ig"


def _published_today_count(channel: str = "ig") -> int:
    """Post del canale pubblicati oggi (UTC), contati da published/
    (in git → conteggio condiviso fra rail locale e cloud)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    if not PUBLISHED_DIR.exists():
        return 0
    for f in PUBLISHED_DIR.glob("*.md"):
        parsed = _parse_post(f)
        if not parsed:
            continue
        fm, _ = parsed
        if _channel(fm) == channel and                 str(fm.get("published_at", "")).startswith(today):
            n += 1
    return n


def _resolve_image_url(fm: dict) -> tuple[str | None, str]:
    """→ (url_pubblico, nota). Vedi docstring del modulo per l'ordine."""
    img = str(fm.get("image") or "").strip()
    if img.startswith(("http://", "https://")):
        return img, "frontmatter url"
    if img:
        local = (ROOT_DIR / img.lstrip("/")).resolve()
        if local.exists() and ROOT_DIR in local.parents:
            rel = local.relative_to(ROOT_DIR)
            return f"{SITE_BASE}/{rel.as_posix()}", "frontmatter path"
        return None, f"image {img!r} non trovata nel repo"
    slug = str(fm.get("journal_slug") or "").strip()
    if slug:
        for ext in ("jpg", "jpeg", "png", "webp"):
            cand = ROOT_DIR / "blog" / "assets" / "img" / f"{slug}-hero.{ext}"
            if cand.exists():
                rel = cand.relative_to(ROOT_DIR)
                return f"{SITE_BASE}/{rel.as_posix()}", "hero articolo"
        return None, f"hero per slug {slug!r} non trovata"
    return None, "nessuna immagine (frontmatter senza image/journal_slug)"


def _wait_image_live(url: str, timeout_s: int = 90) -> bool:
    """HEAD-poll finché GitHub Pages serve l'immagine (stesso pattern
    del warm-up CDN della digest)."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                if 200 <= r.status < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)
    return False


def _build_caption(fm: dict, body: str) -> str:
    caption = body.strip()
    tags = fm.get("hashtags")
    if not tags:
        # deriva dai topic_tags + brand set
        raw = [re.sub(r"[^a-zA-Z0-9]", "", str(t).title())
               for t in (fm.get("topic_tags") or [])]
        tags = [t for t in raw if t] + BRAND_HASHTAGS
    # dedup preservando l'ordine, max 12 hashtag
    seen, final = set(), []
    for t in tags:
        t = str(t).lstrip("#")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            final.append(t)
    hash_line = " ".join(f"#{t}" for t in final[:12])
    # companion: aggiungi il link all'articolo nel testo (IG non rende
    # cliccabili i link in caption, ma il riferimento "link in bio /
    # articolo su myvilla.la" resta utile)
    url = str(fm.get("article_url") or "").strip()
    if url and url not in caption:
        caption += f"\n\nArticolo completo su myvilla.la (link in bio)"
    return f"{caption}\n\n{hash_line}" if hash_line else caption


def _list_approved(channel: str = "ig") -> list[tuple[Path, dict, str]]:
    out = []
    if not APPROVED_DIR.exists():
        return out
    for f in sorted(APPROVED_DIR.glob("*.md"),
                    key=lambda p: p.stat().st_mtime):
        parsed = _parse_post(f)
        if not parsed:
            continue
        fm, body = parsed
        if _channel(fm) == channel:
            out.append((f, fm, body))
    return out


def _list_ig_approved():
    return _list_approved("ig")


def _move_to_published(path: Path, fm: dict, body: str,
                       result: dict) -> Path:
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    fm = dict(fm)
    fm["status"] = "published"
    fm["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if _channel(fm) == "x":
        fm["x_tweet_id"] = result.get("tweet_id", result.get("post_id", ""))
        fm["x_url"] = result.get("url", "")
    else:
        fm["ig_media_id"] = result.get("post_id", "")
        fm["ig_permalink"] = result.get("url", "")
    new_raw = "---\n" + yaml.safe_dump(fm, allow_unicode=True,
                                       sort_keys=False).strip() + \
              "\n---\n\n" + body + "\n"
    dest = PUBLISHED_DIR / path.name
    dest.write_text(new_raw, encoding="utf-8")
    path.unlink()
    return dest


def whoami() -> int:
    """Valida le credenziali e stampa l'account collegato."""
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    account_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
    if not token:
        print("✗ IG_ACCESS_TOKEN mancante in .env")
        return 2
    host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com")
    ver = os.environ.get("IG_API_VERSION", "v23.0")
    url = (f"https://{host}/{ver}/me"
           f"?fields=id,username,account_type"
           f"&access_token={urllib.parse.quote(token)}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)
        print(f"✓ Token valido — @{d.get('username')} "
              f"(id {d.get('id')}, tipo {d.get('account_type', '?')})")
        if account_id and account_id != str(d.get("id")):
            print(f"  ⚠ IG_BUSINESS_ACCOUNT_ID in .env ({account_id}) ≠ "
                  f"id del token ({d.get('id')})")
        elif not account_id:
            print(f"  → aggiungi in .env: IG_BUSINESS_ACCOUNT_ID={d.get('id')}")
        return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        print(f"✗ Token NON valido (HTTP {e.code}): {body}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"✗ Errore rete: {e}")
        return 1


def refresh_token() -> int:
    """Rinnova il token long-lived (validità 60gg, rinnovabile dopo
    24h dall'emissione). Stampa il NUOVO token da incollare in .env
    e nel GitHub secret IG_ACCESS_TOKEN."""
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    if not token:
        print("✗ IG_ACCESS_TOKEN mancante in .env")
        return 2
    host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com")
    url = (f"https://{host}/refresh_access_token"
           f"?grant_type=ig_refresh_token"
           f"&access_token={urllib.parse.quote(token)}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)
        new_tok = d.get("access_token", "")
        days = int(d.get("expires_in", 0) / 86400)
        print(f"✓ Token rinnovato (valido ~{days} giorni).")
        print("\nAggiorna .env:")
        print(f"  IG_ACCESS_TOKEN={new_tok}")
        print("\n…e il GitHub secret IG_ACCESS_TOKEN (repo ivogiuliani/LA).")
        return 0
    except urllib.error.HTTPError as e:
        print(f"✗ Refresh fallito (HTTP {e.code}): "
              f"{e.read().decode('utf-8', errors='replace')[:300]}")
        return 1


def run(*, dry_run: bool = False, cap: int | None = None) -> dict:
    """→ {published: [...], skipped: [...], pending: int} per la digest."""
    _load_dotenv()
    cap = cap if cap is not None else \
        int(os.environ.get("IG_DAILY_CAP", DEFAULT_DAILY_CAP))

    queue = _list_ig_approved()
    published, skipped = [], []

    token = os.environ.get("IG_ACCESS_TOKEN", "")
    account = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
    if (not token or not account) and not dry_run:
        # In dry-run si prosegue comunque: nessuna API call viene fatta,
        # e la preview serve proprio a testare PRIMA del setup Meta.
        if queue:
            print(f"  [ig] credenziali Meta non configurate — "
                  f"{len(queue)} post approvati restano in coda "
                  f"(vedi _system/docs/instagram_setup.md)")
        return {"published": [], "skipped": [],
                "pending": len(queue), "needs_setup": True}

    done_today = _published_today_count()
    budget = max(0, cap - done_today)
    if budget == 0 and queue:
        print(f"  [ig] cap giornaliero raggiunto ({done_today}/{cap}) — "
              f"{len(queue)} in coda per domani")
        return {"published": [], "skipped": [], "pending": len(queue)}

    from publish_social import publish_to_instagram

    for path, fm, body in queue:
        if len(published) >= budget:
            break
        image_url, img_note = _resolve_image_url(fm)
        if not image_url:
            skipped.append({"file": path.name, "reason": img_note})
            print(f"  [ig] ~ skip {path.name}: {img_note}")
            continue

        caption = _build_caption(fm, body)

        if dry_run:
            print(f"  [ig] [DRY] pubblicherebbe {path.name}")
            print(f"        img: {image_url} ({img_note})")
            print(f"        caption ({len(caption)} ch): "
                  f"{caption[:120]}...")
            published.append({"file": path.name, "dry_run": True,
                              "image": image_url,
                              "caption_preview": caption[:200]})
            continue

        if not _wait_image_live(image_url):
            skipped.append({"file": path.name,
                            "reason": f"immagine non raggiungibile: {image_url}"})
            print(f"  [ig] ~ skip {path.name}: immagine 404 ({image_url})")
            continue

        result = publish_to_instagram(caption, image_url=image_url)
        if result.get("ok"):
            dest = _move_to_published(path, fm, body, result)
            published.append({
                "file": dest.name,
                "media_id": result.get("post_id", ""),
                "permalink": result.get("url", ""),
                "image": image_url,
                "caption_preview": caption[:200],
            })
            print(f"  [ig] ✓ pubblicato {path.name} → "
                  f"{result.get('url') or result.get('post_id')}")
        else:
            skipped.append({"file": path.name,
                            "reason": result.get("error", "errore ignoto")})
            print(f"  [ig] ✗ {path.name}: {result.get('error')}")
            if "token" in str(result.get("error", "")).lower() or \
                    "OAuth" in str(result.get("error", "")):
                # Token scaduto/invalido: inutile insistere sugli altri.
                break

    # ── Coda X (testo puro, niente immagine richiesta) ─────────────
    x_cap = int(os.environ.get("X_DAILY_CAP", DEFAULT_DAILY_CAP))
    x_queue = _list_approved("x")
    if x_queue:
        x_done = _published_today_count("x")
        x_budget = max(0, x_cap - x_done)
        if x_budget == 0:
            print(f"  [x] cap giornaliero raggiunto ({x_done}/{x_cap}) — "
                  f"{len(x_queue)} in coda per domani")
        else:
            try:
                from publish_social import publish_to_x
                from send_email import sanitize_founder_name
            except ImportError as e:
                publish_to_x = None
                print(f"  [x] modulo publish non importabile: {e}")
            for path, fm, body in x_queue:
                if x_budget <= 0 or publish_to_x is None:
                    break
                text = sanitize_founder_name(body.strip())[:280]
                if dry_run:
                    print(f"  [x] [DRY] pubblicherebbe {path.name}: "
                          f"{text[:90]}...")
                    published.append({"file": path.name, "channel": "x",
                                      "dry_run": True,
                                      "caption_preview": text[:200]})
                    x_budget -= 1
                    continue
                result = publish_to_x(text)
                if result.get("ok"):
                    dest = _move_to_published(path, fm, body, result)
                    published.append({"file": dest.name, "channel": "x",
                                      "permalink": result.get("url", ""),
                                      "caption_preview": text[:200]})
                    print(f"  [x] ✓ pubblicato {path.name} → "
                          f"{result.get('url') or ''}")
                    x_budget -= 1
                elif result.get("needs_setup"):
                    print(f"  [x] credenziali X non configurate — "
                          f"{len(x_queue)} post restano in coda")
                    break
                else:
                    skipped.append({"file": path.name, "channel": "x",
                                    "reason": result.get("error", "?")})
                    print(f"  [x] ✗ {path.name}: {result.get('error')}")

    remaining = len(_list_ig_approved()) + len(_list_approved("x"))
    return {"published": published, "skipped": skipped,
            "pending": remaining}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Publisher Instagram (fase 1)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cap", type=int, default=None,
                   help=f"Override cap giornaliero (default {DEFAULT_DAILY_CAP})")
    p.add_argument("--whoami", action="store_true",
                   help="Valida il token e stampa l'account collegato")
    p.add_argument("--refresh-token", action="store_true",
                   help="Rinnova il token long-lived (60gg) e stampa il nuovo")
    args = p.parse_args(argv)

    _load_dotenv()
    if args.whoami:
        return whoami()
    if args.refresh_token:
        return refresh_token()

    res = run(dry_run=args.dry_run, cap=args.cap)
    print(f"\nIG: {len(res['published'])} pubblicati, "
          f"{len(res['skipped'])} saltati, {res['pending']} in coda.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
