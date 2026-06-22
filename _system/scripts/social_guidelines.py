#!/usr/bin/env python3
"""
social_guidelines.py — fonte UNICA delle linee guida social di My Villa
(decise da Ivo, 2026-06-15). Importata dai generatori di contenuti
(generate_social, generate_ig_companion, generate_x_companion) così ogni
post/caption rispetta lo stesso tono di voce e lo stesso obiettivo.

Documento esteso (anche le parti non automatizzabili — frequenza,
linguaggio fotografico, riferimenti visivi, stories): vedi
_system/config/social_guidelines.md
"""

# ── Tono di voce + obiettivo → iniettato nei system prompt ───────────
# Subset "testuale" delle guidelines: ciò che l'LLM può applicare a ogni
# post. Le regole visive/grafiche stanno nel .md (sono per chi produce
# le immagini).
VOICE_RULES = """\
SOCIAL GUIDELINES — tono di voce e obiettivo (linee guida ufficiali My Villa, 2026-06-15):
- Direct and clear. Short, incisive sentences. No filler, no run-ons.
- NOT self-celebratory: never boast about My Villa; let the idea/insight speak.
- NEVER mention prices, costs, "$", budgets, or how much anything is worth.
- Use numbers ONLY when they back a real data point, result, or percentage that
  conveys My Villa's values (resilience, materials, climate). Otherwise no numbers.
- OBJECTIVE — brand awareness: position My Villa as the new reference for
  contemporary luxury in Los Angeles. Frame each villa NOT as a real-estate
  product but as a symbol of a desirable, sophisticated lifestyle — a slower,
  authentic Los Angeles immersed in greenery. Sell the life, not the building."""

# ── Scope SOLO per X/Twitter (deciso da Ivo, 2026-06-22) ─────────────
# Su X trattiamo UNICAMENTE la competenza costruttiva: come COSTRUIRE,
# CERTIFICARE e ottenere PERMESSI per una casa resiliente e assicurabile a
# Los Angeles. Posizionarci come l'esperto nel momento in cui costruire/
# assicurare/permessi è complesso. Iniettato SOLO nei generatori X (NON in
# quelli Instagram, che restano brand-awareness/lifestyle + evergreen).
X_SCOPE = """\
X/TWITTER — SCOPE (applies ONLY to X posts, never Instagram):
- Treat ONE subject family only: HOW to build, HOW to certify, and HOW to get
  permits for a fire-resilient, INSURABLE home in Los Angeles. Position My Villa
  as THE expert guiding owners and builders through this complex moment —
  permits, building codes, certification and insurability are the real barriers
  right now, and that is exactly our terrain.
- Tie to the day's conversation (especially what is discussed or going viral on
  X), but ALWAYS pivot to the build / certify / permit / insure angle for someone
  trying to build in LA. Audience: prospective LA home buyers and builders.
- Lead with a concrete fact or credential, never a fear hook. Draw on My Villa's
  construction expertise (from the site): reinforced-concrete / ICF 250mm shell,
  Type I non-combustible structure (4+ hour fire rating), 2026 WUI Code (Title 24
  Part 7) compliance built-in, IBHS "Wildfire Prepared Home Plus", California
  "Safer from Wildfires" 12/12 measures met natively, FAIR Plan / admitted-carrier
  insurability. Builder pedigree: DGU (Palazzo Grassi, Aman, Kimbell Art Museum).
- Framing is always "pro-insurable / here is how it's actually done" — never
  "anti-fire", never "bunker", never fear. If a story is pure lifestyle, market
  or design with NO build/certify/permit/insure angle, it does not belong on X
  (leave that to Instagram)."""

# ── Linguaggio fotografico → hint per le query immagini (Unsplash) ───
# Parziale: lo stock non eguaglia la fotografia reale delle ville, ma
# orienta la scelta automatica verso l'estetica giusta. Il controllo
# vero resta umano (vedi .md + instagram_reels_playbook.md).
IMAGE_STYLE_HINT = "golden hour minimalist architecture symmetric soft shadows"
