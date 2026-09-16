# Fase 2 — modulo `data-assets` (handoff)

Data: 2026-09-16 · Agente: data-assets · Perimetro: asset linkabili + feed.

## Cosa c'è

| Componente | File | Stato |
|---|---|---|
| Westside Rebuild Tracker (script) | `_system/scripts/rebuild_tracker.py` | primo run reale eseguito |
| Tracker — dati | `research/data/westside-rebuild-permits.csv` (riga per permesso), `research/data/westside-rebuild-permits.json` (meta + aggregati + righe), `research/data/westside-rebuild-history.json` (snapshot settimanali) | generati |
| Tracker — grafico | `research/img/westside-rebuild-tracker.png` (1200×720, matplotlib, palette carta/inchiostro/terracotta, fonte e data in calce) | generato |
| Tracker — pagina pubblica | `research/westside-rebuild-tracker.html` (gtag, meta, canonical, OG/Twitter, schema Dataset + BreadcrumbList, metodo, limiti, licenza, Cite/Embed, download, CTA → private-briefing) | generata, 10 link esterni OK |
| Workflow settimanale | `.github/workflows/rebuild-tracker.yml` (lunedì 05:30 UTC, `pip install matplotlib`, commit `[skip ci]` dei soli `research/**`) | pronto, nessun secret |
| Answer page (script) | `_system/scripts/build_answer_page.py` | eseguito |
| Answer page | `insurable-home-california.html` (root, indicizzabile; 10 fatti chiave + 3 fatti Zone 0, tutti verificati contro i sidecar; estimator JS con SOLE le percentuali 16.4% e "up to 50%" presenti nei key_data; FAQ 7 + FAQPage; hub 33 articoli; schema Article + FAQPage + ItemList + BreadcrumbList) | 53 link esterni OK |
| Sitemap / feed / news | `_system/scripts/update_sitemap.py` → `sitemap.xml` (112 URL, lastmod reali), `feed.xml` (Atom, 30 articoli, hero come enclosure), `news-sitemap.xml` (articoli ultime 48 h, "My Villa Journal", en) | generati, XML validi |
| IndexNow delta | `_system/history/indexnow_last.json` (stato loc → lastmod; si invia solo il delta) | primo invio: 112 URL, HTTP 200 |

## Sorgente dati del tracker (verificata 2026-09-16)

- Portale: `https://data.lacity.org` (Socrata SODA, senza token, paginazione 5000).
- Dataset primario: `gwh9-jnip` "Building and Safety - Building Permits Submitted from 2020 to Present" (contiene anche i permessi in plan check). Companion: `pi9x-tg5x` (Issued).
- Campi usati: `permit_nbr, primary_address, zip_code, cpa, hl, permit_type, permit_sub_type, use_desc, submitted_date, issue_date, cofo_date, status_desc, status_date, square_footage, valuation, construction, height, work_desc, lat, lon`.
- **Il dataset HA il tipo costruttivo** (`construction`: Type V-A/V-B legno, Type I/II non combustibile, Type III/IV). Al primo run: 2.719 permessi, 91,2 % Type V, 0,4 % Type I/II, 8,3 % non dichiarato.
- Flag rebuild: `work_desc` che contiene "WILDFIRE REBUILD" (dicitura LADBS).
- Filtro: `permit_type='Bldg-New'`, `permit_sub_type='1 or 2 Family Dwelling'`, `submitted_date >= 2025-01-08`, ZIP 90272 / 90049 / 90077+90210 (solo City of LA) / 91316+91356+91436.
- **Malibu non è in LADBS** (città incorporata, permessi propri); nemmeno Topanga/aree non incorporate (LA County) né Beverly Hills città. Dichiarato in pagina e nel JSON.
- Licenza: record pubblici della City of Los Angeles (terms of use del portale); aggregazione/grafico My Villa CC BY 4.0; disclaimer "as is".

## Come si lancia

```bash
# Tracker (run reale; --dry-run non scrive; --offline rigenera PNG/JSON/HTML dal CSV)
python3 _system/scripts/rebuild_tracker.py

# Answer page (dateModified cambia SOLO se il contenuto cambia; datePublished conservato)
python3 _system/scripts/build_answer_page.py

# Sitemap + feed.xml + news-sitemap.xml + IndexNow delta (MYVILLA_INDEXNOW=0 per saltare)
python3 _system/scripts/update_sitemap.py
python3 _system/scripts/update_sitemap.py --dry-run
```

## Note di progetto

- `update_sitemap.py` resta compatibile con i chiamanti esistenti (`publish_all_drafts.py`, `approve.py`: nessun argomento). Novità: lastmod articoli da sidecar (`dateModified` > `_date` > datePublished HTML, mai prima della pubblicazione); lastmod statiche/categorie = data dell'ultimo commit git del file (today se il file ha modifiche non committate; mtime solo se non tracciato) — così funziona anche sul runner GitHub dove il checkout azzera gli mtime.
- Le 16 note insurance in `noindex` (potatura SEO) restano linkate nell'hub dell'answer page (sono live) ma non entrano in sitemap/feed.
- Le pagine consumer CDI "Safer from Wildfires" (`insurance.ca.gov/01-consumers/...cfm`) sono 404 dopo il restyling del sito CDI: per i fatti "12 misure" e "3 layer" l'answer page usa le fonti alternative già presenti nei sidecar (Insurance Journal, Coverage Cat); il fatto "Class A roof" è stato escluso dalla checklist Zone 0 perché tutte le sue fonti sono 404/403.
- `build_answer_page.py` legge `research/data/westside-rebuild-permits.json` (se esiste) per la quota di permessi non combustibili: rilanciarlo dopo il tracker aggiorna quel numero.

## Cosa manca / decisioni per il coordinatore

- Aggiungere `Sitemap: https://myvilla.la/news-sitemap.xml` (e opzionalmente `feed.xml`) a `robots.txt` — file di seo-geo-journal.
- Agganciare `build_answer_page.py` nella lista script post-pubblicazione (`publish_all_drafts.py`, prima di `update_sitemap.py`) perché l'hub e il numero del tracker si aggiornino da soli.
- Registrare `news-sitemap.xml` e `feed.xml` in Google Search Console (azione Ivo).
- Link interni verso le due nuove pagine da homepage / landing Palisades / articolo `wildfire-rebuild-tracker-california-permits-2026` (perimetro conversion-layer e seo-geo-journal).
- Eventuale estensione del tracker a Hollywood Hills (90046/90068) se utile: già verificato che i dati ci sono.
