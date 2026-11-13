#!/usr/bin/env python3
"""Backfill del blocco "In short" (quick_answer) e delle FAQ sugli articoli
del Journal pubblicati prima che il template li prevedesse (2026-11-13).

I motori AI citano più facilmente una pagina che apre con una risposta breve
e chiude con domande e risposte. Il template di `generate_journal.py` lo fa
dal 2026-10-13; gli articoli più vecchi no. Questo script li allinea.

Per ogni `blog/<slug>.html` indicizzabile (meta robots senza `noindex`) con
sidecar `blog/<slug>.json`:

1. SIDECAR — se manca `quick_answer` e/o `faq`, li genera con il modello
   (SOLO via `llm_client.py`: Claude Code headless sull'abbonamento, mai la
   Claude API) partendo esclusivamente da `title`, `subtitle`, `body_html`,
   `key_data` e `sources`. Ogni risposta passa controlli deterministici prima
   di essere salvata:
     · quick_answer: 2 frasi, 40-60 parole, una fonte del sidecar nominata;
     · faq: 2 voci {q, a}, domanda che finisce con "?", risposta 30-70 parole;
     · ogni cifra dell'output deve comparire nel sidecar (testo, key_data,
       fonti, data di pubblicazione);
     · ogni nome proprio (parola maiuscola non a inizio frase) deve comparire
       nel sidecar;
     · mai "Mezzalama", mai "IT'S Architecture", niente markup.
   Se un controllo fallisce il modello riceve l'elenco degli errori e riprova
   (max 3 tentativi); se fallisce ancora l'articolo resta com'è e finisce nel
   riepilogo.
2. HTML — inserisce, con lo stesso markup e lo stesso CSS del template:
     · `<!-- MV-QA:START/END -->`   in testa a `<article class="article-content">`
     · `<!-- MV-FAQ:START/END -->`  prima di `<!-- MV-RELATED:START -->`
     · `<!-- MV-FAQLD:START/END -->` (FAQPage JSON-LD) nell'head, prima di <style>
     · `<style id="mv-qa-css">`     con le sole regole CSS che la pagina non ha
   Gli articoli che hanno già il blocco nativo del template non vengono toccati.

Idempotente: i blocchi tra marker vengono tolti e ricostruiti dal sidecar a
ogni esecuzione, quindi una seconda esecuzione non modifica nulla (e non
chiama il modello). Dopo il backfill lanciare, nell'ordine:
    seo_enrich_articles.py (abstract nello schema Article), inject_article_cta.py,
    update_sitemap.py.

Uso:
    python3 backfill_quick_answer.py                 # genera i mancanti + HTML
    python3 backfill_quick_answer.py --limit 5       # solo i primi 5 mancanti
    python3 backfill_quick_answer.py --only slug-a,slug-b
    python3 backfill_quick_answer.py --html-only     # nessuna chiamata al modello
    python3 backfill_quick_answer.py --dry-run       # solo conteggi, nessuna scrittura
    python3 backfill_quick_answer.py --check         # ricontrolla i testi già salvati
"""
from __future__ import annotations

import argparse
import html as html_module
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
BLOG_DIR = ROOT_DIR / "blog"

QA_START, QA_END = "<!-- MV-QA:START -->", "<!-- MV-QA:END -->"
FAQ_START, FAQ_END = "<!-- MV-FAQ:START -->", "<!-- MV-FAQ:END -->"
LD_START, LD_END = "<!-- MV-FAQLD:START -->", "<!-- MV-FAQLD:END -->"
CSS_OPEN = '<style id="mv-qa-css">'

QA_RE = re.compile(r"[ \t]*" + re.escape(QA_START) + r".*?" + re.escape(QA_END) + r"\n", re.S)
FAQ_RE = re.compile(re.escape(FAQ_START) + r".*?" + re.escape(FAQ_END) + r"\n[ \t]*", re.S)
LDB_RE = re.compile(re.escape(LD_START) + r".*?" + re.escape(LD_END) + r"\n", re.S)
CSS_RE = re.compile(re.escape(CSS_OPEN) + r".*?</style>\n", re.S)

ART_ANCHOR = '<article class="article-content">\n'
REL_START = "<!-- MV-RELATED:START -->"
PILLAR_START = "<!-- PILLAR-XLINK:START -->"
BACK_ANCHOR = '<a href="https://myvilla.la/blog/" class="back-link">'
ROBOTS_RE = re.compile(r'<meta name="robots" content="([^"]*)"')
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)

# Stesse regole di generate_journal.py (render_article_html), una per riga.
CSS_QA = (
    ".quick-answer { background: var(--white); border: 1px solid var(--light-linen); "
    "border-left: 3px solid var(--tuscan-gold); border-radius: 0 6px 6px 0; "
    "padding: 22px 26px; margin: 0 0 36px; }\n"
    ".quick-answer-label { font-family: var(--sans); font-size: 11px; font-weight: 600; "
    "letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); "
    "margin-bottom: 10px; }\n"
    ".quick-answer-text { font-family: var(--serif); font-size: 20px; line-height: 1.5; "
    "color: var(--espresso); margin: 0; }\n"
    "@media (max-width: 600px) { .quick-answer-text { font-size: 18px; } }\n"
)
CSS_FAQ = (
    ".faq-section { padding: 32px 0; border-top: 1px solid var(--light-linen); }\n"
    ".faq-title { font-family: var(--sans); font-size: 11px; font-weight: 600; "
    "letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); "
    "margin-bottom: 20px; }\n"
    ".faq-item { border-bottom: 1px solid var(--light-linen); padding: 16px 0; }\n"
    ".faq-item:last-child { border-bottom: none; }\n"
    ".faq-item summary { list-style: none; cursor: pointer; font-family: var(--serif); "
    "font-size: 19px; font-weight: 500; color: var(--espresso); line-height: 1.35; "
    "padding-right: 30px; position: relative; }\n"
    ".faq-item summary::-webkit-details-marker { display: none; }\n"
    ".faq-item summary::after { content: '+'; position: absolute; right: 0; top: 0; "
    "font-size: 22px; font-weight: 300; color: var(--pacific-blue); transition: transform 0.3s; }\n"
    ".faq-item[open] summary::after { transform: rotate(45deg); }\n"
    ".faq-a { font-size: 15px; line-height: 1.65; color: var(--charcoal); opacity: 0.85; "
    "margin-top: 12px; }\n"
)


def esc(text):
    """Come esc() di generate_journal.py."""
    return html_module.escape(str(text)) if text else ""


# ── rendering (stesso markup del template) ───────────────────────────
def clean(text):
    return " ".join(str(text or "").split())


def valid_faq(faq):
    return [q for q in (faq or []) if isinstance(q, dict) and q.get("q") and q.get("a")]


def render_quick_answer(quick_answer):
    return ('<div class="quick-answer">'
            '<div class="quick-answer-label">In short</div>'
            f'<p class="quick-answer-text">{esc(clean(quick_answer))}</p>'
            '</div>')


def render_faq_block(faq):
    items = [
        f'<details class="faq-item">\n'
        f'        <summary class="faq-q">{esc(q["q"])}</summary>\n'
        f'        <div class="faq-a">{esc(q["a"])}</div>\n'
        f'      </details>'
        for q in valid_faq(faq)
    ]
    return ('<div class="faq-section">\n'
            '      <div class="faq-title">Frequently asked</div>\n      '
            + "\n      ".join(items) + '\n    </div>')


def render_faq_ld(faq):
    items = ",".join(
        '{"@type":"Question","name":' + json.dumps(q["q"]) +
        ',"acceptedAnswer":{"@type":"Answer","text":' + json.dumps(q["a"]) + '}}'
        for q in valid_faq(faq)
    )
    body = '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[' + items + ']}'
    return ('<script type="application/ld+json">\n' + body.replace("</", "<\\/") + '\n</script>')


def strip_blocks(html):
    for rx in (QA_RE, FAQ_RE, LDB_RE, CSS_RE):
        html = rx.sub("", html)
    return html


def apply_html(html, side):
    """HTML con i blocchi allineati al sidecar. Ritorna (html, avvisi)."""
    warns = []
    base = strip_blocks(html)
    out = base
    qa = clean(side.get("quick_answer"))
    faq = valid_faq(side.get("faq"))

    if qa and 'class="quick-answer"' not in base:
        if ART_ANCHOR in out:
            block = f"    {QA_START}{render_quick_answer(qa)}{QA_END}\n"
            out = out.replace(ART_ANCHOR, ART_ANCHOR + block, 1)
        else:
            warns.append("manca <article class=\"article-content\">")

    if faq and 'class="faq-section"' not in base:
        block = f"{FAQ_START}\n    {render_faq_block(faq)}\n    {FAQ_END}\n    "
        for anchor in (REL_START, PILLAR_START, BACK_ANCHOR):
            if anchor in out:
                out = out.replace(anchor, block + anchor, 1)
                break
        else:
            warns.append("manca l'ancora per il blocco FAQ")

    if faq and '"FAQPage"' not in base:
        i = out.find("\n<style>")
        if i >= 0:
            if not out[:i].endswith("\n"):
                i += 1                      # nessuna riga vuota prima di <style>
            out = out[:i] + f"{LD_START}\n{render_faq_ld(faq)}\n{LD_END}\n" + out[i:]
        else:
            warns.append("manca <style> nell'head")

    css = ""
    if qa and ".quick-answer {" not in base:
        css += CSS_QA
    if faq and ".faq-section {" not in base:
        css += CSS_FAQ
    if css:
        if "</head>" in out:
            out = out.replace("</head>", f"{CSS_OPEN}\n{css}</style>\n</head>", 1)
        else:
            warns.append("manca </head>")

    if warns:
        return html, warns
    for m in LD_RE.finditer(out):        # ogni blocco JSON-LD deve restare JSON valido
        json.loads(m.group(1))
    return out, warns


# ── corpus dei fatti e controlli ─────────────────────────────────────
MONTHS = ("January February March April May June July August September "
          "October November December").split()
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’*]*")
NUM_WORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
             "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
             "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
             "seventy": "70", "eighty": "80", "ninety": "90", "dozen": "12"}
# nomi sempre ammessi (il soggetto che parla e la sua geografia)
ALLOWED_NAMES = {"my", "villa", "los", "angeles", "la", "california", "californian",
                 "american", "americans", "us", "i"}
ABBREV_RE = re.compile(r"\b(?:U\.S|U\.K|St|Mt|Dr|Mr|Mrs|Ms|Jr|Sr|Inc|Co|Corp|Ltd|No|vs|approx|"
                       r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|a\.m|p\.m|e\.g|i\.e|sq|ft)\.")
BANNED = (("mezzalama", "names Paolo Mezzalama (never name any individual at My Villa)"),
          ("it's architecture", "mentions IT'S Architecture (omit it)"),
          ("it’s architecture", "mentions IT'S Architecture (omit it)"),
          ("the article", "refers to 'the article' (state the fact and name its source instead)"),
          ("this article", "refers to 'this article' (state the fact and name its source instead)"),
          ("the piece", "refers to 'the piece' (state the fact and name its source instead)"))
MARKUP_RE = re.compile(r"<[a-zA-Z/][^>]*>|\*\*|\]\(|https?://|&[a-z#0-9]+;")


def strip_tags(s):
    s = re.sub(r"<(script|style).*?</\1>", " ", str(s or ""), flags=re.S)
    s = re.sub(r"<h[23][^>]*>", "\n\n## ", s)
    s = re.sub(r"</(p|h[1-6]|li|tr|blockquote|div)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_module.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n\s*", "\n\n", s).strip()


def date_text(side):
    d = str(side.get("_date") or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
    if not m:
        return ""
    return f"{MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}, {m.group(1)}"


def fact_corpus(side):
    """Tutto il testo da cui il modello può attingere fatti."""
    parts = [side.get("title"), side.get("subtitle"), strip_tags(side.get("body_html")), date_text(side)]
    for k in side.get("key_data") or []:
        if isinstance(k, dict):
            parts += [k.get("number"), k.get("label")]
    for s in side.get("sources") or []:
        if isinstance(s, dict):
            parts += [s.get("name"), s.get("title"), s.get("excerpt")]
    return "\n".join(html_module.unescape(str(p)) for p in parts if p)


def numbers_in(text):
    out = set()
    for m in NUM_RE.finditer(text):
        tok = m.group(0).replace(",", "").rstrip(".")
        if tok:
            out.add(tok)
            if "." in tok and tok.rstrip("0").endswith("."):
                out.add(tok.split(".")[0])
    return out


def words_in(text):
    out = set()
    for m in WORD_RE.finditer(text):
        w = m.group(0).lower().replace("’", "'").strip("'*")
        out.add(w)
        if w.endswith("'s"):
            out.add(w[:-2])
        elif w.endswith("s'"):
            out.add(w[:-1])
    return out


def sentences(text):
    t = ABBREV_RE.sub(lambda m: m.group(0).replace(".", "§"), text)
    parts = re.split(r"(?<=[.!?])[\"”’)\]]*\s+(?=[\"“‘(\[]?[A-Z0-9$#])", t)
    return [p for p in (x.strip() for x in parts) if p]


def proper_names(text):
    """Parole maiuscole non a inizio frase (candidati nomi propri)."""
    found = []
    for sent in sentences(text):
        toks = list(WORD_RE.finditer(sent))
        for i, m in enumerate(toks):
            w = m.group(0)
            if i == 0 or not w[0].isupper():
                continue
            # dopo due punti, virgolette o trattino lungo può iniziare una frase
            prev = sent[:m.start()].rstrip()
            if prev.endswith((":", "“", '"', "‘")):
                continue
            found.append(w)
    return found


def check_text(label, text, corpus, c_nums, c_words):
    errs = []
    low = text.lower()
    for needle, why in BANNED:
        if needle in low:
            errs.append(f"{label}: {why}")
    if MARKUP_RE.search(text):
        errs.append(f"{label}: contains markup, a URL or an HTML entity (plain text only)")
    bad = sorted(n for n in numbers_in(text) if n not in c_nums)
    if bad:
        errs.append(f"{label}: figure(s) not present in the article data: {', '.join(bad)} "
                    "(use only figures that appear in the article, written the same way)")
    t_words = words_in(text)
    for w, digit in NUM_WORDS.items():
        if w in t_words and w not in c_words and digit not in c_nums:
            errs.append(f"{label}: the number word '{w}' is not in the article data")
    names = []
    for w in proper_names(text):
        for part in re.split(r"[-/]", w):
            k = part.lower().replace("’", "'").strip("'*")
            if k.endswith("'s"):
                k = k[:-2]
            if len(k) < 3 or k in ALLOWED_NAMES or k in c_words:
                continue
            if k.endswith("s") and k[:-1] in c_words:
                continue
            names.append(part)
    if names:
        errs.append(f"{label}: name(s) not present in the article data: "
                    f"{', '.join(sorted(set(names)))} (only name entities the article names; "
                    "do not capitalize common nouns)")
    return errs


GENERIC_SOURCE_WORDS = {
    "the", "news", "times", "magazine", "journal", "daily", "report", "post", "california",
    "angeles", "department", "office", "state", "county", "city", "institute", "association",
    "university", "national", "american", "insurance", "business", "home", "real", "estate",
    "architecture", "design", "group", "center", "board", "commission", "service", "house"}


def source_named(text, side):
    """True se il testo nomina una fonte del sidecar: nome intero, acronimo
    ("California Department of Insurance" -> CDI) o una sua parola distintiva."""
    low = text.lower()
    for s in side.get("sources") or []:
        name = html_module.unescape(str((s or {}).get("name") or "")).strip()
        if not name:
            continue
        if name.lower() in low:
            return True
        toks = [t.strip(".'*") for t in re.findall(r"[A-Za-z][A-Za-z&'*.]+", name)]
        acro = "".join(t[0] for t in toks if t[:1].isupper())
        if len(acro) >= 3 and re.search(r"\b" + re.escape(acro) + r"\b", text):
            return True
        for t in toks:
            distinctive = len(t) >= 4 or (t.isupper() and len(t) >= 3)
            if distinctive and t.lower() not in GENERIC_SOURCE_WORDS and \
                    re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", text):
                return True
    return False


def validate(side, quick_answer=None, faq=None):
    """Lista di problemi (vuota = ok). Controlla solo i campi passati."""
    corpus = fact_corpus(side)
    c_nums, c_words = numbers_in(corpus), words_in(corpus)
    for w, digit in NUM_WORDS.items():
        if w in c_words:
            c_nums.add(digit)
    errs = []
    if quick_answer is not None:
        qa = clean(quick_answer)
        n = len(qa.split())
        if not 40 <= n <= 60:
            errs.append(f"quick_answer: {n} words (must be 40-60)")
        ns = len(sentences(qa))
        if ns != 2:
            errs.append(f"quick_answer: {ns} sentence(s) (must be exactly 2)")
        if not source_named(qa, side):
            names = [str((s or {}).get("name")) for s in side.get("sources") or [] if (s or {}).get("name")]
            errs.append("quick_answer: no source named — name one of: " + "; ".join(names))
        errs += check_text("quick_answer", qa, corpus, c_nums, c_words)
    if faq is not None:
        items = valid_faq(faq)
        if len(items) != 2 or len(faq) != 2:
            errs.append(f"faq: {len(faq)} item(s) (must be exactly 2, each with q and a)")
        for i, q in enumerate(items, 1):
            qq, aa = clean(q["q"]), clean(q["a"])
            if not qq.endswith("?"):
                errs.append(f"faq {i}: the question must end with '?'")
            if len(qq.split()) > 20:
                errs.append(f"faq {i}: question too long ({len(qq.split())} words, max 20)")
            n = len(aa.split())
            if not 30 <= n <= 70:
                errs.append(f"faq {i}: answer is {n} words (must be 30-70)")
            errs += check_text(f"faq {i} question", qq, corpus, c_nums, c_words)
            errs += check_text(f"faq {i} answer", aa, corpus, c_nums, c_words)
        if len(items) == 2 and clean(items[0]["q"]).lower() == clean(items[1]["q"]).lower():
            errs.append("faq: the two questions are the same")
    return errs


# ── generazione (solo llm_client: abbonamento, niente API) ───────────
SYSTEM = """You write the short answer block and the FAQ for articles of the My Villa Journal \
(myvilla.la), a design studio for luxury villas in architectural concrete in Los Angeles. \
The readers are prospective clients planning to build or buy a high-end home in Los Angeles, \
and the advisors, agents and architects around them. AI assistants quote these passages, \
so each one must be accurate and make sense with no other context.

HARD RULES
- Use ONLY facts stated in the ARTICLE DATA below (title, subtitle, body, key data, sources). \
No outside knowledge, no new facts, no inference presented as fact.
- Every figure (amount, percentage, year, count, code or bill number) must appear in the \
ARTICLE DATA, written the same way. Do not compute, round, convert or combine figures.
- Only name people, companies, agencies, places and publications that the ARTICLE DATA names.
- Keep the article's hedges and attributions. If it says "analysts say", "is expected", \
"proposed", "would", "according to", do not turn that into an established fact, and do not \
attach to a source a detail that the article does not attribute to it.
- Never refer to "the article", "this article" or "the piece": state the fact and name the \
publication or organization it comes from.
- Spell out counts from zero to nine as words ("three layers", "no special techniques", not \
"3 layers" or "0 special techniques"), unless the digit is part of a name, a code, a unit of \
measure, a ratio or a price.
- Plain text only: no HTML, no markdown, no links, no quotation of whole sentences from the body.
- American English. Factual, calm, specific. No hype, no sales language, no advice to contact anyone.
- Default to neutral third person. Use "we" / "at My Villa" only if the article body itself \
speaks about My Villa. Never name any individual at My Villa. Never mention IT'S Architecture.
- Write time references so they stay true later: anchor them to the article's date or the \
source's date ("as of April 2026"), never "this week", "today" or "recently".

quick_answer
- Exactly 2 sentences, 40 to 60 words in total.
- A direct answer to the question the title poses, self-contained.
- It contains the single most important figure of the article and names its source by the \
publication or organization name given in SOURCES (e.g. "according to IBHS", "CAL FIRE reports").
- Do not copy the article's opening sentence.

faq
- Exactly 2 items. Each question is one a prospective client would really type or ask \
(about cost, timing, insurability, permits, risk, what it means for their own project), \
phrased naturally, ending with "?", at most 18 words. The two questions cover different points \
and do not repeat the title.
- Each answer: 2 to 3 sentences, 35 to 60 words, specific, with the relevant figure if the \
article has one, and the source named when a figure is cited. The answer starts with the \
answer itself, not with a restatement of the question.
- The answers must not repeat the quick_answer."""

SCHEMA_FULL = {
    "type": "object",
    "properties": {
        "quick_answer": {"type": "string"},
        "faq": {"type": "array", "minItems": 2, "maxItems": 2, "items": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
            "required": ["q", "a"], "additionalProperties": False}},
    },
    "required": ["quick_answer", "faq"], "additionalProperties": False,
}
SCHEMA_QA = {
    "type": "object",
    "properties": {"quick_answer": {"type": "string"}},
    "required": ["quick_answer"], "additionalProperties": False,
}


def article_prompt(side, need_faq):
    lines = ["ARTICLE DATA", "", f"TITLE: {html_module.unescape(str(side.get('title') or ''))}"]
    if side.get("subtitle"):
        lines.append(f"SUBTITLE: {html_module.unescape(str(side['subtitle']))}")
    if date_text(side):
        lines.append(f"PUBLISHED: {date_text(side)}")
    lines += ["", "SOURCES:"]
    for s in side.get("sources") or []:
        if isinstance(s, dict) and s.get("name"):
            ex = f" | {clean(s.get('excerpt'))}" if s.get("excerpt") else ""
            lines.append(f"- {clean(s.get('name'))} — {clean(s.get('title'))}{ex}")
    kd = [k for k in side.get("key_data") or [] if isinstance(k, dict) and k.get("number")]
    if kd:
        lines += ["", "KEY DATA:"]
        lines += [f"- {clean(k.get('number'))}: {clean(k.get('label'))}" for k in kd]
    lines += ["", "BODY:", strip_tags(side.get("body_html")), ""]
    if need_faq:
        lines.append("Return the quick_answer and the 2 faq items for this article.")
    else:
        existing = "; ".join(clean(q["q"]) for q in valid_faq(side.get("faq")))
        lines.append("Return only the quick_answer for this article. The page already has an FAQ "
                     f"({existing}): do not repeat its answers.")
    return html_module.unescape("\n".join(lines))


def generate(side, need_qa, need_faq, tier, attempts=3):
    """Ritorna (dict con i campi nuovi, errori dell'ultimo tentativo)."""
    from llm_client import complete_json  # import tardivo: --html-only non ne ha bisogno
    schema = SCHEMA_FULL if need_faq else SCHEMA_QA
    base = article_prompt(side, need_faq)
    prompt, errs = base, []
    for _ in range(attempts):
        data = complete_json(prompt, schema, system=SYSTEM, tier=tier, timeout=600)
        out = {}
        if need_qa:
            out["quick_answer"] = clean(data.get("quick_answer"))
        if need_faq:
            out["faq"] = [{"q": clean(q.get("q")), "a": clean(q.get("a"))}
                          for q in (data.get("faq") or []) if isinstance(q, dict)]
        errs = validate(side, out.get("quick_answer"), out.get("faq"))
        if not errs:
            return out, []
        prompt = (base + "\n\nYour previous answer was rejected by the automatic checks.\n"
                  "PREVIOUS ANSWER:\n" + json.dumps(out, ensure_ascii=False, indent=1) +
                  "\nPROBLEMS:\n" + "\n".join(f"- {e}" for e in errs) +
                  "\nReturn a corrected version that fixes every problem and still follows all rules.")
    return None, errs


def save_sidecar(path, raw, side, new):
    """Scrive il sidecar mettendo quick_answer e faq dove li mette il template
    (dopo meta_keywords, quick_answer prima di faq)."""
    merged = {k: v for k, v in side.items() if k not in new}
    keys = list(merged)
    if "faq" in merged and "quick_answer" in new:            # faq già presente
        at = keys.index("faq")
    elif "quick_answer" in merged and "faq" in new:          # quick_answer già presente
        at = keys.index("quick_answer") + 1
    else:
        anchor = next((k for k in ("meta_keywords", "meta_description", "tag_label", "title")
                       if k in merged), None)
        at = keys.index(anchor) + 1 if anchor else len(keys)
    order = keys[:at] + [k for k in ("quick_answer", "faq") if k in new] + keys[at:]
    out = {k: (new[k] if k in new else merged[k]) for k in order}
    text = json.dumps(out, indent=2, ensure_ascii=False)
    path.write_text(text + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")
    return out


# ── main ─────────────────────────────────────────────────────────────
def load_targets():
    arts = []
    for hp in sorted(BLOG_DIR.glob("*.html")):
        if hp.stem == "index":
            continue
        jp = hp.with_suffix(".json")
        if not jp.exists():
            continue
        html = hp.read_text(encoding="utf-8")
        rm = ROBOTS_RE.search(html)
        if rm and "noindex" in rm.group(1).lower():
            continue
        raw = jp.read_text(encoding="utf-8")
        try:
            side = json.loads(raw)
        except Exception:
            continue
        if not isinstance(side, dict) or not side.get("body_html"):
            continue
        arts.append({"slug": hp.stem, "html_path": hp, "json_path": jp,
                     "html": html, "raw": raw, "side": side})
    return arts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="genera al massimo N articoli")
    ap.add_argument("--only", default="", help="slug separati da virgola")
    ap.add_argument("--html-only", action="store_true", help="nessuna chiamata al modello")
    ap.add_argument("--dry-run", action="store_true", help="nessuna scrittura, nessuna chiamata")
    ap.add_argument("--check", action="store_true", help="ricontrolla i testi già nei sidecar")
    ap.add_argument("--regen", action="store_true",
                    help="con --only: rigenera anche i testi già inseriti da questo script")
    ap.add_argument("--tier", default="writer", help="tier di llm_client (default: writer)")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args(argv)

    os.environ.setdefault("MYVILLA_LLM_PARALLEL", str(max(1, args.workers)))
    sys.path.insert(0, str(SCRIPT_DIR))

    arts = load_targets()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        arts = [a for a in arts if a["slug"] in only]

    if args.check:
        bad = 0
        for a in arts:
            s = a["side"]
            native = 'class="quick-answer"' in strip_blocks(a["html"])
            errs = validate(s, s.get("quick_answer") if not native else None,
                            s.get("faq") if FAQ_START in a["html"] else None)
            if errs:
                bad += 1
                print(f"✗ {a['slug']}\n   " + "\n   ".join(errs))
        print(f"[backfill_quick_answer] check: {len(arts)} articoli, {bad} con problemi")
        return 1 if bad else 0

    regen = args.regen and bool(only)
    for a in arts:
        s = a["side"]
        # --regen tocca solo ciò che questo script ha inserito (blocchi tra marker)
        a["need_qa"] = not clean(s.get("quick_answer")) or (regen and QA_START in a["html"])
        a["need_faq"] = not valid_faq(s.get("faq")) or (regen and FAQ_START in a["html"])
    todo = [a for a in arts if a["need_qa"] or a["need_faq"]]
    planned = todo[:args.limit] if args.limit else todo
    generated, failed = 0, []

    if planned and not (args.html_only or args.dry_run):
        from llm_client import LLMUnavailable

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {pool.submit(generate, a["side"], a["need_qa"], a["need_faq"], args.tier): a
                    for a in planned}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    new, errs = fut.result()
                except LLMUnavailable as exc:
                    failed.append((a["slug"], [f"modello non disponibile: {exc}"]))
                    print(f"  ✗ {a['slug']}: modello non disponibile ({str(exc)[:80]})", flush=True)
                    continue
                except Exception as exc:  # noqa: BLE001
                    failed.append((a["slug"], [f"errore: {exc}"]))
                    print(f"  ✗ {a['slug']}: {str(exc)[:120]}", flush=True)
                    continue
                if not new:
                    failed.append((a["slug"], errs))
                    print(f"  ✗ {a['slug']}: controlli non superati dopo 3 tentativi", flush=True)
                    continue
                # il sidecar può essere cambiato nel frattempo (pipeline quotidiana): rileggi
                raw = a["json_path"].read_text(encoding="utf-8")
                a["side"] = save_sidecar(a["json_path"], raw, json.loads(raw), new)
                generated += 1
                print(f"  ✓ {a['slug']}", flush=True)

    html_changed, warned = 0, []
    for a in arts:
        html = a["html_path"].read_text(encoding="utf-8")
        new_html, warns = apply_html(html, a["side"])
        if warns:
            warned.append((a["slug"], warns))
            continue
        if new_html != html:
            html_changed += 1
            if not args.dry_run:
                a["html_path"].write_text(new_html, encoding="utf-8")

    still = [a["slug"] for a in arts
             if not clean(a["side"].get("quick_answer")) or not valid_faq(a["side"].get("faq"))]
    print(f"[backfill_quick_answer] {date.today().isoformat()} · indicizzabili: {len(arts)} · "
          f"da generare: {len(todo)} · generati ora: {generated} · falliti: {len(failed)} · "
          f"HTML aggiornati: {html_changed}{' (dry-run)' if args.dry_run else ''} · "
          f"ancora senza quick_answer/faq: {len(still)}")
    for slug, errs in failed:
        print(f"  FALLITO {slug}:\n     " + "\n     ".join(errs))
    for slug, warns in warned:
        print(f"  AVVISO {slug}: " + "; ".join(warns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
