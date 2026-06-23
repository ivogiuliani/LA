#!/usr/bin/env python3
"""
My Villa — Journal Article Generator
Converts radar JSON → Journal articles (HTML) via Claude Opus

Usage:
  python3 generate_journal.py \
    --radar _system/radar/reports/radar_2026-04-09.json \
    --output blog/

  python3 generate_journal.py --radar radar.json --dry-run --max-articles 1
"""

import json
import os
import re
import sys
import argparse
import html as html_module
from datetime import datetime, timedelta
from pathlib import Path

import urllib.parse
import yaml

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

try:
    from image_picker import fetch_source_images, download_source_image, fetch_hero_image
    IMAGE_PICKER_OK = True
except ImportError:
    IMAGE_PICKER_OK = False

try:
    from validate_links import (
        process_file as validate_links_file,
        check_url as _check_url_live,
        validate_links as _validate_links_live,
        extract_links as _extract_links_live,
        strip_broken_anchors as _strip_broken_anchors_live,
    )
    VALIDATE_LINKS_OK = True
except ImportError:
    VALIDATE_LINKS_OK = False

# ── Paths ────────────────────────────────────────────────────────────
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
_WRITER_MODEL = _resolve_model("writer")
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
KNOWLEDGE_DIR = SYSTEM_DIR / "knowledge"
HISTORY_DIR = SYSTEM_DIR / "history"
TEMPLATE_DIR = SYSTEM_DIR / "templates"
BLOG_DIR = SYSTEM_DIR.parent / "blog"


def load_dotenv():
    env_file = SYSTEM_DIR.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v

load_dotenv()


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    """Atomic write: tmp + os.replace. Un kill a metà scrittura del
    ledger lasciava un JSON troncato che faceva crashare OGNI run
    successivo finché non si riparava a mano."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ── Perspective knowledge base ───────────────────────────────────────
# Facts used by the "Our Perspective" block are grounded in two files:
#   1. project_brief.md — extracted from MyItalianVilla_LA.pdf (the
#      master project document: typology, construction system, interiors).
#   2. site_content.md  — extracted from the live myvilla.la homepage
#      (public-facing copy, positioning, approved narrative).
# Voice rules (forbidden terms, tone) live in brand-voice.yml and are
# handled separately — they are rules, not facts.
PERSPECTIVE_KB_FILES = ["project_brief.md", "site_content.md"]

# Content strategy file — NOT a source of facts. It tells the LLM who we
# are writing to, which geographies to prioritise in titles, and what
# commercial-intent keywords to target. Loaded into the prompt as a
# separate "STRATEGY" block so the generator can produce SEO-fit titles
# aligned with myvilla.la's business objectives.
STRATEGY_FILE = "content_strategy.md"


def load_content_strategy(knowledge_dir):
    """Load the canonical content strategy document if present."""
    path = knowledge_dir / STRATEGY_FILE
    if not path.exists():
        return ""
    try:
        return path.read_text().strip()
    except Exception as exc:
        print(f"  [Strategy] Failed to read {path.name}: {exc}")
        return ""


def load_perspective_kb(knowledge_dir):
    """Load factual knowledge base used to ground the 'Our Perspective' block.

    Returns a single concatenated string with clear section markers, or
    an empty string if no files are found (script still runs, just
    without extra grounding).
    """
    parts = []
    for fname in PERSPECTIVE_KB_FILES:
        path = knowledge_dir / fname
        if not path.exists():
            print(f"  [KB] Missing: {path.name} — skipping")
            continue
        try:
            text = path.read_text().strip()
        except Exception as exc:
            print(f"  [KB] Failed to read {path.name}: {exc}")
            continue
        if not text:
            continue
        parts.append(f"### SOURCE: {fname}\n\n{text}")
    if not parts:
        return ""
    return "\n\n---\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════
# JOURNAL CRITERIA & DEDUP
# ══════════════════════════════════════════════════════════════════════

def load_journal_config():
    sections_config = load_yaml(CONFIG_DIR / "journal-sections.yml")
    return sections_config


def load_ledger():
    path = HISTORY_DIR / "journal_ledger.json"
    if not path.exists():
        return {
            "published_articles": [],
            "topic_cooldowns": {},
            "data_point_cooldowns": {},
            "source_cooldowns": {},
            "section_cooldowns": {},
        }
    return load_json(path)


def save_ledger(ledger):
    save_json(HISTORY_DIR / "journal_ledger.json", ledger)


# ── Fuzzy-title duplicate detection ──────────────────────────────────
#
# The topic_cooldown mechanism (below) only catches articles that share
# an EXACT topic_tag string. In practice the LLM invents subtly
# different tags every run ("fair-plan-rate-hike" vs "fair-plan-
# october-2026") and the cooldown misses the duplication. This second
# layer compares the SOURCE TITLE of the candidate against:
#   1. titles of articles already in _drafts/journal/ (pending queue)
#   2. titles of articles published in the last `lookback_days` days
# Two titles are considered the same topic when they share at least
# `overlap_threshold` significant tokens (4+ char alphabetic words,
# minus a stopword list).
#
# Tuning: threshold of 3 catches 'FAIR Plan 29% Hike' vs 'FAIR Plan 29%
# Rate Hike' (5 shared tokens) while staying under generic news drift
# like 'Mercury Insurance California' vs 'Travelers Insurance California'
# (2 shared tokens, both common across stories).

_TOPIC_STOPWORDS = {
    # Articles, prepositions, common verbs
    "the", "and", "for", "with", "from", "into", "onto", "this", "that",
    "these", "those", "have", "been", "being", "were", "will", "would",
    "could", "should", "shall", "must", "what", "when", "where", "which",
    "while", "after", "before", "about", "above", "below", "than", "more",
    "less", "most", "least", "some", "many", "much", "both", "either",
    "neither",
    # Common but non-discriminating in OUR domain (these words appear in
    # most My Villa journal titles, so they don't disambiguate stories).
    # NOTE: words like "rate", "fire", "code", "wildfire" are KEPT in the
    # signature on purpose — they're the words that DO distinguish.
    "california", "home", "house", "luxury", "angeles", "year",
}


def _stem(word):
    """Naive plural stripper. Good enough for title-level dedup.

    Rules:
      'rates' → 'rate'  (simple -s plural; the common case for our domain)
      'companies' → 'company'  (-ies plural)
      'houses' → 'house'  (the trailing -e survives because we only strip -s)

    Edge case we accept: 'taxes' → 'taxe' is technically wrong (should be
    'tax'), but the word never appears in our journal pipeline so the
    loss of accuracy is irrelevant. Pinning to "-es means strip both
    chars" was the bug that caused 'rates' → 'rat'.
    """
    if len(word) <= 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s"):
        return word[:-1]
    return word


def _topic_signature(title):
    """Significant tokens from a title — stopwords removed, plurals stemmed.

    Stemming matters: without it, 'rate' vs 'rates' or 'home' vs 'homes'
    look like different tokens and the overlap drops below threshold.
    With stemming, 'FAIR Plan rates' and 'FAIR Plan Rate Hike' share
    {fair, plan, rate} — 3 tokens → caught as duplicate.
    """
    if not title:
        return set()
    out = set()
    for w in re.findall(r"[A-Za-z]{4,}", title.lower()):
        if w in _TOPIC_STOPWORDS:
            continue
        stem = _stem(w)
        if stem in _TOPIC_STOPWORDS:
            continue
        out.add(stem)
    return out


def is_duplicate_topic(candidate_title, ledger,
                       *, lookback_days=21, overlap_threshold=3):
    """Detect duplicate-topic articles before they get generated.

    Returns (is_dup: bool, conflict_title: str). False/"" when no
    overlap found. Compares against:
      a) titles of pending drafts in _drafts/journal/*.json (any age)
      b) titles of recently-published articles in the ledger (last
         `lookback_days` days)
    """
    cand_sig = _topic_signature(candidate_title)
    if len(cand_sig) < overlap_threshold:
        return False, ""

    # Pending drafts — high-signal: if we have an unpublished article on
    # this topic, generating another now is almost always wrong.
    drafts_dir = SYSTEM_DIR.parent / "_drafts" / "journal"
    if drafts_dir.exists():
        for f in drafts_dir.glob("*.json"):
            try:
                draft = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            other_title = draft.get("title") or ""
            other_sig = _topic_signature(other_title)
            if other_sig and len(cand_sig & other_sig) >= overlap_threshold:
                return True, other_title

    # Published articles — last `lookback_days` days only. Beyond that,
    # the world has moved on and a follow-up piece is legitimate.
    try:
        cutoff = datetime.now() - timedelta(days=lookback_days)
    except Exception:
        cutoff = None
    for art in ledger.get("published_articles") or []:
        # NB: update_ledger scrive "published_date" — senza quella chiave
        # qui, pub_str restava vuoto, il lookback di 21gg non scattava
        # mai e TUTTI gli articoli storici (80+) bloccavano per sempre
        # ogni nuovo titolo con >=3 token in comune (over-blocking).
        pub_str = (art.get("published_date") or art.get("published_at")
                   or art.get("date") or "")
        if cutoff is not None and pub_str:
            try:
                if datetime.strptime(pub_str[:10], "%Y-%m-%d") < cutoff:
                    continue
            except Exception:
                pass
        other_title = art.get("title") or ""
        other_sig = _topic_signature(other_title)
        if other_sig and len(cand_sig & other_sig) >= overlap_threshold:
            return True, other_title

    return False, ""


def is_in_cooldown(ledger, today_str):
    """Return blocked topics, data points, sources, sections."""
    blocked = {
        "topics": [],
        "data_points": [],
        "sources": [],
        "sections": [],
    }
    today = datetime.strptime(today_str, "%Y-%m-%d")

    for topic, date_str in ledger.get("topic_cooldowns", {}).items():
        cooldown_end = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=21)
        if today < cooldown_end:
            blocked["topics"].append(topic)

    for dp, date_str in ledger.get("data_point_cooldowns", {}).items():
        cooldown_end = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=45)
        if today < cooldown_end:
            blocked["data_points"].append(dp)

    for source, date_str in ledger.get("source_cooldowns", {}).items():
        cooldown_end = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=14)
        if today < cooldown_end:
            blocked["sources"].append(source)

    for section, date_str in ledger.get("section_cooldowns", {}).items():
        cooldown_end = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=3)
        if today < cooldown_end:
            blocked["sections"].append(section)

    return blocked


def _saturated_keywords(journal_config):
    """Keyword 'core' già SATURE in blog/ (≥ cap articoli che le trattano):
    non vanno generati altri articoli su queste — è la cannibalizzazione che
    l'audit SEO ha trovato (decine di pagine per la stessa query). Conta dai
    SLUG in blog/ (ground truth del live). → set di keyword sature (lowercase)."""
    caps = journal_config.get("keyword_caps") or {}
    if not caps:
        return set()
    slugs = [p.stem.lower() for p in BLOG_DIR.glob("*.html")] \
        if BLOG_DIR.exists() else []
    saturated = set()
    for kw, cap in caps.items():
        token = str(kw).lower().replace(" ", "-")
        if sum(1 for s in slugs if token in s) >= int(cap):
            saturated.add(str(kw).lower().replace("-", " "))
    return saturated


def filter_candidates(radar_data, ledger, journal_config, max_articles=2, min_score_override=None):
    """Filter radar results by journal criteria."""
    # `is not None` (non `or`): --min-score 0 è falsy e veniva ignorato.
    min_score = (min_score_override if min_score_override is not None
                 else journal_config.get("journal_criteria", {}).get("min_score", 17))
    today_str = radar_data.get("date", datetime.now().strftime("%Y-%m-%d"))
    blocked = is_in_cooldown(ledger, today_str)

    qualified = radar_data.get("qualified", [])
    candidates = []
    saturated_kw = _saturated_keywords(journal_config)
    if saturated_kw:
        print(f"  [Filter] Keyword sature (cap raggiunto, niente nuovi "
              f"articoli): {', '.join(sorted(saturated_kw))}")

    for item in qualified:
        score = item.get("ai_score", item.get("preliminary_score", 0))
        if score < min_score:
            continue

        # Check section cooldown
        cluster = item.get("cluster", "")
        section = cluster_to_section(cluster, journal_config)
        if section in blocked["sections"]:
            print(f"  [Filter] Skipped (section cooldown): {item.get('title', '')[:50]}")
            continue

        # Check source cooldown
        pub = item.get("publication", "")
        if pub.lower() in [s.lower() for s in blocked["sources"]]:
            print(f"  [Filter] Skipped (source cooldown): {item.get('title', '')[:50]}")
            continue

        # Fuzzy duplicate-topic guard: compare the candidate's source
        # title against existing drafts + recently-published articles.
        # Catches near-duplicates the topic_cooldown misses because the
        # LLM invents slightly different topic_tag strings each run.
        cand_title = item.get("title", "")
        is_dup, conflict = is_duplicate_topic(cand_title, ledger)
        if is_dup:
            print(f"  [Filter] Skipped (duplicate topic): {cand_title[:60]}")
            print(f"             conflicts with: {conflict[:60]}")
            continue

        # Anti-cannibalizzazione: se la keyword core del candidato è già
        # satura in blog/, NON aggiungere un'altra pagina che compete per la
        # stessa query (la copertura commerciale viene dalle pillar dedicate).
        _t = cand_title.lower()
        _hit = next((kw for kw in saturated_kw if kw in _t), None)
        if _hit:
            print(f"  [Filter] Skipped (keyword satura '{_hit}'): {cand_title[:55]}")
            continue

        # Attach the resolved section so the diversity pass below can
        # spread the day's picks across sections.
        item["_resolved_section"] = section
        candidates.append(item)

    # ── Section-diverse selection ──────────────────────────────────
    # Don't just take the top-N by score: if the top items are all
    # 'insurance' (high news volume), we'd publish 2-3 insurance pieces
    # and starve other sections. Instead:
    #   Pass 1: best-scoring candidate from EACH section (max variety)
    #   Pass 2: fill remaining slots with the next-best overall
    # Within a section, higher score wins. This guarantees that when
    # multiple sections have material, a single run spans them.
    def _score(it):
        return it.get("ai_score", it.get("preliminary_score", 0))

    candidates.sort(key=_score, reverse=True)  # global score order

    selected = []
    selected_ids = set()   # identità (id()), non uguaglianza per valore:
    # due item radar distinti ma value-equal venivano dedupati per errore
    seen_sections = set()
    # Pass 1 — one per section, in score order
    for it in candidates:
        if len(selected) >= max_articles:
            break
        sec = it.get("_resolved_section")
        if sec not in seen_sections:
            selected.append(it)
            selected_ids.add(id(it))
            seen_sections.add(sec)
    # Pass 2 — fill any remaining slots with best remaining (allows a
    # 2nd article from an already-used section only if nothing else left)
    if len(selected) < max_articles:
        for it in candidates:
            if len(selected) >= max_articles:
                break
            if id(it) not in selected_ids:
                selected.append(it)
                selected_ids.add(id(it))

    candidates = selected
    by_sec = {}
    for it in candidates:
        by_sec[it.get("_resolved_section")] = by_sec.get(it.get("_resolved_section"), 0) + 1
    spread = ", ".join(f"{k}:{v}" for k, v in by_sec.items()) or "none"
    print(f"  [Filter] {len(candidates)} candidates passed "
          f"(min_score={min_score}, max={max_articles}) — sezioni: {spread}")
    return candidates


def cluster_to_section(cluster, journal_config):
    """Map a radar cluster to a journal section ID.

    The radar assigns cluster names somewhat freely (config clusters +
    AI/RSS-derived names like 'luxury_real_estate', 'architecture_blogs').
    So we match in three passes, most-specific first:
      1. Exact match against a section's declared clusters (config).
      2. Exact match against a hardcoded fallback table.
      3. Substring/keyword heuristic — robust to NEW cluster names so
         nothing silently dumps into the wrong section (this was the
         bug: 'luxury_real_estate' matched nothing and defaulted to
         'materials', starving the 'market' section).
    """
    cl = (cluster or "").lower()
    sections = journal_config.get("sections", [])

    # 1. Exact match against config-declared clusters
    for sec in sections:
        if cluster in sec.get("clusters", []):
            return sec["id"]

    # 2. Exact fallback table
    mapping = {
        "rebuild_direct": "permits",
        "rebuild_secondary": "permits",
        "materials_construction": "materials",
        "insurance_regulation": "insurance",
        "luxury_architecture_LA": "market",
        "luxury_real_estate_LA": "market",
        "luxury_real_estate": "market",
        "luxury_insurable_newbuild": "market",
        "architecture_blogs": "concrete_arch",
        "italian_mediterranean_villa": "concrete_arch",
        "concrete_architecture": "concrete_arch",
        "climate_resilience": "climate",
    }
    if cluster in mapping:
        return mapping[cluster]

    # 3. Substring heuristic — robust to unseen cluster names
    if "insurance" in cl or "fair_plan" in cl or "regulation" in cl:
        return "insurance"
    if "climate" in cl or "resilience" in cl or "sustainab" in cl:
        return "climate"
    if "rebuild" in cl or "permit" in cl or "policy" in cl:
        return "permits"
    if "real_estate" in cl or "market" in cl or "newbuild" in cl or "new_build" in cl:
        return "market"
    if "concrete" in cl or "architecture" in cl or "villa" in cl or "design" in cl:
        return "concrete_arch"
    if "material" in cl or "construction" in cl:
        return "materials"
    # Last resort: materials (broadest construction bucket)
    return "materials"


def section_info(section_id, journal_config):
    """Get section name and accent color."""
    for sec in journal_config.get("sections", []):
        if sec["id"] == section_id:
            return sec.get("name", section_id), sec.get("accent_color", "#C2714F")
    return section_id, "#C2714F"


# ══════════════════════════════════════════════════════════════════════
# ARTICLE GENERATION (Claude Opus)
# ══════════════════════════════════════════════════════════════════════

def build_generation_prompt(item, section_id, section_name, brand_voice, blocked,
                            perspective_kb="", prior_articles=None,
                            content_strategy=""):
    """Build the full prompt for Opus article generation.

    `prior_articles` — list of recent published articles (from ledger) with
    at least {title, slug, section, our_perspective}. Injected so the LLM
    can actively vary wording, framing, credentials cited, and closings.

    `content_strategy` — string with the canonical content strategy
    document (geographies, SEO priorities, buyer persona). Steers the
    title + framing of the article toward commercial-intent queries.
    """
    # Brand voice = rules only (forbidden terms, voice rules). Facts about
    # My Villa are provided separately via `perspective_kb`.
    forbidden = ", ".join(brand_voice.get("forbidden_terms", []))
    voice_rules = "\n".join(
        f"- {r['rule']}: {r['detail']}" for r in brand_voice.get("voice_rules", []))

    kb_block = ""
    if perspective_kb:
        kb_block = f"""
## MY VILLA KNOWLEDGE BASE (ground-truth for the 'Our Perspective' block)
The facts, positioning, construction details, and narrative used in the
"Our Perspective" block MUST come from the sources below. Do NOT invent
new claims about My Villa, do NOT use general training-data knowledge.
If a claim is not supported by these sources, do not make it.

{perspective_kb}
"""

    strategy_block = ""
    if content_strategy:
        strategy_block = f"""
## CONTENT STRATEGY (audience, geography, SEO priorities)
The title, framing, meta description, and keywords of this article MUST
align with the strategy below. This is NOT a fact source — it is the
editorial brief telling you WHO this article is for, WHICH geographies
to anchor in the title, and WHICH search queries we are trying to rank
for.

SPECIFIC TITLE RULES (hard constraints):
- 50–65 characters preferred, max 70.
- Primary keyword in first half of title when possible.
- Geography anchor order of preference: California > Los Angeles >
  Malibu > Beverly Hills > Bel Air / Brentwood / Westside. Use
  Palisades or Altadena in the title ONLY when the story is
  specifically about those places and no broader framing exists.
- Prefer buyer-intent framing ("Fire-Resistant Home Cost in California:
  …") over clever news headlines ("The 3% Decision: …"). Keep
  cleverness in the subtitle.
- NO rhetorical questions as the primary title.
- Include at least one of: luxury, insurable, fire-resistant,
  reinforced concrete, Italian villa, Mediterranean villa, custom
  home, new construction, rebuild (only if truly rebuild-focused).
- The current year (2026) is a trust signal — use where genuine.

META DESCRIPTION RULES (hard constraints):
- 140–160 characters.
- Lead with the buyer's question or pain point.
- Include the primary title keyword verbatim.
- End with a reason to click (concrete number, credential, insight).

KEYWORDS RULES:
- Include at minimum 3–4 of the primary SEO targets listed in the
  strategy doc that match the article's topic.
- Prefer geography tokens from Tier 1–2 of the strategy doc.

STRATEGY DOCUMENT:
{content_strategy}
"""

    # Prior-coverage memory: tells the LLM what's already been said so it
    # doesn't echo its own boilerplate (DGU/Piano/Palazzo Grassi verbatim,
    # "material compounds over decades" closings, etc.).
    prior_block = ""
    prior_articles = prior_articles or []
    if prior_articles:
        lines = []
        dgu_recent_count = 0  # how many of the last entries cite DGU/Piano/Palazzo
        for pa in prior_articles:
            title = pa.get("title") or pa.get("slug", "")
            persp = (pa.get("our_perspective") or "").strip().replace("\n", " ")
            if len(persp) > 260:
                persp = persp[:257] + "..."
            if persp and any(k in persp for k in ("DGU", "Kimbell", "Palazzo Grassi",
                                                   "Renzo Piano", "Pinault")):
                dgu_recent_count += 1
            lines.append(f"- **{title}**\n  Our Perspective excerpt: \"{persp}\"")
        dgu_rule = (
            "DGU / Renzo Piano / Kimbell / Palazzo Grassi / Pinault Collection "
            "were cited in the last prior articles — you MUST NOT cite any of "
            "these in this article's Our Perspective. Choose a different "
            "credential (e.g. IT'S Architecture, Transsolar, Mezzalama partner "
            "architect track record, the Italian villa typology itself, or the "
            "reinforced-concrete system's technical qualities) and make the "
            "Perspective work without that specific name-drop."
            if dgu_recent_count >= 2 else
            "If you cite DGU / Renzo Piano / Kimbell / Palazzo Grassi, phrase "
            "the credential DIFFERENTLY from the prior articles — do not reuse "
            "the same sentence construction. Prefer rotating to a DIFFERENT "
            "credential (Transsolar, IT'S Architecture, Mezzalama, Italian "
            "typology) if the prior article already used DGU."
        )
        prior_block = f"""
## PRIOR COVERAGE — DO NOT REPEAT YOURSELF
The following articles have already been published. Your "Our Perspective"
block MUST:
- NOT echo the framing, wording, credentials, or closing maxim used in
  these prior entries.
- Find a DISTINCT angle for why material/system choice matters in this
  specific story — avoid generic repetitions of "material compounds over
  decades", "not what style but what system", or similar recurring
  formulations visible below.
- Rotate credentials: {dgu_rule}
- Avoid the word "compounds" and the phrase "over decades" if they appear
  in the prior excerpts below.

Prior articles (most recent first):
{chr(10).join(lines)}
"""

    return f"""You are writing for the My Villa Journal (myvilla.la/blog/).
The Journal is an editorial voice on insurance, construction, materials,
and regulation in Los Angeles — grounded in data, informed by daily monitoring.

## WRITING RULES (voice only — not a source of facts)
{voice_rules}
{strategy_block}
{kb_block}
## SOURCE MATERIAL (PRIMARY SOURCE — MANDATORY)
This article is based on the following radar signal. The URL below is the
ONLY verified external link. You MUST use this exact URL for the primary
source citation. Do NOT shorten it to a root domain. Do NOT replace it.
- Source: {item.get('publication', 'Unknown')}
- Title: {item.get('title', '')}
- URL: {item.get('url', '')}
- Snippet: {item.get('snippet', '')}
- Summary: {item.get('summary', '')}
- Score: {item.get('ai_score', item.get('preliminary_score', 0))}
- Cluster: {item.get('cluster', '')} → Section: {section_name}

## LINK RULES (STRICT)
- The `sources` array MUST include the primary source above as its FIRST entry,
  with the EXACT URL shown (no edits, no truncation, no root-domain fallback).
- Every `sources[].url` MUST be a deep link to a specific article, study, or
  government page. NEVER use a homepage or root domain (e.g. NOT
  `https://www.latimes.com` — use the actual article URL).
- If you cannot cite a deep link for a secondary claim, DO NOT fabricate one:
  either omit the source entry entirely, or cite the primary source again.
- Inside `body_html`, any `<a>` tag that references external material MUST
  point to one of the URLs listed in `sources[].url`. Do NOT link to publication
  homepages inside the body text.

## ARTICLE STRUCTURE
Write a 600-900 word article following this format:

1. OPENING (100-150 words): State the news/development factually.
   Cite the source publication explicitly with a link.

2. DATA & CONTEXT (200-300 words): Provide the relevant data points.
   Use inline source citations for every claim.
   Include a KEY DATA block with 2-3 headline numbers if applicable.

3. ANALYSIS (150-200 words): What this means for the LA market.
   This is where editorial voice enters — authoritative, informed, specific.

4. OUR PERSPECTIVE (80-120 words): A clearly labeled "Our Perspective" block.
   This is My Villa's take — connect the news to the thesis that construction
   material is the most consequential variable. Do NOT be salesy.
   Tone: informed operator sharing a professional viewpoint.
   VOICE RULES (mandatory):
   - Speak as My Villa in FIRST PERSON ("we", "our", "at My Villa").
   - NEVER name Paolo Mezzalama. No "Paolo Mezzalama's practice",
     "Paolo Mezzalama's view", "led by Paolo Mezzalama", etc.
     The voice is the brand, not the person.
   - Mention "IT'S Architecture" only if STRICTLY necessary to the
     specific argument (rare). Default: omit — when My Villa speaks,
     naming the studio is usually irrelevant.
   - Allowed as system credentials (not personal names): DGU, Transsolar,
     named precedent buildings (Kimbell Art Museum, Palazzo Grassi,
     Mercedes-Benz Museum, Harvard LEED Platinum Complex).
   GROUNDING: every factual claim about My Villa (construction system,
   typology, design references, credentials, positioning) MUST be
   traceable to the MY VILLA KNOWLEDGE BASE section above. If the KB
   does not support a claim, do not make it.

5. CLOSING (50-80 words): Forward-looking statement.
   No call to action in the body text.

## CONSTRAINTS
- NEVER use: {forbidden}
- NEVER mention specific delivery timelines (e.g. "18 months")
- The thought leader (only if a person attribution is unavoidable in the
  article body) is Paolo Mezzalama (not Ivo Giuliani). The "Our Perspective"
  block must NOT name him — see VOICE RULES above.
- My Villa has NOT built any homes yet — do not claim otherwise
- All text MUST be in English
- Cite every factual claim with source attribution
- DO NOT make the article about My Villa — make it about the topic.
  My Villa appears ONLY in the "Our Perspective" block.

## DEDUP CONSTRAINTS
Topics in cooldown (used recently): {', '.join(blocked['topics']) or 'none'}
Data points in cooldown: {', '.join(blocked['data_points']) or 'none'}
Find a FRESH angle even if the broad topic has been covered.
{prior_block}

## OUTPUT FORMAT
Return a JSON object (no markdown fences):
{{
  "slug": "kebab-case-url-slug",
  "title": "Article title",
  "subtitle": "One-sentence italic subtitle for hero",
  "section": "{section_id}",
  "tag_label": "{section_name}",
  "meta_description": "SEO meta description (150-160 chars)",
  "meta_keywords": "comma,separated,keywords",
  "sources": [
    {{
      "name": "Publication Name",
      "title": "Article title at source",
      "url": "https://...",
      "type": "article|study|gov|x|reddit",
      "excerpt": "Brief description of what this source contributes"
    }}
  ],
  "key_data": [
    {{"number": "33%", "label": "Projected loss reduction\\nwith IBHS standards"}}
  ],
  "our_perspective": "Text for the Our Perspective block",
  "body_html": "Full article body in HTML using these CSS classes: p.reveal, h2.reveal, .source-citation, .key-data, .pullquote. IMPORTANT: do NOT include the Our Perspective block inside body_html — it is rendered separately from the our_perspective field above. NEVER output <div class=\"perspective\"> or <h2>Our Perspective</h2> inside body_html.",
  "excerpt": "2-3 sentence excerpt for the Journal index card",
  "image_prompt": "Prompt for AI image generation — architectural photography style, muted warm tones",
  "topic_tags": ["tag-1", "tag-2"],
  "data_points_cited": ["specific stat 1", "specific stat 2"],
  "read_time_min": 6
}}"""


def _is_deep_link(url):
    """Return True if url is a specific article/page, not a root domain."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except Exception:
        return False
    if not p.netloc:
        return False
    # Require a meaningful path beyond "/" (at least one segment with 2+ chars)
    path = (p.path or "").strip("/")
    if not path:
        return False
    # Reject single-segment navigation slugs like "about", "contact"
    segments = [s for s in path.split("/") if s]
    if len(segments) == 0:
        return False
    # A real article usually has a multi-char slug somewhere
    if all(len(s) < 3 for s in segments):
        return False
    return True


def _root_domain(url):
    try:
        from urllib.parse import urlparse
        return re.sub(r"^www\.", "", (urlparse(url).netloc or "").lower())
    except Exception:
        return ""


def sanitize_sources(article, item):
    """Guarantee the primary radar source is present and drop root-domain fakes.

    - Force the first sources[] entry to be the primary radar URL.
    - Remove any sources[] entry whose URL is a bare homepage/root domain.
    - Rewrite any <a> inside body_html that points to a root domain so it
      instead links to the primary source URL (preserving visible link text).
    """
    primary_url = (item.get("url") or "").strip()
    primary_title = (item.get("title") or "").strip()
    primary_pub = (item.get("publication") or "").strip()

    sources = article.get("sources") or []
    cleaned = []
    seen = set()

    if primary_url and _is_deep_link(primary_url):
        primary_entry = {
            "name": primary_pub or "Primary Source",
            "title": primary_title or primary_pub or "Primary Source",
            "url": primary_url,
            "type": "article",
            "excerpt": item.get("snippet", "") or item.get("summary", ""),
        }
        # If Opus already included an entry matching the primary URL, prefer its metadata
        for s in sources:
            if (s.get("url") or "").strip() == primary_url:
                primary_entry = {**primary_entry, **{k: v for k, v in s.items() if v}}
                primary_entry["url"] = primary_url
                break
        cleaned.append(primary_entry)
        seen.add(primary_url)

    dropped = []
    for s in sources:
        url = (s.get("url") or "").strip()
        if not url or url in seen:
            continue
        if not _is_deep_link(url):
            dropped.append(url or s.get("name", "?"))
            continue
        cleaned.append(s)
        seen.add(url)

    article["sources"] = cleaned
    if dropped:
        print(f"  [Sanitize] Dropped {len(dropped)} non-deep-link source(s): {dropped}")

    # Repair body_html anchors that point at root domains.
    body = article.get("body_html") or ""
    if body and primary_url:
        def _fix_anchor(match):
            href = match.group(1)
            rest = match.group(2)
            if _is_deep_link(href):
                return match.group(0)
            # Anchors, relative links, mailto, etc. — leave alone
            if not href.startswith(("http://", "https://")):
                return match.group(0)
            return f'<a href="{primary_url}"{rest}'

        body_new = re.sub(r'<a\s+href="([^"]+)"([^>]*)', _fix_anchor, body)
        if body_new != body:
            print("  [Sanitize] Rewrote root-domain anchors in body_html → primary URL")
            article["body_html"] = body_new

    return article


def verify_sources_live(article, item=None, strict=True):
    """Live HEAD-check every URL in sources[] and body_html.

    Two-stage enforcement against LLM URL fabrication:
      1. Drop any sources[] entry whose URL fails a live HEAD/GET probe.
      2. Unwrap every <a href="http…"> in body_html that fails, preserving text.

    If ``strict`` and the PRIMARY radar source itself fails, returns None so
    the caller can abort/retry instead of publishing an article whose anchor
    citation is a 404.

    Returns the modified article dict (or None on hard failure in strict mode).
    Logs with the [Verify] prefix so it's greppable in CI logs.
    """
    if not VALIDATE_LINKS_OK:
        print("  [Verify] validate_links module not available — skipping live URL check")
        return article

    sources = article.get("sources") or []
    body = article.get("body_html") or ""

    # Collect every external URL we care about (sources list + body anchors)
    urls = []
    seen = set()
    for s in sources:
        u = (s.get("url") or "").strip()
        if u and u not in seen:
            urls.append(u)
            seen.add(u)
    for u in _extract_links_live(body):
        if u not in seen:
            urls.append(u)
            seen.add(u)

    if not urls:
        return article

    results = _validate_links_live(urls)
    status_by_url = {r["url"]: r for r in results}
    broken_urls = {r["url"] for r in results if not r["ok"]}

    primary_url = ""
    if item is not None:
        primary_url = (item.get("url") or "").strip()

    if broken_urls and primary_url and primary_url in broken_urls and strict:
        # Primary radar URL is dead — the article has no anchor citation. Abort.
        reason = status_by_url.get(primary_url, {}).get("reason", "unknown")
        print(f"  [Verify] ABORT: primary radar URL failed live check ({reason}): {primary_url}")
        return None

    # Drop broken sources[] entries
    if broken_urls:
        kept = []
        dropped = []
        for s in sources:
            u = (s.get("url") or "").strip()
            if u in broken_urls:
                reason = status_by_url.get(u, {}).get("reason", "?")
                dropped.append(f"{u} [{reason}]")
            else:
                kept.append(s)
        if dropped:
            print(f"  [Verify] Dropped {len(dropped)} broken source URL(s):")
            for d in dropped:
                print(f"             - {d}")
        article["sources"] = kept

        # Unwrap broken anchors inside body_html (preserve visible text)
        if body:
            new_body, n = _strip_broken_anchors_live(body, broken_urls)
            if n:
                print(f"  [Verify] Unwrapped {n} broken <a> anchor(s) in body_html")
                article["body_html"] = new_body
    else:
        print(f"  [Verify] All {len(urls)} source/body URL(s) resolved OK")

    # Sanity floor: if strict and we just killed every source, bail.
    if strict and not article.get("sources"):
        print("  [Verify] ABORT: no verified sources remain after live check")
        return None

    return article


def generate_article(item, section_id, section_name, brand_voice, blocked,
                     model=_WRITER_MODEL, perspective_kb="",
                     prior_articles=None, content_strategy=""):
    """Generate a Journal article using Opus."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        print("  [Generate] No valid API key — returning placeholder")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_generation_prompt(item, section_id, section_name, brand_voice, blocked,
                                     perspective_kb=perspective_kb,
                                     prior_articles=prior_articles,
                                     content_strategy=content_strategy)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        # Output troncato a max_tokens → JSON incompleto: distinguilo
        # dal generico parse error (token pagati, retry sensato a mano).
        if getattr(response, "stop_reason", None) == "max_tokens":
            print("  [Generate] ABORT: output troncato a max_tokens — "
                  "articolo troppo lungo, alzare max_tokens o accorciare il brief")
            return None
        text = response.content[0].text.strip()

        # Handle potential markdown fences
        if text.startswith("```"):
            text = re.sub(r'^```json?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)

        article = json.loads(text)
        # Sanitizza lo slug PRIMA di qualunque uso come filename/URL:
        # uno slug LLM con '/', '..', spazi o maiuscole causava path
        # traversal / FileNotFoundError / canonical URL rotti.
        raw_slug = str(article.get("slug") or "")
        safe_slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug.lower()).strip("-")[:80]
        article["slug"] = safe_slug or f"article-{datetime.now():%Y%m%d%H%M%S}"
        # Repair/validate source URLs against the primary radar signal.
        article = sanitize_sources(article, item)
        # Live HEAD-check every remaining URL and drop/unwrap 404s.
        # Protects against LLM-fabricated deep URLs that pass structural checks.
        article = verify_sources_live(article, item=item, strict=True)
        if article is None:
            print("  [Generate] Aborted: article failed live URL verification")
            return None
        print(f"  [Generate] Created: {article.get('slug', 'unknown')}")
        return article

    except json.JSONDecodeError as e:
        print(f"  [Generate] JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"  [Generate] Error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# HTML RENDERING
# ══════════════════════════════════════════════════════════════════════

def esc(text):
    return html_module.escape(str(text)) if text else ""


def source_icon_svg(source_type):
    """Return SVG icon for source type."""
    icons = {
        "article": '<svg viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.2"/><path d="M6 7h4M6 9.5h4M6 12h2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>',
        "study": '<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="6" r="3" stroke="currentColor" stroke-width="1.2"/><path d="M4 13c0-2.2 1.8-4 4-4s4 1.8 4 4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>',
        "gov": '<svg viewBox="0 0 16 16" fill="none"><path d="M3 14h10M4 10v4M8 7v7M12 10v4M3 7l5-5 5 5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        "x": '<svg viewBox="0 0 16 16"><path d="M9.5 6.7L14.9 1h-1.3L8.9 5.8 5.5 1H1l5.7 8.3L1 15.6h1.3l5-5.7 4 5.7H15L9.5 6.7z" fill="currentColor"/></svg>',
        "reddit": '<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.2"/><circle cx="6" cy="7" r="1" fill="currentColor"/><circle cx="10" cy="7" r="1" fill="currentColor"/><path d="M5.5 10c.8.8 2.2 1.2 2.5 1.2s1.7-.4 2.5-1.2" stroke="currentColor" stroke-width="1" stroke-linecap="round"/></svg>',
    }
    return icons.get(source_type, icons["article"])


def render_source_chips(sources):
    """Render the source strip chips."""
    chips = []
    for s in sources:
        stype = s.get("type", "article")
        icon_class = f"sci-{stype}"
        chips.append(
            f'<a href="{esc(s.get("url", "#"))}" class="source-chip" target="_blank">'
            f'<span class="source-chip-icon {icon_class}">{source_icon_svg(stype)}</span>'
            f'{esc(s.get("name", ""))}'
            f'</a>'
        )
    return "\n    ".join(chips)


def render_sources_list(sources):
    """Render the footer sources list."""
    items = []
    for s in sources:
        url = s.get("url", "")
        name = s.get("name", "")
        title = s.get("title", "")
        if url:
            items.append(f'<li><strong>{esc(name)}</strong> — <a href="{esc(url)}" target="_blank">{esc(title)}</a></li>')
        else:
            items.append(f'<li><strong>{esc(name)}</strong> — {esc(title)}</li>')
    return "\n      ".join(items)


def render_faq_block(faq):
    """Render a visible FAQ section. Required for Google FAQ rich-result eligibility:
    the same Q&A rendered in the FAQPage JSON-LD must be visible on the page.
    Uses <details>/<summary> so it's expandable but remains crawlable text.
    """
    if not faq:
        return ""
    items = []
    for q in faq:
        question = q.get("q", "")
        answer = q.get("a", "")
        if not question or not answer:
            continue
        items.append(
            f'<details class="faq-item">\n'
            f'        <summary class="faq-q">{esc(question)}</summary>\n'
            f'        <div class="faq-a">{esc(answer)}</div>\n'
            f'      </details>'
        )
    if not items:
        return ""
    return (
        '<div class="faq-section">\n'
        '      <div class="faq-title">Frequently asked</div>\n      '
        + "\n      ".join(items)
        + '\n    </div>'
    )


def render_related_block(related):
    """Render the 'Continue reading' related-articles block from a curated list.

    `related` is a list of {slug, title, tag_label} objects, max 3-4 items.
    Returns an empty string if `related` is falsy — the template then omits the block.
    Each card links to /blog/{slug}.html and surfaces the peer's tag + title.
    """
    if not related:
        return ""
    cards = []
    for r in related[:4]:
        slug = r.get("slug", "")
        title = r.get("title", "")
        tag = r.get("tag_label") or r.get("tag") or ""
        if not slug or not title:
            continue
        cards.append(
            f'<a href="/blog/{esc(slug)}.html" class="related-card">'
            f'<span class="related-card-tag">{esc(tag)}</span>'
            f'<h4 class="related-card-title">{esc(title)}</h4>'
            f'</a>'
        )
    if not cards:
        return ""
    return (
        '<div class="related-section">\n'
        '      <div class="related-title">Continue reading</div>\n'
        '      <div class="related-grid">\n        '
        + "\n        ".join(cards)
        + '\n      </div>\n    </div>'
    )


def section_accent_class(section_id):
    mapping = {
        "insurance": "insurance",
        "materials": "construction",
        "concrete_arch": "construction",
        "permits": "permits",
        "market": "market",
        "climate": "climate",
    }
    return mapping.get(section_id, "insurance")


def _wrap_key_data_blocks(body_html):
    """Wrap consecutive <div class="key-data">...</div> elements in a
    <div class="key-data-grid"> container and rename individual items
    to key-data-item for proper grid layout.
    """
    if not body_html or "key-data" not in body_html:
        return body_html
    import re
    # Match groups of 2+ consecutive key-data divs (possibly with whitespace between)
    pattern = re.compile(
        r'((?:<div\s+class="key-data">.*?</div>\s*){2,})',
        re.DOTALL,
    )
    def _replacer(m):
        block = m.group(0)
        items = block.replace('class="key-data"', 'class="key-data-item"')
        return f'<div class="key-data-grid">\n{items}</div>'
    return pattern.sub(_replacer, body_html)


def _strip_inline_perspective(body_html):
    """Remove any <div class="perspective">...</div> blocks that Opus may
    have inlined inside body_html. The perspective is rendered once by the
    template from the `our_perspective` field, so any in-body occurrence is
    a duplicate.
    """
    if not body_html or "perspective" not in body_html.lower():
        return body_html
    import re
    # Match balanced-ish <div class="perspective"> ... </div>. We use a
    # non-greedy match up to the next closing </div>. This is safe because
    # the perspective block in generated HTML never nests other <div>s.
    pattern = re.compile(
        r'<div[^>]*class=["\'][^"\']*\bperspective\b[^"\']*["\'][^>]*>.*?</div>\s*',
        re.IGNORECASE | re.DOTALL,
    )
    cleaned = pattern.sub("", body_html)
    # Also strip any stray "<h2>Our Perspective</h2>" that might remain
    # outside a div (some models emit it as a plain heading).
    cleaned = re.sub(
        r'<h2[^>]*>\s*Our\s+Perspective\s*</h2>\s*',
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def render_article_html(article, date_str):
    """Render a full standalone HTML article page."""
    slug = article.get("slug", "untitled")
    title = article.get("title", "Untitled")
    # SEO title: short-form used ONLY for <title>, og:title, twitter:title, schema headline.
    # Falls back to full title. Keeps H1 = editorial long title.
    seo_title = article.get("seo_title") or title
    subtitle = article.get("subtitle", "")
    section = article.get("section", "materials")
    tag_label = article.get("tag_label", "Journal")
    meta_desc = article.get("meta_description", "")
    meta_kw = article.get("meta_keywords", "")
    sources = article.get("sources", [])
    body_html = article.get("body_html", "<p>Article content pending.</p>")
    # Defensive sanitizers
    body_html = _strip_inline_perspective(body_html)
    body_html = _wrap_key_data_blocks(body_html)
    our_perspective = article.get("our_perspective", "")
    key_data = article.get("key_data", [])
    read_time = article.get("read_time_min", 6)
    related = article.get("related") or []
    faq = article.get("faq") or []
    accent = section_accent_class(section)
    hero_image = article.get("hero_image") or {}

    # Render the hero image block (if present)
    hero_image_html = ""
    if hero_image and hero_image.get("web_path"):
        alt = esc(hero_image.get("alt_description") or title)
        author_name = esc(hero_image.get("author_name") or "Unknown")
        author_url = esc(hero_image.get("author_url") or "")
        article_url = esc(hero_image.get("unsplash_url") or "")
        web_path = esc(hero_image.get("web_path"))
        # origin: explicit > derive from source > default unsplash. The brand
        # fallback uses source="brand_fallback" and gets a dedicated branch
        # below (no credit line — it's our own asset).
        src = hero_image.get("source") or ""
        origin = hero_image.get("origin")
        if not origin:
            if src == "cited_source":
                origin = "source"
            elif src == "brand_fallback":
                origin = "brand"
            else:
                origin = "unsplash"
        author_link = (
            f'<a href="{author_url}" target="_blank" rel="noopener">{author_name}</a>'
            if author_url else author_name
        )
        if origin == "source":
            # Credit the publication with a link to the cited article
            article_link = (
                f'<a href="{article_url}" target="_blank" rel="noopener">original article</a>'
                if article_url else "original article"
            )
            credit = f'Photo: {author_link} — via {article_link}'
        elif origin == "brand":
            # Brand-asset fallback: no figcaption — our wordmark, not a
            # third-party photo. Showing "Photo: Unknown / Unsplash" here
            # would be misleading (and wrong — it's not from Unsplash).
            credit = None
        else:
            unsplash_link = (
                f'<a href="{article_url}" target="_blank" rel="noopener">Unsplash</a>'
                if article_url else "Unsplash"
            )
            credit = f'Photo: {author_link} / {unsplash_link}'
        figcaption_html = (
            f'<figcaption class="hero-credit">{credit}</figcaption>' if credit else ''
        )
        hero_image_html = (
            '<div class="hero-image-wrap">'
            '<figure class="hero-image">'
            f'<img src="{web_path}" alt="{alt}" loading="eager" decoding="async" referrerpolicy="no-referrer">'
            f'{figcaption_html}'
            '</figure>'
            '</div>'
        )

    # Format date
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%B %d, %Y")  # e.g. "April 9, 2026"
    except ValueError:
        date_display = date_str

    canonical = f"https://myvilla.la/blog/{slug}.html"
    from urllib.parse import quote as _urlquote
    share_url_enc = _urlquote(canonical, safe='')
    share_title_enc = _urlquote(title, safe='')

    # FAQPage JSON-LD block — emitted ONLY when article carries a `faq` list of {q,a}.
    # Google eligibility: FAQ rich results are shown for authoritative content with
    # genuine Q&A. Keeping this optional (absent → empty string → no empty schema).
    if faq:
        import json as _json
        _faq_items_json = ",".join(
            '{"@type":"Question","name":' + _json.dumps(q.get("q", "")) +
            ',"acceptedAnswer":{"@type":"Answer","text":' + _json.dumps(q.get("a", "")) + '}}'
            for q in faq if q.get("q") and q.get("a")
        )
        faq_schema_block = (
            '<script type="application/ld+json">\n'
            '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[' +
            _faq_items_json + ']}\n'
            '</script>'
        )
    else:
        faq_schema_block = ""

    # Article-specific OG image: prefer the hero image, fall back to the generic journal OG.
    # Hero path is stored relative (e.g. "assets/img/{slug}-hero.jpg"); articles live at /blog/,
    # so the absolute URL is https://myvilla.la/blog/{web_path}.
    _hero_wp = (hero_image.get("web_path") if hero_image else "") or ""
    if _hero_wp:
        _hero_abs = f"https://myvilla.la/blog/{_hero_wp}"
        og_image_url = _hero_abs
        og_image_has_dims = False  # hero not guaranteed 1200x630
    else:
        og_image_url = "https://myvilla.la/assets/img/myvilla-og-journal.jpg"
        og_image_has_dims = True
    og_image_dims_tags = (
        '<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">'
        if og_image_has_dims else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-D6HJX7BNZN"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-D6HJX7BNZN');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{esc(seo_title)} — My Villa Journal</title>
<meta name="description" content="{esc(meta_desc)}">
<meta name="keywords" content="{esc(meta_kw)}">
<meta name="author" content="My Villa">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{canonical}">

<meta property="og:type" content="article">
<meta property="og:url" content="{canonical}">
<meta property="og:title" content="{esc(seo_title)} — My Villa Journal">
<meta property="og:description" content="{esc(meta_desc)}">
<meta property="og:site_name" content="My Villa">
<meta property="og:image" content="{og_image_url}">
{og_image_dims_tags}
<meta property="og:locale" content="en_US">
<meta property="article:section" content="{esc(tag_label)}">
<meta property="article:published_time" content="{date_str}T08:00:00-07:00">
<meta property="article:author" content="https://myvilla.la">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(seo_title)}">
<meta name="twitter:description" content="{esc(meta_desc)}">
<meta name="twitter:image" content="{og_image_url}">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">

<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "{esc(seo_title)}",
  "description": "{esc(meta_desc)}",
  "datePublished": "{date_str}",
  "dateModified": "{date_str}",
  "author": {{
    "@type": "Organization",
    "name": "My Villa Editorial Team",
    "url": "https://myvilla.la/team.html"
  }},
  "publisher": {{
    "@type": "Organization",
    "name": "My Villa",
    "url": "https://myvilla.la",
    "logo": {{ "@type": "ImageObject", "url": "https://myvilla.la/assets/img/myvilla-logo.png" }}
  }},
  "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{canonical}" }},
  "image": "{og_image_url}",
  "inLanguage": "en-US",
  "keywords": "{esc(meta_kw)}"
}}
</script>
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {{ "@type": "ListItem", "position": 1, "name": "Home", "item": "https://myvilla.la/" }},
    {{ "@type": "ListItem", "position": 2, "name": "Journal", "item": "https://myvilla.la/blog/" }},
    {{ "@type": "ListItem", "position": 3, "name": "{esc(title)}", "item": "{canonical}" }}
  ]
}}
</script>
{faq_schema_block}

<style>
*, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
:root {{
  --terracotta: #C2714F; --olive: #5C6B4F; --tuscan-gold: #C4A265;
  --warm-sand: #D4B896; --espresso: #3E2F2B; --coastal-sage: #A8B5A8;
  --dusk-blue: #8A9B9E; --pacific-blue: #6B8F9E; --sky-mist: #9BB8C4;
  --sand-white: #F0EBE3; --charcoal: #2C2C2C; --stone-grey: #A09890;
  --light-linen: #EDE6DC; --cream: #FAF8F5; --offblack: #1a1816; --white: #FFFFFF;
  --serif: 'Cormorant Garamond', Georgia, serif;
  --sans: 'Montserrat', 'Helvetica Neue', Arial, sans-serif;
  --section-pad: clamp(56px, 8vh, 100px);
  --side-pad: clamp(20px, 5vw, 120px);
}}
html {{ -webkit-font-smoothing: antialiased; }}
@media (prefers-reduced-motion: no-preference) and (min-width: 901px) {{ html {{ scroll-behavior: smooth; }} }}
body {{ font-family: var(--sans); background: var(--cream); color: var(--charcoal); overflow-x: hidden; line-height: 1.6; }}
img {{ display: block; max-width: 100%; }}
a {{ color: inherit; text-decoration: none; }}

.nav {{ position: fixed; top: 0; left: 0; width: 100%; z-index: 100; display: flex; align-items: center; justify-content: space-between; padding: 22px var(--side-pad); padding-top: max(22px, env(safe-area-inset-top)); background: rgba(62,47,43,0.97); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); transition: padding 0.3s; }}
.nav.scrolled {{ padding: 14px var(--side-pad); }}
.nav-logo-wrap {{ display: flex; flex-direction: column; text-decoration: none; }}
.nav-logo {{ font-family: var(--serif); font-size: clamp(18px, 2vw, 24px); font-weight: 400; letter-spacing: 0.22em; color: var(--white); }}
.nav-payoff {{ font-family: var(--sans); font-size: 10px; font-weight: 400; letter-spacing: 0.18em; text-transform: uppercase; color: var(--white); opacity: 0.6; margin-top: 2px; }}
.nav-links {{ display: flex; gap: 24px; align-items: center; }}
.nav-links a {{ font-size: 11px; font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase; color: var(--white); opacity: 0.6; transition: color 0.3s, opacity 0.3s; position: relative; }}
.nav-links a::after {{ content: ''; position: absolute; bottom: -4px; left: 0; width: 0; height: 1px; background: var(--pacific-blue); transition: width 0.3s; }}
.nav-links a:hover {{ color: var(--white); opacity: 1; }}
.nav-links a:hover::after {{ width: 100%; }}
.nav-links a.active {{ opacity: 1; }}
.nav-links a.active::after {{ width: 100%; }}
.nav-cta {{ font-size: 10px !important; font-weight: 600 !important; letter-spacing: 0.12em !important; opacity: 1 !important; border: 1px solid var(--terracotta) !important; padding: 10px 24px; transition: all 0.3s !important; }}
.nav-cta:hover {{ background: var(--pacific-blue) !important; border-color: var(--pacific-blue) !important; }}
.nav-cta::after {{ display: none !important; }}
.nav-burger {{ display: none; flex-direction: column; gap: 5px; cursor: pointer; width: 28px; padding: 4px 0; }}
.nav-burger span {{ display: block; height: 1.5px; background: var(--white); transition: all 0.3s; }}
.mobile-menu {{ position: fixed; inset: 0; background: rgba(62,47,43,0.98); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 32px; z-index: 99; opacity: 0; pointer-events: none; transition: opacity 0.4s; }}
.mobile-menu.open {{ opacity: 1; pointer-events: all; }}
.mobile-menu a {{ font-family: var(--serif); font-size: 28px; color: var(--white); transition: color 0.3s; }}
.mobile-menu a:hover {{ color: var(--terracotta); }}
@media (max-width: 900px) {{ .nav-links {{ display: none; }} .nav-burger {{ display: flex; }} }}

.article-hero {{ background: var(--espresso); color: var(--cream); padding: 110px var(--side-pad) 56px; text-align: center; }}
.article-hero-breadcrumb {{ font-family: var(--sans); font-size: 11px; letter-spacing: 0.08em; color: var(--stone-grey); margin-bottom: 20px; }}
.article-hero-breadcrumb a {{ color: var(--stone-grey); transition: color 0.3s; }}
.article-hero-breadcrumb a:hover {{ color: var(--white); }}
.article-hero-breadcrumb span {{ margin: 0 8px; opacity: 0.4; }}
.article-hero-tag {{ font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.2em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 16px; }}
.article-hero-title {{ font-family: var(--serif); font-size: clamp(28px, 4.2vw, 46px); font-weight: 400; line-height: 1.18; max-width: 820px; margin: 0 auto 18px; }}
.article-hero-title em {{ font-style: italic; font-weight: 300; }}
.article-hero-subtitle {{ font-family: var(--serif); font-size: clamp(16px, 1.6vw, 20px); font-weight: 300; font-style: italic; line-height: 1.5; color: var(--warm-sand); max-width: 640px; margin: 0 auto 20px; }}
.article-hero-divider {{ width: 48px; height: 1px; background: var(--tuscan-gold); margin: 0 auto 16px; }}
.article-hero-meta {{ font-size: 12px; color: var(--stone-grey); letter-spacing: 0.05em; }}

.hero-image-wrap {{ background: var(--espresso); padding: 0 var(--side-pad) 36px; }}
.hero-image {{ max-width: 1040px; margin: 0 auto; }}
.hero-image img {{ display: block; width: 100%; height: auto; max-height: 560px; object-fit: cover; border-radius: 2px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); }}
.hero-image .hero-credit {{ margin-top: 10px; font-family: var(--sans); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: rgba(255,255,255,0.5); text-align: right; }}
.hero-image .hero-credit a {{ color: rgba(255,255,255,0.8); text-decoration: none; border-bottom: 1px solid rgba(255,255,255,0.2); transition: border-color 0.3s; }}
.hero-image .hero-credit a:hover {{ border-color: var(--tuscan-gold); }}

.source-strip {{ background: var(--espresso); padding: 0 var(--side-pad) 36px; text-align: center; }}
.source-strip-inner {{ display: inline-flex; flex-wrap: wrap; gap: 10px; justify-content: center; }}
.source-strip-label {{ font-size: 10px; font-weight: 500; letter-spacing: 0.1em; text-transform: uppercase; color: var(--stone-grey); align-self: center; margin-right: 4px; }}
.source-chip {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 100px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); font-size: 11px; font-weight: 500; letter-spacing: 0.04em; color: rgba(255,255,255,0.7); transition: all 0.3s; text-decoration: none; }}
.source-chip:hover {{ background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.2); color: var(--white); }}
.source-chip-icon {{ width: 16px; height: 16px; border-radius: 3px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
.source-chip-icon svg {{ width: 10px; height: 10px; }}
.source-chip-icon.sci-article {{ background: var(--espresso); border: 1px solid rgba(255,255,255,0.15); }}
.source-chip-icon.sci-article svg {{ stroke: var(--cream); }}
.source-chip-icon.sci-study {{ background: var(--olive); }}
.source-chip-icon.sci-study svg {{ stroke: #fff; }}
.source-chip-icon.sci-gov {{ background: var(--pacific-blue); }}
.source-chip-icon.sci-gov svg {{ stroke: #fff; }}
.source-chip-icon.sci-x {{ background: #1a1816; border: 1px solid rgba(255,255,255,0.15); }}
.source-chip-icon.sci-x svg {{ fill: #fff; }}
.source-chip-icon.sci-reddit {{ background: #FF4500; }}
.source-chip-icon.sci-reddit svg {{ fill: #fff; }}

.article-body {{ background: var(--cream); padding: 56px var(--side-pad) var(--section-pad); }}
.article-content {{ max-width: 680px; margin: 0 auto; }}
.article-content p {{ font-size: clamp(16px, 1.5vw, 18px); line-height: 1.75; color: var(--charcoal); margin-bottom: 24px; }}
.article-content h2 {{ font-family: var(--serif); font-size: clamp(22px, 2.8vw, 28px); font-weight: 400; color: var(--espresso); margin-top: 48px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 2px solid var(--terracotta); line-height: 1.25; }}
.article-content h2:first-child {{ margin-top: 0; }}
.article-content h3 {{ font-family: var(--serif); font-size: clamp(19px, 2.3vw, 22px); font-weight: 400; color: var(--espresso); margin-top: 36px; margin-bottom: 14px; }}
.article-content a:not(.source-citation) {{ color: var(--pacific-blue); text-decoration: underline; text-underline-offset: 2px; text-decoration-color: rgba(107,143,158,0.3); }}

/* Inline citation link used within paragraphs as <a class="source-citation">Publication</a> */
a.source-citation, .source-citation {{ display: inline; color: var(--pacific-blue); font-weight: 500; text-decoration: none; background-image: linear-gradient(transparent 88%, rgba(107,143,158,0.28) 88%); background-repeat: no-repeat; padding: 0 1px; transition: background-image 0.3s, color 0.3s; white-space: normal; }}
a.source-citation:hover, .source-citation:hover {{ color: var(--espresso); background-image: linear-gradient(transparent 88%, rgba(194,113,79,0.35) 88%); }}
a.source-citation::after {{ content: '↗'; display: inline-block; font-size: 0.75em; margin-left: 2px; opacity: 0.6; transform: translateY(-1px); }}

.pullquote {{ border-left: 3px solid var(--tuscan-gold); padding: 4px 0 4px 28px; margin: 40px 0; font-family: var(--serif); font-style: italic; font-size: 22px; line-height: 1.6; color: var(--espresso); }}
.pullquote-attr {{ display: block; margin-top: 12px; font-family: var(--sans); font-style: normal; font-size: 12px; font-weight: 500; letter-spacing: 0.06em; color: var(--stone-grey); }}

/* Key data highlight grid — wraps consecutive .key-data-item cards */
.key-data-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--light-linen); border: 1px solid var(--light-linen); border-radius: 6px; overflow: hidden; margin: 40px 0; }}
.key-data-item {{ background: var(--white); padding: 28px 20px; text-align: center; font-size: 12px; font-weight: 500; letter-spacing: 0.04em; color: var(--stone-grey); line-height: 1.45; }}
.key-data-item > span {{ display: block; font-family: var(--serif); font-size: clamp(26px, 3.5vw, 34px); font-weight: 400; color: var(--terracotta); line-height: 1.1; margin-bottom: 10px; }}
/* Fallback: single key-data div (not inside a grid) */
.key-data {{ display: flex; align-items: baseline; gap: 16px; background: var(--white); border: 1px solid var(--light-linen); border-left: 3px solid var(--terracotta); border-radius: 0 6px 6px 0; padding: 20px 24px; margin: 24px 0; font-size: 13px; color: var(--stone-grey); line-height: 1.5; }}
.key-data > span {{ font-family: var(--serif); font-size: clamp(24px, 3vw, 30px); font-weight: 400; color: var(--terracotta); line-height: 1; white-space: nowrap; }}
@media (max-width: 600px) {{ .key-data-grid {{ grid-template-columns: 1fr; }} }}

.perspective {{ margin: 48px 0; padding: 32px; background: var(--white); border: 1px solid var(--light-linen); border-radius: 4px; position: relative; }}
.perspective::before {{ content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 3px; background: linear-gradient(90deg, var(--terracotta), var(--pacific-blue)); border-radius: 4px 4px 0 0; }}
.perspective-label {{ font-size: 10px; font-weight: 600; letter-spacing: 0.2em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 12px; }}
.perspective-text {{ font-family: var(--serif); font-size: 19px; font-style: italic; line-height: 1.65; color: var(--espresso); }}

.article-footer {{ background: var(--cream); padding: 0 var(--side-pad) 60px; }}
.article-footer-inner {{ max-width: 720px; margin: 0 auto; }}
.sources-section {{ padding: 32px 0; border-top: 1px solid var(--light-linen); }}
.sources-title {{ font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); margin-bottom: 16px; }}
.sources-list {{ list-style: none; padding: 0; }}
.sources-list li {{ font-size: 13px; line-height: 1.6; color: var(--charcoal); opacity: 0.7; margin-bottom: 8px; padding-left: 16px; position: relative; }}
.sources-list li::before {{ content: ''; position: absolute; left: 0; top: 9px; width: 4px; height: 4px; border-radius: 50%; background: var(--stone-grey); }}
.sources-list a {{ color: var(--pacific-blue); text-decoration: underline; text-underline-offset: 2px; text-decoration-color: rgba(107,143,158,0.3); transition: text-decoration-color 0.3s; }}
.sources-list a:hover {{ text-decoration-color: var(--pacific-blue); }}
.back-link {{ display: inline-flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: var(--pacific-blue); padding: 24px 0; transition: gap 0.3s; }}
.back-link:hover {{ gap: 12px; }}

.faq-section {{ padding: 32px 0; border-top: 1px solid var(--light-linen); }}
.faq-title {{ font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); margin-bottom: 20px; }}
.faq-item {{ border-bottom: 1px solid var(--light-linen); padding: 16px 0; }}
.faq-item:last-child {{ border-bottom: none; }}
.faq-item summary {{ list-style: none; cursor: pointer; font-family: var(--serif); font-size: 19px; font-weight: 500; color: var(--espresso); line-height: 1.35; padding-right: 30px; position: relative; }}
.faq-item summary::-webkit-details-marker {{ display: none; }}
.faq-item summary::after {{ content: '+'; position: absolute; right: 0; top: 0; font-size: 22px; font-weight: 300; color: var(--pacific-blue); transition: transform 0.3s; }}
.faq-item[open] summary::after {{ transform: rotate(45deg); }}
.faq-a {{ font-size: 15px; line-height: 1.65; color: var(--charcoal); opacity: 0.85; margin-top: 12px; }}

.related-section {{ padding: 32px 0; border-top: 1px solid var(--light-linen); }}
.related-title {{ font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); margin-bottom: 20px; }}
.related-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }}
.related-card {{ display: block; padding: 20px; background: var(--white); border: 1px solid var(--light-linen); border-radius: 4px; transition: border-color 0.3s, transform 0.3s; color: inherit; }}
.related-card:hover {{ border-color: var(--pacific-blue); transform: translateY(-2px); }}
.related-card-tag {{ display: inline-block; font-family: var(--sans); font-size: 9px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 10px; }}
.related-card-title {{ font-family: var(--serif); font-size: 17px; line-height: 1.35; color: var(--espresso); font-weight: 500; }}
@media (max-width: 720px) {{ .related-grid {{ grid-template-columns: 1fr; gap: 12px; }} .related-card {{ padding: 16px; }} }}

.journal-cta {{ background: var(--espresso); padding: var(--section-pad) var(--side-pad); text-align: center; }}
.journal-cta-inner {{ max-width: 620px; margin: 0 auto; }}
.journal-cta-label {{ font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.25em; text-transform: uppercase; color: var(--terracotta); margin-bottom: 8px; }}
.journal-cta-divider {{ width: 60px; height: 1px; background: var(--tuscan-gold); margin: 0 auto 28px; }}
.journal-cta-title {{ font-family: var(--serif); font-size: clamp(28px, 4vw, 40px); font-weight: 400; line-height: 1.2; color: var(--white); margin-bottom: 20px; }}
.journal-cta-title em {{ font-style: italic; font-weight: 300; }}
.journal-cta-text {{ font-size: clamp(15px, 1.5vw, 17px); line-height: 1.8; color: var(--white); opacity: 0.6; margin-bottom: 40px; }}
.journal-cta-buttons {{ display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }}
.cta-btn {{ display: inline-block; font-family: var(--sans); font-size: 11px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; padding: 14px 32px; border-radius: 2px; transition: all 0.3s; text-decoration: none; }}
.cta-btn-primary {{ background: var(--terracotta); color: var(--white); border: 1px solid var(--terracotta); }}
.cta-btn-primary:hover {{ background: var(--pacific-blue); border-color: var(--pacific-blue); }}
.cta-btn-secondary {{ background: transparent; color: var(--white); border: 1px solid rgba(255,255,255,0.25); }}
.cta-btn-secondary:hover {{ border-color: var(--white); }}
.journal-cta-studio {{ max-width: 480px; margin: 0 auto 28px; font-family: var(--sans); font-size: 12px; line-height: 1.55; color: rgba(255,255,255,0.45); letter-spacing: 0.02em; }}
.journal-cta-studio a {{ color: var(--tuscan-gold); border-bottom: 1px solid rgba(212,170,90,0.35); transition: border-bottom-color 0.3s, color 0.3s; }}
.journal-cta-studio a:hover {{ color: var(--white); border-bottom-color: var(--white); }}

.footer {{ background: var(--offblack); padding: 30px var(--side-pad); display: flex; align-items: center; justify-content: space-between; gap: 24px; }}
.footer-brand-wrap {{ display: flex; align-items: baseline; gap: 12px; }}
.footer-brand {{ font-family: var(--serif); font-size: 16px; letter-spacing: 0.2em; color: var(--white); }}
.footer-brand-payoff {{ font-size: 10px; letter-spacing: 0.15em; text-transform: uppercase; color: rgba(255,255,255,0.5); }}
.footer-copy {{ font-size: 12px; color: rgba(255,255,255,0.5); }}
.footer-links {{ display: flex; gap: 24px; }}
.footer-links a {{ font-size: 13px; color: var(--white); opacity: 0.8; transition: color 0.3s; }}
.footer-links a:hover {{ color: var(--pacific-blue); }}
@media (max-width: 600px) {{ .footer {{ flex-direction: column; text-align: center; gap: 16px; }} }}

.share-bar {{ display: flex; align-items: center; gap: 16px; padding: 24px 0; border-top: 1px solid var(--light-linen); margin-top: 48px; }}
.share-bar-label {{ font-size: 10px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--stone-grey); white-space: nowrap; }}
.share-bar-icons {{ display: flex; gap: 8px; }}
.share-btn {{ display: flex; align-items: center; justify-content: center; width: 40px; height: 40px; border-radius: 50%; border: 1px solid var(--light-linen); background: var(--white); cursor: pointer; transition: all 0.3s; text-decoration: none; }}
.share-btn:hover {{ border-color: var(--terracotta); background: var(--terracotta); }}
.share-btn svg {{ width: 16px; height: 16px; fill: var(--charcoal); transition: fill 0.3s; }}
.share-btn:hover svg {{ fill: var(--white); }}
.share-btn-copied {{ position: relative; }}
.share-btn-copied::after {{ content: 'Copiato!'; position: absolute; bottom: -24px; left: 50%; transform: translateX(-50%); font-size: 10px; font-weight: 600; color: var(--terracotta); white-space: nowrap; animation: fadeUp 0.3s ease-out; }}
@media (max-width: 768px) {{
  .share-bar {{ gap: 12px; }}
  .share-btn {{ width: 36px; height: 36px; }}
  .share-btn svg {{ width: 14px; height: 14px; }}
}}

.reveal {{ opacity: 0; transform: translateY(20px); transition: opacity 0.7s ease-out, transform 0.7s ease-out; }}
.reveal.visible {{ opacity: 1; transform: none; }}
@keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(16px); }} to {{ opacity: 1; transform: none; }} }}
@media (max-width: 768px) {{
  .nav {{ padding: 16px var(--side-pad); }}
  .nav-payoff {{ display: none; }}
  .article-hero {{ padding: 90px var(--side-pad) 40px; }}
  .article-hero-breadcrumb {{ margin-bottom: 14px; font-size: 10px; }}
  .article-hero-tag {{ margin-bottom: 12px; }}
  .article-hero-subtitle {{ margin-bottom: 16px; }}
  .article-body {{ padding: 40px var(--side-pad) var(--section-pad); }}
  .article-content p {{ font-size: 16px; line-height: 1.7; margin-bottom: 20px; }}
  .article-content h2 {{ margin-top: 36px; }}
  .pullquote {{ font-size: 18px; padding-left: 20px; margin: 32px 0; }}
  .perspective {{ margin: 32px 0; padding: 24px 20px; }}
  .perspective-text {{ font-size: 17px; }}
  .hero-image-wrap {{ padding: 0 var(--side-pad) 24px; }}
  .hero-image img {{ max-height: 340px; }}
  .source-strip {{ padding: 0 var(--side-pad) 28px; }}
  .source-strip-inner {{ gap: 8px; }}
  .source-chip {{ font-size: 10px; padding: 6px 12px; }}
  .key-data-grid {{ margin: 28px 0; }}
  .key-data-item {{ padding: 20px 14px; font-size: 11px; }}
  .key-data-item > span {{ font-size: 24px; }}
  .key-data {{ padding: 16px 18px; gap: 12px; margin: 20px 0; }}
}}
@media (max-width: 480px) {{
  .article-hero {{ padding-top: 80px; }}
  .article-hero-breadcrumb {{ font-size: 9px; }}
  .nav-logo {{ font-size: 16px; letter-spacing: 0.18em; }}
  .nav-cta {{ padding: 8px 16px; }}
}}
</style>
</head>
<body>

<nav class="nav" id="nav" role="navigation" aria-label="Main navigation">
  <a href="https://myvilla.la" class="nav-logo-wrap">
    <span class="nav-logo">MY VILLA</span>
    <span class="nav-payoff">Italian Soul &middot; Californian Body</span>
  </a>
  <div class="nav-links">
    <a href="https://myvilla.la/#thesis">Vision</a>
    <a href="https://myvilla.la/#collection">Collection</a>
    <a href="https://myvilla.la/#ethical">Approach</a>
    <a href="https://myvilla.la/#resilience">Resilience</a>
    <a href="https://myvilla.la/blog/" class="active">Journal</a>
    <a href="https://myvilla.la/#contact" class="nav-cta">Request Briefing</a>
  </div>
  <div class="nav-burger" id="burger" aria-label="Menu" onclick="document.getElementById('mobileMenu').classList.toggle('open')">
    <span></span><span></span><span></span>
  </div>
</nav>

<div class="mobile-menu" id="mobileMenu">
  <a href="https://myvilla.la/#thesis" onclick="closeMobile()">Vision</a>
  <a href="https://myvilla.la/#collection" onclick="closeMobile()">Collection</a>
  <a href="https://myvilla.la/#ethical" onclick="closeMobile()">Approach</a>
  <a href="https://myvilla.la/#resilience" onclick="closeMobile()">Resilience</a>
  <a href="https://myvilla.la/blog/" onclick="closeMobile()">Journal</a>
  <a href="https://myvilla.la/#contact" onclick="closeMobile()">Request Briefing</a>
</div>

<main>

<section class="article-hero">
  <div class="article-hero-breadcrumb">
    <a href="https://myvilla.la">My Villa</a>
    <span>/</span>
    <a href="https://myvilla.la/blog/">Journal</a>
    <span>/</span>
    {esc(tag_label)}
  </div>
  <div class="article-hero-tag">{esc(tag_label)}</div>
  <h1 class="article-hero-title">{title}</h1>
  <p class="article-hero-subtitle">{esc(subtitle)}</p>
  <div class="article-hero-divider"></div>
  <div class="article-hero-meta">{date_display} &middot; {read_time} min read</div>
</section>

{hero_image_html}

<div class="source-strip">
  <div class="source-strip-inner">
    <span class="source-strip-label">Sources:</span>
    {render_source_chips(sources)}
  </div>
</div>

<section class="article-body">
  <article class="article-content">
    {body_html}

    <div class="perspective">
      <div class="perspective-label">Our Perspective</div>
      <div class="perspective-text">{esc(our_perspective)}</div>
    </div>

    <div class="share-bar">
      <span class="share-bar-label">Condividi</span>
      <div class="share-bar-icons">
        <a class="share-btn" href="https://www.linkedin.com/sharing/share-offsite/?url={share_url_enc}" target="_blank" rel="noopener" title="Condividi su LinkedIn">
          <svg viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
        </a>
        <a class="share-btn" href="https://twitter.com/intent/tweet?url={share_url_enc}&text={share_title_enc}" target="_blank" rel="noopener" title="Condividi su X">
          <svg viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
        </a>
        <a class="share-btn" href="https://www.facebook.com/sharer/sharer.php?u={share_url_enc}" target="_blank" rel="noopener" title="Condividi su Facebook">
          <svg viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
        </a>
        <a class="share-btn" href="https://api.whatsapp.com/send?text={share_title_enc}%20{share_url_enc}" target="_blank" rel="noopener" title="Condividi su WhatsApp">
          <svg viewBox="0 0 24 24"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/></svg>
        </a>
        <button class="share-btn" onclick="copyShareLink(this)" title="Copia link">
          <svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
        </button>
      </div>
    </div>
  </article>
</section>

<div class="article-footer">
  <div class="article-footer-inner">
    <div class="sources-section">
      <div class="sources-title">Sources &amp; References</div>
      <ul class="sources-list">
        {render_sources_list(sources)}
      </ul>
    </div>
    {render_faq_block(faq)}
    {render_related_block(related)}
    <a href="https://myvilla.la/blog/" class="back-link">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M13 8H3M7 4L3 8l4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Back to Journal
    </a>
  </div>
</div>

</main>

<section class="journal-cta">
  <div class="journal-cta-inner">
    <div class="journal-cta-label">Curated by My Villa</div>
    <p class="journal-cta-studio">The Los Angeles practice of IT&#x27;s Architecture (Rome &middot; Paris) &mdash; <a href="https://myvilla.la/malibu-custom-home-builder.html">fire-resilient luxury home builders in Malibu</a>. <a href="https://myvilla.la/team.html">Meet the team &rarr;</a></p>
    <div class="journal-cta-divider"></div>
    <h2 class="journal-cta-title">Building <em>Permanence</em> in<br>Los Angeles</h2>
    <p class="journal-cta-text">We design and coordinate luxury reinforced concrete villas for the Los Angeles market. European engineering. Californian lifestyle.</p>
    <div class="journal-cta-buttons">
      <a href="https://myvilla.la" class="cta-btn cta-btn-primary">Explore My Villa</a>
      <a href="https://myvilla.la/#contact" class="cta-btn cta-btn-secondary">Request a Briefing</a>
    </div>
  </div>
</section>

<footer class="footer">
  <div class="footer-brand-wrap">
    <span class="footer-brand">MY VILLA</span>
    <span class="footer-brand-payoff">Italian Soul &middot; Californian Body</span>
  </div>
  <span class="footer-copy">&copy; 2026 My Villa. All rights reserved.</span>
  <div class="footer-links">
    <a href="https://myvilla.la/team.html">Studio</a>
    <a href="https://myvilla.la/privacy.html">Privacy</a>
  </div>
</footer>

<script>
// Scroll nav
window.addEventListener('scroll', () => {{
  document.getElementById('nav').classList.toggle('scrolled', window.scrollY > 40);
}});
// Reveal on scroll
const obs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{ if (e.isIntersecting) {{ e.target.classList.add('visible'); obs.unobserve(e.target); }} }});
}}, {{ threshold: 0.1 }});
// Mobile menu
function closeMobile() {{ document.getElementById('mobileMenu').classList.remove('open'); }}
// Copy share link
function copyShareLink(btn) {{
  const url = '{canonical}';
  navigator.clipboard.writeText(url).then(() => {{
    btn.classList.add('share-btn-copied');
    setTimeout(() => btn.classList.remove('share-btn-copied'), 2000);
  }});
}}
document.querySelectorAll('.reveal').forEach(el => obs.observe(el));
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
# LEDGER UPDATE
# ══════════════════════════════════════════════════════════════════════

def update_ledger(ledger, article, date_str):
    """Update the journal ledger after generating an article."""
    slug = article.get("slug", "")

    # Add to published. Title + our_perspective are stored so future runs
    # can inject them as "prior coverage" memory into the generation prompt.
    ledger.setdefault("published_articles", []).append({
        "slug": slug,
        "title": article.get("title", ""),
        "published_date": date_str,
        "section": article.get("section", ""),
        "topic_tags": article.get("topic_tags", []),
        "data_points_cited": article.get("data_points_cited", []),
        "sources_cited": [s.get("name", "") for s in article.get("sources", [])],
        "our_perspective": article.get("our_perspective", ""),
    })

    # Update cooldowns
    for tag in article.get("topic_tags", []):
        ledger.setdefault("topic_cooldowns", {})[tag] = date_str

    for dp in article.get("data_points_cited", []):
        ledger.setdefault("data_point_cooldowns", {})[dp] = date_str

    for s in article.get("sources", []):
        name = s.get("name", "")
        if name:
            ledger.setdefault("source_cooldowns", {})[name] = date_str

    section = article.get("section", "")
    if section:
        ledger.setdefault("section_cooldowns", {})[section] = date_str

    return ledger


# ══════════════════════════════════════════════════════════════════════
# AUTO-IMAGE FETCHING
# ══════════════════════════════════════════════════════════════════════

_BRAND_FALLBACK_HERO = SYSTEM_DIR.parent / "img" / "hero.png"


def _copy_brand_fallback_hero(slug, img_dir):
    """Copy img/hero.png into blog/assets/img/<slug>-hero.png when both
    source-image scraping and Unsplash failed. Returns the same shape
    fetch_hero_image returns so the caller treats it uniformly.

    This is the third tier of fallback. Some articles cite only sources
    that block scraping (X tweets, codes.iccsafe.org behind CloudFront)
    AND have a topic too generic to match cleanly on Unsplash. Rather
    than ship those articles without a hero (and trigger the digest
    placeholder block), we use the brand wordmark image as a stable
    visual anchor — same idea behind the digest's gradient placeholder
    but applied site-wide so the article page itself looks polished.
    """
    if not _BRAND_FALLBACK_HERO.exists():
        return None
    try:
        import shutil as _shutil
        dest = Path(img_dir) / f"{slug}-hero.png"
        _shutil.copy(_BRAND_FALLBACK_HERO, dest)
        # web_path is relative to the article (in blog/), so the same
        # 'assets/img/<slug>-hero.png' the other paths return.
        return {
            "web_path": f"assets/img/{slug}-hero.png",
            "source": "brand_fallback",
            "origin": "brand",  # explicit so render_article_html doesn't
                                # have to derive it from `source`
            "credit": "My Villa",
        }
    except Exception as e:  # noqa: BLE001
        print(f"  [Image] brand-fallback copy failed: {e}")
        return None


def auto_fetch_hero_image(article, slug, img_dir):
    """Try to automatically fetch a hero image for the article.
    1. First try source images from cited articles.
    2. Fallback to Unsplash search.
    3. Final fallback: copy the brand hero image from img/hero.png so
       no article ships without a visual anchor.
    Returns hero_image dict or None (only if even the brand asset is
    missing — extremely unlikely).
    """
    if not IMAGE_PICKER_OK:
        print("  [Image] image_picker not available — skipping")
        return None

    img_dir = Path(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)

    # 1. Try source images from cited articles
    sources = article.get("sources", [])
    if sources:
        source_list = [
            {"url": s.get("url", ""), "publication": s.get("name", "")}
            for s in sources if s.get("url")
        ]
        if source_list:
            print(f"  [Image] Searching {len(source_list)} cited sources...")
            try:
                candidates = fetch_source_images(source_list, max_sources=4, per_source=6)
                if candidates:
                    print(f"  [Image] Found {len(candidates)} source candidates, downloading best...")
                    result = download_source_image(candidates[0], slug, img_dir)
                    if result:
                        print(f"  [Image] ✓ Source image: {result['web_path']}")
                        return result
            except Exception as e:
                print(f"  [Image] Source fetch error: {e}")

    # 2. Fallback: Unsplash
    title = article.get("title", "")
    image_prompt = article.get("image_prompt", "")
    query = image_prompt or title
    if query:
        print(f"  [Image] Trying Unsplash: '{query[:50]}...'")
        try:
            result = fetch_hero_image(
                query=query,
                slug=slug,
                out_dir=img_dir,
                orientation="landscape",
                fallback_queries=[title] if image_prompt else None,
            )
            if result:
                print(f"  [Image] ✓ Unsplash image: {result['web_path']}")
                return result
        except Exception as e:
            print(f"  [Image] Unsplash error: {e}")

    # 3. Final fallback: copy the brand hero image so every article
    # has at least a visual anchor (the My Villa wordmark image).
    print("  [Image] Source + Unsplash failed — using brand fallback")
    result = _copy_brand_fallback_hero(slug, img_dir)
    if result:
        print(f"  [Image] ✓ Brand fallback: {result['web_path']}")
        return result

    print("  [Image] No image found (brand fallback also unavailable)")
    return None


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Journal Article Generator")
    parser.add_argument("--radar", "-r", required=True,
                        help="Path to radar JSON output")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory for articles (default: blog/)")
    parser.add_argument("--config", default=None,
                        help="Path to config directory")
    parser.add_argument("--knowledge", default=None,
                        help="Path to knowledge directory")
    parser.add_argument("--model", default=_WRITER_MODEL,
                        help="Claude model for generation")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Override min score from config")
    parser.add_argument("--max-articles", type=int, default=2,
                        help="Max articles per run (default: 2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate but don't save")
    parser.add_argument("--ledger", default=None,
                        help="Path to journal_ledger.json")
    parser.add_argument("--ignore-cadence", action="store_true",
                        help="Genera anche fuori dai publish_weekdays (test/forzatura)")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\nMy Villa — Journal Generator")
    print(f"{'='*50}")

    # Load configs
    journal_config = load_journal_config()

    # ── Cadenza (pivot SEO 2026-06): il journal gira solo nei publish_weekdays
    # (default lun/mer/ven). Un dominio nuovo non deve sfornare news ogni
    # giorno (segnale "scaled content"): 2-3 evergreen/settimana. --ignore-cadence
    # forza (test). Il radar/social restano comunque quotidiani.
    pub_days = (journal_config.get("journal_criteria", {}) or {}).get("publish_weekdays")
    if pub_days and not args.ignore_cadence:
        wd = datetime.now().weekday()
        if wd not in pub_days:
            names = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
            print(f"  [cadence] Oggi è {names[wd]}: non è un giorno di "
                  f"pubblicazione journal (publish_weekdays={pub_days}). "
                  f"Skip — ora 2-3 evergreen/settimana. (--ignore-cadence per forzare)")
            return

    brand_voice = load_yaml(CONFIG_DIR / "brand-voice.yml")

    # Load 'Our Perspective' knowledge base (project PDF + site content).
    # CLI --knowledge overrides the default KNOWLEDGE_DIR.
    knowledge_dir = Path(args.knowledge) if args.knowledge else KNOWLEDGE_DIR
    perspective_kb = load_perspective_kb(knowledge_dir)
    if perspective_kb:
        print(f"  [KB] Loaded perspective KB ({len(perspective_kb)} chars) from {knowledge_dir}")
    else:
        print(f"  [KB] No perspective KB loaded — 'Our Perspective' will run ungrounded")

    # Load canonical content strategy (audience, geography, SEO priorities).
    content_strategy = load_content_strategy(knowledge_dir)
    if content_strategy:
        print(f"  [Strategy] Loaded content strategy ({len(content_strategy)} chars)")
    else:
        print(f"  [Strategy] No content_strategy.md found — titles will not have SEO steering")

    # Load radar data
    radar_data = load_json(Path(args.radar))
    date_str = radar_data.get("date", today)
    print(f"Date: {date_str} | Model: {args.model}")

    # Load ledger
    ledger_path = Path(args.ledger) if args.ledger else (HISTORY_DIR / "journal_ledger.json")
    ledger = load_json(ledger_path) if ledger_path.exists() else {
        "published_articles": [], "topic_cooldowns": {},
        "data_point_cooldowns": {}, "source_cooldowns": {},
        "section_cooldowns": {},
    }

    # Filter candidates
    print("\nFiltering candidates...")
    candidates = filter_candidates(radar_data, ledger, journal_config,
                                    max_articles=args.max_articles,
                                    min_score_override=args.min_score)

    if not candidates:
        print("\nNo candidates passed filtering. Done.")
        return

    # Get blocked items for dedup
    blocked = is_in_cooldown(ledger, date_str)

    # Pull the last 8 published articles (most recent first) so the generator
    # can see what framings / credentials / closings it has already used and
    # actively vary them. Only entries that include `our_perspective` are
    # useful here (older ledger entries lacked it).
    prior_articles = list(reversed(ledger.get("published_articles", [])))[:8]
    prior_articles = [pa for pa in prior_articles if pa.get("our_perspective")]

    # Output directory
    # Default: _drafts/journal/ (review before publish)
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = SYSTEM_DIR.parent / "_drafts" / "journal"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate articles
    generated = []
    for i, item in enumerate(candidates):
        cluster = item.get("cluster", "")
        section_id = cluster_to_section(cluster, journal_config)
        section_name, _ = section_info(section_id, journal_config)

        print(f"\nGenerating article {i+1}/{len(candidates)}: {item.get('title', '')[:50]}...")
        print(f"  Cluster: {cluster} → Section: {section_name}")

        article = generate_article(
            item, section_id, section_name, brand_voice, blocked,
            model=args.model, perspective_kb=perspective_kb,
            prior_articles=prior_articles,
            content_strategy=content_strategy)

        if not article:
            print("  Failed — skipping")
            continue

        # Render HTML
        html_content = render_article_html(article, date_str)
        slug = article.get("slug", f"article-{i}")
        filename = f"{slug}.html"

        if not args.dry_run:
            # Auto-fetch hero image
            img_dir = SYSTEM_DIR.parent / "blog" / "assets" / "img"
            hero = auto_fetch_hero_image(article, slug, img_dir)
            if hero:
                article["hero_image"] = hero
                # Re-render HTML with the image
                html_content = render_article_html(article, date_str)

            filepath = output_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html_content)

            # Final step: validate every external link. --fix strips broken
            # anchors (preserving inner text) and removes container chips for
            # dead URLs so the published article never ships with 404s.
            if VALIDATE_LINKS_OK:
                try:
                    report = validate_links_file(filepath, fix=True)
                    broken = report.get("broken", []) or []
                    total = report.get("total", 0)
                    orphans = report.get("orphans_cleaned", 0)
                    if broken:
                        print(f"  Links: {total - len(broken)}/{total} OK — "
                              f"auto-stripped {len(broken)} broken:")
                        for b in broken[:8]:
                            print(f"    ✗ {b.get('status') or '---'}  {b.get('url')}")
                        if len(broken) > 8:
                            print(f"    ... (+{len(broken) - 8} more)")
                    else:
                        print(f"  Links: {total}/{total} OK")
                    if orphans:
                        print(f"  Cleaned {orphans} orphan source-chip(s)")
                except Exception as e:
                    print(f"  [WARN] link validation failed: {e}")
            else:
                print(f"  [WARN] validate_links not available — links not verified")

            # Also save structured JSON (for edit/revise workflow)
            json_filepath = output_dir / f"{slug}.json"
            article_with_meta = dict(article)
            article_with_meta["_date"] = date_str
            article_with_meta["_section_id"] = section_id
            article_with_meta["_section_name"] = section_name
            article_with_meta["_source_item"] = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "publication": item.get("publication", ""),
                "cluster": item.get("cluster", ""),
            }
            with open(json_filepath, "w", encoding="utf-8") as f:
                json.dump(article_with_meta, f, indent=2, ensure_ascii=False)

            print(f"  Saved: {filepath}")
            print(f"  Size: {filepath.stat().st_size / 1024:.1f} KB")
            print(f"  JSON:  {json_filepath.name}")

            # Update ledger
            ledger = update_ledger(ledger, article, date_str)
        else:
            print(f"  [Dry run] Would save: {filename}")
            print(f"  Title: {article.get('title', '')}")
            print(f"  Excerpt: {article.get('excerpt', '')[:100]}...")

        generated.append(article)

    # Save ledger
    if not args.dry_run and generated:
        save_json(ledger_path, ledger)
        print(f"\nLedger updated: {ledger_path}")

    print(f"\n{'='*50}")
    print(f"Generated {len(generated)} article(s)")


if __name__ == "__main__":
    main()
