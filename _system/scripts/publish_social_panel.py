#!/usr/bin/env python3
"""
publish_social_panel.py — static, password-gated content panel for the
social media manager at https://myvilla.la/team/social/.

Architettura per piattaforma (step 1, 2026-09-01 — solo produzione
contenuti, nessun servizio a pagamento):

  Tab Instagram / X / LinkedIn, ognuna con:
    1. Conversazioni da presidiare  → PLACEHOLDER (step 2: riattivazione
       ig_viral_radar/Grok + nuovo radar LinkedIn; niente costi per ora)
    2. Proposte del giorno          → dai Journal freschi, formato nativo
       per canale, link con UTM (utm_source=<canale>)
    3. Coda preparata               → caption esistenti filtrate per canale
       (+ pacchetti editoriali sotto Instagram)
    4. Già pubblicati               → archivio anti-duplicati del canale
  Tab Journal (riferimento compatto) e Brand kit.

  Tutto è SOLO PROPOSTO: nessun publisher automatico. La social media
  manager copia e pubblica a mano; "Segna come fatto" è un toggle
  localStorage nel suo browser (nessun backend).

Threat model = stesso soft gate del vecchio /team/radar/ ("Strada 1"):
SHA-256 client-side + noindex + robots Disallow. Il repo è pubblico:
un attaccante determinato legge l'HTML alla fonte. Solo collaboratori
fidati; per auth vera → Cloudflare Tunnel.

Usage:
    python3 publish_social_panel.py                     # default password
    python3 publish_social_panel.py --password segreta  # custom gate
Chiamato da publish_all_drafts.py prima dell'autopush quotidiano.
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

DEFAULT_PASSWORD = "villasocial26"
JOURNAL_LIMIT = 20

PLATFORMS = ("instagram", "x", "linkedin")
PLATFORM_LABEL = {"instagram": "Instagram", "x": "X", "linkedin": "LinkedIn"}
BADGE_CLASS = {"instagram": "ig", "x": "x", "linkedin": "li", "ig": "ig"}

BASE_HASHTAGS_IG = "#MyVilla #MyVillaLA #FireResilient #ReinforcedConcrete #LuxuryHomes #LosAngeles"
BASE_HASHTAGS_LI = "#Architecture #FireResilience #LosAngeles #LuxuryRealEstate"


def _utm(url: str, platform: str) -> str:
    return f"{url}?utm_source={platform}&utm_medium=social&utm_campaign=journal"


# --------------------------------------------------------------------------- #
# Proposte per canale (template, zero API)
# --------------------------------------------------------------------------- #

def _tagify(tag: str) -> str:
    """'housing market' -> '#HousingMarket' (alnum only)."""
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
    """Instagram: sottotitolo + bullet dati + hashtag generosi."""
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
    """X: garantito entro 280 caratteri effettivi (link t.co = 23)."""
    title = d.get("title") or ""
    sub = d.get("subtitle") or d.get("meta_description") or ""
    tags = "#MyVilla #FireResilient"
    T_CO = 23
    budget = 280 - T_CO - len(tags) - 6  # separatori/newline
    body = title[:budget]
    remaining = budget - len(body)
    if sub and remaining > 40:
        room = remaining - 2
        s = sub if len(sub) <= room else sub[: room - 1].rsplit(" ", 1)[0] + "…"
        body = f"{body}\n\n{s}"
    return f"{body}\n\n{url}\n{tags}"


def _li_proposal(d: dict, url: str) -> str:
    """LinkedIn: tono professionale, hook + contesto + dati + link,
    3-5 hashtag sobri. I link nei post LinkedIn sono cliccabili."""
    title = d.get("title") or ""
    sub = d.get("subtitle") or d.get("meta_description") or ""
    parts = [title, ""]
    if sub:
        parts += [sub, ""]
    bullets = _key_bullets(d)
    if bullets:
        parts += ["The numbers that frame it:"] + bullets + [""]
    parts += [
        "We unpack what this means for resilient residential construction "
        "in Los Angeles on the My Villa Journal:",
        url,
        "",
    ]
    tags = [t for t in (d.get("topic_tags") or [])[:2]]
    hashtags = " ".join(filter(None, [BASE_HASHTAGS_LI] + [_tagify(t) for t in tags]))
    parts.append(hashtags)
    return "\n".join(parts)


def _x_effective_len(text: str, url: str) -> int:
    """Lunghezza come la conta X: ogni URL vale 23 char via t.co."""
    return len(text) - len(url) + 23


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #

def collect_journal() -> list[dict]:
    """Ultimi articoli Journal pubblicati, con proposta per ogni canale."""
    items = []
    for jf in BLOG.glob("*.json"):
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or jf.stem
        if not (BLOG / f"{slug}.html").exists():
            continue  # sidecar senza HTML pubblicato = bozza
        hero_rel = None
        hero = d.get("hero_image") or {}
        if isinstance(hero, dict) and hero.get("local_path"):
            base = Path(hero["local_path"]).name
            if (BLOG / "assets" / "img" / base).exists():
                hero_rel = f"/blog/assets/img/{base}"
        clean_url = f"https://myvilla.la/blog/{slug}.html"
        x_text = _x_proposal(d, _utm(clean_url, "x"))
        items.append({
            "slug": slug,
            "title": d.get("title") or slug,
            "date": d.get("_date") or "",
            "section": d.get("_section_name") or d.get("section") or "",
            "hero": hero_rel,
            "url": clean_url,
            "proposals": {
                "instagram": _ig_proposal(d, _utm(clean_url, "instagram")),
                "x": x_text,
                "linkedin": _li_proposal(d, _utm(clean_url, "linkedin")),
            },
            "x_len": _x_effective_len(x_text, _utm(clean_url, "x")),
            # Bottoni "Pubblica": compositori nativi delle piattaforme,
            # la pubblicazione resta un gesto della social media manager.
            "share": {
                # X intent: apre il compositore col post GIÀ scritto.
                "x": "https://x.com/intent/post?text=" + quote(x_text, safe=""),
                # LinkedIn share-offsite: precarica solo l'URL (il testo
                # non è supportato dall'endpoint) — il click copia anche
                # la proposta negli appunti, lei la incolla sopra.
                "linkedin": ("https://www.linkedin.com/sharing/share-offsite/?url="
                             + quote(_utm(clean_url, "linkedin"), safe="")),
            },
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:JOURNAL_LIMIT]


def _parse_post_md(path: Path) -> dict | None:
    """Parse di una bozza social .md (frontmatter YAML + caption)."""
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
    channel = (meta.get("channel") or "?").replace("instagram", "ig").replace("twitter", "x")
    return {
        "file": path.name,
        "channel": channel,
        "platform": {"ig": "instagram", "x": "x", "li": "linkedin"}.get(channel, channel),
        "type": meta.get("type") or "",
        "date": str(meta.get("date") or ""),
        "slug": meta.get("slug") or path.stem,
        "image": img_rel,
        "source_url": meta.get("url") or meta.get("article_url") or "",
        "caption": body,
    }


def collect_queue() -> dict[str, list[dict]]:
    """Caption preparate, raggruppate per piattaforma."""
    by_platform: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    for status in ("planned", "approved", "reactive"):
        folder = POSTS / status
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md"), reverse=True):
            p = _parse_post_md(f)
            if not p or p["platform"] not in by_platform:
                continue
            p["status"] = status
            by_platform[p["platform"]].append(p)
    for lst in by_platform.values():
        lst.sort(key=lambda x: x["date"], reverse=True)
    return by_platform


def collect_editorial() -> list[dict]:
    """Pacchetti editoriali pronti (Instagram); copia gli asset nel pannello."""
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
        out.append({
            "name": pkg.name,
            "caption": caption,
            "image": img_rel,
            "slides": slide_rels,
            "fmt": post.get("format") or "",
            "pillar": post.get("pillar") or "",
            "hashtags": " ".join("#" + h for h in (post.get("hashtags") or [])),
        })
    return out


def collect_published() -> dict[str, list[dict]]:
    """Archivio pubblicati per piattaforma (riferimento anti-duplicati)."""
    by_platform: dict[str, list[dict]] = {p: [] for p in PLATFORMS}
    folder = POSTS / "published"
    if folder.is_dir():
        for f in sorted(folder.glob("*.md")):
            p = _parse_post_md(f)
            if p and p["platform"] in by_platform:
                by_platform[p["platform"]].append(p)
    ed_pub = POSTS / "editorial" / "published"
    if ed_pub.is_dir():
        for pkg in sorted(ed_pub.iterdir()):
            if pkg.is_dir():
                by_platform["instagram"].append({
                    "file": pkg.name, "channel": "ig", "platform": "instagram",
                    "type": "editorial", "date": pkg.name[:10],
                    "slug": pkg.name[11:], "image": None,
                    "source_url": "", "caption": "",
                })
    for lst in by_platform.values():
        lst.sort(key=lambda x: x["date"], reverse=True)
    return by_platform


def collect_brand() -> dict:
    """Brand asset copiati fuori dalle dir _ (non servite da Jekyll)."""
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


def _copy_block(caption: str, label: str = "Copia", extra: str = "") -> str:
    return (
        f'<div class="cap"><textarea readonly rows="6">{_e(caption)}</textarea>'
        f'<div class="row"><button class="copy" type="button">{_e(label)}</button>{extra}'
        f'<button class="done-toggle" type="button">Segna come fatto</button></div></div>'
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
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:#fff;border:1px solid var(--sand);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;position:relative}
.card.is-done{opacity:.45}
.card.is-done::after{content:"✓ fatto";position:absolute;top:10px;right:10px;background:#5f7a5a;color:#fff;font:600 10px 'Montserrat';letter-spacing:.1em;text-transform:uppercase;padding:3px 9px;border-radius:99px}
.card img.hero{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--sand)}
.card .pad{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.card h3{font-size:18px;line-height:1.25}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:var(--sand);color:var(--ink)}
.badge.ig{background:#e8d5e2}.badge.x{background:#d7dee8}.badge.li{background:#cfe0ee}.badge.st{background:#efe6d8;color:#7a6a4f}
.cap{margin-top:4px}
.cap textarea{width:100%;border:1px solid var(--sand);border-radius:8px;padding:10px;font:12.5px/1.5 'Montserrat',sans-serif;color:var(--ink);background:#fdfcfa;resize:vertical}
.cap .row{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap}
button.copy{border:1px solid var(--terra);background:var(--terra);color:#fff;padding:7px 14px;border-radius:99px;font:600 11px 'Montserrat',sans-serif;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
button.copy.ok{background:#5f7a5a;border-color:#5f7a5a}
button.done-toggle{border:1px solid var(--sand);background:#fff;color:var(--mut);padding:7px 14px;border-radius:99px;font:600 11px 'Montserrat',sans-serif;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
a.pubbtn{display:inline-block;border:1px solid var(--ink);background:var(--ink);color:#fff;padding:7px 14px;border-radius:99px;font:600 11px 'Montserrat',sans-serif;letter-spacing:.1em;text-transform:uppercase;text-decoration:none}
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
footer{margin-top:60px;color:var(--mut);font-size:11.5px;border-top:1px solid var(--sand);padding-top:16px}
@media (max-width:640px){.grid{grid-template-columns:1fr}.platform-head h2{font-size:27px}}
"""

APP_JS = """
(function() {
  var LS_TAB = 'mv-panel-tab', LS_DONE = 'mv-panel-done';
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function doneSet() {
    try { return new Set(JSON.parse(lsGet(LS_DONE) || '[]')); } catch (e) { return new Set(); }
  }
  function saveDone(s) { lsSet(LS_DONE, JSON.stringify(Array.from(s))); }

  // Tabs
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

  // Done state
  var done = doneSet();
  document.querySelectorAll('.card[data-done-id]').forEach(function(c) {
    if (done.has(c.dataset.doneId)) c.classList.add('is-done');
  });
  document.addEventListener('click', function(ev) {
    var t = ev.target.closest('button.done-toggle');
    if (t) {
      var card = t.closest('.card');
      var id = card && card.dataset.doneId;
      if (!id) return;
      var s = doneSet();
      if (s.has(id)) { s.delete(id); card.classList.remove('is-done'); }
      else { s.add(id); card.classList.add('is-done'); }
      saveDone(s);
      return;
    }
    // "Pubblica su LinkedIn": il compositore accetta solo l'URL, quindi
    // copiamo anche il testo proposto mentre l'anchor apre la nuova tab.
    var pub = ev.target.closest('a.pubbtn[data-autocopy]');
    if (pub) {
      var pta = pub.closest('.cap') && pub.closest('.cap').querySelector('textarea');
      if (pta) { try { navigator.clipboard.writeText(pta.value); } catch (e) {} }
      return; // niente preventDefault: l'anchor prosegue verso LinkedIn
    }
    var btn = ev.target.closest('button.copy');
    if (btn) {
      var ta = btn.closest('.cap').querySelector('textarea');
      if (!ta) return;
      navigator.clipboard.writeText(ta.value).then(function() {
        var old = btn.textContent;
        btn.textContent = 'Copiata \\u2713';
        btn.classList.add('ok');
        setTimeout(function() { btn.textContent = old; btn.classList.remove('ok'); }, 1600);
      });
    }
  });
})();
"""


def _journal_card(a: dict, platform: str) -> str:
    img = f'<img class="hero" loading="lazy" src="{_e(a["hero"])}" alt="">' if a["hero"] else ""
    extra = f' · {a["x_len"]}/280' if platform == "x" else ""
    label = f"Copia post {PLATFORM_LABEL[platform]}"
    share = a.get("share", {}).get(platform)
    pub_btn = ""
    if share and platform == "x":
        pub_btn = (f'<a class="pubbtn" href="{_e(share)}" target="_blank" rel="noopener">'
                   f'Pubblica su X ↗</a>')
    elif share and platform == "linkedin":
        # data-autocopy: al click il JS copia anche il testo negli appunti
        # (LinkedIn non accetta testo precompilato via URL).
        pub_btn = (f'<a class="pubbtn" data-autocopy="1" href="{_e(share)}" '
                   f'target="_blank" rel="noopener">Pubblica su LinkedIn ↗</a>')
    return f"""
<div class="card" data-done-id="{_e(platform)}:{_e(a["slug"])}">{img}<div class="pad">
  <div class="meta"><span>{_e(a["date"])}</span>{f'<span class="badge">{_e(a["section"])}</span>' if a["section"] else ''}<span class="badge {BADGE_CLASS[platform]}">{_e(PLATFORM_LABEL[platform])}{extra}</span></div>
  <h3>{_e(a["title"])}</h3>
  <div class="links"><a href="{_e(a["url"])}" target="_blank" rel="noopener">Apri articolo →</a></div>
  {_copy_block(a["proposals"][platform], label, pub_btn)}
</div></div>"""


def _queue_card(p: dict) -> str:
    img = f'<img class="hero" loading="lazy" src="{_e(p["image"])}" alt="">' if p["image"] else ""
    src = f'<div class="links"><a href="{_e(p["source_url"])}" target="_blank" rel="noopener">Fonte →</a></div>' if p["source_url"] else ""
    return f"""
<div class="card" data-done-id="{_e(p["platform"])}:{_e(p["file"])}">{img}<div class="pad">
  <div class="meta"><span>{_e(p["date"])}</span><span class="badge st">{_e(p.get("status", ""))}</span></div>
  <h3 style="font-size:15px;font-family:'Montserrat',sans-serif;font-weight:600">{_e(p["slug"].replace("-", " "))}</h3>
  {src}
  {_copy_block(p["caption"])}
</div></div>"""


def _editorial_card(p: dict) -> str:
    img = f'<img class="hero" loading="lazy" src="{_e(p["image"])}" alt="">' if p["image"] else ""
    slides = ""
    if p["slides"]:
        thumbs = "".join(f'<a href="{_e(s)}" target="_blank"><img src="{_e(s)}" alt=""></a>' for s in p["slides"])
        slides = f'<div class="slides">{thumbs}</div>'
    cap_full = p["caption"] + ("\n\n" + p["hashtags"] if p["hashtags"] else "")
    dl = f'<div class="links"><a href="{_e(p["image"])}" download>Scarica immagine</a></div>' if p["image"] else ""
    return f"""
<div class="card" data-done-id="instagram:editorial:{_e(p["name"])}">{img}<div class="pad">
  <div class="meta"><span class="badge ig">Instagram</span><span class="badge">{_e(p["fmt"] or "post")}</span><span>{_e(p["pillar"])}</span></div>
  <h3 style="font-size:15px;font-family:'Montserrat',sans-serif;font-weight:600">{_e(p["name"][11:].replace("-", " ") or p["name"])}</h3>
  {slides}
  {_copy_block(cap_full)}
  {dl}
</div></div>"""


def _published_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="placeholder">Nessun post pubblicato registrato per questo canale.</div>'
    trs = []
    for p in rows:
        link = f'<a href="{_e(p["source_url"])}" target="_blank" rel="noopener">link</a>' if p["source_url"] else ""
        trs.append(
            f'<tr><td>{_e(p["date"])}</td><td>{_e(p["slug"].replace("-", " "))}</td>'
            f'<td>{_e(p["type"])}</td><td>{link}</td></tr>'
        )
    return (
        '<div style="overflow-x:auto"><table class="pub">'
        "<tr><th>Data</th><th>Post</th><th>Tipo</th><th>Fonte</th></tr>"
        f'{"".join(trs)}</table></div>'
    )


FLOW_NOTES = {
    "instagram": ("Flusso: copia la caption, scarica la foto dell'articolo "
                  "(tieni premuto / tasto destro) e pubblica dall'app."),
    "x": ("Flusso: “Pubblica su X” apre il compositore col post già "
          "scritto — controlla e premi Post."),
    "linkedin": ("Flusso: “Pubblica su LinkedIn” copia il testo e apre il "
                 "compositore con il link dell'articolo già caricato — "
                 "incolla il testo sopra l'anteprima e pubblica."),
}


def _platform_pane(platform: str, journal: list[dict], queue: list[dict],
                   editorial: list[dict], published: list[dict]) -> str:
    label = PLATFORM_LABEL[platform]
    flow_note = FLOW_NOTES.get(platform, "")
    j_cards = "".join(_journal_card(a, platform) for a in journal)
    q_cards = "".join(_queue_card(p) for p in queue)
    if not q_cards:
        q_cards = ('<div class="placeholder">Nessuna caption preparata per questo canale, per ora: '
                   "usa le proposte del giorno qui sopra.</div>")
    ed_html = ""
    if platform == "instagram" and editorial:
        ed_cards = "".join(_editorial_card(p) for p in editorial)
        ed_html = f"""
<section>
  <h2>Pacchetti editoriali pronti</h2>
  <p class="lead">Post evergreen completi di visual (carosello dove indicato; le slide segnaposto sono da completare in pubblicazione).</p>
  <div class="grid">{ed_cards}</div>
</section>"""
    return f"""
<div class="tabpane" id="tab-{platform}">
  <div class="platform-head"><h2>{label}</h2><span class="note">tutto è solo proposto: pubblichi tu, poi "Segna come fatto"</span></div>

  <section>
    <h2>Conversazioni da presidiare</h2>
    <p class="lead">Post e thread dove conviene entrare con un commento, con risposta suggerita.</p>
    <div class="placeholder">🔭 Il monitoraggio conversazioni per {label} si attiva nello <strong>step 2</strong>
    (richiede la riattivazione dei servizi di scansione). Per ora questa sezione resta vuota di proposito.</div>
  </section>

  <section>
    <h2>Proposte del giorno</h2>
    <p class="lead">Dagli ultimi {len(journal)} articoli del Journal, già nel formato giusto per {label}. {flow_note} I link hanno UTM per misurare il traffico in Analytics.</p>
    <div class="grid">{j_cards}</div>
  </section>

  {ed_html}

  <section>
    <h2>Coda preparata</h2>
    <p class="lead">Caption già scritte dalla redazione per {label}.</p>
    <div class="grid">{q_cards}</div>
  </section>

  <section>
    <h2>Già pubblicati</h2>
    <p class="lead">Archivio anti-duplicati: prima di postare, controlla qui.</p>
    {_published_table(published)}
  </section>
</div>"""


def render(journal, queue, editorial, published, brand, password_hash) -> str:
    now = datetime.now().strftime("%d %b %Y, %H:%M")

    panes = "".join(
        _platform_pane(p, journal, queue[p], editorial, published[p])
        for p in PLATFORMS
    )

    ref_items = "".join(
        f'<li><span class="d">{_e(a["date"])}</span> <a href="{_e(a["url"])}" target="_blank" rel="noopener">{_e(a["title"])}</a></li>'
        for a in journal
    )
    journal_pane = f"""
<div class="tabpane" id="tab-journal">
  <div class="platform-head"><h2>Journal</h2><span class="note">riferimento — gli articoli si gestiscono in redazione</span></div>
  <section>
    <p class="lead">Gli ultimi {len(journal)} articoli pubblicati su myvilla.la. Le proposte social per ciascuno sono nelle tab Instagram / X / LinkedIn.</p>
    <ul class="reflist">{ref_items}</ul>
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
  I contenuti sono proposte: la pubblicazione sui canali è manuale. "Segna come fatto" è salvato solo su questo browser.
</footer>
</div>
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
    page = render(
        collect_journal(),
        collect_queue(),
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
