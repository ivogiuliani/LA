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

# ── Linguaggio fotografico → hint per le query immagini (Unsplash) ───
# Parziale: lo stock non eguaglia la fotografia reale delle ville, ma
# orienta la scelta automatica verso l'estetica giusta. Il controllo
# vero resta umano (vedi .md + instagram_reels_playbook.md).
IMAGE_STYLE_HINT = "golden hour minimalist architecture symmetric soft shadows"
