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

# ── Scope per X/Twitter (deciso da Ivo 2026-06-22, ALLARGATO 2026-07-01) ──
# ALLARGAMENTO 2026-07-01 (richiesta Ivo): X non tratta più SOLO incendi/
# assicurazioni/permessi. Ora DUE famiglie di conversazione, entrambe di prima
# classe: (A) come costruire/certificare/permessi/assicurare a LA (competenza)
# e (B) l'ARCHITETTURA DI LUSSO DI LOS ANGELES — design, cemento a vista,
# tipologia villa italiana/mediterranea, materiali e maestria. Entrare dal vivo
# nelle conversazioni di architettura, non solo come esperto di assicurazioni.
# Iniettato SOLO nei generatori X (NON Instagram, che resta lifestyle+evergreen).
X_SCOPE = """\
X/TWITTER — SCOPE (applies ONLY to X posts, never Instagram):
- TWO conversation families, BOTH first-class (a post may live entirely in either):
  (A) EXPERTISE — HOW to build, certify, permit and INSURE a fire-resilient
      luxury home in Los Angeles. Permits, WUI code, certification, insurability
      and underwriting are real barriers right now and that is our terrain.
  (B) LA LUXURY ARCHITECTURE & DESIGN — residential architecture, exposed /
      reinforced concrete, Italian & Mediterranean villa typology, materials,
      craft and museum-grade design (Tadao Ando lineage; DGU pedigree: Palazzo
      Grassi, Aman, Kimbell), and the Westside luxury-home world. Enter these as
      a knowledgeable peer who BUILDS serious architecture — not only as the
      insurance expert.
- Tie to the day's conversation (what is discussed / going viral on X). You may
  LEAD with a design or architecture observation and you do NOT have to pivot
  every post to insurance. Audience: LA luxury-home buyers, builders, architects,
  and design-minded owners.
- Lead with a concrete fact, a design observation, or a credential — never a fear
  hook. Substance to draw on: reinforced-concrete / ICF 250mm shell, Type I
  non-combustible (4+ hour fire rating), 2026 WUI Code (Title 24 Part 7), IBHS
  "Wildfire Prepared Home Plus", California "Safer from Wildfires" 12/12, FAIR
  Plan / admitted-carrier insurability; and the design/material pedigree above.
- Framing is always positive and expert — "here is how it's actually done", "what
  makes this beautiful and built to last" — never "anti-fire", "bunker",
  "fireproof", or fear. Pure celebrity gossip or off-topic politics still don't
  belong on X."""

# ── Linguaggio fotografico → hint per le query immagini (Unsplash) ───
# Parziale: lo stock non eguaglia la fotografia reale delle ville, ma
# orienta la scelta automatica verso l'estetica giusta. Il controllo
# vero resta umano (vedi .md + instagram_reels_playbook.md).
IMAGE_STYLE_HINT = "golden hour minimalist architecture symmetric soft shadows"
