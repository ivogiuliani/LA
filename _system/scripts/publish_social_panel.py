#!/usr/bin/env python3
"""
publish_social_panel.py — static, password-gated content panel for the
social media manager at https://myvilla.la/team/social/.

Scope (deliberately narrow — "opzione 1 scoped", 2026-09-01):
  - Journal articles (fresh daily) with a template suggested caption
  - Prepared social captions (posts/planned + approved + reactive)
  - Editorial packages ready to publish (_publish_ready)
  - Published-post archive (anti-duplicate reference)
  - Brand assets + Instagram caption bank (copied out of _-prefixed
    dirs, which Jekyll does NOT serve on GitHub Pages)

NOT included on purpose: journalist replies, outreach emails, radar
contacts — nothing sensitive to press relations.

Threat model = same soft gate the operator chose for the old
/team/radar/ page ("Strada 1", commit aafabe15 → removed in 912f1f36):
client-side SHA-256 password prompt + noindex + robots Disallow.
The repo is public: a determined attacker can read the HTML from
GitHub and bypass the gate. Good enough for trusted collaborators;
for real auth use the Cloudflare Tunnel path.

Usage:
    python3 publish_social_panel.py                     # default password
    python3 publish_social_panel.py --password segreta  # custom gate
Called automatically by publish_all_drafts.py before the daily
autopush so the Journal section stays current.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

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

BASE_HASHTAGS = "#MyVilla #MyVillaLA #FireResilient #ReinforcedConcrete #LuxuryHomes #LosAngeles"


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #

def _tagify(tag: str) -> str:
    """'housing market' -> '#HousingMarket' (alnum only)."""
    words = re.findall(r"[A-Za-z0-9]+", tag)
    return "#" + "".join(w.capitalize() if not w.isupper() else w for w in words) if words else ""


def _ig_proposal(d: dict, url: str) -> str:
    """Instagram post proposal: title, subtitle, key-data bullets,
    link, generous hashtag block (IG rewards them)."""
    parts = [d.get("title") or "", ""]
    sub = d.get("subtitle") or d.get("meta_description") or ""
    if sub:
        parts += [sub, ""]
    bullets = []
    for k in (d.get("key_data") or [])[:3]:
        num = (k.get("number") or "").strip()
        lab = " ".join((k.get("label") or "").split())
        if num and lab:
            bullets.append(f"▪ {num} — {lab}")
    if bullets:
        parts += bullets + [""]
    tags = [t for t in (d.get("topic_tags") or [])[:5]]
    hashtags = " ".join(filter(None, [BASE_HASHTAGS] + [_tagify(t) for t in tags]))
    parts += [f"Full analysis on the My Villa Journal → {url}", "", hashtags]
    return "\n".join(parts)


def _x_proposal(d: dict, url: str) -> str:
    """X post proposal, guaranteed within 280 chars (t.co link = 23)."""
    title = d.get("title") or ""
    sub = d.get("subtitle") or d.get("meta_description") or ""
    tags = "#MyVilla #FireResilient"
    T_CO = 23
    budget = 280 - T_CO - len(tags) - 6  # separators/newlines
    body = title[:budget]
    remaining = budget - len(body)
    if sub and remaining > 40:
        room = remaining - 2
        s = sub if len(sub) <= room else sub[: room - 1].rsplit(" ", 1)[0] + "…"
        body = f"{body}\n\n{s}"
    return f"{body}\n\n{url}\n{tags}"


def _x_effective_len(text: str, url: str) -> int:
    """Length as X counts it: any URL costs 23 chars via t.co."""
    return len(text) - len(url) + 23


def collect_journal() -> list[dict]:
    """Latest published Journal articles from the blog/*.json sidecars."""
    items = []
    for jf in BLOG.glob("*.json"):
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or jf.stem
        if not (BLOG / f"{slug}.html").exists():
            continue  # sidecar without published HTML = draft, skip
        hero_rel = None
        hero = d.get("hero_image") or {}
        if isinstance(hero, dict) and hero.get("local_path"):
            base = Path(hero["local_path"]).name
            if (BLOG / "assets" / "img" / base).exists():
                hero_rel = f"/blog/assets/img/{base}"
        url = f"https://myvilla.la/blog/{slug}.html"
        x_text = _x_proposal(d, url)
        items.append({
            "slug": slug,
            "title": d.get("title") or slug,
            "subtitle": d.get("subtitle") or "",
            "date": d.get("_date") or "",
            "section": d.get("_section_name") or d.get("section") or "",
            "hero": hero_rel,
            "url": url,
            "ig": _ig_proposal(d, url),
            "x": x_text,
            "x_len": _x_effective_len(x_text, url),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:JOURNAL_LIMIT]


def _parse_post_md(path: Path) -> dict | None:
    """Parse a social draft .md (YAML frontmatter + caption body)."""
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
    img = (meta.get("image") or "").strip()
    img_rel = f"/{img}" if img and (ROOT / img).exists() else None
    return {
        "file": path.name,
        "channel": (meta.get("channel") or "?").replace("instagram", "ig").replace("twitter", "x"),
        "type": meta.get("type") or "",
        "date": str(meta.get("date") or ""),
        "slug": meta.get("slug") or path.stem,
        "image": img_rel,
        "source_url": meta.get("url") or meta.get("article_url") or "",
        "caption": body,
    }


def collect_captions() -> list[dict]:
    """Prepared captions grouped by slug, from planned/approved/reactive."""
    groups: dict[str, dict] = {}
    for status in ("planned", "approved", "reactive"):
        folder = POSTS / status
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            p = _parse_post_md(f)
            if not p:
                continue
            g = groups.setdefault(p["slug"], {
                "slug": p["slug"], "date": p["date"], "status": status,
                "image": None, "source_url": "", "variants": [],
            })
            g["variants"].append({"channel": p["channel"], "caption": p["caption"]})
            g["image"] = g["image"] or p["image"]
            g["source_url"] = g["source_url"] or p["source_url"]
            g["date"] = max(g["date"], p["date"])
    return sorted(groups.values(), key=lambda x: x["date"], reverse=True)


def collect_editorial() -> list[dict]:
    """Ready editorial packages; copies their assets under the panel dir."""
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
        target = meta.get("publish_target") or {}
        post = meta.get("post") or {}
        out.append({
            "name": pkg.name,
            "caption": caption,
            "image": img_rel,
            "slides": slide_rels,
            "platform": target.get("platform") or "instagram",
            "fmt": post.get("format") or "",
            "pillar": post.get("pillar") or "",
            "hashtags": " ".join("#" + h for h in (post.get("hashtags") or [])),
        })
    return out


def collect_published() -> list[dict]:
    """Archive of already-published posts (anti-duplicate reference)."""
    out = []
    folder = POSTS / "published"
    if folder.is_dir():
        for f in sorted(folder.glob("*.md")):
            p = _parse_post_md(f)
            if p:
                out.append(p)
    # editorial published subfolder holds package dirs, list names only
    ed_pub = POSTS / "editorial" / "published"
    if ed_pub.is_dir():
        for pkg in sorted(ed_pub.iterdir()):
            if pkg.is_dir():
                out.append({"file": pkg.name, "channel": "ig", "type": "editorial",
                            "date": pkg.name[:10], "slug": pkg.name[11:],
                            "image": None, "source_url": "", "caption": ""})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def collect_brand() -> dict:
    """Copy brand assets out of the Jekyll-hidden _social_handoff dir."""
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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def _copy_block(caption: str, label: str = "Copia caption") -> str:
    return (
        f'<div class="cap"><textarea readonly rows="6">{_e(caption)}</textarea>'
        f'<button class="copy" type="button">{_e(label)}</button></div>'
    )


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
:root{--ink:#2b2620;--cream:#faf8f5;--sand:#e9e2d6;--terra:#b06a4a;--mut:#8a8177;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--cream);color:var(--ink);font-family:'Montserrat',-apple-system,sans-serif;font-size:15px;line-height:1.55}
h1,h2,h3{font-family:'Cormorant Garamond',Georgia,serif;font-weight:600}
a{color:var(--terra)}
.wrap{max-width:1060px;margin:0 auto;padding:0 18px 80px}
header.top{padding:26px 18px 14px;border-bottom:1px solid var(--sand);background:var(--cream);}
header.top .brand{font-family:'Cormorant Garamond',serif;font-size:26px;letter-spacing:.14em}
header.top .sub{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.22em;margin-top:2px}
header.top .stamp{color:var(--mut);font-size:12px;margin-top:8px}
nav.sticky{position:sticky;top:0;z-index:50;background:var(--cream);border-bottom:1px solid var(--sand);overflow-x:auto;white-space:nowrap;padding:10px 18px;display:flex;gap:18px}
nav.sticky a{color:var(--ink);text-decoration:none;font-size:12px;text-transform:uppercase;letter-spacing:.14em;padding:4px 0;border-bottom:2px solid transparent}
nav.sticky a:hover{border-color:var(--terra)}
section{margin-top:46px}
section>h2{font-size:30px;margin-bottom:4px}
section>p.lead{color:var(--mut);font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card img.hero{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--sand)}
.card .pad{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.card h3{font-size:19px;line-height:1.25}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:var(--sand);color:var(--ink)}
.badge.ig{background:#e8d5e2}.badge.x{background:#d7dee8}.badge.st{background:#efe6d8;color:#7a6a4f}
.cap{margin-top:4px}
.cap textarea{width:100%;border:1px solid var(--sand);border-radius:8px;padding:10px;font:12.5px/1.5 'Montserrat',sans-serif;color:var(--ink);background:#fdfcfa;resize:vertical}
button.copy{margin-top:6px;border:1px solid var(--terra);background:var(--terra);color:#fff;padding:7px 14px;border-radius:99px;font:600 11px 'Montserrat',sans-serif;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
button.copy.done{background:#5f7a5a;border-color:#5f7a5a}
.links a{font-size:12.5px}
details{border:1px solid var(--sand);border-radius:10px;background:#fff;padding:12px 16px}
details summary{cursor:pointer;font-weight:600;font-size:13.5px}
details pre{white-space:pre-wrap;font:12.5px/1.6 'Montserrat',sans-serif;margin-top:10px;color:var(--ink)}
table.pub{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden;font-size:13px}
table.pub th,table.pub td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--sand)}
table.pub th{font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--mut);background:#fdfcfa}
.brandgrid{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:16px}
.brandgrid .bcard{background:#fff;border:1px solid var(--sand);border-radius:10px;padding:14px;text-align:center;width:150px}
.brandgrid img{max-width:110px;max-height:60px;object-fit:contain}
.brandgrid a{display:block;font-size:11px;margin-top:8px}
.slides{display:flex;gap:6px;flex-wrap:wrap}
.slides img{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--sand)}
footer{margin-top:60px;color:var(--mut);font-size:11.5px;border-top:1px solid var(--sand);padding-top:16px}
@media (max-width:640px){.grid{grid-template-columns:1fr}section>h2{font-size:25px}}
"""

COPY_JS = """
document.addEventListener('click', function(ev) {
  var btn = ev.target.closest('button.copy');
  if (!btn) return;
  var ta = btn.parentElement.querySelector('textarea');
  if (!ta) return;
  navigator.clipboard.writeText(ta.value).then(function() {
    var old = btn.textContent;
    btn.textContent = 'Copiata \\u2713';
    btn.classList.add('done');
    setTimeout(function() { btn.textContent = old; btn.classList.remove('done'); }, 1600);
  });
});
"""


def render(journal, captions, editorial, published, brand, password_hash) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")

    j_cards = []
    for a in journal:
        img = f'<img class="hero" loading="lazy" src="{_e(a["hero"])}" alt="">' if a["hero"] else ""
        j_cards.append(f"""
<div class="card">{img}<div class="pad">
  <div class="meta"><span>{_e(a["date"])}</span>{f'<span class="badge">{_e(a["section"])}</span>' if a["section"] else ''}</div>
  <h3>{_e(a["title"])}</h3>
  <div class="links"><a href="{_e(a["url"])}" target="_blank" rel="noopener">Apri articolo →</a></div>
  <div class="meta" style="margin-top:6px"><span class="badge ig">IG</span><span>Proposta Instagram</span></div>
  {_copy_block(a["ig"], "Copia post Instagram")}
  <div class="meta" style="margin-top:6px"><span class="badge x">X</span><span>Proposta X · {a["x_len"]}/280</span></div>
  {_copy_block(a["x"], "Copia post X")}
</div></div>""")

    c_cards = []
    for g in captions:
        img = f'<img class="hero" loading="lazy" src="{_e(g["image"])}" alt="">' if g["image"] else ""
        variants = "".join(
            f'<div class="meta" style="margin-top:6px"><span class="badge {v["channel"]}">{_e(v["channel"].upper())}</span></div>'
            + _copy_block(v["caption"])
            for v in g["variants"]
        )
        src = f'<div class="links"><a href="{_e(g["source_url"])}" target="_blank" rel="noopener">Fonte →</a></div>' if g["source_url"] else ""
        c_cards.append(f"""
<div class="card">{img}<div class="pad">
  <div class="meta"><span>{_e(g["date"])}</span><span class="badge st">{_e(g["status"])}</span></div>
  <h3 style="font-size:16px;font-family:'Montserrat',sans-serif;font-weight:600">{_e(g["slug"].replace("-", " "))}</h3>
  {src}{variants}
</div></div>""")

    e_cards = []
    for p in editorial:
        img = f'<img class="hero" loading="lazy" src="{_e(p["image"])}" alt="">' if p["image"] else ""
        slides = ""
        if p["slides"]:
            thumbs = "".join(f'<a href="{_e(s)}" target="_blank"><img src="{_e(s)}" alt=""></a>' for s in p["slides"])
            slides = f'<div class="slides">{thumbs}</div>'
        cap_full = p["caption"] + ("\n\n" + p["hashtags"] if p["hashtags"] else "")
        e_cards.append(f"""
<div class="card">{img}<div class="pad">
  <div class="meta"><span class="badge ig">{_e(p["platform"])}</span><span class="badge">{_e(p["fmt"] or "post")}</span><span>{_e(p["pillar"])}</span></div>
  <h3 style="font-size:16px;font-family:'Montserrat',sans-serif;font-weight:600">{_e(p["name"][11:].replace("-", " ") or p["name"])}</h3>
  {slides}
  {_copy_block(cap_full)}
  {f'<div class="links"><a href="{_e(p["image"])}" download>Scarica immagine</a></div>' if p["image"] else ''}
</div></div>""")

    p_rows = []
    for p in published:
        link = f'<a href="{_e(p["source_url"])}" target="_blank" rel="noopener">link</a>' if p["source_url"] else ""
        p_rows.append(
            f'<tr><td>{_e(p["date"])}</td><td><span class="badge {_e(p["channel"])}">{_e(p["channel"].upper())}</span></td>'
            f'<td>{_e(p["slug"].replace("-", " "))}</td><td>{_e(p["type"])}</td><td>{link}</td></tr>'
        )

    b_cards = "".join(
        f'<div class="bcard"><img src="{_e(f)}" alt=""><a href="{_e(f)}" download>{_e(Path(f).name)}</a></div>'
        for f in brand["files"]
    )
    bank = ""
    if brand["caption_bank"]:
        bank = f'<details><summary>Caption bank Instagram (maggio 2026)</summary><pre>{_e(brand["caption_bank"])}</pre></details>'

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
  <div class="stamp">Aggiornato: {now} · rigenerato automaticamente dalla pipeline quotidiana</div>
</header>
<nav class="sticky">
  <a href="#journal">Journal</a>
  <a href="#captions">Caption pronte</a>
  <a href="#editorial">Pacchetti editoriali</a>
  <a href="#published">Già pubblicati</a>
  <a href="#brand">Brand kit</a>
</nav>
<div class="wrap">

<section id="journal">
  <h2>Dal Journal — articoli freschi</h2>
  <p class="lead">Gli ultimi {len(journal)} articoli pubblicati su myvilla.la. Ogni card ha due proposte di post pronte da copiare — una per Instagram (con dati chiave e hashtag) e una per X (già entro i 280 caratteri). Adattale liberamente; la foto è quella dell'articolo.</p>
  <div class="grid">{''.join(j_cards)}</div>
</section>

<section id="captions">
  <h2>Caption pronte</h2>
  <p class="lead">{len(captions)} post preparati dalla redazione (varianti Instagram e X). Immagini già sul sito: aprile con tap sull'anteprima, salva e posta.</p>
  <div class="grid">{''.join(c_cards)}</div>
</section>

<section id="editorial">
  <h2>Pacchetti editoriali pronti</h2>
  <p class="lead">Post evergreen completi di visual (formato carosello dove indicato). Le slide segnaposto mancanti sono da completare in fase di pubblicazione.</p>
  <div class="grid">{''.join(e_cards)}</div>
</section>

<section id="published">
  <h2>Già pubblicati</h2>
  <p class="lead">Archivio anti-duplicati: prima di postare, controlla qui.</p>
  <div style="overflow-x:auto"><table class="pub">
    <tr><th>Data</th><th>Canale</th><th>Post</th><th>Tipo</th><th>Fonte</th></tr>
    {''.join(p_rows)}
  </table></div>
</section>

<section id="brand">
  <h2>Brand kit</h2>
  <p class="lead">Loghi ufficiali (SVG vettoriale + PNG) e la caption bank di riferimento.</p>
  <div class="brandgrid">{b_cards}</div>
  {bank}
</section>

<footer>
  Pagina riservata al team My Villa — non indicizzata. Non condividere il link né la password all'esterno.<br>
  Contenuti: articoli © My Villa Journal · myvilla.la · info@myvilla.la
</footer>
</div>
<script>{COPY_JS}</script>
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
    page = render(
        collect_journal(),
        collect_captions(),
        collect_editorial(),
        collect_published(),
        collect_brand(),
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
