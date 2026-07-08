#!/usr/bin/env python3
"""
My Villa — Instagram Editorial Generator

Takes a planned slot from a monthly calendar (produced by editorial_planner.py)
and generates a brand-voice-compliant Instagram caption + hashtag set + image
suggestion. Output is a .md draft compatible with approve.py's draft format,
written to _drafts/social_editorial/.

The slot's status in the calendar YAML is updated from "planned" to "draft"
once a draft is successfully generated. Re-running on the same slot will
regenerate (overwriting the draft) only if --force is passed.

Usage:
  python3 editorial_generator.py --month 2026-05                    # generate next 14 days of slots
  python3 editorial_generator.py --month 2026-05 --slot 2026-05-04  # one specific slot
  python3 editorial_generator.py --month 2026-05 --limit 3          # cap at first N planned slots
  python3 editorial_generator.py --month 2026-05 --dry-run          # preview, don't write
  python3 editorial_generator.py --month 2026-05 --force            # regenerate even if draft exists
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# Sibling script for partner-echo posts (Apify-backed scrape cache)
SCRIPT_DIR_FOR_IMPORT = Path(__file__).resolve().parent
if str(SCRIPT_DIR_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR_FOR_IMPORT))
try:
    import partner_scraper
    PARTNER_SCRAPER_OK = True
except ImportError:
    PARTNER_SCRAPER_OK = False

try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

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

SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
CONFIG_DIR = SYSTEM_DIR / "config"
KNOWLEDGE_DIR = SYSTEM_DIR / "knowledge"
SOCIAL_DIR = SYSTEM_DIR / "social"
CALENDAR_DIR = SOCIAL_DIR / "calendar"
DRAFTS_DIR = ROOT_DIR / "_drafts" / "social_editorial"
IMG_DIR = ROOT_DIR / "img"

CONFIG_FILE = CONFIG_DIR / "editorial-calendar.yml"
BRAND_VOICE_FILE = CONFIG_DIR / "brand-voice.yml"
PROJECT_BRIEF = KNOWLEDGE_DIR / "project_brief.md"

DEFAULT_MODEL = _resolve_model("balanced")


# ══════════════════════════════════════════════════════════════════════
# .env loader (same pattern as siblings)
# ══════════════════════════════════════════════════════════════════════

def load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v


load_dotenv()


# ══════════════════════════════════════════════════════════════════════
# Asset picking — explicit map per (pillar, sub_topic) + anti-repeat
# ══════════════════════════════════════════════════════════════════════

# For each (pillar, sub_topic) we list candidate images in PREFERRED order.
# The picker walks the list and picks the first one that:
#   (a) exists in /img/
#   (b) hasn't been used in the last EDITORIAL_IMAGE_REPEAT_COOLDOWN_DAYS
# If all candidates are in cooldown, fall back to the LAST candidate
# (we'd rather repeat a thematically-correct image than pick a wrong one).

EDITORIAL_IMAGE_REPEAT_COOLDOWN_DAYS = 30

# Editorial-grade asset whitelist. ONLY real photos / high-res renders.
# Schematic / infographic files (resilience-*, biophilic-*, material-permanence,
# tectonics, structural, structural-diagram) are blacklisted — they sell
# concept, not architecture, and read as sales material in the IG feed.

# Vision sub_topics — historical / cultural references first.
VISION_IMAGES = {
    "Two millennia of villa typology — adaptability + permanence":
        ["amanvari-01.webp", "amanvari-02.webp", "palazzo-grassi.webp", "courtyard-interior.webp"],
    "Why LA needs villas, not lightweight construction":
        ["kimbell.webp", "punta-dogana.webp", "palazzo-grassi.webp", "amanvari-01.webp"],
    "Italian Soul, Californian Body — what it really means":
        ["amanvari-02.webp", "courtyard-interior.webp", "amanvari-01.webp", "palazzo-grassi.webp"],
    "Resilience as continuity, not as fear":
        ["punta-dogana.webp", "kimbell.webp", "amanvari-01.webp", "courtyard-interior.webp"],
    "The villa as a cultural artefact":
        ["palazzo-grassi.webp", "punta-dogana.webp", "amanvari-01.webp", "kimbell.webp"],
    "Compact spatial planning + thermal mass + elevated foundations":
        ["amanvari-02.webp", "courtyard-interior.webp", "kimbell.webp", "punta-dogana.webp"],
    "Historical references in dialogue with LA modernism":
        ["courtyard-interior.webp", "amanvari-02.webp", "amanvari-01.webp", "palazzo-grassi.webp"],
    "Permanence vs renewal — why concrete outlasts trends":
        ["kimbell.webp", "palazzo-grassi.webp", "punta-dogana.webp", "amanvari-01.webp"],
}

# System sub_topics — partner cache cross-pollination first when topic is
# partner-specific; real architectural photos otherwise.
SYSTEM_IMAGES = {
    "On-site mobile concrete plants — why we produce locally":
        ["__PARTNER:dgu_baja__", "kimbell.webp", "punta-dogana.webp"],
    "Concrete tones — the moodboard":
        # Mood swatches are intentional (this topic IS about tone palette);
        # paired with concrete masterworks for editorial weight.
        ["mood-cream.webp", "mood-sand.webp", "mood-terracotta.webp",
         "mood-sage.webp", "mood-rose.webp", "kimbell.webp", "punta-dogana.webp"],
    "Modularity without monotony":
        ["col-fluted.webp", "col-rounded.webp", "col-square.webp",
         "amanvari-02.webp", "kimbell.webp"],
    "Assembly as design decision":
        ["__PARTNER:dgu_baja__", "kimbell.webp", "punta-dogana.webp"],
    "Personalisation within a system":
        ["int-bespoke-layout.webp", "int-walnut-wood.webp", "int-designer-chair.webp",
         "int-stone-basin.webp", "int-luxury-living.webp"],
    "DGU lineage — Kimbell Art Museum (Renzo Piano)":
        ["kimbell.webp", "__PARTNER:dgu_baja__", "palazzo-grassi.webp"],
    "DGU lineage — Palazzo Grassi & Punta della Dogana (Pinault Collection)":
        ["palazzo-grassi.webp", "punta-dogana.webp", "__PARTNER:dgu_baja__"],
    "Transsolar climate engineering — High Comfort Low Impact":
        ["__PARTNER:transsolar_klimaengineering__", "amanvari-02.webp",
         "courtyard-interior.webp", "kimbell.webp"],
    "BUROMILAN — engineering architecture":
        ["__PARTNER:buromilan__", "kimbell.webp", "palazzo-grassi.webp"],
    "Why exposed concrete beats cladding":
        ["kimbell.webp", "palazzo-grassi.webp", "punta-dogana.webp",
         "facade-cream.webp", "facade-sage.webp"],
    "Resource recovery and reuse — circular concrete":
        ["__PARTNER:dgu_baja__", "kimbell.webp", "punta-dogana.webp"],
    "Durable material for lasting constructions":
        ["kimbell.webp", "punta-dogana.webp", "palazzo-grassi.webp", "amanvari-01.webp"],
}

# Archetype carousel cover (slide 1). Use only photo-grade assets for the
# archetypes that have them; for podium/portico/pergola/fence we fall back
# to architectural references rather than diagrams.
ARCHETYPE_IMAGES = {
    "Courtyard": ["courtyard.webp", "courtyard-interior.webp", "courtyard-interior.jpg"],
    "Podium":    ["amanvari-02.webp", "amanvari-01.webp", "kimbell.webp"],
    "Portico":   ["amanvari-01.webp", "col-rounded.webp", "col-fluted.webp"],
    "Pergola":   ["amanvari-02.webp", "int-window-detail.webp", "courtyard-interior.webp"],
    "Fireplace": ["int-walnut-wood.webp", "library.webp", "int-warm-workspace.webp"],
    "Window":    ["int-window-detail.webp", "int-reeded-glass.webp", "int-sheer-curtains.webp"],
    "Living Green Roof":
                 ["amanvari-02.webp", "courtyard-interior.webp", "amanvari-01.webp"],
    "Fence":     ["amanvari-01.webp", "external.webp", "facade-sage.webp"],
}

# Pillar-level fallbacks — photographic / editorial only, never schematic.
PILLAR_FALLBACK_IMAGES = {
    "vision":   ["amanvari-01.webp", "palazzo-grassi.webp", "courtyard-interior.webp",
                 "kimbell.webp", "punta-dogana.webp"],
    "system":   ["kimbell.webp", "punta-dogana.webp", "palazzo-grassi.webp",
                 "facade-cream.webp"],
    "archetype": ["amanvari-02.webp", "courtyard-interior.webp", "external.webp"],
}

# Files to NEVER use in editorial — schematics / infographics / diagrams.
# Listed for documentation; the picker simply doesn't reference them.
EDITORIAL_BLACKLIST = {
    "structural-diagram.svg",
    "structural.webp",
    "tectonics.webp",
    "resilient.webp",
    "resilience-climate.webp",
    "resilience-energy.webp",
    "biophilic.webp",
    "biophilic-design.webp",
    "biophilic-design.png",
    "material-permanence.webp",
    "material-permanence.png",
    "hero.webp",   # too generic — reserve for last-resort fallback only
}


# ── Partner cache resolver ──────────────────────────────────────────
# When a candidate is "__PARTNER:<handle>__", expand it into the freshest
# usable post's local thumbnail from that partner's Apify cache.

def _resolve_partner_token(token: str, used_shortcodes: set) -> Optional[dict]:
    """Return a {'filename', 'web_path', 'abs_path'} dict for the freshest
    usable post in the partner's cache. None if cache empty / nothing fresh."""
    if not token.startswith("__PARTNER:") or not token.endswith("__"):
        return None
    handle = token[len("__PARTNER:"):-len("__")]
    if not PARTNER_SCRAPER_OK:
        return None
    post = partner_scraper.pick_post_for_slot(
        handle, used_shortcodes=used_shortcodes, max_age_days=180,
    )
    if not post or not post.get("local_thumbnail"):
        return None
    rel = post["local_thumbnail"]
    abs_path = SYSTEM_DIR.parent / rel
    if not abs_path.exists():
        return None
    return {
        "filename": Path(rel).name,
        "web_path": "/" + rel,
        "abs_path": str(abs_path),
        "_partner_handle": handle,
        "_partner_shortcode": post.get("shortcode", ""),
        "_partner_url": post.get("url", ""),
    }


from typing import Optional  # noqa: E402  — placed here to keep edit minimal


def _load_image_ledger():
    """Load the per-image last-used ledger. Stored alongside other history."""
    ledger_path = SYSTEM_DIR / "history" / "editorial_image_ledger.json"
    if not ledger_path.exists():
        return {}, ledger_path
    try:
        return json.loads(ledger_path.read_text()), ledger_path
    except json.JSONDecodeError:
        return {}, ledger_path


def _save_image_ledger(ledger, ledger_path):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True))


def _filename_in_cooldown(filename: str, ledger: dict, today: date) -> bool:
    last = ledger.get(filename)
    if not last:
        return False
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (today - last_dt).days < EDITORIAL_IMAGE_REPEAT_COOLDOWN_DAYS


def _candidates_for_slot(slot: dict) -> list:
    """Return the explicit candidate-image list for this slot, in preferred
    order. Entries can be:
      - A filename in /img/   (e.g. "kimbell.webp")
      - A partner token       (e.g. "__PARTNER:dgu_baja__")
    Falls back to PILLAR_FALLBACK_IMAGES if no exact map entry."""
    pillar = slot.get("pillar", "")
    sub_topic = slot.get("sub_topic", "")

    if pillar == "vision":
        return VISION_IMAGES.get(sub_topic, []) + PILLAR_FALLBACK_IMAGES["vision"]
    if pillar == "system":
        return SYSTEM_IMAGES.get(sub_topic, []) + PILLAR_FALLBACK_IMAGES["system"]
    if pillar == "archetype":
        return ARCHETYPE_IMAGES.get(sub_topic, []) + PILLAR_FALLBACK_IMAGES["archetype"]
    # partner_echo: image comes from the scraper, not from /img/
    return []


def pick_image(slot, *, register_use: bool = True, used_partner_shortcodes=None):
    """Pick the highest-priority image that:
    - is in IMG_DIR (or in partner cache if a __PARTNER:…__ token wins)
    - is not in 30-day cooldown
    Records the use in the ledger so consecutive runs vary the image.

    Returns: {filename, web_path, abs_path[, _partner_*]} or None.
    """
    ledger, ledger_path = _load_image_ledger()
    today = date.today()
    used = used_partner_shortcodes or set()

    candidates = _candidates_for_slot(slot)

    # First pass: walk candidates in priority order; skip in-cooldown.
    # Partner-cache images skip the filename cooldown — dedup happens at
    # shortcode level via _used_partner_shortcodes (across all drafts).
    chosen = None
    for token in candidates:
        if token.startswith("__PARTNER:"):
            partner_img = _resolve_partner_token(token, used)
            if partner_img is None:
                continue
            chosen = partner_img
            break
        # Local /img/ candidate
        if not IMG_DIR.exists():
            continue
        path = IMG_DIR / token
        if not path.exists():
            continue
        if _filename_in_cooldown(token, ledger, today):
            continue
        chosen = {
            "filename": path.name,
            "web_path": f"/img/{path.name}",
            "abs_path": str(path),
        }
        break

    # Second pass: ignore cooldown but still respect existence.
    if chosen is None:
        for token in candidates:
            if token.startswith("__PARTNER:"):
                partner_img = _resolve_partner_token(token, used)
                if partner_img is not None:
                    chosen = partner_img
                    break
            else:
                path = IMG_DIR / token
                if path.exists():
                    chosen = {
                        "filename": path.name,
                        "web_path": f"/img/{path.name}",
                        "abs_path": str(path),
                    }
                    break

    if chosen is None:
        hero = IMG_DIR / "hero.webp"
        if hero.exists():
            chosen = {
                "filename": "hero.webp",
                "web_path": "/img/hero.webp",
                "abs_path": str(hero),
            }
        else:
            return None

    if register_use:
        ledger[chosen["filename"]] = today.strftime("%Y-%m-%d")
        _save_image_ledger(ledger, ledger_path)

    return chosen


# ══════════════════════════════════════════════════════════════════════
# Vision / System framing angles — variety injection
# ══════════════════════════════════════════════════════════════════════
# When the planner doesn't pre-assign a framing_angle, the generator
# picks one here, biased by what was used recently (see ledger).

VISION_FRAMING_ANGLES = [
    {
        "key": "manifesto",
        "instruction": (
            "Lead with a bold declarative statement — a thesis-sized observation. "
            "Two short sentences max. Authoritative tone, no hedging."
        ),
    },
    {
        "key": "juxtaposition",
        "instruction": (
            "Open by contrasting two things directly: two cities, two eras, two "
            "construction logics, two attitudes toward landscape. Make the contrast "
            "the engine of the post."
        ),
    },
    {
        "key": "anecdote",
        "instruction": (
            "Open with a single concrete historical detail — Hadrian planning his "
            "villa, Palladio's cousin commissioning La Rotonda, Scarpa drawing a "
            "fence at Querini Stampalia — then pivot to what it means today."
        ),
    },
    {
        "key": "observation",
        "instruction": (
            "Start with a small architectural observation — a roof angle, a wall "
            "thickness, a courtyard proportion — and let it open into a larger idea. "
            "Avoid generality in the opening line."
        ),
    },
    {
        "key": "question",
        "instruction": (
            "Open with a sharp, non-rhetorical question. Then answer it precisely "
            "in the next sentence. The question must be specific to architecture, "
            "not 'what does luxury mean'."
        ),
    },
    {
        "key": "data",
        "instruction": (
            "Lead with a specific number, year, ratio, or measurement (Villa Adriana "
            "is 120 hectares, La Rotonda was begun in 1566, thermal mass shifts peak "
            "load by 6 hours, etc). The number must be true and load-bearing for the "
            "argument."
        ),
    },
]

SYSTEM_FRAMING_ANGLES = [
    {
        "key": "process",
        "instruction": (
            "Describe how something is actually made or assembled, in terms a "
            "builder would recognise. Concrete, present-tense verbs. No abstractions."
        ),
    },
    {
        "key": "comparison",
        "instruction": (
            "Compare reinforced concrete (or whatever the sub-topic is) against the "
            "common alternative — wood frame, light steel, EIFS cladding. Be specific "
            "about what changes."
        ),
    },
    {
        "key": "anatomy",
        "instruction": (
            "Break the topic into 2–3 named components or steps. Use them as the "
            "spine of the caption. Architectural / engineering vocabulary welcomed."
        ),
    },
    {
        "key": "lineage",
        "instruction": (
            "Anchor the topic in a specific built precedent (Kimbell, Palazzo "
            "Grassi, Punta della Dogana, Aman Venice) and explain what My Villa "
            "inherits from that lineage."
        ),
    },
    {
        "key": "principle",
        "instruction": (
            "State a single engineering or material principle, then show one "
            "consequence the homeowner experiences (light, sound, temperature, "
            "longevity). One principle, one consequence."
        ),
    },
]


def pick_framing_angle(pillar: str, recent_keys: list) -> dict:
    """Pick a framing angle that hasn't been used in the last 4 posts of this pillar."""
    pool = VISION_FRAMING_ANGLES if pillar == "vision" else SYSTEM_FRAMING_ANGLES
    if not pool:
        return {"key": "default", "instruction": ""}
    avoid = set(recent_keys[-4:])
    available = [a for a in pool if a["key"] not in avoid]
    if not available:
        available = pool
    return random.choice(available)


# ══════════════════════════════════════════════════════════════════════
# Prompt construction
# ══════════════════════════════════════════════════════════════════════

EDITORIAL_SYSTEM_PROMPT = """\
You are the Instagram editorial writer for My Villa — a luxury reinforced \
concrete villa company in Los Angeles (myvilla.la). This is the EDITORIAL \
channel (brand foundation), distinct from reactive news posts.

LANGUAGE: ALL captions MUST be in English. Never Italian or any other language.

VOICE:
- Visual-first, aspirational but grounded. Editorial register, not salesy.
- Value before product: lead with an architectural idea or insight, never \
  with "come see our villas".
- Italian Soul, Californian Body — the underlying tagline.
- Italian masters and historical references are credentials, not name-drops.

ABSOLUTE RULES:
- My Villa has NOT built any homes yet. Never imply a delivered project.
- Paolo Mezzalama (Founder) is the public voice — only name him if directly \
  attributing a specific quote. Do not call out Ivo Giuliani.
- IT'S Architecture only when relevant; default omit.
- DGU, Transsolar, BUROMILAN are partner credentials when relevant.

FORBIDDEN TERMS (never use):
bunker, fortress, fortezza, fireproof, dream home, anti-fire, "protect your \
family", "survive the next fire", luxurylifestyle, dreamhome.

FORBIDDEN PHRASES (boilerplate to avoid):
"material, not accessories, compounds over decades", "not what style but \
what system", "come visit our villas", "discover our portfolio".

STRUCTURE (single-image post):
- Total caption ≤ 250 chars before the "...more" fold.
- First 125 chars = hook (architectural idea, not a label).
- One core idea per post. No bullet lists.
- End with 5-6 hashtags on a single line.

STRUCTURE (carousel post — when format == "carousel"):
- The CAPTION is the same single text (≤250 chars) — Instagram doesn't \
  caption per-slide.
- Provide slide-by-slide TEXT for the visuals separately, in the JSON output.
- Slide 1 = archetype name + one-line definition.
- Slides 2..N-1 = historical references (master / work / year).
- Slide N = how My Villa interprets the archetype today.

HASHTAG RULES:
- Always include the 3 core: #MyVilla #MyVillaLA #ReinforcedConcrete.
- Add 2-3 rotational tags from {form, place, theme} categories — pick what \
  the post is actually about, not generic luxury tags.
- Total: 5-6. Never use any forbidden term as a hashtag.

CRITICAL OUTPUT RULE:
- The "caption" field MUST contain ONLY the prose caption text. Never \
  include hashtags inside it. Hashtags go ONLY in the separate "hashtags" \
  JSON array; the hashtag line will be appended automatically.
- char_count = number of characters in caption (without hashtags).
"""


# Defensive: strip any hashtag tail from caption if model still includes it.
HASHTAG_TAIL_RE = re.compile(r"\n+\s*(#\w+(?:\s+#\w+)*)\s*$")


def _strip_hashtag_tail(caption: str) -> str:
    """Remove a trailing line of hashtags if present."""
    if not caption:
        return caption
    return HASHTAG_TAIL_RE.sub("", caption.rstrip()).rstrip()


# ── Hashtag allowlist (built from config) ────────────────────────────
# Used to flag obvious typos in model-generated hashtags. We match
# case-insensitively against the union of core + rotational + an extra
# permissive set of partnerships/credentials we know are on-brand.
_EXTRA_ALLOWED_HASHTAGS = {
    # NOTE: never list typos here. The correct token is `ConcreteArchitecture`,
    # which is already in editorial-calendar.yml → hashtags.rotational.form
    # and gets picked up by _build_hashtag_allowlist().  AI-typo variants
    # like "concretarchitecture" must FAIL validation (typo guard), not be
    # silently accepted.
    "concretedesign",
    "italianheritage", "myvillamoodboard",
    "structuralengineering", "engineeringarchitecture",
    "climateengineering", "passivedesign", "thermalmass",
    "renzopiano", "kimbellartmuseum", "palazzograssi", "puntadelladogana",
    "amanvenice", "italianmasters", "italiantypology",
    "concretelineage", "italiansoul", "californianbody",
    "casamalaparte", "villarotonda", "villaadriana",
    "myvillaprojects", "myvillapartner", "myvillapartners",
    "designedinrome", "designedinitaly", "engineeredinitaly",
    "exposedreinforcedconcrete", "concreteformwork",
    "californianarchitecture", "ladesign",
}


def _build_hashtag_allowlist(config: dict) -> set:
    """Lower-cased set of known-good hashtag tokens (no #)."""
    allowed = set()
    h = config.get("hashtags", {}) or {}
    for cat in ("core", "rotational"):
        v = h.get(cat)
        if isinstance(v, list):
            allowed.update(t.lower() for t in v if isinstance(t, str))
        elif isinstance(v, dict):
            for sub in v.values():
                if isinstance(sub, list):
                    allowed.update(t.lower() for t in sub if isinstance(t, str))
    allowed.update(_EXTRA_ALLOWED_HASHTAGS)
    return allowed


# ══════════════════════════════════════════════════════════════════════
# validate_generated_post — hard quality gate before write_draft
# ══════════════════════════════════════════════════════════════════════

def validate_generated_post(post: dict, config: dict) -> dict:
    """Validate a Claude-generated post against editorial constraints.

    Returns: {"ok": bool, "errors": [...], "warnings": [...]}.
    Errors block draft writing; warnings get persisted in the draft frontmatter
    but don't block.
    """
    errors = []
    warnings = []

    if "error" in post:
        errors.append(f"upstream error: {post['error']}")
        return {"ok": False, "errors": errors, "warnings": warnings}

    caption = _strip_hashtag_tail(post.get("caption", "") or "")

    # ── Strip invented @handles (e.g. @eaaorg) before persisting ──
    # The generators were tagging non-existent/wrong source accounts; keep only
    # our own + operator-verified handles, drop the rest. Mutate post so the
    # written draft is clean.
    try:
        from verify_handles import sanitize as _vh_sanitize
        _clean, _removed = _vh_sanitize(caption)
        if _removed:
            caption = _clean
            post["caption"] = caption
            warnings.append("stripped unverified @handles: "
                            + ", ".join("@" + r for r in _removed))
    except Exception:  # noqa: BLE001
        pass

    hashtags = post.get("hashtags") or []

    # ── Caption length ──
    if len(caption) > 250:
        errors.append(f"caption too long: {len(caption)} chars (max 250)")
    if len(caption) < 30:
        errors.append(f"caption too short: {len(caption)} chars (min 30)")

    # ── Hashtag count ──
    h_cfg = config.get("hashtags", {}) or {}
    pp = (h_cfg.get("per_post", {}) or {})
    h_min = int(pp.get("min", 5))
    h_max = int(pp.get("max", 6))
    if not isinstance(hashtags, list):
        errors.append(f"hashtags is not a list: {type(hashtags).__name__}")
        hashtags = []
    if len(hashtags) < h_min:
        errors.append(f"too few hashtags: {len(hashtags)} (min {h_min})")
    if len(hashtags) > h_max:
        # Programmer-review fix: too many hashtags is a hard error, not a
        # warning. The "5-6" target means the generator must not write a
        # draft with 7+ hashtags.
        errors.append(f"too many hashtags: {len(hashtags)} (max {h_max})")

    # Normalize for comparisons
    norm_hashtags = [(h or "").lstrip("#") for h in hashtags]
    norm_lower = [h.lower() for h in norm_hashtags]

    # ── Core hashtags must be present ──
    core = [c.lower() for c in (h_cfg.get("core", []) or [])]
    missing_core = [c for c in core if c not in norm_lower]
    if missing_core:
        errors.append(f"missing core hashtags: {missing_core}")

    # ── Forbidden hashtags ──
    forbidden_h = [c.lower() for c in (h_cfg.get("forbidden", []) or [])]
    bad_h = [h for h in norm_lower if h in forbidden_h]
    if bad_h:
        errors.append(f"forbidden hashtags: {bad_h}")

    # ── Forbidden caption terms (substring, case-insensitive) ──
    forbidden_terms = (config.get("constraints", {}) or {}).get("forbidden_terms", []) or []
    cap_lower = caption.lower()
    bad_terms = [t for t in forbidden_terms if t and t.lower() in cap_lower]
    if bad_terms:
        errors.append(f"forbidden term(s) in caption: {bad_terms}")

    # ── Hashtag typo / off-allowlist check ──
    allowed = _build_hashtag_allowlist(config)
    unknown = [h for h, lo in zip(norm_hashtags, norm_lower)
               if lo and lo not in allowed]
    if unknown:
        warnings.append(
            f"hashtags not in allowlist (possible typos): {unknown}"
        )

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def build_user_prompt(slot, brand_voice, brief_excerpt, framing_angle=None,
                      partner_post=None, recent_phrases=None):
    """Build the user-side prompt for a single slot.

    framing_angle: dict with key+instruction for Vision/System variety
    partner_post:  shaped Apify post dict for partner_echo slots
    recent_phrases: list of opening phrases used in the last few drafts (avoid)
    """
    pillar = slot["pillar"]
    pillar_name = slot.get("pillar_name", pillar)
    fmt = slot.get("format", "single_image")
    target_chars = slot.get("caption_target_chars", 220)
    sub_topic = slot.get("sub_topic", "")

    out = f"""Generate an Instagram editorial post for My Villa.

PILLAR: {pillar_name} ({pillar})
SUB-TOPIC: {sub_topic}
DATE: {slot['date']} ({slot.get('day', '')}) at {slot.get('time', '09:00')} {slot.get('timezone', 'PT')}
FORMAT: {fmt}
CAPTION TARGET: ~{target_chars} chars (hard ceiling 250)
"""

    # Framing-angle injection (Vision/System) — drives variety
    if framing_angle:
        out += f"\nFRAMING ANGLE: {framing_angle['key']}\n"
        out += f"  {framing_angle['instruction']}\n"

    # Anti-repeat: recent opening phrases the model must avoid
    if recent_phrases:
        out += "\nAVOID OPENING PHRASES (used in recent posts):\n"
        for p in recent_phrases[-6:]:
            out += f"  - {p[:80]}\n"

    # Archetype-specific: include masters context
    if pillar == "archetype":
        masters = slot.get("archetype_masters", [])
        definition = slot.get("archetype_definition", "")
        slides = slot.get("slides", 6)
        out += f"\nARCHETYPE DEFINITION: {definition}\n"
        out += f"HISTORICAL REFERENCES (for slides 2-{slides-1}):\n"
        for m in masters:
            out += f"  - {m}\n"
        out += f"\nSlide 1: archetype name + one-line definition.\n"
        out += f"Slides 2-{slides-1}: distribute references one per slide.\n"
        out += f"Slide {slides}: My Villa's contemporary interpretation.\n"

    # Partner echo: REAL post from Apify cache (not stub anymore).
    # The partners listed here are MyVilla's OPERATIONAL partners — not
    # reference firms we admire. They are co-creating MyVilla projects
    # with us right now (design, engineering, construction). The post must
    # make the working partnership EXPLICIT, not just credit their feed.
    if pillar == "partner_echo":
        ph = slot.get("partner_handle", "")
        focus = slot.get("partner_focus", "")
        role_in_myvilla = slot.get("partner_role_in_myvilla", "").strip()
        out += f"\nPARTNER HANDLE: @{ph}\n"
        out += f"PARTNER FOCUS: {focus}\n"
        if role_in_myvilla:
            out += f"PARTNER ROLE IN MY VILLA (must be made explicit in caption):\n"
            out += f"  {role_in_myvilla}\n"
        if partner_post:
            relevance = partner_post.get("relevance_score")
            rationale = partner_post.get("relevance_rationale", "")
            out += "\nORIGINAL PARTNER POST (this is what we are echoing):\n"
            out += f"  URL: {partner_post.get('url', '')}\n"
            out += f"  POSTED: {partner_post.get('timestamp_iso', 'unknown')} ({partner_post.get('age_days', '?')} days ago)\n"
            out += f"  TYPE: {partner_post.get('type', '')}\n"
            if relevance is not None:
                out += f"  EDITORIAL RELEVANCE: {relevance}/10 — {rationale}\n"
            partner_caption = (partner_post.get("caption") or "").strip()
            if partner_caption:
                cap_excerpt = partner_caption[:600] + ("…" if len(partner_caption) > 600 else "")
                out += f"  PARTNER CAPTION (verbatim):\n  \"\"\"\n  {cap_excerpt}\n  \"\"\"\n"
            out += f"""
INSTRUCTION — partner echo that POSITIONS @{ph} AS A MY VILLA PARTNER:

These partners are NOT credentialed firms we cite from a distance. They are
co-creators of MyVilla — designing / engineering / pouring our villas with
us, right now. The reader must finish this post knowing that @{ph} is part
of the team building MyVilla, not just a famous studio we tag in passing.

A reader scrolling past must understand: "ah, @{ph} works on MyVilla
projects." Not just "@{ph} is good at concrete."

We have NOT delivered a villa yet — frame the partnership as ongoing work,
never as a completed project. Use phrases like "We work with @{ph} on…",
"Together with @{ph} we…", "@{ph} engineers / designs / builds the … of
MyVilla", "Our partner @{ph}…". Avoid passive past tense.

Caption STRUCTURE — HARD CEILING 230 chars total (counts include
newlines and the "via @{ph}" line). Stay tight:
  Line 1:  Hook 4-8 words naming the partner's CONTRIBUTION to MyVilla.
           ~50 chars. Examples (don't copy):
             "The structure of every MyVilla starts here."
             "Climate engineered, not hoped for."

  Line 2:  via @{ph}                 ← exactly this, on its own line

  Line 3:  Short verbatim quote from THEIR caption, in quotation marks.
           Translate Italian → English if needed.
           ≤70 chars including the quotation marks.

  Line 4:  ONE short sentence explicitly stating the working partnership.
           ≤60 chars.
           Pattern: "Our [role] partner — [verb] [what] of MyVilla."
           e.g. "Our climate partner — engineering thermal mass into MyVilla."

CRITICAL: Count chars carefully. If your draft would exceed 230, shorten
Line 1 first, then Line 4, then Line 3. NEVER drop Line 2 (via @{ph}).

Voice rules (still apply):
  - No forbidden terms (bunker, fortress, fireproof, dream home, etc.)
  - No claim of delivered villas (use ongoing-tense)
  - Don't paraphrase what the partner already said — quote OR add context
  - The partner is the protagonist. We are co-author, not curator.

The "caption" field MUST contain Lines 1-4 joined by newlines, blank line
between Line 2 and Line 3.
"""
        else:
            out += (
                "\nNO FRESH PARTNER POST AVAILABLE in the scrape cache for this "
                "handle within the configured max-age window. RETURN ONLY:\n"
                "  {\"error\": \"no_partner_post\", \"handle\": \"" + ph + "\"}\n"
            )

    out += f"""
BRAND VOICE — preferred terms:
{json.dumps(brand_voice.get('preferred_terms', {}), indent=2)}

BRAND VOICE — credential anchors (use only if relevant):
{json.dumps(brand_voice.get('credential_anchors', {}), indent=2)}

PROJECT BRIEF EXCERPT (factual ground truth):
{brief_excerpt}

Return ONLY a JSON object with this shape (no markdown fences):
{{
  "caption": "Full IG caption ≤250 chars (caption text only, NO hashtags inline)",
  "hashtags": ["MyVilla", "MyVillaLA", "ReinforcedConcrete", ...5-6 total],
  "char_count": <int>,
  "slides": [          // ONLY for carousel format; omit for single_image
    {{"slide": 1, "headline": "...", "body": "..."}},
    ...
  ],
  "image_caption_alt": "1-line alt text for accessibility (≤120 chars)",
  "topic_tags": ["tag1", "tag2"],   // internal, for ledger / dedup
  "voice_self_check": "one sentence confirming the post follows brand voice (no forbidden terms, value-first, no claim of delivered villas)"
}}
"""
    return out


def get_brief_excerpt(slot, max_chars=2500):
    """Pull a relevant excerpt from project_brief.md. The brief is structured
    by 'Page N' markers; for now we return the first ~max_chars unless we
    can match a pillar-specific section heuristically."""
    if not PROJECT_BRIEF.exists():
        return ""
    text = PROJECT_BRIEF.read_text()

    pillar = slot.get("pillar", "")
    sub_topic = slot.get("sub_topic", "")

    # Crude heuristic: search for matching headings
    if pillar == "archetype" and sub_topic:
        # Find pages mentioning the archetype name
        markers = [m.start() for m in re.finditer(rf"\b{re.escape(sub_topic)}\b", text, re.IGNORECASE)]
        if markers:
            start = max(0, markers[0] - 200)
            return text[start:start + max_chars]

    if pillar == "system":
        for keyword in ("CONCRETE", "STRUCTURE", "MODULARITY", "MOODBOARD", "PERSONALISATION"):
            idx = text.upper().find(keyword)
            if idx >= 0:
                return text[idx:idx + max_chars]

    if pillar == "vision":
        # First meaningful paragraph (page 3 onwards)
        idx = text.find("The Italian villa")
        if idx >= 0:
            return text[idx:idx + max_chars]

    return text[:max_chars]


# ══════════════════════════════════════════════════════════════════════
# Generation
# ══════════════════════════════════════════════════════════════════════

def _scan_recent_drafts_for_phrases(pillar: str, n: int = 6) -> list:
    """Read existing editorial drafts (any month) and pull the FIRST sentence of
    each caption — used to tell the model what NOT to repeat. Filtered by pillar
    so Vision learns from Vision, etc."""
    drafts = []
    if not DRAFTS_DIR.exists():
        return []
    for f in sorted(DRAFTS_DIR.glob("*.md"), reverse=True):
        try:
            raw = f.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if not m:
                continue
            fm = yaml.safe_load(m.group(1)) or {}
            if fm.get("pillar") != pillar:
                continue
            body = m.group(2).strip()
            # First sentence of the caption (before any blank-line break)
            cap = body.split("\n\n")[0]
            first_sentence = re.split(r"(?<=[.!?])\s+", cap)[0]
            drafts.append(first_sentence.strip())
            if len(drafts) >= n:
                break
        except Exception:
            continue
    return drafts


def _scan_recent_framing_keys(pillar: str, n: int = 4) -> list:
    """Read recent drafts to remember which framing-angle keys were used,
    so we don't repeat the same angle 2 posts in a row."""
    keys = []
    if not DRAFTS_DIR.exists():
        return []
    for f in sorted(DRAFTS_DIR.glob("*.md"), reverse=True):
        try:
            raw = f.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not m:
                continue
            fm = yaml.safe_load(m.group(1)) or {}
            if fm.get("pillar") != pillar:
                continue
            k = fm.get("framing_angle")
            if k:
                keys.append(k)
            if len(keys) >= n:
                break
        except Exception:
            continue
    return keys


def _used_partner_shortcodes(handle: str) -> set:
    """Look across existing drafts (any month) and gather partner-post
    shortcodes already used, so we don't repeat the same partner post."""
    used = set()
    if not DRAFTS_DIR.exists():
        return used
    for f in DRAFTS_DIR.glob("*.md"):
        try:
            raw = f.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not m:
                continue
            fm = yaml.safe_load(m.group(1)) or {}
            if fm.get("partner_handle") == handle and fm.get("partner_post_shortcode"):
                used.add(fm["partner_post_shortcode"])
        except Exception:
            continue
    return used


def generate_post_for_slot(slot, config, brand_voice, model=DEFAULT_MODEL,
                           max_retries: int = 1):
    """Call Claude to generate caption + hashtags + slides for a slot.

    Validates the result against editorial constraints and retries up to
    `max_retries` times with corrective feedback if validation fails.
    A final invalid post is returned with an error so the caller skips
    writing the draft (rather than producing low-quality output).

    Returns the post dict (caption, hashtags, slides, ...) plus auxiliary
    keys: _framing_angle, _partner_post (so the writer can persist them).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_OK or not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        return {"error": "no_api_key"}

    pillar = slot["pillar"]

    # ── Pillar-specific context resolution ──────────────────────
    framing_angle = None
    partner_post = None
    recent_phrases = None

    if pillar in ("vision", "system"):
        recent_keys = _scan_recent_framing_keys(pillar)
        framing_angle = pick_framing_angle(pillar, recent_keys)
        recent_phrases = _scan_recent_drafts_for_phrases(pillar)
        slot["framing_angle"] = framing_angle["key"]   # bubble up to writer

    if pillar == "partner_echo":
        if not PARTNER_SCRAPER_OK:
            return {"error": "partner_scraper_unavailable"}
        ph = slot.get("partner_handle", "")
        used = _used_partner_shortcodes(ph)
        # Be generous on freshness — some partners post infrequently. We'd
        # rather echo a 60-day-old strong post than fabricate a stub.
        max_age = config.get("cooldowns", {}).get("partner_post_max_age_days", 90)
        partner_post = partner_scraper.pick_post_for_slot(
            ph, used_shortcodes=used, max_age_days=max_age,
        )
        if not partner_post:
            return {"error": "no_partner_post", "handle": ph}
        # Persist ONLY stable partner_post metadata to the slot dict (which is
        # serialized to calendar YAML). The full partner_post payload — CDN
        # URLs, likes/comments, raw Apify fields — is passed to write_draft
        # via the `post` dict only, never into the calendar.
        slot["partner_post_shortcode"] = partner_post.get("shortcode", "")
        slot["partner_post_url"] = partner_post.get("url", "")
        slot["partner_post_local_thumbnail"] = partner_post.get("local_thumbnail", "")
        slot["partner_post_image_count"] = partner_post.get("image_count", 1)
        slot["partner_post_relevance_score"] = partner_post.get("relevance_score")
        slot["partner_post_relevance_rationale"] = partner_post.get("relevance_rationale", "")

    client = anthropic.Anthropic(api_key=api_key)
    brief_excerpt = get_brief_excerpt(slot)
    base_prompt = build_user_prompt(
        slot, brand_voice, brief_excerpt,
        framing_angle=framing_angle,
        partner_post=partner_post,
        recent_phrases=recent_phrases,
    )

    last_text = ""
    last_validation = None
    for attempt in range(max_retries + 1):
        if attempt == 0:
            user_prompt = base_prompt
        else:
            # Retry with corrective feedback so the model fixes specific issues
            errors_block = "\n  - ".join(last_validation["errors"])
            warnings_block = "\n  - ".join(last_validation.get("warnings", [])) or "(none)"
            user_prompt = (
                base_prompt +
                "\n\nRETRY — your previous attempt failed validation:\n  - " +
                errors_block +
                f"\n\nWarnings (informational): {warnings_block}\n\n"
                "Regenerate the JSON keeping the same topic and pillar; fix"
                " the listed errors. Pay special attention to: caption length"
                " (≤250), exactly the core hashtags + 2-3 rotational ones from"
                " the allowlist, and zero forbidden terms in the caption."
            )

        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=EDITORIAL_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            last_text = text
            post = json.loads(text)
        except json.JSONDecodeError as e:
            last_validation = {
                "ok": False,
                "errors": [f"invalid_json: {e}"],
                "warnings": [],
            }
            continue
        except Exception as e:
            return {"error": "api_error", "exception": str(e)}

        # Edge case: model returned the no_partner_post error structure
        if post.get("error") == "no_partner_post":
            return post

        # Hard-validate before accepting
        last_validation = validate_generated_post(post, config)
        if not last_validation["ok"]:
            if attempt < max_retries:
                continue
            return {
                "error": "validation_failed",
                "errors": last_validation["errors"],
                "warnings": last_validation["warnings"],
                "raw": text[:500],
            }

        # Persist any non-blocking warnings on the post for the writer
        if last_validation["warnings"]:
            post["_warning_validation"] = last_validation["warnings"]

        # Legacy sub-warning kept for backwards compatibility (UI relies on it)
        forbidden = config.get("constraints", {}).get("forbidden_terms", [])
        caption_lower = post.get("caption", "").lower()
        hits = [f for f in forbidden if f.lower() in caption_lower]
        if hits:
            post["_warning_forbidden_terms"] = hits

        # Bubble up auxiliary context for the writer
        if framing_angle:
            post["_framing_angle"] = framing_angle["key"]
        if partner_post:
            post["_partner_post"] = partner_post

        return post

    # Shouldn't be reachable (every iteration returns) — defensive fallback
    return {
        "error": "validation_failed",
        "errors": last_validation["errors"] if last_validation else ["unknown"],
        "raw": last_text[:500],
    }


# ══════════════════════════════════════════════════════════════════════
# Draft writing (.md format compatible with approve.py)
# ══════════════════════════════════════════════════════════════════════

def write_draft(slot, post, image, dry_run=False):
    """Write a draft .md file to _drafts/social_editorial/.

    For partner_echo posts, image comes from the partner's local thumbnail
    (downloaded by partner_scraper.py), not from /img/.
    """
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

    slug = slot.get("slug") or _slugify(slot.get("sub_topic", "post"))[:40]
    fname = f"{slot['date']}-ig-editorial-{slug}.md"
    path = DRAFTS_DIR / fname

    caption = _strip_hashtag_tail(post.get("caption", ""))
    hashtags = post.get("hashtags", [])
    hashtag_line = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    body = caption + ("\n\n" + hashtag_line if hashtag_line else "")

    # Resolve image: for partner_echo, prefer the scraped partner thumbnail.
    # For cross-pollinated slots (system pillar w/ __PARTNER:__ candidate),
    # `image` already came from the partner cache and carries _partner_*.
    image_filename = ""
    image_web_path = ""
    cross_pollinated_partner = None  # populated if the image is a partner one
    if slot["pillar"] == "partner_echo" and post.get("_partner_post"):
        pp = post["_partner_post"]
        if pp.get("local_thumbnail"):
            # local_thumbnail is "_system/social/partner_cache/<handle>/<shortcode>.jpg"
            image_filename = Path(pp["local_thumbnail"]).name
            image_web_path = "/" + pp["local_thumbnail"]
    if not image_filename and image:
        image_filename = image["filename"]
        image_web_path = image["web_path"]
        if image.get("_partner_handle"):
            cross_pollinated_partner = {
                "handle": image["_partner_handle"],
                "shortcode": image.get("_partner_shortcode", ""),
                "url": image.get("_partner_url", ""),
            }

    frontmatter = {
        "channel": "instagram",
        "type": "editorial",
        "subtype": slot["pillar"],
        "date": slot["date"],
        "scheduled_time": slot.get("time", "09:00"),
        "timezone": slot.get("timezone", "America/Los_Angeles"),
        "pillar": slot["pillar"],
        "sub_topic": slot.get("sub_topic", ""),
        "format": slot.get("format", "single_image"),
        "slug": slug,
        "status": "draft",
        "char_count": len(caption),   # caption only, hashtags excluded
        "hashtags": hashtags,
        "topic_tags": post.get("topic_tags", []),
        "image_filename": image_filename,
        "image_web_path": image_web_path,
        "image_alt": post.get("image_caption_alt", ""),
        "voice_self_check": post.get("voice_self_check", ""),
    }

    # Vision/System: persist framing_angle for ledger / future anti-repeat
    if post.get("_framing_angle"):
        frontmatter["framing_angle"] = post["_framing_angle"]

    if slot["pillar"] == "archetype" and post.get("slides"):
        frontmatter["slides"] = post["slides"]

    if slot["pillar"] == "partner_echo":
        frontmatter["partner_handle"] = slot.get("partner_handle", "")
        frontmatter["partner_focus"] = slot.get("partner_focus", "")
        if post.get("_partner_post"):
            pp = post["_partner_post"]
            frontmatter["partner_post_shortcode"] = pp.get("shortcode", "")
            frontmatter["partner_post_url"] = pp.get("url", "")
            frontmatter["partner_post_age_days"] = pp.get("age_days")
            frontmatter["partner_post_caption"] = pp.get("caption_excerpt", "")
            frontmatter["partner_post_image_count"] = pp.get("image_count", 1)
            # Carousel slides — full list of partner thumbnails for the
            # dashboard image picker. Cover (index 0) is the default.
            if pp.get("local_thumbnails"):
                frontmatter["partner_post_thumbnails"] = pp["local_thumbnails"]
    elif cross_pollinated_partner:
        # Non-partner_echo slot using a partner image (e.g. System "DGU
        # lineage" rendering with a real DGU construction photo).
        frontmatter["partner_handle"] = cross_pollinated_partner["handle"]
        frontmatter["partner_post_shortcode"] = cross_pollinated_partner["shortcode"]
        frontmatter["partner_post_url"] = cross_pollinated_partner["url"]
        frontmatter["image_source"] = "partner_cross_pollination"

    if post.get("_warning_forbidden_terms"):
        frontmatter["warning_forbidden_terms"] = post["_warning_forbidden_terms"]
    if post.get("_warning_validation"):
        frontmatter["warning_validation"] = post["_warning_validation"]

    md = "---\n"
    md += yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    md += "---\n\n"
    md += body
    md += "\n"

    if dry_run:
        print(f"\n[dry-run] Would write {path.relative_to(ROOT_DIR)}")
        print(md)
        return path

    path.write_text(md)
    print(f"  ✓ Draft → {path.relative_to(ROOT_DIR)} ({len(caption)} chars)")
    return path


def _slugify(text):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s


# ══════════════════════════════════════════════════════════════════════
# Calendar I/O
# ══════════════════════════════════════════════════════════════════════

def load_calendar(month_str):
    """month_str: 'YYYY-MM'. Returns dict or None."""
    f = CALENDAR_DIR / f"{month_str}.yml"
    if not f.exists():
        return None
    return yaml.safe_load(f.read_text())


def _strip_slot_internal_keys(calendar: dict) -> dict:
    """Remove keys prefixed with `_` from every slot before persisting to
    YAML. These are runtime-only fields (e.g. _partner_post payload) that
    should never bloat the calendar file."""
    for s in calendar.get("slots", []) or []:
        for k in list(s.keys()):
            if isinstance(k, str) and k.startswith("_"):
                s.pop(k, None)
    return calendar


def save_calendar(month_str, calendar):
    f = CALENDAR_DIR / f"{month_str}.yml"
    _strip_slot_internal_keys(calendar)
    f.write_text(yaml.safe_dump(calendar, sort_keys=False, allow_unicode=True))


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="My Villa — IG Editorial Generator")
    parser.add_argument("--month", required=True, help="YYYY-MM (e.g. 2026-05)")
    parser.add_argument("--slot", help="Specific slot date YYYY-MM-DD (default: all planned in next 14 days)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of slots generated this run")
    parser.add_argument("--days-ahead", type=int, default=14,
                        help="Generate slots within N days from today (default 14)")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if draft already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print without writing files")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model (default {DEFAULT_MODEL})")
    args = parser.parse_args()

    print(f"\nMy Villa — IG Editorial Generator")
    print(f"{'=' * 50}")
    print(f"  Month: {args.month}")
    print(f"  Model: {args.model}")

    cal = load_calendar(args.month)
    if cal is None:
        print(f"✗ No calendar found for {args.month}. Run editorial_planner.py first.")
        sys.exit(1)

    config = yaml.safe_load(CONFIG_FILE.read_text())
    brand_voice = yaml.safe_load(BRAND_VOICE_FILE.read_text())

    today = date.today()
    cutoff = today + timedelta(days=args.days_ahead)

    # Filter slots
    target_slots = []
    for s in cal["slots"]:
        try:
            s_date = datetime.strptime(s["date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        if args.slot and s["date"] != args.slot:
            continue
        if not args.slot:
            if s_date < today or s_date > cutoff:
                continue

        # Skip already-drafted unless --force
        if s.get("status") not in ("planned", "planned_pending_scrape") and not args.force:
            continue

        target_slots.append(s)

    if args.limit:
        target_slots = target_slots[:args.limit]

    if not target_slots:
        print(f"  No matching slots to generate.")
        return

    print(f"  Slots to process: {len(target_slots)}\n")

    # Generate
    success = 0
    for s in target_slots:
        print(f"  → {s['date']}  {s['pillar']:13}  {s.get('sub_topic') or s.get('partner_handle', '')}")
        post = generate_post_for_slot(s, config, brand_voice, model=args.model)

        if "error" in post:
            print(f"    ✗ {post['error']}: {post.get('exception', post.get('raw', '')[:100])}")
            continue

        # Collect all partner shortcodes already used (any handle, any pillar)
        # so cross-pollinated partner images don't duplicate one already
        # featured as partner_echo in another draft.
        all_used_shortcodes = set()
        for ph in ("buromilan", "dgu_baja", "transsolar_klimaengineering", "its__vision"):
            all_used_shortcodes |= _used_partner_shortcodes(ph)
        image = pick_image(s, used_partner_shortcodes=all_used_shortcodes)
        if not image:
            print(f"    ⚠ No image candidate found in /img/")

        write_draft(s, post, image, dry_run=args.dry_run)

        if not args.dry_run:
            s["status"] = "draft"
            s["draft_generated_at"] = datetime.now().isoformat(timespec="seconds")
        success += 1

    if not args.dry_run and success > 0:
        save_calendar(args.month, cal)
        print(f"\n  ✓ Calendar updated: {success}/{len(target_slots)} slots → status=draft")
    elif args.dry_run:
        print(f"\n  [dry-run] Generated {success}/{len(target_slots)} samples")


if __name__ == "__main__":
    main()
