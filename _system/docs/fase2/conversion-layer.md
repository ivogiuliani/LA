# Fase 2 — conversion-layer (handoff)

Data: 2026-09-16 · Agente: conversion-layer · Perimetro: sito v1 live (GitHub Pages)

## Cosa c'è

### Script
- `_system/scripts/inject_conversion_layer.py` — patch idempotenti a marker `CONV:<nome>:START/END`
  (HTML: `<!-- … -->`, dentro `<script>`/`<style>`: `/* … */`). Legge tutto da
  `_system/config/lead_settings.yml` (prezzo, timeline, promessa di risposta, landing, thank-you,
  endpoint lead API, Formspree, GA4, founder_title, disclaimer "not built yet").
  - `python3 _system/scripts/inject_conversion_layer.py` → set completo su `index.html`
  - `--dry-run` → solo diff · `--target v2/index.html --dry-run` → verifica sul v2 (skippa gli anchor assenti, non scrive)
  - `--shared <file…>` → solo snippet condivisi: CONSENT (Consent Mode v2), `data-page-type` sul body,
    FORMJS (solo se la pagina ha un `<form>`), MVTRACK, COOKIE_UI (banner/toast, solo se la pagina non ha già il banner)
  - `--print-form <form_id>` / `--print-form-css` → markup e CSS del form canonico per una nuova pagina
  - Sanity prima di scrivere index.html: gtag `G-D6HJX7BNZN`, marker `DESK:*`, id `contactForm/ctaSuccess/stickyCta/nav/burger/mobileMenu`.
- `_system/scripts/optimize_media.py` — ricompressione PIL (q80, max 1920px) dei webp/jpg > 400 KB in `img/`
  (esclude `img/social/**` di default: `--include-social`), sovrascrive solo se −20%. Eseguito: `courtyard-interior.webp`
  1,40 MB → 366 KB (3500→1920 px), `amanvari-02.jpg` 502 → 135 KB.

### Patch su index.html (tutte marcate CONV)
CONSENT · CSS · NAV · NAV_MOBILE · HERO (2 bottoni) · VIDEO + VIDEOJS (video hero via JS solo ≥900px, `preload="none"`,
niente autoplay su mobile) · CTX_RESILIENCE/PROCESS/TEAM · TRUST_LABEL ("DGU — Selected Works") + TRUST_NOTE ·
FAQ (8 domande visibili in #investment, `<details>`; le 5 storiche + 3 nuove) · SCHEMA (JSON-LD riscritto: un solo
`Organization` @id `#organization` con logo `/img/myvilla-logo.png`, `alternateName`, `founder` → `Person`
`team.html#paolo-mezzalama`; il vecchio `LocalBusiness` è stato assorbito; FAQPage allineata alle 8 visibili) ·
FORM (form canonico, stesso `id="contactForm"` + `onsubmit="handleSubmit(event)"`) · FORM_LEGACY (vecchio submit rimosso;
`const FORMSPREE_ID` eliminato per evitare collisioni) · COOKIE (banner opt-in se timezone `Europe/*`, toast altrove;
`acceptCookies/declineCookies` → `gtag('consent','update')`) · STICKY (≤900px, "Private Briefing", z-index 9995 sopra il banner 9990;
si alza di `--mv-banner-h` quando il banner è visibile) · FORMJS · MVTRACK.

### Pagine
- `private-briefing.html` (nuova, indicizzabile, canonical) — hero + form canonico `form_id=landing` + 3 FAQ + "What happens next" + footer.
  Schema: Organization (ref) + WebPage + BreadcrumbList + FAQPage.
- `briefing-received.html` (nuova, `noindex,nofollow`, NON in sitemap) — legge `?form=&via=` e manda `briefing_received` una volta (guardia sessionStorage).
- `malibu-custom-home-builder.html` — sezione `#brief` con form canonico `form_id=malibu_pillar` (sostituisce il vecchio blocco `.pillar-cta`),
  CTA hero (2 bottoni) e mid (`.mid-cta` prima di "The cost framework"), nav-cta → `#brief`.
- 4 landing (`beverly-hills…`, `italian-villa…`, `icf-concrete…`, `pacific-palisades…`) — form esistente esteso:
  `data-mv-form`, select obbligatorie con placeholder, `how_found` + `referred_by`, consenso nurture, hidden di attribuzione,
  riga privacy, errore inline; vecchio IIFE Formspree rimosso (submit unificato in FORMJS). `form_id`:
  `beverly_pillar`, `italian_villa_pillar`, `concrete_pillar`, `palisades_pillar`.
- `team.html` — blocco `#paolo-mezzalama` (titolo = `canonical.founder_title`, link profilo its.vision), bottoni
  "Request a Private Briefing" → landing `?src=team_page|team_cta`, Person schema con @id coerente con index.html.
- `privacy.html` — riscritta (12 sezioni con id: `#controller #what-we-collect #purposes #processors #transfers #retention
  #rights #cookies #dnt #security #children #changes`), placeholder visibile `[Legal entity and postal address to be confirmed]`,
  widget "cookie choices" (stesso storage key del banner), GPC/DNT onorati.

### Form canonico — campi
Obbligatori: `first_name, email, project_type, timeline, how_found`. Opzionali: `last_name, phone, site_location, referred_by,
message, consent_nurture ("yes"|assente → il JS manda "no" alla lead API)`. Hidden: `_gotcha, _subject, form_id, source_page,
page_type, referrer, landing_url, utm_source/medium/campaign/content/term, cta_src` (da `?src=` del link CTA).
Valori select (invariati rispetto allo storico Formspree): `project_type` ∈ {New custom build, Rebuild after fire,
Future site / land search, General interest}; `timeline` ∈ {Ready now, 6-12 months, 12-24 months, Exploring};
`how_found` ∈ {Google search, ChatGPT or another AI assistant, Instagram, LinkedIn, X, Press or article, Referral, Other}.
Submit: `Promise.allSettled([Formspree (FormData), lead API (JSON, cors)])` → se almeno uno ok → `gtag generate_lead
{form_id, how_found}` con `event_callback` + timeout 600 ms → `thank_you_url?form=&via=`; altrimenti errore inline con mailto.
Il payload JSON alla lead API aggiunge `submitted_at` (ISO).

### Eventi GA4 (mvTrack)
`cta_click {cta_id, page_type, …dataset}` su ogni `[data-ev]`, `form_start {form_id}` al primo focus, `contact_email_click`
sui mailto, `generate_lead {form_id, how_found}` al submit, `briefing_received {form_id, how_found}` sulla thank-you.
`cta_id` usati: `nav_briefing, nav_mobile_briefing, hero_briefing, hero_collection, ctx_resilience, ctx_process, ctx_team,
sticky_briefing, malibu_hero_brief, malibu_mid_brief, nav_brief, team_contact_brief, team_cta_brief, received_journal, received_home`.

## Come si lancia / QA
```
python3 _system/scripts/inject_conversion_layer.py --dry-run
python3 _system/scripts/inject_conversion_layer.py
python3 _system/scripts/inject_conversion_layer.py --shared team.html privacy.html private-briefing.html briefing-received.html \
   malibu-custom-home-builder.html beverly-hills-custom-home.html italian-villa-california-builder.html \
   icf-concrete-home-builder-los-angeles.html pacific-palisades-rebuild.html
python3 _system/scripts/optimize_media.py --dry-run
python3 _system/scripts/validate_links.py index.html private-briefing.html team.html malibu-custom-home-builder.html
```

## Cosa manca / da agganciare
- `update_sitemap.py` (data-assets): aggiungere `private-briefing.html` a `STATIC_ENTRIES` (priority 0.9). `briefing-received.html` NON va in sitemap (già assente).
- Lead API (lead-lifecycle): l'endpoint `https://content.myvilla.la/api/lead` riceve `POST application/json` cross-origin da
  `https://myvilla.la` → deve rispondere a `OPTIONS` (preflight) con `Access-Control-Allow-Origin: https://myvilla.la`,
  `Access-Control-Allow-Methods: POST, OPTIONS`, `Access-Control-Allow-Headers: Content-Type`, e `2xx` al POST.
  Finché non è deployato, il fetch fallisce silenziosamente e vale solo Formspree.
- `img/myvilla-logo.png` referenziato dallo schema (creato da seo-geo-journal).
- Titolare + indirizzo postale in `privacy.html` (placeholder visibile).
- v2 (`v2/index.html`): lo script non scrive; alla promozione ripetere le patch (anchor diversi: NAV_MOBILE, VIDEO, CTX_TEAM, TRUST_*, FORM, COOKIE da adattare).

## QA eseguita (2026-09-16)
- `py_compile` ok su `inject_conversion_layer.py` e `optimize_media.py`; script idempotenti (seconda esecuzione: "nessuna modifica").
- Parsing `html.parser` (bilanciamento tag) ok su index, private-briefing, briefing-received, team, privacy, malibu + 4 landing; JSON-LD valido ovunque; marker `DESK:*` e gtag `G-D6HJX7BNZN` intatti.
- `validate_links.py`: index 6/6, private-briefing 5/5, team 7/7, malibu 10/10, briefing-received 5/5, privacy 6/6 link esterni OK.
- Preview in `/tmp/myvilla-preview` su :8123 + screenshot headless Chrome 1440×900 (index, malibu, private-briefing, briefing-received) e viste mobile 375×812 nel Browser pane: hero CTA, sticky (sopra il banner, si alza di `--mv-banner-h`), CTA contestuali, trust note, FAQ, form (2 colonne desktop / 1 mobile), nessun overflow orizzontale (`scrollWidth` = 375).
- Test funzionali in pagina: `cta_click`, `form_start`, `generate_lead {form_id, how_found}` in `dataLayer`; error path (fetch falliti) → errore inline + bottone riabilitato, nessun `generate_lead`; success path → redirect a `briefing-received.html?form=home&via=…`; `mvCookieChoice('declined')` → `consent update analytics_storage denied`.
- Nota: gli screenshot headless con `--window-size=390` non emulano il viewport mobile (Chrome impone ~500px min): per il mobile usare l'emulazione del Browser pane.
