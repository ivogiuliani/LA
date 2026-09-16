# Fase 2 — Modulo backlink-engine (handoff, 2026-09-16)

Motore backlink di My Villa: dati, script, materiali e workflow per passare da
0 referring domain (baseline settembre 2026) a 25 entro marzo 2027 (min 18, >=10 dofollow).
Nessuno script di questo modulo invia email da solo: l'unico invio possibile è
`press_submit.py --send`, che passa dal policy layer `send_email.py` con kind
`submission` e resta bloccato finché `submission` è in `dry_run_kinds` e
`brand.postal_address` è vuoto in `_system/config/lead_settings.yml`.

## Cosa c'è

| Percorso | Cosa |
|---|---|
| `_system/backlinks/prospects.yml` | 40 prospect in 8 motori (network 7, editorial 10, press_it 7, directory 3, journo 3, asset 5, award 1, podcast 4). 17 `todo` + 6 `drafted`, 17 `needs_contact` (contatto non verificato → vuoto). Contatti solo caselle generiche/form verificati con fetch il 2026-09-16. |
| `_system/backlinks/ledger.jsonl` | eventi (check/live/lost/mention/submission_draft/journo_*/resource_audit/linkedin_founder_draft). Mai PII. |
| `_system/backlinks/status.json` | fotografia dell'ultimo `backlink_check` (KPI + per prospect + next_actions). |
| `_system/backlinks/mentions.json` | menzioni trovate (Brave/CSE), dedup per URL, linked/unlinked. |
| `_system/backlinks/resource_pages.yml` | 31 pagine risorse .gov/.org/.edu (LA County Recovers, ca.gov/LAfires, Malibu, UC ANR, PPCC, Malibu Rebuilds, Altadena, LA Rises, IBHS…). |
| `_system/backlinks/resource_audit.json` | esito audit link rotti (run 2026-09-16: 31 pagine, 20 con link rotti). |
| `_system/backlinks/backlinklib.py` | helper condiviso: fetch con UA onesto, parsing `<a href>` verso myvilla.la (rel/anchor), ledger, prospects, Claude via `model_resolver.resolve(tier)`, lettura Journal (`blog/*.json`). |
| `_system/backlinks/README.md` | formato dei file dati. |
| `_system/scripts/backlink_check.py` | check link live/lost + `status.json` + `digest_block()` HTML "Link e citazioni". `--discover` (GA4: skip con messaggio), `--bing` (BING_WMT_API_KEY, altrimenti skip), `--digest`. |
| `_system/scripts/mention_monitor.py` | Brave + Google CSE; esclude domini nostri, omonimi (`my-?villa*` ≠ myvilla.la, villa.edu, villaforyou, myprivatevillas…) e piattaforme social/store; unlinked → prospect `needs_contact`. |
| `_system/scripts/resource_page_audit.py` | link esterni di ogni pagina risorse testati con `validate_links.check_url`; bozze in `_drafts/backlinks/resource_outreach/<id>.md` (offerta tracker/checklist). |
| `_system/scripts/journalist_requests.py` | Gmail label `MV/JournoRequests` (creata il 2026-09-16), parser tollerante SOS/Featured/Qwoted, scoring sui cluster di `radar-keywords.yml`, bozze in voce Paolo (`resolve("heavy")`) in `_drafts/journo_requests/`, tetto 3/settimana, mai invio. |
| `_system/scripts/press_submit.py` | `--list`, `--mode dossier --typology …`, dry-run di default (bozze in `_drafts/backlinks/submissions/`), `--send` con gate (kind `submission`, tetto `budgets_per_day.submission` ∧ `cadence.max_per_day`, backoff 30 gg, esclusiva Dezeen 7 gg, CAN-SPAM). |
| `_system/scripts/linkedin_founder_drafts.py` | 1 post/settimana per Paolo (150-220 parole, dato del Journal, link in fondo) in `_drafts/linkedin_founder/<date>.md`. Prima bozza generata: `2026-09-16.md` (211 parole). |
| `_system/scripts/feature_pitch.py` (riparato) | `no_email` → `paused` con backoff 30 gg per outlet (prima 891 retry inutili); parser risposta Claude robusto ai blocchi non-text (KeyError `'text'`); prompt senza "current build"/opere costruite, con vincolo "concept design". Comportamento di default invariato. |
| `_system/outreach/press_submissions.yml` | 10 testate (Dezeen, ArchDaily unbuilt, designboom, Divisare, Metalocus/BowerBird, e-architect, ArchEyes, Amazing Architecture, Architizer, Archello) con how/url/requirements/accepts_unbuilt/exclusive_days/contact. |
| `_system/outreach/press_it.yml` | scaffold 8 testate/istituzioni IT-FR (Artribune, Giornale dell'Architettura, professioneArchitetto verificate; IoArch, IIC LA, ICE LA da verificare; THE PLAN e Chroniques via form). |
| `_drafts/backlinks/dossier_{courtyard,hill,l,deconstructed}.md` | dossier 800/500/300 parole + crediti (IT'S Architecture, DGU, Transsolar KlimaEngineering, BUROMILAN) + lista immagini da `img/`. Sempre "concept design". |
| `_drafts/backlinks/partner_requests/01…06` | 6 richieste firmate Paolo (its.vision, BUROMILAN, Cooperative LA Perspective co-firmato, Transsolar, DGU, istruzioni profili Archilovers/Archello), anchor "My Villa" → team.html. |
| `_drafts/backlinks/press_release_{it,en,fr}.md` | comunicato ~330-380 parole, storia NAJAP 2008 → LA, dato CDI (12 misure Safer from Wildfires) + FAIR Plan +29,1% dal 15/10/2026 (fonti: key_data del Journal). |
| `_drafts/backlinks/podcast_pitches.md` | 6 pitch (Second Studio, EntreArchitect, Business of Architecture, Archispeak, DESIGN:ED, USModernist) + 3 domande ciascuno. |
| `_drafts/backlinks/submissions/2026-09-16-*-courtyard.md` | 10 bozze di submission (dry-run di test). |
| `.github/workflows/weekly-backlinks.yml` | lunedì 06:00 UTC: backlink_check `--bing` + mention_monitor (+ resource_page_audit nelle settimane ISO pari); commit `[skip ci]` dei soli `_system/backlinks/*.json` + `ledger.jsonl`. |

## Come si lancia

```bash
python3 _system/scripts/backlink_check.py [--only ID] [--dry-run] [--discover] [--bing] [--digest]
python3 _system/scripts/mention_monitor.py [--dry-run] [--recheck]
python3 _system/scripts/resource_page_audit.py [--only ID] [--dry-run] [--max-links 80]
python3 _system/scripts/journalist_requests.py [--days 3] [--threshold 4] [--max 3] [--dry-run] [--setup-help]
python3 _system/scripts/press_submit.py --list
python3 _system/scripts/press_submit.py --mode dossier --typology courtyard            # bozze
python3 _system/scripts/press_submit.py --typology courtyard --outlet dezeen --send    # invio reale (gate)
python3 _system/scripts/linkedin_founder_drafts.py [--days 7] [--dry-run] [--force]
python3 _system/scripts/feature_pitch.py --dry-run --max 1
```

## Agganci per il coordinatore

- **Digest** (`publish_all_drafts.py`, riga ~1982 dove il template inserisce `{pitch_block}`): aggiungere prima
  `backlink_block = ""` + `try: from backlink_check import digest_block; backlink_block = digest_block()` (`except Exception: pass`),
  e nel template `{backlink_block}` subito dopo `{pitch_block}`. Nessuna rete: legge `status.json`.
- **Gmail filtro** (Ivo): Da `(sourceofsources.com OR featured.com OR helpareporter.com OR qwoted.com)` → etichetta `MV/JournoRequests`, salta inbox.
  Iscrizioni con info@myvilla.la: SOS, Featured (HARO digest), Qwoted free.
- **launchd / daily_publish.sh**: `journalist_requests.py` quotidiano (dopo il digest), `linkedin_founder_drafts.py` lunedì.
- **Secrets GitHub**: `BING_WMT_API_KEY` (opzionale; BRAVE/CSE già presenti).
- **send_email.py** (lead-lifecycle): il kind `submission` deve leggere `budgets_per_day.submission` e `dry_run_kinds` da lead_settings; press_submit applica già gli stessi gate in locale.

## Cosa manca / decisioni

- 17 prospect `needs_contact`: caselle da confermare a mano (Dezeen press@, ArchEyes hello@, Amazing Architecture info@, e-architect info@, IoArch, IIC LA desk@, ICE LA, EntreArchitect podcast@, DGU sito, Cooperative LA e Transsolar via referente diretto di Paolo).
- Google CSE risponde 403 (ticket aperto, vedi `_system/docs/google_support_ticket_cse_403.md`): mention_monitor usa solo Brave finché non si risolve.
- `--discover` (referral GA4) richiede Analytics Data API + service account: predisposto, non attivo.
- Le pagine che offrono il "tracker" puntano a `https://myvilla.la/research/` (modulo data-assets) e la checklist a `insurable-home-california.html` (data-assets): non inviare le bozze `resource_outreach` prima che quelle pagine siano live.
- Award Architizer A+ Unbuilt: spesa (~283 USD early, deadline da riverificare) decisa da Ivo.
- Profili Architizer/AIA|LA: parere legale B&P §5536 prima di pubblicare qualsiasi profilo con "architect".
- Il workflow non committa `prospects.yml` (gestione locale): backlink_check legge il ledger per non ri-segnalare i link già visti.
