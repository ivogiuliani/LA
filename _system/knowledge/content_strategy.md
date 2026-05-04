# My Villa — Content Strategy (Canonical)

**Last updated:** 2026-04-19
**Owner:** Ivo Giuliani
**Consumers:** `_system/config/radar-keywords.yml`, `_system/scripts/generate_journal.py`, future pillar pages

This is the single source of truth for who we write to, what we write about,
and which Google queries we are trying to rank for. The radar config and
the Journal generator prompt should BOTH align with this document.

---

## 1. Target buyer

**Persona:** Ultra-wealthy Californian (≈$10M+ liquid net worth, often UHNW)
commissioning or rebuilding a luxury home who:

- Wants the home to stay **insurable** through California's ongoing
  fire-insurance crisis (non-renewals, FAIR Plan, IBHS-driven underwriting).
- Wants the home to be **physically resilient** to future wildfire cycles
  (not just patched after one event).
- Values Italian / Mediterranean design lineage and museum-grade execution.
- Is willing to pay a 3-10% construction premium for long-term insurability
  and permanence.
- Is NOT necessarily a fire victim — may be building new on an existing lot
  or on an undeveloped parcel, or upgrading an older home.

**We are NOT writing to:** victims looking for emergency rebuild help at
any price point. That coverage remains in scope as news / PR value but is
no longer the commercial target.

---

## 2. Priority geographies (ordered)

| Tier | Areas | Rationale |
|------|-------|-----------|
| **1** | Malibu, Beverly Hills | Highest concentration of target buyer + highest insurance pressure |
| **2** | Bel Air, Holmby Hills, Brentwood, Hidden Hills, Calabasas | Adjacent luxury Westside markets |
| **3** | Mandeville Canyon, Topanga, Pacific Palisades | Fire-zone luxury; relevant but no longer leading |
| **4** | Montecito, Newport Coast, Santa Barbara Foothills | Secondary California luxury geographies |

When the story allows geographic flexibility, lead with Tier 1 in the title.
When the story IS specific to a single location (e.g. LAFD brush-clearance
notices for Palisades), keep it specific — authenticity wins over SEO cramming.

---

## 3. Primary content clusters

### Cluster I — Insurability & Underwriting (California, statewide)
Fire-insurance crisis, FAIR Plan, non-renewals, IBHS discounts, Mercury /
AAA / USAA underwriting shifts, Prop 103, California Department of Insurance
rulings. **This is our single most commercially valuable cluster** — the
pain is real, statewide, and the search intent is urgent.

### Cluster II — Fire-Resilient & Insurable New Construction
Reinforced concrete homes, ICF, Class A roofs, non-combustible assemblies,
2026 WUI Code compliance, Safer from Wildfires 12 measures, IBHS Wildfire
Prepared Home Plus. Frame around "building insurable from day one", not
just "repairing after fire".

### Cluster III — Italian / Mediterranean Villa Typology in California
Italian villa architecture, Tuscan / Mediterranean style homes, modern
Italian residential design, European architects working in California.
This is the brand's lifestyle entry point and differentiates us from
generic fire-resistant builders.

### Cluster IV — Luxury Market Signals (Westside + Coastal)
Malibu / Beverly Hills / Westside luxury real estate trends, spec homes,
HNW buyer behavior, ultra-luxury transactions in priority geographies.

### Cluster V — Rebuild News (SECONDARY)
Palisades / Altadena / Malibu post-fire rebuild coverage. Kept for PR value,
backlink potential, and authority building — but **no longer leads** the
portfolio and should not dominate future radar scans.

---

## 4. Primary SEO targets

These are the commercial-intent Google queries we are trying to rank for
(listed in approximate priority order for the coming 6–12 months):

1. `luxury home builder Malibu`
2. `custom home Beverly Hills`
3. `fire resistant home California`
4. `insurable home California`
5. `concrete home builder Los Angeles`
6. `Italian villa California builder`
7. `Mediterranean villa California`
8. `California fire insurance solution home`
9. `FAIR Plan alternative luxury home`
10. `ICF home builder California`
11. `fireproof home California`
12. `Bel Air custom home builder`
13. `architect luxury home Malibu`
14. `concrete home cost California`
15. `how to build fire-resistant luxury home California`

The Journal generator should aim to rank for these via cluster content;
pillar pages (Phase 2) will sit above the cluster for the top 4–5 queries.

---

## 5. SEO title conventions for Journal articles

Every new article title MUST satisfy these rules:

- **Length:** 50–65 characters preferred, max 70.
- **Primary keyword in first half** of the title where possible.
- **Geography anchor:** prefer `California` (broadest), `Los Angeles`,
  `Malibu`, `Beverly Hills`. Avoid leading with `Palisades` or `Altadena`
  unless the story is specifically about those places and no broader
  framing exists.
- **Buyer framing over news framing:** prefer "Fire-Resistant Home Cost
  in California: …" to "The 3% Decision: …". Keep cleverness in the
  subtitle.
- **No rhetorical questions, no puns** as primary title. Puns are OK only
  for clearly news-driven stories where PR value outweighs SEO.
- **Mention at least ONE of:** `luxury`, `insurable`, `fire-resistant`,
  `reinforced concrete`, `Italian villa`, `Mediterranean villa`,
  `custom home`, `new construction`, `rebuild` (only where the story is
  explicitly about rebuilding).
- **Current year (2026)** is a natural trust signal for queries about
  codes, insurance, and rebuild — use where genuine.

Example good title:
> `Fire-Resistant Home Construction Cost in California: The 3% Premium That Unlocks Up to 50% Insurance Savings`

Example bad title:
> `The 3% Decision: How Construction Choice Reshapes Insurance Math in LA`

---

## 6. Meta description conventions

- 140–160 characters (Google truncates around 155–160).
- Lead with the buyer's question or pain point — not the story's hook.
- Include the primary keyword from the title verbatim.
- End with a reason to click (concrete number, credential, or specific
  insight).

---

## 7. "Our Perspective" block — rotation rules

See also: `generate_journal.py` → `build_generation_prompt` PRIOR COVERAGE
block. Enforced programmatically.

- Rotate credentials across articles. Do NOT cite DGU / Renzo Piano /
  Kimbell / Palazzo Grassi in more than 2 of any 7 consecutive articles.
- Rotate between credentials: DGU, IT's Architecture (Paolo Mezzalama's
  Rome + Paris practice), Transsolar (climate engineering), the Italian
  villa typology itself, reinforced-concrete system technical qualities.
- Vary framing: do not always start with "this story is about X but the
  real variable is material choice". Sometimes lead with the
  insurability lens, sometimes with the typology, sometimes with the
  homeowner's ROI math.
- Ban on recurring boilerplate phrases:
  - "material, not accessories, compounds over decades"
  - "not what style but what system"
  - Any verbatim reuse of a prior article's closing maxim.

---

## 8. Source citation & E-E-A-T standards

- Primary sources only, deep-linked. No homepage links inside body prose.
- Named publication + author where available.
- JSON-LD `author` field points to `https://myvilla.la/team.html`
  ("My Villa Editorial Team") — not just the Organization.
- `datePublished` and `dateModified` both present.
- BreadcrumbList JSON-LD with `Home → Journal → Article`.
- Every article should cite 2–4 primary sources.

---

## 9. Cross-linking map (current Journal cluster)

Commercial cluster (heavy interlink):
- `rebuild-math-3-percent-...` ↔ `insurance-decides-palisades-...` ↔
  `concrete-innovations-fire-rebuilding-...`

Secondary / news articles link INTO the commercial cluster:
- `altadena-hoa-bills-...` → `rebuild-math-...` + `insurance-decides-...`
- `altadena-rebuild-technology-...` → `concrete-innovations-...` + `rebuild-math-...`
- `la-rebuild-pace-34-homes-...` → `concrete-innovations-...`
- `palisades-brush-clearance-...` → `insurance-decides-...`

Every article links to `/team.html` once from the "Our Perspective" block.

---

## 10. Pillar pages (Phase 2 — not yet built)

These will sit above the cluster and target the top SEO queries:

1. **`/malibu-custom-home-builder.html`** — target: *luxury home builder Malibu*
2. **`/beverly-hills-custom-home.html`** — target: *custom home Beverly Hills*
3. **`/california-fire-insurance-solution.html`** — target: *California fire insurance solution home*
4. **`/italian-villa-california.html`** — target: *Italian villa California builder*

Each pillar is 2,000–3,000 words, evergreen, FAQ schema, links out to
cluster articles as supporting evidence. Journal articles link UP to the
relevant pillar.

---

## 11. What this file is NOT

- Not a marketing brief. Keep that in `brand-voice.yml`.
- Not a factual knowledge base. Keep facts in `project_brief.md` and
  `site_content.md`.
- Not a stylesheet. Keep voice rules in `brand-voice.yml`.

This is a **strategy** document — it tells us what topics to cover, in
what priority, for what audience, targeting which search queries. When in
doubt about whether a topic or title fits, this file is the tiebreaker.
