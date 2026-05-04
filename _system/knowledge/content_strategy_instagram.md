# My Villa — Instagram Editorial Strategy (Canonical)

**Last updated:** 2026-04-24
**Owner:** Ivo Giuliani
**Account:** `@myvilla.la` (publishing — Phase 1: STUB MODE; Phase 4: Meta Graph API)

> **Phase 1 publishing is STUB MODE.** `publish_instagram.py` does NOT call
> the Meta Graph API. It produces a copy-paste folder under
> `_system/social/posts/editorial/_publish_ready/<date>-<slug>/` with
> `caption.txt`, the resolved image, optional `slides.txt` for carousels,
> and `metadata.json`. The user pastes manually into the Instagram app
> until the IG↔FB Page link is enabled and Phase 4 swaps in real publishing.
**Consumers:** `_system/scripts/editorial_planner.py`, `_system/scripts/partner_scraper.py`, `_system/scripts/editorial_generator.py`, `_system/scripts/publish_instagram.py`, `_system/scripts/approve.py` (Editorial tab)

Single source of truth for the Instagram editorial channel. Parallel to (not replacement for) `content_strategy.md` (Journal/blog SEO strategy).

---

## 1. Purpose & scope

**Editorial IG** tells the **architectural thesis** behind My Villa: why the Italian villa typology in LA, why exposed reinforced concrete, why resilience is a design consequence (not a sales pitch). Evergreen. Brand foundation.

**Reactive IG** (existing, `generate_social.py`) comments the news cycle from daily radar signals.

|  | Editorial (new) | Reactive (existing) |
|---|---|---|
| Trigger | Calendar-driven | Radar signal |
| Cadence | 3 posts/week | ~2 posts/week |
| Lifespan | Evergreen | 24–72h |
| Source | Pillars + `project_brief.md` + partners | News articles |
| Script | `editorial_generator.py` (new) | `generate_social.py` (existing) |

Total Instagram throughput: **~5 posts/week**.

---

## 2. Target audience & tone

Primary: same persona as Journal (`content_strategy.md` §1) — UHNW California rebuilders and new-construction commissioners.
Adjacent IG-native: architects, designers, luxury real-estate agents, architectural press.

**Tone** (inherits from `brand-voice.yml` → `tone.instagram`):
- Visual-first, aspirational but grounded
- Never salesy, never fear-driven ("bunker", "protect your family", etc. — see `forbidden_terms`)
- Value before product: lead with an architectural idea, not "come see our villas"
- All copy in **English**
- Thought leader: **Paolo Mezzalama** (but only named in quote attributions — never in captions proactively)

---

## 3. Four content pillars

Target mix over a 4-week rolling window: **~25% each**.

### Pillar 1 — Vision (The thesis)
**What:** Why the villa typology in LA. Resilience as architectural continuity. "Italian Soul, Californian Body" manifesto.
**Sources:** `project_brief.md` pp.3–12 (Introduction, LA: Sun Sand Surf), pp.53–59 (Resilient Design).
**Format:** Single image OR quote-card. Caption 150–250 chars before fold.
**Example hook:** *"The villa has balanced permanence with adaptability for two millennia. In Los Angeles, that balance becomes a structural choice."*

### Pillar 2 — Archetypes (Form language)
**What:** The 8 archetypes from the brief — **Courtyard, Podium, Portico, Pergola, Fireplace, Window, Living Green Roof, Fence** — each paired with Italian masters (Ponti, Palladio, Scarpa, Zanuso, Magistretti, Moretti, De Carlo, Libera, Riva, Ponis, Gardella).
**Sources:** `project_brief.md` pp.15–44.
**Format:** Carousel (5–7 slides).
- Slide 1: archetype name + one-line definition
- Slides 2–5: 2–4 historical references (master + work + year)
- Slide 6: how My Villa interprets it today
**Cadence:** 1 carousel per archetype → 8 carousels ≈ 2.5 months of Pillar-2 content before rotation.

### Pillar 3 — System (Concrete as language)
**What:** Exposed reinforced concrete — moodboard tones, structural system, modularity, assembly, personalisation. DGU lineage (Palazzo Grassi, Punta della Dogana, Kimbell Art Museum). Transsolar climate engineering. Never "bunker" — always **"material permanence"**.
**Sources:** `project_brief.md` pp.68–92 (Construction System, Moodboard), `brand-voice.yml` → `credential_anchors`.
**Format:** Single image (render / moodboard) OR carousel for process-heavy posts.

### Pillar 4 — Partner echo (Curated repost)
**What:** Repost from partner IG when their post aligns with Pillars 1–3. Never a raw repost — always reframed with My Villa perspective.
**Handles:**
- `@buromilan` — structural engineering
- `@dgu_baja` — concrete construction
- `@transsolar_klimaengineering` — climate engineering
- `@its__vision` — architecture (IT'S)

**Rules:**
- First line: `via @partner_handle`
- 2–3 sentence framing tying the post to one of Pillars 1/2/3
- Max 1 repost per partner per month (avoid feed-as-aggregator feel)
- Never repost: company anniversaries, holiday greetings, people-centric shots without architecture, content that contradicts brand-voice

**Caption pattern:**
```
[Partner post subject — 1 line]
via @partner_handle

[Why this matters for My Villa — 2-3 sentences linking to Pillar 1/2/3]

#MyVilla #MyVillaLA [+ 2-3 rotational tags]
```

---

## 4. Weekly cadence

**3 editorial + ~2 reactive = 5 posts/week**

| Day | Slot | Source | Pillar (editorial only) |
|---|---|---|---|
| Mon | 09:00 PT | editorial_planner | Vision (P1) |
| Tue | 09:00 PT | generate_social (radar) | — (reactive) |
| Wed | 09:00 PT | editorial_planner | Archetype (P2) — alternating with System (P3) |
| Thu | 09:00 PT | partner_scraper + editorial_generator | Partner echo (P4) |
| Fri | 09:00 PT | generate_social (radar) | — (reactive) |
| Sat / Sun | — | No scheduled posts (reactive only if breaking news) | — |

**Monthly target (4 weeks):** 12 editorial posts.
- 4 Vision + 4 Archetype or System (2+2 alternating) + 4 Partner echo.

---

## 5. Hashtag strategy

**Core (always, 3 tags):** `#MyVilla #MyVillaLA #ReinforcedConcrete`

**Rotational (pick 2–3 per post from):**
- Form: `#ItalianVilla #ArchitecturalConcrete #ConcreteArchitecture #VillaDesign #ExposedConcrete`
- Place: `#Malibu #BeverlyHills #LosAngeles #CaliforniaArchitecture #BelAir`
- Theme: `#FireResilient #ResilientDesign #BiophilicDesign #ItalianDesign #LuxuryHomes #ContemporaryArchitecture`

**Forbidden:** `#fireproof #bunker #investment #luxurylifestyle #dreamhome` (off-brand — see `brand-voice.yml` `forbidden_terms`).

Target total: **5–6 hashtags per post**. Quality over quantity — never hit the 30 IG max.

---

## 6. Visual strategy

**Phase 1 — existing assets only:**
- `/img/` folder on website (renders, moodboard, interiors)
- Images extracted from `project_brief.md` PDF
- Partner IG (for echo posts, via `partner_scraper.py`)

**Phase 2 — generative (when Phase 1 assets exhausted):**
- OpenAI `gpt-image-1` OR Google `gemini-2.5-flash-image` (nano-banana — cheaper)
- Style guardrails: `_system/config/image-style.yml`
- **Never AI-generate:** partner logos, team photos, anything that could mislead as a real project delivered

Every visual must read as "My Villa": concrete-tone palette (from brief moodboard pp.88–92), architectural clarity, no stock-photo feel.

---

## 7. Technical workflow

### New scripts
```
_system/scripts/
  editorial_planner.py      # populate monthly calendar from 4 pillars
  partner_scraper.py        # Apify IG scraper → filtered shortlist
  editorial_generator.py    # caption + hashtag generation (Claude Sonnet 4.6)
  publish_instagram.py      # Phase 1 STUB: writes copy-paste publish package
                            # Phase 4 (later): Meta Graph API → schedule + publish
  approve.py                # EXTENDED: new "📆 Editorial" tab
```

### Storage
```
_system/config/
  editorial-calendar.yml    # pillars + weekly rhythm config
  partners.yml              # handles + filter rules + repost frequency

_system/social/
  calendar/
    2026-05.yml             # one file per month, draft + approved slots
  posts/
    editorial/              # approved editorial drafts (pre-publish)
    partner_echo/           # approved partner reposts
    reactive/               # existing (from generate_social.py)
    published/              # post-publication archive with Meta media_id
  partner_cache/
    buromilan.json          # scraper cache per handle (TTL 24h)
    dgu_baja.json
    transsolar_klimaengineering.json
    its__vision.json

_drafts/social_editorial/   # pre-approval staging, visible in dashboard
```

### Dashboard (approve.py extension)
- **New tab:** `📆 Editorial` (alongside existing Journal / Social / Replies / Email)
- **View:** weekly calendar grid, current week + next 2 weeks visible
- **Card per slot:** image preview, caption (editable), hashtag list, pillar tag, scheduled date+time, partner handle (for P4)
- **Actions:**
  - `Approve & Schedule` — push to Meta Graph Publishing API
  - `Regenerate caption` — re-call Claude
  - `Swap image` — pick from `/img/` or re-scrape partner
  - `Skip slot` — mark as intentionally empty
  - `Dismiss` — move to `_dismissed/`
- **Header buttons:**
  - `🔄 Scrape partners` (on-demand Apify pull)
  - `📝 Plan next month` (generates draft calendar for following month)

---

## 8. Cost model

| Service | Purpose | Monthly cost |
|---|---|---|
| Meta Graph API | Publishing to @myvilla.la | **$0** (free, requires IG Business + FB Page linked) |
| Apify Instagram Scraper | 4 partner handles, daily poll | **~$2–3** (fits Apify's $5/mo free credit) |
| Claude API (Sonnet 4.6) | Caption + framing generation | **~$1–2** (12 editorial + 8–12 echo × ~2k tokens) |
| **Phase 1 total** | | **~$0–5/mo** |
| Image generation (Phase 2) | gpt-image-1 or gemini-2.5-flash-image | **~$1–3** (20–30 images/mo × $0.04–0.08) |
| **Phase 2 total** | | **~$3–8/mo** |

**One-time setup:**
- Meta app + IG Business linkage + long-lived token: 30–60 min
- Apify account + API token: 10 min
- No paid licenses

---

## 9. Rollout phases

**Phase 1 — Editorial MVP (target: 1 week of implementation)**
1. Extend `approve.py` with Editorial tab (calendar view + slot cards)
2. `editorial_planner.py` — generate May 2026 calendar draft from pillars 1–3
3. `editorial_generator.py` — caption generation with `brand-voice.yml` enforcement
4. Manual image selection from `/img/`
5. `publish_instagram.py` — Meta Graph API integration (publish + schedule)

**Phase 2 — Partner echo (target: 1 week after Phase 1 validated)**
1. `partner_scraper.py` — Apify integration for 4 handles
2. Filter + rank logic (freshness, content type, brand-voice compat)
3. Dashboard: partner-echo cards with source link back to partner post
4. Framing-caption generation (My Villa perspective layer)

**Phase 3 — Generative visuals (when `/img/` is exhausted)**
1. `image-style.yml` guardrails (concrete palette, architectural framing rules)
2. Extend `generate_images.py` for editorial context
3. Human-in-the-loop for every generated image (mandatory approval before publish)

**Phase 4 — Scheduled automation & monitoring (later)**
1. Cron: weekly plan regeneration + partner scrape
2. Cron: daily publish of approved-and-scheduled posts
3. DM + comment monitoring on @myvilla.la (follows pattern of `reply_monitor.py`)

---

## 10. What this file is NOT

- **Not** a replacement for `content_strategy.md` (that's the Journal SEO strategy — long-form, written articles).
- **Not** a stylesheet — voice rules live in `brand-voice.yml`.
- **Not** a factual source — facts come from `project_brief.md` + `site_content.md`.
- **Not** the reactive/news pipeline — that stays in `generate_social.py`.

When choosing between this and `content_strategy.md`:
- **Journal** = written article, long-form, ranks on Google, commercial-intent SEO.
- **Editorial IG** = visual-first, brand-foundation, builds architectural thesis and discovery funnel.

This file is the tiebreaker for all Editorial IG decisions (pillar mix, cadence, partner handling, costs, phases).
