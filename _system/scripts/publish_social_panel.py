#!/usr/bin/env python3
"""
publish_social_panel.py — static, password-gated planning panel for the
social media manager at https://myvilla.la/team/social/.

v3 (2026-09-01, feedback della social media manager): il pannello è uno
strumento di PIANO EDITORIALE, non di copia.
  - niente bottone "Copia": si seleziona, non si copia
  - "Elimina" nasconde le proposte non utili (localStorage, reversibile
    con "Ripristina eliminati"); "Segna come fatto" ⇄ "Riattiva"
  - "Scarica immagine" su ogni card; "Cambia immagine" apre un picker
    di alternative on-brand (render del sito + hero recenti)
  - un'unica sezione "Piano editoriale — proposte" per piattaforma che
    fonde: proposte dai Journal freschi + caption reattive preparate +
    pacchetti editoriali (badge di origine: Journal / Reattivo /
    Editoriale)
  - solo immagini TRACCIATE in git vengono referenziate (le locali non
    committate diventavano 404 su GitHub Pages)
  - restano i bottoni "Pubblica su X" (intent, testo precompilato) e
    "Pubblica su LinkedIn" (share-offsite + autocopy) — la
    pubblicazione è sempre un gesto manuale della manager

Tab: Instagram / X / LinkedIn / Journal (riferimento) / Brand kit.
Conversazioni da presidiare = placeholder fino allo step 2 (servizi di
scansione spenti per scelta).

Threat model = soft gate "Strada 1" (SHA-256 client-side + noindex +
robots Disallow): repo pubblico, solo collaboratori fidati.

Usage:
    python3 publish_social_panel.py [--password segreta]
Chiamato da publish_all_drafts.py prima dell'autopush quotidiano.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent

BLOG = ROOT / "blog"
POSTS = ROOT / "_system" / "social" / "posts"
EDITORIAL_READY = POSTS / "editorial" / "_publish_ready"
HANDOFF = ROOT / "_social_handoff"

OUT_DIR = ROOT / "team" / "social"
ASSETS = OUT_DIR / "assets"

DEFAULT_PASSWORD = "ivopaolo"
JOURNAL_LIMIT = 20
POOL_BLOG_HEROES = 18

PLATFORMS = ("instagram", "x", "linkedin")
PLATFORM_LABEL = {"instagram": "Instagram", "x": "X", "linkedin": "LinkedIn"}
BADGE_CLASS = {"instagram": "ig", "x": "x", "linkedin": "li"}

BASE_HASHTAGS_IG = "#MyVilla #MyVillaLA #FireResilient #ReinforcedConcrete #LuxuryHomes #LosAngeles"
BASE_HASHTAGS_LI = "#Architecture #FireResilience #LosAngeles #LuxuryRealEstate"

# Render on-brand del sito per il picker "Cambia immagine".
SITE_POOL = [
    "img/hero.webp", "img/external.webp", "img/courtyard.webp",
    "img/courtyard-interior.webp", "img/dining.webp", "img/bedroom.webp",
    "img/bathtub.webp", "img/biophilic.webp", "img/biophilic-design.webp",
    "img/facade-terracotta.webp", "img/facade-cream.webp", "img/facade-sand.webp",
    "img/facade-sage.webp", "img/facade-rose.webp",
    "img/amanvari-01.webp", "img/amanvari-02.webp",
    "img/int-green-lounge.webp", "img/int-green-marble.webp",
    "img/int-green-terrazzo.webp", "img/int-bespoke-layout.webp",
    "img/int-designer-chair.webp", "img/int-designer-lamp.webp",
    "img/int-bronze-faucet.webp",
]


def _tracked_files() -> set[str]:
    """Path (relativi a ROOT) tracciati in git = ciò che esiste su Pages.
    Fallback: set vuoto → si degrada al solo exists() locale."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            return set(out.stdout.splitlines())
    except Exception:
        pass
    return set()


_TRACKED = _tracked_files()


def _public_img(rel: str | None) -> str | None:
    """Ritorna /path solo se l'immagine esisterà su GitHub Pages."""
    if not rel:
        return None
    rel = rel.lstrip("/")
    if not (ROOT / rel).exists():
        return None
    if _TRACKED and rel not in _TRACKED:
        return None  # locale ma mai committata → sarebbe un 404 live
    return "/" + rel


def _utm(url: str, platform: str) -> str:
    return f"{url}?utm_source={platform}&utm_medium=social&utm_campaign=journal"


# --------------------------------------------------------------------------- #
# Proposte per canale (template, zero API)
# --------------------------------------------------------------------------- #

def _tagify(tag: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", tag)
    return "#" + "".join(w.capitalize() if not w.isupper() else w for w in words) if words else ""


def _key_bullets(d: dict, n: int = 3) -> list[str]:
    out = []
    for k in (d.get("key_data") or [])[:n]:
        num = (k.get("number") or "").strip()
        lab = " ".join((k.get("label") or "").split())
        if num and lab:
            out.append(f"▪ {num} — {lab}")
    return out


def _ig_proposal(d: dict, url: str) -> str:
    parts = [d.get("title") or "", ""]
    sub = d.get("subtitle") or d.get("meta_description") or ""
    if sub:
        parts += [sub, ""]
    bullets = _key_bullets(d)
    if bullets:
        parts += bullets + [""]
    tags = [t for t in (d.get("topic_tags") or [])[:5]]
    hashtags = " ".join(filter(None, [BASE_HASHTAGS_IG] + [_tagify(t) for t in tags]))
    parts += [f"Full analysis on the My Villa Journal → {url}", "", hashtags]
    return "\n".join(parts)


def _x_proposal(d: dict, url: str) -> str:
    title = d.get("title") or ""
    sub = d.get("subtitle") or d.get("meta_description") or ""
    tags = "#MyVilla #FireResilient"
    T_CO = 23
    budget = 280 - T_CO - len(tags) - 6
    body = title[:budget]
    remaining = budget - len(body)
    if sub and remaining > 40:
        room = remaining - 2
        s = sub if len(sub) <= room else sub[: room - 1].rsplit(" ", 1)[0] + "…"
        body = f"{body}\n\n{s}"
    return f"{body}\n\n{url}\n{tags}"


LI_MAX_CHARS = 400


def _li_proposal(d: dict, url: str) -> str:
    """LinkedIn: tono esplicativo/didascalico, MAX 400 caratteri totali.
    Niente URL nel testo: il bottone "Pubblica su LinkedIn" allega già
    l'articolo come anteprima link (pattern nativo LinkedIn)."""
    title = (d.get("title") or "").strip()
    explainer = (d.get("meta_description") or d.get("subtitle") or "").strip()
    hashtags = "#Architecture #FireResilience #LosAngeles"

    # Un dato chiave come frase didascalica, se il budget lo consente.
    key_line = ""
    for k in (d.get("key_data") or [])[:1]:
        num = (k.get("number") or "").strip()
        lab = " ".join((k.get("label") or "").split())
        if num and lab:
            key_line = f"One figure to hold: {num} ({lab[:1].lower()}{lab[1:]})."

    fixed = len(title) + len(hashtags) + 4  # separatori \n\n
    room = LI_MAX_CHARS - fixed
    body = ""
    if explainer and room > 60:
        avail = room - (len(key_line) + 2 if key_line else 0)
        if len(explainer) > avail:
            explainer = explainer[: max(0, avail - 1)].rsplit(" ", 1)[0].rstrip(".,;: ") + "…"
            key_line = ""  # explainer pieno: il dato non entra
        body = explainer
        if key_line and len(body) + 2 + len(key_line) <= room:
            body = f"{body}\n\n{key_line}"

    parts = [title]
    if body:
        parts.append(body)
    parts.append(hashtags)
    return "\n\n".join(parts)


def _x_effective_len(text: str, url: str) -> int:
    return len(text) - len(url) + 23


def _x_intent(text: str) -> str:
    return "https://x.com/intent/post?text=" + quote(text, safe="")


def _li_share(url: str) -> str:
    return "https://www.linkedin.com/sharing/share-offsite/?url=" + quote(url, safe="")


# --------------------------------------------------------------------------- #
# Data collection → entries unificate per il piano
# --------------------------------------------------------------------------- #

def collect_journal_entries() -> dict[str, list[dict]]:
    """Articoli Journal → una entry per piattaforma (origine: Journal)."""
    arts = []
    for jf in BLOG.glob("*.json"):
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or jf.stem
        if not (BLOG / f"{slug}.html").exists():
            continue
        hero_rel = None
        hero = d.get("hero_image") or {}
        if isinstance(hero, dict) and hero.get("local_path"):
            hero_rel = _public_img(f"blog/assets/img/{Path(hero['local_path']).name}")
        arts.append((d, slug, hero_rel))
    arts.sort(key=lambda t: t[0].get("_date") or "", reverse=True)
    arts = arts[:JOURNAL_LIMIT]

    out: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    for d, slug, hero_rel in arts:
        clean_url = f"https://myvilla.la/blog/{slug}.html"
        x_text = _x_proposal(d, _utm(clean_url, "x"))
        texts = {
            "instagram": _ig_proposal(d, _utm(clean_url, "instagram")),
            "x": x_text,
            "linkedin": _li_proposal(d, _utm(clean_url, "linkedin")),
        }
        for p in PLATFORMS:
            e = {
                "id": f"{p}:{slug}",
                "origin": "Journal",
                "date": d.get("_date") or "",
                "title": d.get("title") or slug,
                "subtitle": d.get("subtitle") or d.get("meta_description") or "",
                "section": d.get("_section_name") or d.get("section") or "",
                "image": hero_rel,
                "article_url": clean_url,
                "source_url": "",
                "text": texts[p],
                "slides": [],
                "share_x": _x_intent(x_text) if p == "x" else None,
                "share_li": _li_share(_utm(clean_url, "linkedin")) if p == "linkedin" else None,
                "x_len": _x_effective_len(x_text, _utm(clean_url, "x")) if p == "x" else None,
            }
            out[p].append(e)
    return out


def _parse_post_md(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    body = m.group(2).strip()
    channel = (meta.get("channel") or "?").replace("instagram", "ig").replace("twitter", "x")
    platform = {"ig": "instagram", "x": "x", "li": "linkedin"}.get(channel, channel)
    return {
        "file": path.name,
        "platform": platform,
        "type": meta.get("type") or "",
        "date": str(meta.get("date") or ""),
        "slug": meta.get("slug") or path.stem,
        "title": meta.get("title") or "",
        "image": _public_img(meta.get("image") or ""),
        "source_url": meta.get("url") or meta.get("article_url") or "",
        "score": meta.get("radar_score"),
        "caption": body,
    }


_X_STATUS_RX = re.compile(r"^https?://(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/", re.I)


def _x_author(url: str) -> tuple[str, str]:
    """Da un URL x.com/<handle>/status/... ritorna (@handle, profilo)."""
    m = _X_STATUS_RX.match(url or "")
    if not m:
        return "", ""
    handle = m.group(1)
    return f"@{handle}", f"https://x.com/{handle}"


def collect_queue_entries() -> dict[str, list[dict]]:
    """Caption preparate (planned/approved/reactive) → entries.

    I metadati (url del post originale, immagine, score) spesso vivono
    solo su UNA variante di canale (es. la gemella IG ha l'URL x.com
    che manca alla variante X): prima passata per fondere i metadati
    per slug, seconda per costruire le card arricchite."""
    parsed: list[dict] = []
    for status in ("planned", "approved", "reactive"):
        folder = POSTS / status
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md"), reverse=True):
            p = _parse_post_md(f)
            if p and p["platform"] in PLATFORMS:
                parsed.append(p)

    by_slug: dict[str, dict] = {}
    for p in parsed:
        m = by_slug.setdefault(p["slug"], {"source_url": "", "image": None, "score": None})
        m["source_url"] = m["source_url"] or p["source_url"]
        m["image"] = m["image"] or p["image"]
        if m["score"] is None:
            m["score"] = p["score"]

    out: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    for p in parsed:
        shared = by_slug[p["slug"]]
        source_url = p["source_url"] or shared["source_url"]
        image = p["image"] or shared["image"]
        score = p["score"] if p["score"] is not None else shared["score"]
        author, author_url = _x_author(source_url)
        origin = "Editoriale" if p["type"] == "editorial" else "Reattivo"
        e = {
            "id": f'{p["platform"]}:{p["file"]}',
            "origin": origin,
            "date": p["date"],
            "title": p["title"] or p["slug"].replace("-", " "),
            "section": "",
            "image": image,
            "article_url": "",
            "source_url": source_url,
            "score": score if origin == "Reattivo" else None,
            "author": author,
            "author_url": author_url,
            "text": p["caption"],
            "slides": [],
            "share_x": _x_intent(p["caption"]) if p["platform"] == "x" else None,
            "share_li": (_li_share(source_url)
                         if p["platform"] == "linkedin" and source_url else None),
            "x_len": (_x_effective_len(p["caption"], source_url or "")
                      if p["platform"] == "x" and source_url else None),
        }
        out[p["platform"]].append(e)
    return out


def collect_package_entries() -> list[dict]:
    """Pacchetti editoriali _publish_ready (Instagram) → entries;
    copia gli asset dentro il pannello."""
    out = []
    if not EDITORIAL_READY.is_dir():
        return out
    for pkg in sorted(EDITORIAL_READY.iterdir()):
        if not pkg.is_dir():
            continue
        caption = ""
        cf = pkg / "caption.txt"
        if cf.exists():
            caption = cf.read_text(encoding="utf-8").strip()
        meta = {}
        mf = pkg / "metadata.json"
        if mf.exists():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        dest = ASSETS / "editorial" / pkg.name
        dest.mkdir(parents=True, exist_ok=True)
        img_rel = None
        if (pkg / "image.webp").exists():
            shutil.copy2(pkg / "image.webp", dest / "image.webp")
            img_rel = f"assets/editorial/{pkg.name}/image.webp"
        slides = sorted((pkg / "carousel").glob("*.webp")) if (pkg / "carousel").is_dir() else []
        slide_rels = []
        for s in slides:
            shutil.copy2(s, dest / s.name)
            slide_rels.append(f"assets/editorial/{pkg.name}/{s.name}")
        post = meta.get("post") or {}
        hashtags = " ".join("#" + h for h in (post.get("hashtags") or []))
        out.append({
            "id": f"instagram:editorial:{pkg.name}",
            "origin": "Editoriale",
            "date": pkg.name[:10],
            "title": (pkg.name[11:] or pkg.name).replace("-", " "),
            "section": post.get("pillar") or "",
            "image": img_rel,
            "article_url": "",
            "source_url": "",
            "text": caption + ("\n\n" + hashtags if hashtags else ""),
            "slides": slide_rels,
            "share_x": None,
            "share_li": None,
            "x_len": None,
        })
    return out


def collect_published() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    folder = POSTS / "published"
    if folder.is_dir():
        for f in sorted(folder.glob("*.md")):
            p = _parse_post_md(f)
            if p and p["platform"] in out:
                out[p["platform"]].append(p)
    ed_pub = POSTS / "editorial" / "published"
    if ed_pub.is_dir():
        for pkg in sorted(ed_pub.iterdir()):
            if pkg.is_dir():
                out["instagram"].append({
                    "file": pkg.name, "platform": "instagram",
                    "type": "editorial", "date": pkg.name[:10],
                    "slug": pkg.name[11:], "title": "",
                    "image": None, "source_url": "", "caption": "",
                })
    for lst in out.values():
        lst.sort(key=lambda x: x["date"], reverse=True)
    return out


def collect_brand() -> dict:
    dest = ASSETS / "brand"
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("myvilla-wordmark.svg", "myvilla-wordmark.png",
                 "myvilla-monogram.svg", "myvilla-monogram.png"):
        src = HANDOFF / name
        if src.exists():
            shutil.copy2(src, dest / name)
            files.append(f"assets/brand/{name}")
    bank = ""
    bf = HANDOFF / "captions-instagram.md"
    if bf.exists():
        bank = bf.read_text(encoding="utf-8").strip()
    return {"files": files, "caption_bank": bank}


STOCK_CACHE = ROOT / "_system" / "social" / "stock_cache.json"
STOCK_TTL_DAYS = 7
STOCK_PER_QUERY = 6
STOCK_QUERIES = [
    "italian villa architecture",
    "concrete house exterior",
    "mediterranean courtyard garden",
    "los angeles hillside homes",
    "minimal luxury interior",
    "california coastline aerial",
]


def collect_stock_images() -> list[dict]:
    """Foto stock gratuite (Unsplash) per il picker, via image_picker.py.

    Interrogato a build-time (la chiave resta nel .env, MAI nella pagina:
    il repo è pubblico); risultato cachato 7 giorni in stock_cache.json
    (committato, così anche una build senza chiave — es. il rail cloud —
    serve le stesse foto). Fallback: cache stantia > niente."""
    now = datetime.now().timestamp()
    stale = None
    if STOCK_CACHE.exists():
        try:
            data = json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
            stale = data.get("photos") or None
            if stale and now - data.get("fetched_at", 0) < STOCK_TTL_DAYS * 86400:
                return stale
        except (json.JSONDecodeError, OSError):
            stale = None

    photos: list[dict] = []
    try:
        from image_picker import fetch_candidates
        seen: set[str] = set()
        for q in STOCK_QUERIES:
            for c in fetch_candidates(q, count=STOCK_PER_QUERY):
                full = c.get("full_url") or ""
                if not full or full in seen:
                    continue
                seen.add(full)
                photos.append({
                    "thumb": c.get("thumb_url") or full,
                    "full": full,
                    "author": c.get("author_name") or "",
                    "link": c.get("unsplash_url") or "",
                })
    except Exception:
        photos = []

    if photos:
        try:
            STOCK_CACHE.write_text(
                json.dumps({"fetched_at": now, "photos": photos},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass
        return photos
    return stale or []


def build_image_pool() -> list[str]:
    """Alternative on-brand per il picker: render del sito + hero social
    tracciati + hero recenti del Journal."""
    pool: list[str] = []
    for rel in SITE_POOL:
        p = _public_img(rel)
        if p:
            pool.append(p)
    social_tracked = sorted(
        (f for f in _TRACKED if f.startswith("img/social/")), reverse=True
    )[:20]
    pool.extend("/" + f for f in social_tracked)
    heroes = sorted(
        (f for f in _TRACKED if f.startswith("blog/assets/img/")), reverse=True
    )[:POOL_BLOG_HEROES]
    pool.extend("/" + f for f in heroes)
    seen, out = set(), []
    for p in pool:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def _gate(password_hash: str) -> str:
    return f"""
<meta name="robots" content="noindex,nofollow,noarchive,nocache">
<style id="mv-auth-gate-style">
  html {{ visibility: hidden !important; }}
  html.mv-team-authed {{ visibility: visible !important; }}
</style>
<script id="mv-auth-gate-script">
(function() {{
  var CORRECT_HASH = '{password_hash}';
  var SESSION_KEY  = 'myvilla-social-auth';
  function reveal() {{
    try {{ sessionStorage.setItem(SESSION_KEY, '1'); }} catch (e) {{}}
    document.documentElement.classList.add('mv-team-authed');
  }}
  function deny() {{
    document.documentElement.innerHTML =
      '<body style="font-family:-apple-system,sans-serif;padding:3rem;' +
      'visibility:visible;color:#666;background:#faf8f5">' +
      '<h1 style="color:#333">Access denied</h1>' +
      '<p>Reload the page to retry.</p></body>';
    document.documentElement.classList.add('mv-team-authed');
  }}
  function ask() {{
    var p = prompt('My Villa \\u2014 Social Content Panel\\n\\nPassword:');
    if (p === null || p === '') {{ deny(); return; }}
    if (!window.crypto || !window.crypto.subtle) {{ deny(); return; }}
    var enc = new TextEncoder().encode(p);
    window.crypto.subtle.digest('SHA-256', enc).then(function(buf) {{
      var arr = Array.from(new Uint8Array(buf));
      var hex = arr.map(function(b) {{ return b.toString(16).padStart(2, '0'); }}).join('');
      if (hex === CORRECT_HASH) reveal(); else deny();
    }});
  }}
  try {{
    if (sessionStorage.getItem(SESSION_KEY) === '1') {{ reveal(); return; }}
  }} catch (e) {{}}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', ask);
  }} else {{ ask(); }}
}})();
</script>
"""


CSS = """
:root{--ink:#2b2620;--cream:#faf8f5;--sand:#e9e2d6;--terra:#b06a4a;--mut:#8a8177;--ok:#5f7a5a;--danger:#a4553f;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--cream);color:var(--ink);font-family:'Montserrat',-apple-system,sans-serif;font-size:15px;line-height:1.55}
h1,h2,h3{font-family:'Cormorant Garamond',Georgia,serif;font-weight:600}
a{color:var(--terra)}
.wrap{max-width:1060px;margin:0 auto;padding:0 18px 80px}
header.top{padding:26px 18px 14px;border-bottom:1px solid var(--sand);background:var(--cream)}
header.top .brand{font-family:'Cormorant Garamond',serif;font-size:26px;letter-spacing:.14em}
header.top .sub{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.22em;margin-top:2px}
header.top .stamp{color:var(--mut);font-size:12px;margin-top:8px}
nav.tabs{position:sticky;top:0;z-index:50;background:var(--cream);border-bottom:1px solid var(--sand);overflow-x:auto;white-space:nowrap;padding:0 18px;display:flex;gap:4px}
nav.tabs button{border:none;background:none;color:var(--ink);font:600 12px 'Montserrat',sans-serif;text-transform:uppercase;letter-spacing:.12em;padding:14px 14px 12px;cursor:pointer;border-bottom:2px solid transparent}
nav.tabs button.active{border-color:var(--terra);color:var(--terra)}
.tabpane{display:none}
.tabpane.active{display:block}
section{margin-top:40px}
section>h2{font-size:28px;margin-bottom:4px}
section>p.lead{color:var(--mut);font-size:13px;margin-bottom:18px}
.platform-head{display:flex;align-items:baseline;gap:12px;margin-top:34px}
.platform-head h2{font-size:34px}
.platform-head .note{color:var(--mut);font-size:12.5px}
.planbar{display:flex;gap:14px;align-items:center;margin-bottom:14px;font-size:12.5px;color:var(--mut);flex-wrap:wrap}
.planbar button{border:1px solid var(--sand);background:#fff;color:var(--mut);padding:6px 12px;border-radius:99px;font:600 10.5px 'Montserrat',sans-serif;letter-spacing:.09em;text-transform:uppercase;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;position:relative}
.card.is-done .imgwrap,.card.is-done h3,.card.is-done .cap textarea,.card.is-done .meta,.card.is-done .links{opacity:.4}
.card.is-done::after{content:"✓ fatto";position:absolute;top:10px;right:10px;background:var(--ok);color:#fff;font:600 10px 'Montserrat';letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:99px}
.card.is-removed{display:none}
.imgwrap{position:relative}
.card img.hero{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--sand)}
.card .pad{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.card h3{font-size:18px;line-height:1.25}
.card .excerpt{color:var(--mut);font-size:13px;line-height:1.5}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:var(--sand);color:var(--ink)}
.badge.ig{background:#e8d5e2}.badge.x{background:#d7dee8}.badge.li{background:#cfe0ee}
.badge.o-journal{background:#e3dccd}.badge.o-reattivo{background:#efe6d8;color:#7a6a4f}.badge.o-editoriale{background:#dde8dc;color:#4a6046}
.badge.trend{background:#f3ddc9;color:#8a4f2c;font-weight:700}
.cap{margin-top:4px}
.cap textarea{width:100%;border:1px solid var(--sand);border-radius:8px;padding:10px;font:12.5px/1.5 'Montserrat',sans-serif;color:var(--ink);background:#fdfcfa;resize:vertical}
.row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.row .grow{flex:1}
a.pubbtn{display:inline-block;border:1px solid var(--ink);background:var(--ink);color:#fff;padding:7px 13px;border-radius:99px;font:600 10.5px 'Montserrat',sans-serif;letter-spacing:.09em;text-transform:uppercase;text-decoration:none}
.row .tool{border:1px solid var(--sand);background:#fff;color:var(--ink);padding:7px 13px;border-radius:99px;font:600 10.5px 'Montserrat',sans-serif;letter-spacing:.09em;text-transform:uppercase;cursor:pointer;text-decoration:none;display:inline-block}
button.remove{border:1px solid #e5cfc6;background:#fff;color:var(--danger)}
button.done-toggle{border:1px solid var(--sand);background:#fff;color:var(--mut)}
.card.is-done button.done-toggle{border-color:var(--ok);color:var(--ok)}
.links a{font-size:12.5px}
.placeholder{border:1px dashed var(--sand);border-radius:10px;background:#fdfcfa;color:var(--mut);font-size:13px;padding:16px 18px}
details{border:1px solid var(--sand);border-radius:10px;background:#fff;padding:12px 16px}
details summary{cursor:pointer;font-weight:600;font-size:13.5px}
details pre{white-space:pre-wrap;font:12.5px/1.6 'Montserrat',sans-serif;margin-top:10px;color:var(--ink)}
table.pub{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden;font-size:13px}
table.pub th,table.pub td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--sand)}
table.pub th{font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--mut);background:#fdfcfa}
ul.reflist{list-style:none;background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden}
ul.reflist li{padding:10px 14px;border-bottom:1px solid var(--sand);font-size:13.5px;display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
ul.reflist li span.d{color:var(--mut);font-size:11.5px;white-space:nowrap}
.brandgrid{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:16px}
.brandgrid .bcard{background:#fff;border:1px solid var(--sand);border-radius:10px;padding:14px;text-align:center;width:150px}
.brandgrid img{max-width:110px;max-height:60px;object-fit:contain}
.brandgrid a{display:block;font-size:11px;margin-top:8px}
.slides{display:flex;gap:6px;flex-wrap:wrap}
.slides img{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--sand)}
#imgmodal{display:none;position:fixed;inset:0;background:rgba(43,38,32,.72);z-index:200;padding:24px;overflow-y:auto}
#imgmodal.open{display:block}
#imgmodal .box{max-width:900px;margin:0 auto;background:var(--cream);border-radius:12px;padding:20px}
#imgmodal .box h3{font-size:22px;margin-bottom:4px}
#imgmodal .box p{color:var(--mut);font-size:12.5px;margin-bottom:14px}
#imgmodal .box h4{font:600 12px 'Montserrat',sans-serif;text-transform:uppercase;letter-spacing:.12em;margin:18px 0 8px;color:var(--ink)}
#imgmodal .box h4 .secnote{color:var(--mut);font-weight:400;text-transform:none;letter-spacing:0;font-size:11.5px}
#imgmodal .userimgs:empty::after{content:"Nessuna immagine tua, per ora.";color:var(--mut);font-size:12px}
#imgmodal .userimgs .uwrap{position:relative}
#imgmodal .userimgs .uwrap .udel{position:absolute;top:4px;right:4px;background:rgba(43,38,32,.75);color:#fff;border:none;border-radius:99px;width:22px;height:22px;font-size:12px;cursor:pointer;line-height:1}
#imgmodal .addrow{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
#imgmodal .addrow input[type=url]{flex:1;min-width:200px;border:1px solid var(--sand);border-radius:99px;padding:8px 14px;font:12.5px 'Montserrat',sans-serif;background:#fff;color:var(--ink)}
#imgmodal .thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
#imgmodal .thumbs img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;border:2px solid transparent;cursor:pointer;background:var(--sand)}
#imgmodal .thumbs img:hover{border-color:var(--terra)}
#imgmodal .closebar{display:flex;justify-content:flex-end;margin-top:14px}
footer{margin-top:60px;color:var(--mut);font-size:11.5px;border-top:1px solid var(--sand);padding-top:16px}
@media (max-width:640px){.grid{grid-template-columns:1fr}.platform-head h2{font-size:27px}}
"""

APP_JS = """
(function() {
  var LS_TAB = 'mv-panel-tab', LS_DONE = 'mv-panel-done',
      LS_DEL = 'mv-panel-deleted', LS_IMG = 'mv-panel-img';
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function getSet(k) {
    try { return new Set(JSON.parse(lsGet(k) || '[]')); } catch (e) { return new Set(); }
  }
  function saveSet(k, s) { lsSet(k, JSON.stringify(Array.from(s))); }
  function getMap(k) {
    try { return JSON.parse(lsGet(k) || '{}'); } catch (e) { return {}; }
  }

  // ── Tabs ──
  var tabs = document.querySelectorAll('nav.tabs button');
  function show(id) {
    tabs.forEach(function(b) { b.classList.toggle('active', b.dataset.tab === id); });
    document.querySelectorAll('.tabpane').forEach(function(p) {
      p.classList.toggle('active', p.id === 'tab-' + id);
    });
    lsSet(LS_TAB, id);
  }
  tabs.forEach(function(b) {
    b.addEventListener('click', function() { show(b.dataset.tab); window.scrollTo(0, 0); });
  });
  var saved = lsGet(LS_TAB);
  show(saved && document.getElementById('tab-' + saved) ? saved : 'instagram');

  // ── Stato iniziale card: fatto / eliminate / immagini scelte ──
  var done = getSet(LS_DONE), removed = getSet(LS_DEL), imgs = getMap(LS_IMG);
  document.querySelectorAll('.card[data-done-id]').forEach(function(c) {
    var id = c.dataset.doneId;
    if (done.has(id)) markDone(c, true);
    if (removed.has(id)) c.classList.add('is-removed');
    if (imgs[id]) applyImage(c, imgs[id]);
  });
  refreshCounters();

  function markDone(card, on) {
    card.classList.toggle('is-done', on);
    var b = card.querySelector('button.done-toggle');
    if (b) b.textContent = on ? 'Riattiva' : 'Segna come fatto';
  }
  // ── Immagini della manager (upload/URL) ──
  var LS_USR = 'mv-panel-userimgs';
  function userImgs() {
    try { return JSON.parse(lsGet(LS_USR) || '[]'); } catch (e) { return []; }
  }
  function saveUserImgs(list) {
    try { localStorage.setItem(LS_USR, JSON.stringify(list)); return true; }
    catch (e) {
      alert('Spazio del browser esaurito: elimina qualche immagine caricata e riprova.');
      return false;
    }
  }
  function resolvePick(val) {
    if (!val) return null;
    if (val.indexOf('user:') === 0) {
      var id = val.slice(5);
      var hit = userImgs().filter(function(u) { return u.id === id; })[0];
      return hit ? hit.src : null;
    }
    return val;
  }
  function applyImage(card, pickVal) {
    var src = resolvePick(pickVal);
    if (!src) return;
    var img = card.querySelector('img.hero');
    var dl = card.querySelector('a.dl');
    if (img) { img.src = src; }
    else {
      var wrap = card.querySelector('.imgwrap');
      if (wrap) {
        img = document.createElement('img');
        img.className = 'hero'; img.loading = 'lazy'; img.src = src;
        wrap.appendChild(img);
      }
    }
    if (dl) { dl.href = src; dl.removeAttribute('hidden'); }
  }
  function renderUserImgs() {
    var box = document.querySelector('#imgmodal .userimgs');
    if (!box) return;
    box.innerHTML = '';
    userImgs().forEach(function(u) {
      var w = document.createElement('div');
      w.className = 'uwrap';
      var im = document.createElement('img');
      im.src = u.src; im.dataset.pick = 'user:' + u.id; im.title = u.name || '';
      var del = document.createElement('button');
      del.className = 'udel'; del.type = 'button'; del.textContent = '\\u00d7';
      del.dataset.del = u.id;
      w.appendChild(im); w.appendChild(del);
      box.appendChild(w);
    });
  }
  function refreshCounters() {
    document.querySelectorAll('.planbar').forEach(function(bar) {
      var pane = bar.closest('.tabpane');
      var hidden = pane.querySelectorAll('.card.is-removed').length;
      var span = bar.querySelector('.delcount');
      var btn = bar.querySelector('button.restore');
      if (span) span.textContent = hidden === 0 ? 'nessuna proposta eliminata'
        : (hidden === 1 ? '1 proposta eliminata' : hidden + ' proposte eliminate');
      if (btn) btn.style.display = hidden ? '' : 'none';
    });
  }

  // ── Modal immagini ──
  var modal = document.getElementById('imgmodal');
  var targetCard = null;
  function openModal(card) {
    targetCard = card;
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    modal.classList.remove('open');
    document.body.style.overflow = '';
    targetCard = null;
  }
  modal.addEventListener('click', function(ev) {
    if (ev.target === modal || ev.target.closest('.closebtn')) { closeModal(); return; }
    var del = ev.target.closest('button.udel');
    if (del) {
      saveUserImgs(userImgs().filter(function(u) { return u.id !== del.dataset.del; }));
      renderUserImgs();
      return;
    }
    var t = ev.target.closest('.thumbs img[data-pick]');
    if (t && targetCard) {
      var id = targetCard.dataset.doneId;
      var m = getMap(LS_IMG);
      m[id] = t.dataset.pick;
      lsSet(LS_IMG, JSON.stringify(m));
      applyImage(targetCard, t.dataset.pick);
      closeModal();
    }
  });

  // Upload dal computer: ridimensiona (max 1600px) e salva su localStorage.
  var upload = document.getElementById('imgupload');
  if (upload) upload.addEventListener('change', function() {
    var f = upload.files && upload.files[0];
    upload.value = '';
    if (!f || f.type.indexOf('image/') !== 0) return;
    var img = new Image();
    img.onload = function() {
      var MAX = 1600;
      var scale = Math.min(1, MAX / Math.max(img.width, img.height));
      var cv = document.createElement('canvas');
      cv.width = Math.round(img.width * scale);
      cv.height = Math.round(img.height * scale);
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      var dataUrl = cv.toDataURL('image/jpeg', 0.82);
      URL.revokeObjectURL(img.src);
      var list = userImgs();
      list.unshift({ id: String(Date.now()), src: dataUrl, name: f.name || '' });
      if (saveUserImgs(list)) renderUserImgs();
    };
    img.onerror = function() { URL.revokeObjectURL(img.src); };
    img.src = URL.createObjectURL(f);
  });

  // Aggiunta da URL (deve essere un link diretto a un'immagine).
  var urlBtn = document.getElementById('imgurladd');
  if (urlBtn) urlBtn.addEventListener('click', function() {
    var inp = document.getElementById('imgurl');
    var u = (inp.value || '').trim();
    if (!/^https?:\\/\\//.test(u)) { alert('Incolla un URL valido (https://…)'); return; }
    var list = userImgs();
    list.unshift({ id: String(Date.now()), src: u, name: u.split('/').pop() });
    if (saveUserImgs(list)) { renderUserImgs(); inp.value = ''; }
  });

  renderUserImgs();

  // ── Azioni card ──
  document.addEventListener('click', function(ev) {
    var pub = ev.target.closest('a.pubbtn[data-autocopy]');
    if (pub) {
      var pta = pub.closest('.pad') && pub.closest('.pad').querySelector('textarea');
      if (pta) { try { navigator.clipboard.writeText(pta.value); } catch (e) {} }
      return; // l'anchor prosegue verso LinkedIn
    }
    var card = ev.target.closest('.card[data-done-id]');
    if (!card) return;
    var id = card.dataset.doneId;
    if (ev.target.closest('button.done-toggle')) {
      var s = getSet(LS_DONE);
      var on = !s.has(id);
      if (on) s.add(id); else s.delete(id);
      saveSet(LS_DONE, s);
      markDone(card, on);
      return;
    }
    if (ev.target.closest('button.remove')) {
      var r = getSet(LS_DEL);
      r.add(id); saveSet(LS_DEL, r);
      card.classList.add('is-removed');
      refreshCounters();
      return;
    }
    if (ev.target.closest('button.pickimg')) { openModal(card); return; }
  });

  // ── Ripristina eliminati (per tab) ──
  document.querySelectorAll('button.restore').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var pane = btn.closest('.tabpane');
      var r = getSet(LS_DEL);
      pane.querySelectorAll('.card.is-removed').forEach(function(c) {
        r.delete(c.dataset.doneId);
        c.classList.remove('is-removed');
      });
      saveSet(LS_DEL, r);
      refreshCounters();
    });
  });
})();
"""


def _entry_card(e: dict, platform: str) -> str:
    img_html = ""
    dl_hidden = "" if e["image"] else " hidden"
    if e["image"]:
        img_html = f'<img class="hero" loading="lazy" src="{_e(e["image"])}" alt="">'
    slides = ""
    if e["slides"]:
        thumbs = "".join(
            f'<a href="{_e(s)}" target="_blank"><img src="{_e(s)}" alt=""></a>' for s in e["slides"]
        )
        slides = f'<div class="slides">{thumbs}</div>'
    links = []
    if e["article_url"]:
        links.append(f'<a href="{_e(e["article_url"])}" target="_blank" rel="noopener">Apri articolo →</a>')
    if e.get("author"):
        links.append(f'<a href="{_e(e["author_url"])}" target="_blank" rel="noopener">{_e(e["author"])} →</a>')
    if e["source_url"]:
        label = "Apri il post su X →" if _X_STATUS_RX.match(e["source_url"]) else "Fonte →"
        links.append(f'<a href="{_e(e["source_url"])}" target="_blank" rel="noopener">{label}</a>')
    links_html = f'<div class="links">{" · ".join(links)}</div>' if links else ""

    origin_cls = "o-" + e["origin"].lower()
    xlen = f' · {e["x_len"]}/280' if e.get("x_len") else ""
    sect = f'<span class="badge">{_e(e["section"])}</span>' if e["section"] else ""
    score = ""
    if e.get("score") is not None:
        score = f'<span class="badge trend" title="Score radar: trazione della conversazione al momento del rilevamento">▲ {_e(str(e["score"]))}</span>'

    pub = ""
    if e.get("share_x"):
        pub = f'<a class="pubbtn" href="{_e(e["share_x"])}" target="_blank" rel="noopener">Pubblica su X ↗</a>'
    elif e.get("share_li"):
        pub = (f'<a class="pubbtn" data-autocopy="1" href="{_e(e["share_li"])}" '
               f'target="_blank" rel="noopener">Pubblica su LinkedIn ↗</a>')

    return f"""
<div class="card" data-done-id="{_e(e["id"])}">
  <div class="imgwrap">{img_html}</div>
  <div class="pad">
    <div class="meta"><span>{_e(e["date"])}</span><span class="badge {origin_cls}">{_e(e["origin"])}</span>{score}{sect}<span class="badge {BADGE_CLASS[platform]}">{_e(PLATFORM_LABEL[platform])}{xlen}</span></div>
    <h3>{_e(e["title"])}</h3>
    {links_html}
    {slides}
    <div class="cap"><textarea readonly rows="6">{_e(e["text"])}</textarea></div>
    <div class="row">
      {pub}
      <a class="tool dl" href="{_e(e["image"] or "#")}" download{dl_hidden}>Scarica immagine</a>
      <button class="tool pickimg" type="button">Cambia immagine</button>
      <button class="tool remove" type="button">Elimina</button>
      <button class="tool done-toggle" type="button">Segna come fatto</button>
    </div>
  </div>
</div>"""


def _published_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="placeholder">Nessun post pubblicato registrato per questo canale.</div>'
    trs = []
    for p in rows:
        link = f'<a href="{_e(p["source_url"])}" target="_blank" rel="noopener">link</a>' if p["source_url"] else ""
        trs.append(
            f'<tr><td>{_e(p["date"])}</td><td>{_e((p["title"] or p["slug"]).replace("-", " "))}</td>'
            f'<td>{_e(p["type"])}</td><td>{link}</td></tr>'
        )
    return (
        '<div style="overflow-x:auto"><table class="pub">'
        "<tr><th>Data</th><th>Post</th><th>Tipo</th><th>Fonte</th></tr>"
        f'{"".join(trs)}</table></div>'
    )


FLOW_NOTES = {
    "instagram": ("Per pubblicare: scarica l'immagine e posta dall'app con il testo della card."),
    "x": ("“Pubblica su X” apre il compositore col post già scritto. I post Reattivi "
          "mostrano lo score ▲ (trazione della conversazione al rilevamento) e stanno "
          "in alto; il link @autore e “Apri il post su X” portano alla conversazione "
          "originale per approfondire. Metriche live con lo step 2."),
    "linkedin": ("“Pubblica su LinkedIn” copia il testo e apre il compositore col link "
                 "dell'articolo già allegato come anteprima: incolla il testo sopra. "
                 "Caption entro i 400 caratteri, tono esplicativo."),
}


def _platform_pane(platform: str, entries: list[dict], published: list[dict]) -> str:
    label = PLATFORM_LABEL[platform]
    cards = "".join(_entry_card(e, platform) for e in entries)
    if not cards:
        cards = '<div class="placeholder">Nessuna proposta al momento.</div>'
    return f"""
<div class="tabpane" id="tab-{platform}">
  <div class="platform-head"><h2>{label}</h2><span class="note">seleziona le proposte: elimina ciò che non serve, "fatto" ciò che hai pubblicato</span></div>

  <section>
    <h2>Conversazioni da presidiare</h2>
    <p class="lead">Post e thread dove conviene entrare con un commento, con risposta suggerita.</p>
    <div class="placeholder">🔭 Il monitoraggio conversazioni per {label} si attiva nello <strong>step 2</strong>
    (richiede la riattivazione dei servizi di scansione). Per ora questa sezione resta vuota di proposito.</div>
  </section>

  <section>
    <h2>Piano editoriale — proposte</h2>
    <p class="lead">Tutte le proposte per {label} in un'unica lista, più recente in alto. L'etichetta dice l'origine:
    <strong>Journal</strong> = ricavata da un articolo del sito (si rinnova ogni giorno),
    <strong>Reattivo</strong> = scritta dalla redazione su una notizia,
    <strong>Editoriale</strong> = contenuto istituzionale indipendente dal blog.
    {FLOW_NOTES.get(platform, "")} I link hanno UTM per misurare il traffico in Analytics.</p>
    <div class="planbar"><span class="delcount"></span><button class="restore" type="button">Ripristina eliminati</button></div>
    <div class="grid">{cards}</div>
  </section>

  <section>
    <h2>Già pubblicati</h2>
    <p class="lead">Archivio anti-duplicati: prima di postare, controlla qui.</p>
    {_published_table(published)}
  </section>
</div>"""


def render(entries_by_platform, journal_ref, published, brand, pool, stock,
           password_hash) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")

    panes = "".join(
        _platform_pane(p, entries_by_platform[p], published[p]) for p in PLATFORMS
    )

    j_cards = []
    for a in journal_ref:
        img = f'<img class="hero" loading="lazy" src="{_e(a["image"])}" alt="">' if a["image"] else ""
        mail_subject = quote(f"Journal — richiesta modifica/rimozione: {a['title']}")
        mail_body = quote(
            f"Articolo: {a['title']}\nURL: {a['article_url']}\n\n"
            "Richiesta (modifica testo / correzione / rimozione dal sito):\n"
        )
        mailto = f"mailto:info@myvilla.la?subject={mail_subject}&body={mail_body}"
        excerpt = f'<p class="excerpt">{_e(a["subtitle"])}</p>' if a.get("subtitle") else ""
        sect = f'<span class="badge">{_e(a["section"])}</span>' if a["section"] else ""
        j_cards.append(f"""
<div class="card" data-done-id="journal:{_e(a["id"].split(":", 1)[1])}">
  <div class="imgwrap">{img}</div>
  <div class="pad">
    <div class="meta"><span>{_e(a["date"])}</span>{sect}</div>
    <h3>{_e(a["title"])}</h3>
    {excerpt}
    <div class="links"><a href="{_e(a["article_url"])}" target="_blank" rel="noopener">Apri articolo →</a></div>
    <div class="row">
      <a class="tool" href="{mailto}">✎ Richiedi modifica o rimozione</a>
      <button class="tool remove" type="button">Elimina dalla lista</button>
    </div>
  </div>
</div>""")
    journal_pane = f"""
<div class="tabpane" id="tab-journal">
  <div class="platform-head"><h2>Journal</h2><span class="note">gli articoli del sito, in ordine di pubblicazione</span></div>
  <section>
    <p class="lead">Gli ultimi {len(journal_ref)} articoli pubblicati su myvilla.la — le proposte social ricavate da ciascuno sono nelle tab Instagram / X / LinkedIn.
    "Elimina dalla lista" nasconde l'articolo solo da questa vista; "Richiedi modifica o rimozione" prepara una mail alla redazione, che interviene sull'articolo vero e proprio.</p>
    <div class="planbar"><span class="delcount"></span><button class="restore" type="button">Ripristina eliminati</button></div>
    <div class="grid">{''.join(j_cards)}</div>
  </section>
</div>"""

    b_cards = "".join(
        f'<div class="bcard"><img src="{_e(f)}" alt=""><a href="{_e(f)}" download>{_e(Path(f).name)}</a></div>'
        for f in brand["files"]
    )
    bank = ""
    if brand["caption_bank"]:
        bank = f'<details><summary>Caption bank Instagram (maggio 2026)</summary><pre>{_e(brand["caption_bank"])}</pre></details>'
    brand_pane = f"""
<div class="tabpane" id="tab-brand">
  <div class="platform-head"><h2>Brand kit</h2></div>
  <section>
    <p class="lead">Loghi ufficiali (SVG vettoriale + PNG) e la caption bank di riferimento.</p>
    <div class="brandgrid">{b_cards}</div>
    {bank}
  </section>
</div>"""

    pool_thumbs = "".join(
        f'<img loading="lazy" src="{_e(p)}" data-pick="{_e(p)}" alt="">' for p in pool
    )
    stock_thumbs = "".join(
        f'<img loading="lazy" src="{_e(s["thumb"])}" data-pick="{_e(s["full"])}" '
        f'title="Photo by {_e(s["author"])} on Unsplash" alt="">'
        for s in stock
    )
    stock_section = ""
    if stock_thumbs:
        stock_section = f"""
  <h4>Unsplash — stock gratuite <span class="secnote">selezione rinnovata ogni settimana · crediti nel tooltip</span></h4>
  <div class="thumbs">{stock_thumbs}</div>"""

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Villa — Social Content Panel</title>
{_gate(password_hash)}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Montserrat:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <div class="brand">MY VILLA</div>
  <div class="sub">Social Content Panel — riservato</div>
  <div class="stamp">Aggiornato: {now} · rigenerato automaticamente dalla pipeline quotidiana ·
  le scelte (eliminati, fatti, immagini) restano salvate su questo browser</div>
</header>
<nav class="tabs">
  <button data-tab="instagram">Instagram</button>
  <button data-tab="x">X</button>
  <button data-tab="linkedin">LinkedIn</button>
  <button data-tab="journal">Journal</button>
  <button data-tab="brand">Brand kit</button>
</nav>
<div class="wrap">
{panes}
{journal_pane}
{brand_pane}
<footer>
  Pagina riservata al team My Villa — non indicizzata. Non condividere il link né la password all'esterno.<br>
  Le proposte si pubblicano manualmente sui canali. Eliminazioni, "fatto" e immagini scelte sono salvate solo su questo browser.
</footer>
</div>
<div id="imgmodal"><div class="box">
  <h3>Scegli un'immagine</h3>
  <p>Clicca una miniatura per applicarla alla card.</p>
  <h4>Dal sito <span class="secnote">render, visual social e hero recenti del Journal</span></h4>
  <div class="thumbs">{pool_thumbs}</div>
  {stock_section}
  <h4>Le tue immagini <span class="secnote">salvate solo su questo browser</span></h4>
  <div class="thumbs userimgs"></div>
  <div class="addrow">
    <label class="tool">Carica dal computer<input id="imgupload" type="file" accept="image/*" hidden></label>
    <input id="imgurl" type="url" placeholder="…oppure incolla l'URL di un'immagine">
    <button class="tool" id="imgurladd" type="button">Aggiungi da URL</button>
  </div>
  <div class="closebar"><button class="tool closebtn" type="button">Chiudi</button></div>
</div></div>
<script>{APP_JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_panel(password: str | None = None, out: Path | None = None) -> Path:
    out_dir = (out or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    journal_entries = collect_journal_entries()
    queue_entries = collect_queue_entries()
    packages = collect_package_entries()

    entries_by_platform: dict[str, list[dict]] = {}
    for p in PLATFORMS:
        merged = journal_entries[p] + queue_entries[p]
        if p == "instagram":
            merged += packages
        if p == "x":
            # Richiesta social manager: i post con più trazione in alto
            # (score radar desc), a parità/assenza di score la data.
            merged.sort(
                key=lambda e: (
                    e.get("score") if isinstance(e.get("score"), (int, float)) else -1,
                    e["date"],
                ),
                reverse=True,
            )
        else:
            merged.sort(key=lambda e: e["date"], reverse=True)
        entries_by_platform[p] = merged

    page = render(
        entries_by_platform,
        journal_entries["instagram"],  # riferimento Journal (stessi articoli)
        collect_published(),
        collect_brand(),
        build_image_pool(),
        collect_stock_images(),
        _sha256(password or DEFAULT_PASSWORD),
    )
    target = out_dir / "index.html"
    target.write_text(page, encoding="utf-8")
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    args = ap.parse_args()
    target = build_panel(password=args.password)
    size = target.stat().st_size
    print(f"[social-panel] wrote {target.relative_to(ROOT)} ({size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
