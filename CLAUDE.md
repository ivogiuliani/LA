# MyVilla Content System — Istruzioni per Claude

## Comportamento generale
- Procedi sempre autonomamente senza chiedere conferma a ogni step
- Completa tutte le fasi richieste in sequenza senza pause
- Chiedi conferma solo se c'è un'ambiguita reale o un rischio di perdita dati
- Rispondi in italiano

## Progetto
- Sito: https://myvilla.la/
- Root: questa directory
- Pipeline: `_system/scripts/` (radar → journal → social → validate)
- Bozze: `_drafts/journal/` e `_drafts/social/`
- Pubblicati: `blog/`
- Review server: `python3 _system/scripts/approve.py`

## Pipeline unica (IMPORTANTE)
- **Unico sistema attivo**: `_system/scripts/` in questa directory.
- La vecchia pipeline `../Engagement_Reports/pipeline/` è **archiviata** in `../Engagement_Reports/_archive_legacy/pipeline/` (non più aggiornata, non più schedulata).
- La scheduled task `myvilla-engagement-radar` (Claude Code, 07:52 daily) è **disabilitata** dal 2026-04-20.
- Se serve rischedulare: aggiornare la task esistente a puntare a `_system/scripts/radar.py` + `generate_radar_report.py` + `generate_journal.py` + `generate_social.py`.

## Sito v2 — "Quiet Permanence" (IN STAGING su `v2/`, NON live)
- **Stato (giugno 2026)**: il redesign completo è pronto e committato in `v2/` (homepage, team, Journal: 82 articoli + index + 6 category hub), tutte le pagine **noindex** con canonical → root. Reviewabile su https://myvilla.la/v2/ . **La root serve ancora il sito v1** e la pipeline quotidiana continua a usare i renderer storici (`update_journal_index.py` con renderer interno + `update_homepage_journal.py` a parsing HTML).
- Design system v2: capitoli numerati con plate-header, Cormorant Garamond + Montserrat, palette ink/cream/sand/terracotta, reveal a sipario, parallax, `prefers-reduced-motion` rispettato. Form briefing: stesso endpoint Formspree `mgoljyjl`, honeypot `_gotcha`, evento GA4 `generate_lead`.
- **Builder Journal v2**: `_system/scripts/build_v2.py` rigenera articoli + index + category **dai sidecar `blog/*.json`** (source of truth: `body_html`, `our_perspective`, `key_data`, `sources`, `_date`, `_section_id`; JSON senza HTML gemello = bozza, saltata). Include il ranking featured commerciale (strategia §4).
  - senza flag → staging in `v2/blog/` (noindex) — uso corrente
  - `--root` → scrive in `blog/` indicizzabile — SOLO alla promozione
- **Procedura di promozione (quando il v2 viene approvato)**:
  1. copiare `v2/index.html` e `v2/team.html` a root togliendo il blocco `<!-- PREVIEW FLAG -->` + noindex;
  2. `python3 _system/scripts/build_v2.py --root`;
  3. far delegare `update_journal_index.py` main() a `build_v2.run(root=True, live=True)` e passare `update_homepage_journal.py` alla lettura dei JSON sidecar (le due versioni v2-ready sono nella history: commit `8535c48`);
  4. `update_homepage_journal.py` + `update_sitemap.py`; QA link/schema; push.
- I marker `<!-- DESK:{INSURANCE,FIRE_CODE,REBUILD,MARKET}:START/END -->` esistono sia nella home v1 sia nella v2 — **non rimuoverli mai**.
- Per modifiche al design v2 in review: editare `v2/index.html` / `v2/team.html` direttamente (file hand-authored); per il Journal v2 modificare i template in `build_v2.py` e rilanciarlo senza flag.

## SEO
- Ogni pagina pubblicata deve avere: meta description, keywords, canonical, OG, Twitter Card, Schema.org (Article + BreadcrumbList)
- Aggiornare sempre `sitemap.xml` dopo ogni pubblicazione (include anche le 6 category hubs)
- Obiettivo principale dei contenuti: indicizzazione Google
- FAQPage schema presente in homepage (sezione Investment) — tenerlo allineato alle FAQ visibili

## Output
- Ogni radar scan deve produrre sia il **file HTML dashboard** sia il **file `.md`** giornaliero (via `generate_radar_report.py --markdown`)
- Il `.md` sostituisce il vecchio digest leggibile che veniva prodotto da `radar_pipeline.py`

## "Our Perspective" — regole di voce (blog/Journal)
- La voce è **My Villa in prima persona** ("we", "our", "at My Villa")
- **Mai citare Paolo Mezzalama per nome** nelle Our Perspective (né bio, né frasi, né attribuzioni)
- **IT'S Architecture** (sempre con "S" maiuscola, mai "IT's" o "It's"): nominarlo solo se strettamente necessario all'argomento; di default ometterlo (è irrilevante quando a parlare è My Villa)
- Sedi IT'S: **Rome · Paris · LA opening soon** (LA è in apertura, My Villa è la pratica LA)
- Partner come DGU, Transsolar, i riferimenti a Kimbell/Palazzo Grassi/Harvard/Mercedes-Benz Museum restano ammessi come credenziali di sistema (non sono nomi personali)
- Fonti dei fatti: solo `_system/knowledge/project_brief.md` + `_system/knowledge/site_content.md`

## Email di outreach — invio automatico via Gmail API
- **Modalità**: LIVE (real send) dal 2026-04-22. `_system/outreach/config.yml` ha `dry_run: false`.
- **Mittente**: `info@myvilla.la` (Google Workspace), autenticato via OAuth2 Desktop app
- **Credenziali**: `_system/outreach/credentials/oauth_client.json` + `token.json` (gitignored)
- **Rate limit**: 10 invii/ora (soft, locale)
- **Policy layer**: `_system/scripts/send_email.py` (signature injection Lisa Monelli, log JSONL, dry_run switch, **blacklist guard**)
- **Gmail wrapper**: `_system/scripts/gmail_client.py` (OAuth refresh, send, list)
- **Dashboard UI**: bottone verde "Send now" su ogni card Email Ready in approve.py → POST `/api/send-email`
- **Log**: `_system/outreach/send_log.jsonl` (una riga per tentativo, success o failure)
- **Per sospendere temporaneamente gli invii reali**: flip `dry_run: true` in `config.yml` (niente altro da cambiare)
- **Primo invio reale verificato**: message_id `19db733d29ac8993` (2026-04-22 21:58 UTC, self-send a info@myvilla.la)

## Email verification & bounce prevention (Aprile 2026)
- **Problema rilevato (2026-04-23)**: bounce rate storico 67% (4 su 6 invii reali). Cause: uso di indirizzi `opus`-inferiti (hallucination del LLM) e `pattern_guess` (firstname.lastname@domain) senza verifica. I `editorial_fallback` hanno delivered, i nomi specifici hanno bounciato.
- **Apollo integration** (`_system/scripts/apollo_lookup.py`): step 0 in `generate_radar_report.py` — chiama Apollo People Match API con `reveal_personal_emails=true` prima di qualsiasi fallback. Cache on-disk in `_system/outreach/apollo_cache.json` (TTL 30 giorni) per non bruciare crediti. **Graceful**: se `APOLLO_API_KEY` non è settato in `.env`, skip silente e fallthrough a pattern/editorial.
- **email_source** classifica la qualità dell'indirizzo:
  - `apollo` (verde, Send enabled) — Apollo `email_status: verified`
  - `apollo_likely` (giallo, confirm step extra) — Apollo `email_status: likely/guessed/extrapolated/unverified`
  - `editorial_fallback` (verde, Send enabled) — indirizzo redazione curato in `EDITORIAL_EMAILS`
  - `opus` (rosso, Send DISABLED di default, richiede override `⚠️ Send anyway`) — LLM-inferito
  - `pattern_guess` (rosso, Send DISABLED) — firstname.lastname@domain
- **Blacklist bounce**: `_system/outreach/invalid_addresses.json` mantenuto da `reply_monitor.py`. Ogni DSN rilevato estrae l'indirizzo fallito dal body (RFC-3464 Final-Recipient, Gmail-style "message wasn't delivered to", angle-bracketed), con motivo + bounce_count + source_threads.
- **Guard integrato** (`send_email.send_raw`): prima di ogni send controlla `is_invalid_address(to)` → se true, ritorna `reason=blacklisted`, HTTP 409 alla dashboard. Evita retry su indirizzi già bruciati.
- **UI dashboard**: ogni card email-ready mostra risk banner (rosso = blocked, arancione = risky, giallo = confirm) + badge bounced (`❌ (bounced)`) quando l'indirizzo è in blacklist.
- **Integrazione radar generator**: anche se Apollo è settato, se il risultato è un indirizzo già in blacklist viene scartato e si passa al fallback successivo. Stesso per pattern_guess + editorial_fallback.
- **Per attivare Apollo**: aggiungere `APOLLO_API_KEY=xxx` al `.env`. Nessuna altra configurazione.

## Email di outreach ai giornalisti — regole di voce
- **Canonical voice file**: `_system/knowledge/outreach_voice.md` (letto a runtime da `generate_radar_report.py`)
- Prima mail = **aprire una conversazione**, NON vendere, NON pitch
- **Tono amichevole e personale** (scrivere a un collega, non a un editor via PR)
- **Mission da comunicare sempre**: portare a LA la resilienza costruttiva europea + il design e la vivibilità italiana degli spazi, in case costruite in cemento armato a vista (`cemento a vista`)
- **NON** menzionare "Rome, Paris, LA opening soon" nella prima mail (evidenzia la debolezza)
- **NON** descriverci come "a Los Angeles studio" / "an LA studio" (suona fuorviante dato il track record locale ancora in formazione)
- Dire: "at My Villa, we bring..." → lasciare parlare la mission, senza qualificatori geografici dello studio
- **UN SOLO follow-up angle** per mail (mai 2 o 3)
- Follow-up = osservazione big-picture, NON mapping tecnico tra certificazioni
- **NIENTE allegati nella prima mail** — no press kit, no fact sheet, no PDF. Il materiale si offre dopo, se il giornalista mostra interesse
- **Chiusura obbligatoria con domanda aperta** terminante in `?` — invito low-pressure a ricevere più materiale o organizzare una call con Paolo Mezzalama
- Paolo Mezzalama si nomina SOLO nella domanda di chiusura (non prima)
- Lunghezza: 80-130 parole body, hard max 150
- Subject: 8-12 parole, no em-dash (usare virgole/punti)
- Firma: `Lisa Monelli / My Villa Media Team / info@myvilla.la · myvilla.la`

## Follow-up alle risposte dei giornalisti — semi-automatico
- **Flusso a due tempi**: prima mail (first-touch) → risposta del giornalista → bozza di reply → review umana → invio threaded via Gmail.
- **Monitor**: `_system/scripts/reply_monitor.py` polling Gmail su tutti i thread con un outreach inviato (stato in `_system/outreach/replies/<thread_id>.json`). Filtra automaticamente bounces (`mailer-daemon`, `postmaster`, `Delivery Status Notification`, `undeliverable`, ecc.) e auto-reply (`Auto-Submitted`, `X-Autoreply`, `Precedence: auto_reply/bulk/junk`) → non generano mai draft.
- **Drafter**: `_system/scripts/reply_drafter.py` classifica l'intent del giornalista e produce una bozza via Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`). Cinque pattern: `request_material` (Pattern A — manda press kit + fact sheet), `request_call` (Pattern B — proponi 3 slot 30-min su Google Meet con Paolo), `request_both` (Pattern C — combo), `polite_decline` (Pattern D — ringrazia, lascia la porta aperta, NIENTE attachments), `needs_human` (stub — richiede intervento manuale, Send disabilitato in UI).
- **Voce canonica reply**: `_system/knowledge/reply_voice.md` (letto dal drafter a runtime). Regola chiave: NON ri-pitchare la mission — è già stata detta nella prima mail. Corpo breve, sourced reassurance (CAL FIRE/CDI/FEMA/IBHS), stesse parole vietate di `outreach_voice.md`.
- **Attachments canonici**: `_system/outreach/attachments/MyVilla_Press_Kit.pdf` (3.0 MB) + `_system/outreach/attachments/MyVilla_Fact_Sheet.pdf` (63 KB). Pre-checkati in UI per Pattern A/B/C, disattivi per Pattern D/needs_human.
- **Threading**: `send_email.send_reply()` invia dentro lo stesso thread Gmail (`threadId` + `In-Reply-To` + `References`). Stessa policy layer della prima mail: signature Lisa Monelli, rate limit 10/h, dry_run, JSONL log con `kind: "reply"`.
- **Dashboard**: sezione `↩️ Replies` in cima a `approve.py` (priorità massima). Ogni card mostra: classification badge, confidence, messaggio del giornalista (collassabile), reasoning del drafter, subject+body editabili, checkbox attachments, bottoni Send reply / Re-draft / Dismiss. Bottone `🔄 Scan replies` nell'header per polling on-demand.
- **Confidence threshold**: se il drafter restituisce `confidence < 0.6` downgrade automatico a `needs_human` (safer default).
- **Archivio**: draft inviati finiscono in `_drafts/email_replies/_dismissed/sent/`, quelli scartati in `_drafts/email_replies/_dismissed/` (reversibile, mai eliminati).
- **Endpoint API** (wired in approve.py): POST `/api/scan-replies`, `/api/send-reply`, `/api/redraft-reply`, `/api/dismiss-reply`.

## Link esterni
- Ogni articolo deve passare `python3 _system/scripts/validate_links.py <file>` prima della pubblicazione
- I link rotti vanno eliminati o sostituiti, non lasciati: usare `--fix` per unwrap automatico
- Regola preventiva: `generate_journal.py` deve chiamare il validator come ultimo step prima di scrivere l'HTML finale

## Google Analytics
- Ogni pagina HTML pubblicata (articoli, blog index, categorie, team, homepage) deve includere il blocco gtag.js con `G-D6HJX7BNZN` subito dopo `<head>`
- I template in `generate_journal.py` e `update_journal_index.py` già lo iniettano — non rimuoverlo
