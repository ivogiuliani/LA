# Fase 2 — Lead lifecycle (handoff, 2026-09-16)

Ciclo di vita automatico del lead: form/chat/mailto → registro privato →
tier → ack al lead entro 5 minuti → alert interno → desk nel pannello →
digest "In attesa di te". Nessun dato personale in git: tutto ciò che
contiene email/telefoni vive nel **private dir** (`MYVILLA_PRIVATE_DIR`,
default `~/.myvilla-private`; VPS `/opt/myvilla-private`).

Fonte unica di testi e parametri: `_system/config/lead_settings.yml`.

## Cosa c'è

| File | Ruolo |
|---|---|
| `_system/scripts/lead_settings.py` | loader dello YAML + `private_dir()` (usato da tutti i moduli) |
| `_system/scripts/send_email.py` | policy layer esteso: kind, tetti/giorno, dry_run per kind, gate CAN-SPAM, suppression list, `signature_override` |
| `_system/scripts/lead_ledger.py` | registro append-only `leads/leads.jsonl` + `leads/index.json`, stati e transizioni, CLI, retention |
| `_system/scripts/lead_score.py` | tier A/B/C: regole (`lead.scoring` nello YAML) + rifinitura Claude `resolve("balanced")` sui soli campi progetto |
| `_system/scripts/lead_ack.py` | ack fisso da `_system/knowledge/lead_ack_voice.md` (kind `lead_ack`, firma prospects) + alert senza PII (kind `lead_alert`) |
| `_system/scripts/lead_intake.py` | poller Gmail (label `MV/Lead-Processed` come claim), parser Formspree testo/HTML, `--add-manual`, `--simulate`, `--ephemeral`, `--rebuild-from-label` |
| `_system/scripts/lead_desk.py` | sezione "Leads" del pannello (owner-only), `POST /api/lead-state`, `digest_block()` |
| `_system/scripts/approve.py` | hook minimo (3 righe di render + 1 route) verso `lead_desk` |
| `_system/scripts/lead_api.py` | `POST /api/lead` (127.0.0.1:8788): CORS myvilla.la, honeypot, rate-limit 5/min/IP, dedup 10 min, DNS + blocklist usa-e-getta |
| `_system/scripts/chat_api.py` | `POST /api/chat` (127.0.0.1:8789): solo `_system/knowledge/chat_faq.md`, disclosure, tool `capture_lead`, 20 turni / 30 messaggi, 20 req/min/IP |
| `_system/knowledge/lead_ack_voice.md` | testo canonico dell'ack (inglese, 105 parole) |
| `_system/knowledge/chat_faq.md` | 16 Q&A: unica conoscenza della chat |
| `_system/deploy/lead-api.service`, `chat-api.service`, `lead-intake.service`, `lead-intake.timer`, `Caddyfile.snippet` | deploy VPS (comandi nel README di deploy) |
| `_system/launchd/com.myvilla.lead-intake.plist` | rail Mac ogni 5 minuti |
| `.github/workflows/lead-intake.yml` | rail cloud ogni 15 minuti, ledger effimero |

## Policy layer (`send_email.send_raw`)

Ordine dei controlli, ognuno con `SendResult.reason`:

1. blacklist bounce (`_system/outreach/invalid_addresses.json`) → `blacklisted`
2. suppression list `do_not_contact.json` nel private dir → `do_not_contact`
3. tetto giornaliero per kind (UTC, invii reali) da `budgets_per_day` → `budget_exceeded` (`lead_alert` esente; kind senza tetto = illimitato)
4. allegati mancanti → `missing_attachment`
5. `dry_run_kinds` → `dry_run_kind` (ok=True, dry_run=True, corpo composto)
6. `commercial_kinds` senza `brand.postal_address` → `missing_postal_address`; con indirizzo, footer postale + "Reply STOP or write to info@myvilla.la to stop hearing from us."
7. rate limit orario/giornaliero → `rate_limited`
8. `dry_run` globale (config.yml) → `dry_run`
9. invio.

Firma: `signature_override` > firma prospects automatica per i kind `lead_*` > Lisa Monelli.
Log: i kind `lead_*` scrivono in `<private>/leads/send_log.jsonl` (mai nel
log di repo, che è tracciato). `python3 send_email.py --budget-check` mostra
lo stato dei tetti. Chiamate esistenti (`send_draft`, `send_reply`) invariate.

**Attenzione**: `outreach` è in `commercial_kinds` e `brand.postal_address`
è vuoto → dalla prossima esecuzione le pitch ai giornalisti tornano
`missing_postal_address` finché non si compila l'indirizzo (o si toglie
`outreach` dalla lista). Scelta deliberata (CAN-SPAM), ma va decisa.

## Registro (`lead_ledger.py`)

Campi: `lead_id` (sha1 email+ts, 12 char), `received_at`, `source`
(formspree|lead_api|mailto|manual|chat), `form_id`, `first_name`,
`last_name`, `email`, `phone`, `project_type`, `timeline`,
`site_location`, `message`, `how_found`, `referred_by`, `attribution{}`,
`consent{}`, `tier`, `score`, `state`, `next_action`, `thread_id`,
`history[]`. Stati: `lead.states`; transizioni validate (`--force` per
saltarle). `--purge` applica `lead.retention_months` (24).

```bash
export MYVILLA_PRIVATE_DIR=~/.myvilla-private          # opzionale (default)
python3 _system/scripts/lead_ledger.py list [--state acked] [--full]
python3 _system/scripts/lead_ledger.py show <lead_id>
python3 _system/scripts/lead_ledger.py set-state <lead_id> founder_replied --note "Teams call 18/09"
python3 _system/scripts/lead_ledger.py export --csv ~/Desktop/leads.csv   # PII: mai nel repo
```

Lead esistente (CJ Baran, form 31/08 via ChatGPT) — da registrare a mano
perché la notifica Formspree non è mai arrivata a info@ (`--no-ack`: ha
già avuto risposta / non deve ricevere un ack automatico a 2 settimane):

```bash
python3 _system/scripts/lead_intake.py --add-manual --no-ack --json '{
  "first_name": "CJ", "last_name": "Baran", "email": "<EMAIL DAL FORM>",
  "phone": "<TELEFONO O VUOTO>", "project_type": "<New custom build | Rebuild after fire | Future site / land search | General interest>",
  "timeline": "<Ready now | 6-12 months | 12-24 months | Exploring>",
  "site_location": "<AREA>", "message": "<TESTO DEL FORM>",
  "how_found": "ChatGPT", "source": "formspree", "received_at": "2026-08-31T00:00:00+00:00"
}'
# poi lo stato reale, es.:
python3 _system/scripts/lead_ledger.py set-state <lead_id> founder_replied --note "risposta manuale di Paolo"
```

## Intake e scheduling

- **Mac (primario)**: `cp _system/launchd/com.myvilla.lead-intake.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.myvilla.lead-intake.plist` (riavvio: `launchctl kickstart -k gui/$UID/com.myvilla.lead-intake`). Il plist punta a `/Users/ivogiuliani/Code/myvilla-la` come gli altri: adeguare il path se il checkout è altrove. Log: `_system/logs/lead_intake.log`.
- **VPS**: timer systemd ogni 5 minuti (vedi README deploy).
- **Cloud**: `.github/workflows/lead-intake.yml`, cron `*/15`, `--ephemeral`: ack + alert + label, registro NON conservato (il runner muore). Il Mac/VPS ricostruisce i record mancanti con `python3 _system/scripts/lead_intake.py --rebuild-from-label` (nessun nuovo ack; stato `acked`). Consiglio: lanciarlo una volta al giorno nel rail Mac, o a mano quando arriva un alert e il lead non compare nel desk.
- Anti-doppione fra i tre rail: la label Gmail viene messa **prima** di processare (claim); se il processing fallisce la label viene tolta e si riprova al run successivo.
- Se la notifica non contiene un'email leggibile: record con `needs_human`, nessun ack, alert normale.
- Honeypot `_gotcha` pieno: solo label, nessun record.

## Alert e digest

Alert (`lead_alert`, a `alerts.to`): nome, progetto, area, timeline, tier,
how_found, lead_id, comando `lead_ledger.py show <id>` e link al pannello.
Mai email/telefono. Esente dai tetti.

Digest: `lead_desk.digest_block()` restituisce una `<tr>` (stessa griglia
del digest HTML) con conteggi per stato e la lista dei lead A/B senza
risposta del founder da oltre `lead.sla_hours_founder` (24h), senza PII.
`digest_text()` per la versione plain.

## Pannello

Sezione "🤝 Leads" (ancora `#leads`) visibile se `MYVILLA_OWNER=1` o
`PANEL_PASSWORD` impostata e non `PANEL_MODE=shared`. Tabella: tier,
stato, giorni dall'ultimo contatto (⚠︎ se oltre SLA), lead, next action,
select stato + nota + Save → `POST /api/lead-state {lead_id, state, note}`
(403 non owner, 409 transizione non valida, 404 lead assente). Stato
`do_not_contact` aggiunge l'email alla suppression list.

## Contratto API per il widget (conversion-layer, quando `chat.enabled: true`)

Base: `https://content.myvilla.la` (Caddy, senza basic_auth sui path API).
CORS: solo `Origin: https://myvilla.la`.

**Lead**
```
POST /api/lead
Content-Type: application/json            (oppure application/x-www-form-urlencoded)
{"first_name":"…","last_name":"…","email":"…","phone":"…",
 "project_type":"New custom build","timeline":"Ready now",
 "site_location":"Malibu","message":"…","how_found":"…",
 "consent_nurture":"yes","source_page":"/private-briefing.html",
 "referrer":"…","utm_source":"…","_gotcha":""}
→ 200 {"ok":true,"lead_id":"…","redirect":"https://myvilla.la/briefing-received.html"}
   (form HTML con Accept: text/html → 303 verso redirect)
→ 400 campi mancanti · 422 email rifiutata (sintassi/usa-e-getta/dominio) · 429 rate limit
GET /api/healthz → {"ok":true,"service":"lead_api",…}
```

**Chat**
```
POST /api/chat
{"session_id":"<omesso al primo turno>","message":"…","page":"/"}
→ 200 {"ok":true,"session_id":"…","reply":"…","turn":1,"remaining":29,
       "disclosure":"You're chatting with My Villa's AI assistant, not a person."   (solo al primo turno, altrimenti null)
       ,"lead_captured":false}
→ 429 {"ok":false,"error":"session limit reached","reply":"…scrivi a info@…"}  (30 messaggi) oppure {"error":"too many requests"} (20/min/IP)
GET /api/chat/healthz → {"ok":true,"service":"chat_api","enabled":false,…}
```
Il widget deve: mostrare `disclosure` in evidenza al primo turno,
conservare `session_id` in `sessionStorage`, limitare l'input a 1500
caratteri, non partire se `chat.enabled` è false.

## Test eseguiti

- `send_email.py`: matrice dry-run di tutti i kind (config temporanea, log in scratchpad): `missing_postal_address` (outreach), `dry_run`, `dry_run_kind`, `budget_exceeded` (newsletter=0 e asset_pitch con log seminato), `do_not_contact`, footer CAN-SPAM con indirizzo, `signature_override`, log privato per `lead_*`.
- `lead_intake.py --simulate --no-llm`: parser testo + HTML, dedup 10 min, ack (105 parole) + alert in dry-run.
- `lead_api.py --self-test`: healthz, JSON, dup, honeypot, dominio irrisolvibile (422), usa-e-getta (422), 6ª richiesta stesso IP → 429, ack/alert dry-run.
- `chat_api.py --self-test` (backend finto) e `--self-test --live` (Claude reale, tier cheap): disclosure al primo turno, cattura lead nello stesso turno di nome+email, registro `source=chat`, tetto 30 messaggi → 429.
- `lead_desk`: digest senza PII, 403/409/200 su `/api/lead-state`, `approve.build_dashboard()` renderizza la sezione con `MYVILLA_OWNER=1`.
- `py_compile` su tutti i file Python toccati.

## Cosa manca / decisioni aperte

- `brand.postal_address` vuoto: gate chiuso su tutti i `commercial_kinds`, **outreach compreso**.
- `alerts.to` include `<paolo-alert-address>`: confermare che Paolo voglia l'alert a ogni lead (anche tier C non-vendor).
- Retention: `--purge` va schedulato (es. mensile nel rail Mac) — non è automatico.
- Nudge (`lead_nudge`) non ha ancora un motore: il kind e il tetto esistono, la logica di follow-up ai lead silenziosi è da scrivere (dopo il postal address).
- `_system/logs/lead_intake*.log` non è in `.gitignore` di `_system/logs/` (file fuori perimetro): da aggiungere.
- Reply del lead "STOP" → suppression: oggi è manuale (stato `do_not_contact` dal pannello); `reply_monitor.py` potrebbe rilevarlo in futuro.
