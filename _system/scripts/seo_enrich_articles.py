#!/usr/bin/env python3
"""SEO/GEO enrichment of the published Journal articles (2026-11-13).

Idempotente, senza LLM, gira nella catena post-publish di
publish_all_drafts.py (dopo fix_article_schema.py). Per ogni
`blog/<slug>.html`:

1. "Continue reading": blocco tra i marker MV-RELATED con 3-4 articoli
   correlati (stessa sezione, topic_tags e parole chiave dello slug in
   comune) + link all'hub di categoria. Prima del 2026-11-13, 120 articoli
   indicizzabili su 123 non ricevevano alcun link da altri articoli.
   La scelta è deterministica e senza stato: dipende solo dalla somiglianza
   tra coppie, quindi un nuovo articolo modifica solo i pochi pezzi più
   affini; un secondo passaggio garantisce almeno un link in entrata a ogni
   articolo indicizzabile. I target sono sempre pagine indicizzabili.
2. Breadcrumb visibile: la categoria diventa un link all'hub; il
   BreadcrumbList JSON-LD riceve il livello categoria (Home > Journal >
   Categoria > Articolo).
3. Data: `<time datetime="YYYY-MM-DD">` nella riga meta dell'hero.
4. JSON-LD: le entità HTML (&#x27; &amp; ...) finite nelle stringhe vengono
   decodificate (in un <script> non vengono interpretate: i motori leggevano
   "LA&#x27;s"); l'Article riceve articleSection, wordCount, citation (le
   fonti del sidecar), isPartOf (Blog), abstract (quick_answer se presente)
   e publisher.@id = #organization (collega l'articolo all'entità My Villa).
5. Head: article:modified_time e og:image:width/height (dalle dimensioni
   dell'hero) se mancanti.
6. Pulsanti di condivisione: title in inglese (erano in italiano su pagine
   lang="en").

Uso:
    python3 seo_enrich_articles.py            # applica
    python3 seo_enrich_articles.py --dry-run  # solo conteggi
"""
import html as html_module
import json
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"
BASE = "https://myvilla.la"

HUBS = {
    "insurance": "Insurance & Insurability",
    "materials": "Materials & Construction",
    "concrete_arch": "Concrete Architecture",
    "permits": "Permits & Policy",
    "market": "LA Luxury Market",
    "climate": "Climate & Resilience",
}
# etichette storiche (meta article:section / tag_label) -> id sezione
LABEL_TO_SECTION = {
    "insurance": "insurance", "insurance & insurability": "insurance",
    "materials": "materials", "materials & construction": "materials",
    "construction": "materials",
    "concrete architecture": "concrete_arch", "architecture": "concrete_arch",
    "permits": "permits", "permits & policy": "permits",
    "permits & code": "permits", "permits & regulation": "permits",
    "market": "market", "la luxury market": "market",
    "market & investment": "market",
    "climate": "climate", "climate & resilience": "climate",
}

REL_START = "<!-- MV-RELATED:START -->"
REL_END = "<!-- MV-RELATED:END -->"
REL_RE = re.compile(re.escape(REL_START) + r".*?" + re.escape(REL_END) + r"\n*", re.S)
PILLAR_START = "<!-- PILLAR-XLINK:START -->"
BACK_ANCHOR = '<a href="https://myvilla.la/blog/" class="back-link">'
LD_RE = re.compile(r'(<script type="application/ld\+json">)(.*?)(</script>)', re.S)
ROBOTS_RE = re.compile(r'<meta name="robots" content="([^"]*)"')
BC_RE = re.compile(r'(<div class="article-hero-breadcrumb">)(.*?)(</div>)', re.S)
META_RE = re.compile(r'(<div class="article-hero-meta">)(.*?)(</div>)', re.S)
MONTHS = ("January February March April May June July August September "
          "October November December").split()
DATE_TXT_RE = re.compile(r"^(\s*)((?:%s) \d{1,2}, \d{4})" % "|".join(MONTHS))

STOP = set("""the and for with from that this what why how are was its into
your you can not but has have will new one two per via top best does about
california luxury home homes house houses design designs 2025 2026 2027
los angeles custom build building built builder lesson signal signals
standard villa villas mean means still more than after over under""".split())

SHARE_TITLES = {
    'title="Condividi su LinkedIn"': 'title="Share on LinkedIn"',
    'title="Condividi su X"': 'title="Share on X"',
    'title="Condividi su Twitter"': 'title="Share on X"',
    'title="Condividi su Facebook"': 'title="Share on Facebook"',
    'title="Condividi su WhatsApp"': 'title="Share on WhatsApp"',
    'title="Copia link"': 'title="Copy link"',
    '<span class="share-bar-label">Condividi</span>': '<span class="share-bar-label">Share</span>',
    "content: 'Copiato!'": "content: 'Copied!'",
}


def esc(s):
    return html_module.escape(str(s), quote=True)


def unesc_deep(o):
    """Decodifica le entità HTML in tutte le stringhe di una struttura JSON."""
    if isinstance(o, str):
        prev, cur = None, o
        while prev != cur and re.search(r"&(#x?[0-9a-fA-F]+|[a-zA-Z]+);", cur):
            prev, cur = cur, html_module.unescape(cur)
        return cur
    if isinstance(o, list):
        return [unesc_deep(x) for x in o]
    if isinstance(o, dict):
        return {k: unesc_deep(v) for k, v in o.items()}
    return o


def dump_ld(data):
    return "\n" + json.dumps(data, indent=2, ensure_ascii=False).replace("</", "<\\/") + "\n"


def slug_tokens(slug):
    return {t for t in slug.split("-") if len(t) >= 4 and t not in STOP and not t.isdigit()}


def load_articles():
    arts = {}
    for f in sorted(BLOG_DIR.glob("*.html")):
        if f.stem == "index":
            continue
        html = f.read_text(encoding="utf-8")
        side = {}
        j = f.with_suffix(".json")
        if j.exists():
            try:
                side = json.loads(j.read_text(encoding="utf-8"))
            except Exception:
                side = {}
        rm = ROBOTS_RE.search(html)
        noindex = bool(rm and "noindex" in rm.group(1).lower())
        m = re.search(r'<meta property="article:section" content="([^"]*)"', html)
        label = html_module.unescape(m.group(1)) if m else (side.get("tag_label") or "")
        section = side.get("_section_id") or side.get("section") or ""
        if section not in HUBS:
            section = LABEL_TO_SECTION.get(label.strip().lower(), "")
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        title = html_module.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else (side.get("title") or f.stem)
        m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
        pub = m.group(1) if m else (side.get("_date") or "")
        arts[f.stem] = dict(
            slug=f.stem, path=f, html=html, side=side, noindex=noindex,
            section=section, label=label, title=title, date=pub,
            tags=set(side.get("topic_tags") or []), tokens=slug_tokens(f.stem),
        )
    return arts


def _days(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except Exception:
        return 9999


def similarity(a, b):
    s = 0.0
    if a["section"] and a["section"] == b["section"]:
        s += 3
    s += 2 * min(len(a["tags"] & b["tags"]), 3)
    s += 1 * min(len(a["tokens"] & b["tokens"]), 4)
    if _days(a["date"], b["date"]) <= 45:
        s += 1
    return s


def build_related(arts):
    """slug -> lista di slug correlati (solo target indicizzabili)."""
    targets = [a for a in arts.values() if not a["noindex"]]
    ranked = {}
    for a in arts.values():
        cands = [(similarity(a, b), b["date"], b["slug"]) for b in targets if b["slug"] != a["slug"]]
        cands.sort(key=lambda t: (-t[0], -int(t[1].replace("-", "")) if t[1] else 0, t[2]))
        ranked[a["slug"]] = [c[2] for c in cands]
    related = {s: r[:3] for s, r in ranked.items()}
    # secondo passaggio: ogni articolo indicizzabile riceve almeno un link in entrata
    inbound = {}
    for s, rel in related.items():
        if arts[s]["noindex"]:
            continue  # contano solo i link da pagine indicizzabili
        for t in rel:
            inbound[t] = inbound.get(t, 0) + 1
    hosts = [a["slug"] for a in targets]
    for orphan in sorted(a["slug"] for a in targets if inbound.get(a["slug"], 0) == 0):
        best = None
        for h in hosts:
            if h == orphan or orphan in related[h] or len(related[h]) >= 4:
                continue
            key = (similarity(arts[h], arts[orphan]), arts[h]["date"], h)
            if best is None or key > best[0]:
                best = (key, h)
        if best:
            related[best[1]].append(orphan)
            inbound[orphan] = 1
    return related


def related_block(a, rel, arts):
    items = []
    for s in rel:
        b = arts[s]
        tag = HUBS.get(b["section"], b["label"])
        items.append(
            f'    <li><a href="{BASE}/blog/{s}.html">{esc(b["title"])}</a>'
            + (f' <span style="opacity:.6">&middot; {esc(tag)}</span>' if tag else "") + "</li>"
        )
    if a["section"] in HUBS:
        items.append(
            f'    <li><strong><a href="{BASE}/blog/category/{a["section"]}.html">'
            f'All {esc(HUBS[a["section"]])} notes</a></strong> &mdash; the full desk, by month.</li>'
        )
    if not items:
        return ""
    return (
        f"{REL_START}\n"
        '<div class="sources-section mv-related" style="margin-top:28px">\n'
        '  <div class="sources-title">Continue reading</div>\n'
        '  <ul class="sources-list">\n' + "\n".join(items) + "\n"
        "  </ul>\n"
        "</div>\n"
        f"{REL_END}"
    )


def apply_related(html, block):
    stripped = REL_RE.sub("", html)
    if not block:
        return stripped
    if PILLAR_START in stripped:
        return stripped.replace(PILLAR_START, block + "\n" + PILLAR_START, 1)
    if BACK_ANCHOR in stripped:
        return stripped.replace(BACK_ANCHOR, block + "\n\n    " + BACK_ANCHOR, 1)
    return html


def apply_breadcrumb(html, a):
    if a["section"] not in HUBS:
        return html
    m = BC_RE.search(html)
    if not m or m.group(2).count("<a ") >= 3:
        return html
    inner = m.group(2)
    parts = inner.rsplit("</span>", 1)
    if len(parts) != 2:
        return html
    tail = parts[1]
    text = tail.strip()
    if not text or "<" in text:
        return html
    linked = tail.replace(text, f'<a href="{BASE}/blog/category/{a["section"]}.html">{text}</a>', 1)
    return html[:m.start(2)] + parts[0] + "</span>" + linked + html[m.end(2):]


def apply_time(html, a):
    if not a["date"]:
        return html
    m = META_RE.search(html)
    if not m or "<time" in m.group(2):
        return html
    dm = DATE_TXT_RE.match(m.group(2))
    if not dm:
        return html
    new_inner = (dm.group(1) + f'<time datetime="{a["date"]}">{dm.group(2)}</time>'
                 + m.group(2)[dm.end():])
    return html[:m.start(2)] + new_inner + html[m.end(2):]


def word_count(a):
    side = a["side"]
    src = (side.get("body_html") or "") + " " + (side.get("our_perspective") or "")
    if not src.strip():
        m = re.search(r"<article.*?</article>", a["html"], re.S)
        src = re.sub(r"<(script|style).*?</\1>", " ", m.group(0) if m else "", flags=re.S)
    return len(re.sub(r"<[^>]+>", " ", html_module.unescape(src)).split())


def citations(a):
    out = []
    for s in a["side"].get("sources") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        c = {"@type": "CreativeWork", "name": s.get("title") or s.get("name") or s["url"], "url": s["url"]}
        if s.get("name") and s.get("title"):
            c["publisher"] = {"@type": "Organization", "name": s["name"]}
        out.append(c)
    return out


def apply_ld(html, a):
    canonical = f"{BASE}/blog/{a['slug']}.html"

    def fix(m):
        try:
            orig = json.loads(m.group(2))
        except Exception:
            return m.group(0)
        data = unesc_deep(json.loads(m.group(2)))
        if isinstance(data, dict) and data.get("@type") == "Article":
            if a["section"] in HUBS:
                data["articleSection"] = HUBS[a["section"]]
            wc = word_count(a)
            if wc:
                data["wordCount"] = wc
            cit = citations(a)
            if cit:
                data["citation"] = cit
            qa = (a["side"].get("quick_answer") or "").strip()
            if qa:
                data["abstract"] = qa
            data["isPartOf"] = {"@type": "Blog", "@id": f"{BASE}/blog/#blog",
                                "name": "My Villa Journal", "url": f"{BASE}/blog/"}
            pub = data.get("publisher")
            if isinstance(pub, dict) and "@id" not in pub:
                data["publisher"] = {"@id": f"{BASE}/#organization", **pub}
        elif isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
            items = data.get("itemListElement") or []
            if (len(items) == 3 and a["section"] in HUBS and not a["noindex"]
                    and items[-1].get("item", "").rstrip("/") == canonical):
                last = dict(items[2]); last["position"] = 4
                data["itemListElement"] = items[:2] + [
                    {"@type": "ListItem", "position": 3, "name": HUBS[a["section"]],
                     "item": f"{BASE}/blog/category/{a['section']}.html"}, last]
        if data == orig:
            return m.group(0)
        return m.group(1) + dump_ld(data) + m.group(3)

    return LD_RE.sub(fix, html)


def apply_head(html, a):
    if 'property="article:modified_time"' not in html:
        m = re.search(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
        pm = re.search(r'<meta property="article:published_time" content="([^"]*)">\n?', html)
        if m and pm:
            tail = pm.group(1)[10:] or "T08:00:00-07:00"
            tag = f'<meta property="article:modified_time" content="{m.group(1)}{tail}">\n'
            html = html[:pm.end()] + tag + html[pm.end():]
    if 'property="og:image:width"' not in html:
        og = re.search(r'<meta property="og:image" content="([^"]*)">\n?', html)
        hero = re.search(r'<figure class="hero-image".*?</figure>', html, re.S)
        if og and hero and "-hero." in og.group(1):
            im = re.search(r'<img\b[^>]*>', hero.group(0))
            w = re.search(r'\swidth="(\d+)"', im.group(0)) if im else None
            h = re.search(r'\sheight="(\d+)"', im.group(0)) if im else None
            if w and h:
                tags = (f'<meta property="og:image:width" content="{w.group(1)}">\n'
                        f'<meta property="og:image:height" content="{h.group(1)}">\n')
                end = og.end()
                if not html[:end].endswith("\n"):
                    tags = "\n" + tags
                html = html[:end] + tags + html[end:]
    return html


BROKEN_OG = "https://myvilla.la/assets/img/myvilla-og-journal.jpg"   # mai esistita: 404
DEFAULT_OG = "https://myvilla.la/img/hero-og.jpg"                    # 1200x630


def apply_og_fallback(html, a):
    """27 articoli avevano og:image/twitter:image/JSON-LD image su un file inesistente."""
    if BROKEN_OG not in html:
        return html
    hero = re.search(r'<figure class="hero-image".*?</figure>', html, re.S)
    im = re.search(r'<img\b[^>]*\ssrc="(assets/img/[^"]+)"', hero.group(0)) if hero else None
    if im:
        new_url = f"{BASE}/blog/{im.group(1)}"
        # le dimensioni 1200x630 valevano per il fallback: apply_head rimette quelle dell'hero
        html = re.sub(r'<meta property="og:image:(width|height)" content="\d+">\n?', "", html)
    else:
        new_url = DEFAULT_OG
    return html.replace(BROKEN_OG, new_url)


def apply_share_titles(html):
    for old, new in SHARE_TITLES.items():
        if old in html:
            html = html.replace(old, new)
    return html


def main(argv=None):
    dry = "--dry-run" in (argv or sys.argv[1:])
    arts = load_articles()
    related = build_related(arts)
    changed = 0
    stats = {"related": 0, "breadcrumb": 0, "time": 0, "jsonld": 0, "og": 0, "head": 0, "share": 0}
    for slug, a in arts.items():
        html = a["html"]
        steps = (
            ("related", lambda h: apply_related(h, related_block(a, related.get(slug, []), arts))),
            ("breadcrumb", lambda h: apply_breadcrumb(h, a)),
            ("time", lambda h: apply_time(h, a)),
            ("jsonld", lambda h: apply_ld(h, a)),
            ("og", lambda h: apply_og_fallback(h, a)),
            ("head", lambda h: apply_head(h, a)),
            ("share", apply_share_titles),
        )
        for name, fn in steps:
            new = fn(html)
            if new != html:
                stats[name] += 1
                html = new
        if html != a["html"]:
            # ogni blocco JSON-LD deve restare JSON valido
            for m in LD_RE.finditer(html):
                json.loads(m.group(2))
            changed += 1
            if not dry:
                a["path"].write_text(html, encoding="utf-8")
    idx = [s for s, a in arts.items() if not a["noindex"]]
    inbound = {}
    for s in idx:
        for t in related.get(s, []):
            inbound[t] = inbound.get(t, 0) + 1
    orphans = [s for s in idx if not inbound.get(s)]
    print(f"[seo_enrich_articles] articoli: {len(arts)} (indicizzabili {len(idx)}), "
          f"modificati: {changed}{' (dry-run)' if dry else ''} · " +
          ", ".join(f"{k} {v}" for k, v in stats.items()) +
          f" · senza link in entrata: {len(orphans)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
