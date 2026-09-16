# Fase 2 — modulo `seo-geo-journal` (handoff)

Data: 2026-09-16. Agente: seo-geo-journal. Perimetro: robots.txt, llms.txt,
schema/CTA degli articoli del Journal, template dei nuovi articoli, monitor GEO.

## 1. Cosa c'è

| Cosa | File | Stato |
|---|---|---|
| robots.txt corretto | `robots.txt` | fatto, verificato con `urllib.robotparser` |
| Logo per schema.org | `img/myvilla-logo.png` (512×512), `img/myvilla-logo-wide.png` (1200×400) | creati da `team/social/assets/brand/*.png` con PIL |
| Batch schema articoli | `_system/scripts/fix_article_schema.py` | eseguito su 128 articoli |
| Batch CTA articoli | `_system/scripts/inject_article_cta.py` | eseguito su 128 articoli |
| Template nuovi articoli | `generate_journal.py`, `build_v2.py`, `update_journal_index.py` | aggiornati (importano le CTA da `inject_article_cta.py`) |
| llms.txt rigenerabile | `_system/scripts/build_llms.py` → `llms.txt` | eseguito |
| Monitor GEO | `_system/scripts/geo_monitor.py`, `_system/research/geo/queries.yml`, `runs/<date>.json`, `summary.json` | baseline eseguita |
| Workflow GEO | `.github/workflows/geo-monitor.yml` (lunedì 07:00 UTC) | pronto, usa secrets `ANTHROPIC_API_KEY` + `GEMINI_API_KEY` |

### robots.txt
Ogni gruppo per-User-agent (GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot,
Claude-SearchBot, Claude-Web, anthropic-ai, PerplexityBot, Perplexity-User,
Applebot-Extended, Google-Extended, Bingbot, CCBot, cohere-ai) ripete
`Disallow: /old/ /team/ /v2/` PRIMA di `Allow: /` (i parser first-match-wins
come `urllib.robotparser` altrimenti applicano Allow a tutto). Due Sitemap:
`sitemap.xml` e `news-sitemap.xml` (quest'ultimo lo genera data-assets).

### fix_article_schema.py (idempotente)
Per ogni `blog/<slug>.html` (escluso index): `publisher.logo.url` →
`https://myvilla.la/img/myvilla-logo.png`; `dateModified` = `datePublished` se
manca; `author` = Organization "My Villa Editorial Team" → team.html; canonical
= `https://myvilla.la/blog/<slug>.html`; `<meta name="robots">` =
`index,follow,max-image-preview:large,max-snippet:-1`.
**Eccezione**: i 33 articoli potati da `seo_prune.py` (robots `noindex, follow`
+ canonical verso il pillar del cluster) mantengono robots e canonical
(riceve solo logo/author/dateModified). Le sostituzioni nel JSON-LD sono
regex chirurgiche; il blocco viene ri-parsato con `json.loads` prima di
scrivere.

### inject_article_cta.py (idempotente, byte-per-byte)
- MID-CTA tra `<!-- MV-MIDCTA:START/END -->` dopo il 3° paragrafo (2° se
  corto; prima di Our Perspective se cortissimo): "Planning a home in Malibu or
  Beverly Hills? Request a private briefing with our founder." → 
  `private-briefing.html?src=journal_mid&slug=<slug>`, classe `.mv-midcta`,
  CSS inline, `data-ev="journal_cta_click" data-cta="mid"`. Il blocco esistente
  viene sostituito a ogni run: cambiare il testo nello script e rilanciare
  propaga a tutto l'archivio.
- CTA finale "Request a Briefing" → `?src=journal_end&slug=<slug>` con
  `data-cta="end"`. "Explore My Villa" resta la home.
- `<!-- MV-CTAJS:START/END -->` prima di `</body>`: listener che manda a GA4
  l'evento `journal_cta_click` con `{cta, slug}` (usa il gtag già presente).
- Il sidecar `blog/*.json` resta pulito: la CTA vive solo nell'HTML renderizzato.

### Template
- `generate_journal.py`: `from inject_article_cta import inject_mid_cta,
  end_cta_href, CTA_JS_BLOCK` (con fallback se manca); robots meta e logo
  corretti; mid-CTA iniettato in `render_article_html` dopo
  `_wrap_key_data_blocks`; CTA finale e JS nel template.
- `build_v2.py`: stesso import; `cta_band_html(depth, slug=None)` → con slug
  punta alla landing; author/publisher corretti; robots non-preview con
  max-image-preview; mid-CTA nel `.a-body`; JS prima di `</body>`. **Non ho
  rigenerato `v2/blog/`** (staging): farlo con `python3 _system/scripts/build_v2.py`.
- `update_journal_index.py`: robots meta su index + category; CTA finale →
  `?src=journal_index` / `?src=journal_category&slug=<section>`. Eseguito:
  `blog/index.html` e le 6 category rigenerate (le category insurance/materials
  mostrano anche il riordino per gli articoli pubblicati dopo l'ultimo run).

### build_llms.py
Legge `lead_settings.yml` (brand.*, canonical.*), la meta description da
`site_content.md`, `<title>`+description delle 5 landing, i 6 category hub, gli
ultimi 20 articoli INDICIZZABILI (esclusi i noindex). Sezione "How to engage"
con landing, email, promessa di risposta, `booking_url` se valorizzato, e il
disclaimer "not yet delivered". Data di aggiornamento in testa.

### geo_monitor.py
20 query in `queries.yml` (gruppi: brand/category/cost/insurance/design/rebuild).
Motori: Claude (`web_search_20260209`, max_uses 1, `resolve("balanced")`) e
Gemini REST (`gemini-2.5-flash` → fallback `gemini-2.0-flash`, `google_search`).
Per risposta: testo, URL citati, `cited_myvilla` (dominio nel testo o nelle
fonti), `mentioned_myvilla`, `fact_check.wrong_claims` (Claude `resolve("cheap")`,
solo se il brand compare). Output `runs/<date>.json` + `summary.json`
(citation share per motore, storico). Costo stimato per run ≈ $3-4
(Claude ~35k token input per query con i risultati di ricerca).
Nota Gemini: gli URL di grounding sono redirect `vertexaisearch…`; il dominio
si legge dal `title` del chunk (es. "myvilla.la").

## 2. Come si lancia

```
python3 _system/scripts/fix_article_schema.py [--dry-run] [--json]
python3 _system/scripts/inject_article_cta.py  [--dry-run] [--json]
python3 _system/scripts/build_llms.py          [--dry-run] [--limit 20]
python3 _system/scripts/geo_monitor.py         [--dry-run] [--engine claude|gemini|both] [--limit N] [--date YYYY-MM-DD]
python3 _system/scripts/build_v2.py            # rigenera lo staging v2/blog con le nuove CTA
```

Hook consigliati nella pipeline quotidiana (dopo `publish_all_drafts.py`, prima
di `update_sitemap.py`): `fix_article_schema.py` → `inject_article_cta.py` →
`build_llms.py`. Tutti exit 0, idempotenti, nessuna chiamata API tranne
`geo_monitor.py`.

## 3. Cosa manca / attenzione
- `private-briefing.html` (landing) e `briefing-received.html` sono di
  conversion-layer: finché non sono live le CTA puntano a un 404. Pubblicare i
  due moduli nello stesso commit.
- L'evento GA4 `journal_cta_click` va registrato come conversione/evento chiave
  in GA4 (azione manuale di Ivo).
- Il fact-check GEO è indicativo: nel test iniziale ha segnalato come "sbagliata"
  la frase "designs and builds" presa dalle nostre landing. Il prompt ora
  flagga solo contraddizioni; rivedere i `wrong_claims` a mano nel run.
- `news-sitemap.xml` referenziato in robots.txt esiste solo se data-assets lo
  genera; finché manca è un riferimento a un 404 (innocuo per i crawler).
- `seo_prune.py` scrive `noindex, follow` senza max-image-preview: coerente con
  fix_article_schema (che non tocca i potati).
