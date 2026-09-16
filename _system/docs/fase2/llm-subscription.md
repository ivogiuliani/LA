# Fase 2 — Claude via abbonamento (handoff, 2026-09-16)

<!-- Sezione principale a cura dell'agente ci-and-deploy. Gli altri moduli
     APPENDONO la propria sezione in fondo (## <nome modulo>). -->

## Policy

Dal **2026-09-16** My Villa **non spende più in Claude API a consumo**.
Ogni chiamata ai modelli Claude (radar, journal, pitch, reply drafter,
lead scoring, chat, GEO monitor, ecc.) passa da **Claude Code in modalità
headless** (`claude -p`), che usa l'abbonamento claude.ai di Ivo (piano
**Max**) e non fattura token. Le altre API (Gemini, Grok/xAI, Brave, Google
CSE, Apollo, Unsplash) non cambiano.

Conseguenze pratiche:

- nessuno script della pipeline importa `anthropic` né legge
  `ANTHROPIC_API_KEY` (uniche eccezioni: `llm_client.py` per il backend di
  emergenza e `model_resolver.py`, che resta per compatibilità);
- `ANTHROPIC_API_KEY` viene **rimossa** dai secrets GitHub, dal `.env` del
  VPS e (consigliato) dal `.env` del Mac — vedi checklist;
- se il modello non è raggiungibile (limite d'uso del piano, CLI assente,
  token scaduto) gli script **degradano**: saltano lo step, loggano, exit 0
  dove la pipeline lo richiede. Il run non crasha mai per colpa dell'LLM;
- `requirements.txt` mantiene `anthropic` solo per il backend di emergenza
  (vedi sotto); non è più necessario per il funzionamento normale.

## Come funziona l'adapter (`_system/scripts/llm_client.py`)

```python
from llm_client import complete, complete_json, chat, LLMUnavailable, LLMRefused, status
r = complete("...", system="...", tier="writer")        # r.text, r.model, r.usage, r.duration_ms
d = complete_json("...", schema={...}, tier="cheap")    # dict validato (json_schema → structured_output)
r = complete("...", tier="balanced", web_search=True)   # WebSearch della CLI: testo con URL
r = chat([{"role": "user", "content": "..."}], system="...", tier="cheap")  # multi-turno appiattito
```

- Backend di default `claude_code`: `claude -p --output-format json --model
  <alias> --tools "" --no-session-persistence`, prompt via stdin, system
  prompt con `--system-prompt`, `--json-schema` per l'output strutturato,
  `--tools WebSearch --permission-mode bypassPermissions` solo con
  `web_search=True`.
- La CLI gira in una **cwd neutra** (`$MYVILLA_PRIVATE_DIR/llm-cwd`, default
  `~/.myvilla-private/llm-cwd`): nessun `CLAUDE.md` di progetto viene letto.
- **`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` vengono
  rimosse dall'ambiente del processo figlio**: anche se la chiave fosse
  ancora in un `.env`, la CLI non può usarla (userebbe l'API a consumo).
- Autenticazione: sul Mac il login keychain (`claude auth status` →
  `loggedIn: true, subscriptionType: max`); in CI e sul VPS la variabile
  **`CLAUDE_CODE_OAUTH_TOKEN`** (generata con `claude setup-token`).
- Retry con backoff (30 s → 120 s → 300 s → 600 s) sugli errori transitori
  e sui **limiti d'uso**; poi `LLMUnavailable`. Al massimo
  `MYVILLA_LLM_PARALLEL` (default 2) chiamate CLI in parallelo.
- `max_tokens` è accettato per compatibilità ma ignorato dalla CLI.
- Errori: `LLMUnavailable` (limite/CLI/auth) e `LLMRefused` (backend API
  chiesto senza il flag di emergenza). Gli script li catturano e saltano.

## Mappa tier → modello

| tier | uso tipico | alias CLI | override env |
|---|---|---|---|
| `writer` | articoli Journal, testi brand-critical | `opus` | `MYVILLA_CLI_MODEL_WRITER` |
| `heavy` | pitch giornalisti, revise, ranking | `opus` | `MYVILLA_CLI_MODEL_HEAVY` |
| `balanced` | scoring, validazione, follow-up, chat, GEO | `sonnet` | `MYVILLA_CLI_MODEL_BALANCED` |
| `cheap` | classificazioni di massa, scraper | `haiku` | `MYVILLA_CLI_MODEL_CHEAP` |

Un model id esplicito (es. `claude-fable-5` da `model_resolver.resolve`)
viene mappato **per famiglia**: fable/opus → `opus`, sonnet → `sonnet`,
haiku → `haiku`. Gli alias sono risolti da Claude Code alla versione più
recente disponibile per l'abbonamento (oggi: `claude-opus-5`,
`claude-sonnet-5`, `claude-haiku-4-5`). Negli script preferire `tier=`.

## Limiti d'uso del piano e come leggerli

- Il piano Max ha un budget a **finestre mobili di 5 ore** (più un tetto
  settimanale). Quando la finestra è esaurita la CLI risponde con un errore
  di "usage limit": l'adapter ritenta con backoff (fino a ~17 minuti
  complessivi) e poi solleva `LLMUnavailable` → lo step viene saltato e
  riprende al run successivo. Le chiamate `opus` consumano più budget di
  `sonnet`/`haiku`; le `web_search=True` costano molto (contesto di ricerca
  ~80-100k token in cache): usarle con parsimonia (GEO monitor, radar).
- Ogni chiamata scrive una riga in **`_system/logs/llm_calls.jsonl`**
  (gitignored; senza contenuti):
  `{"ts", "script", "backend", "tier", "model", "web_search", "json", "in",
  "cache_create", "cache_read", "out", "ms", "cost_reported_usd", "billed"}`.
  `backend` deve essere sempre `claude_code` e `billed` sempre `0`;
  `cost_reported_usd` è il **costo equivalente** riportato dalla CLI, utile
  solo per capire quanto pesa ogni step (non è fatturato).
- Lettura rapida:

```bash
# chiamate di oggi per script/tier
grep "$(date +%Y-%m-%d)" _system/logs/llm_calls.jsonl | python3 -c '
import sys, json, collections
c = collections.Counter(); cost = 0.0; billed = 0
for l in sys.stdin:
    e = json.loads(l); c[(e["script"], e["tier"])] += 1
    cost += e.get("cost_reported_usd") or 0; billed += e.get("billed") or 0
for k, v in sorted(c.items()): print(f"{v:4d}  {k[0]:32s} {k[1]}")
print(f"equivalente API: ${cost:.2f}   billed: {billed}")'

# righe con backend diverso da claude_code o billed != 0 (devono essere ZERO)
grep -v '"backend": "claude_code"' _system/logs/llm_calls.jsonl; grep -v '"billed": 0' _system/logs/llm_calls.jsonl
```

- `daily_publish.sh` logga a inizio run lo stato dell'adapter (CLI, auth,
  modelli) e a fine run il conteggio delle chiamate del giorno.
- Stato in qualsiasi momento: `python3 _system/scripts/llm_client.py --status`;
  prova completa (3 chiamate reali): `python3 _system/scripts/llm_client.py --self-test`.

## Dove gira (tre rail)

| Rail | Auth | Note |
|---|---|---|
| Mac (launchd `daily_publish.sh`, pannello, lead-intake) | login keychain di Claude Code | CLI in `~/.npm-global/bin/claude` (l'adapter la trova anche senza PATH) |
| GitHub Actions (`daily-publish`, `lead-intake`, `geo-monitor`) | secret `CLAUDE_CODE_OAUTH_TOKEN` | step "Install Claude Code CLI" (`setup-node` + `npm i -g @anthropic-ai/claude-code`) prima degli step Python; senza il secret gli step LLM degradano |
| VPS (`lead-api`, `chat-api`, `lead-intake.timer`) | `CLAUDE_CODE_OAUTH_TOKEN` in `/opt/myvilla/.env` | node + CLI installati come da `_system/deploy/README.md` |

`weekly-backlinks.yml` e `linkedin-publish.yml` non usano modelli: nessuna
CLI, nessun token.

## Procedura di emergenza (API a consumo, solo se Ivo lo decide)

Se l'abbonamento è bloccato e uno step è davvero urgente:

```bash
MYVILLA_ALLOW_API=1 MYVILLA_LLM_BACKEND=api ANTHROPIC_API_KEY_DISABLED=sk-ant-... \
  python3 _system/scripts/<script>.py ...
```

- Serve **entrambe** le variabili: `MYVILLA_LLM_BACKEND=api` seleziona il
  backend, `MYVILLA_ALLOW_API=1` sblocca il rifiuto (`LLMRefused`).
- La chiave si passa come `ANTHROPIC_API_KEY_DISABLED` (o `ANTHROPIC_API_KEY`)
  **solo per quel comando**, mai nel `.env` permanente né nei secrets CI.
- Le righe di log avranno `backend: "api"`, `billed: 1`: sono la traccia
  della spesa. Richiede `pip install anthropic`.
- Non impostare mai queste variabili nei workflow o nei service systemd.

## Checklist per Ivo

1. **Token per CI/VPS**: sul Mac `claude setup-token` (richiede
   l'abbonamento; genera un token OAuth di lunga durata). Copiarlo una sola
   volta, non committarlo.
2. **GitHub**: Settings → Secrets and variables → Actions →
   `CLAUDE_CODE_OAUTH_TOKEN` = token del punto 1.
3. **GitHub**: **eliminare** il secret `ANTHROPIC_API_KEY` (nessun workflow
   lo legge più; un residuo verrebbe comunque ignorato dall'adapter).
4. **VPS**: seguire `_system/deploy/README.md` → "Claude via abbonamento":
   installare node + CLI, aggiungere `CLAUDE_CODE_OAUTH_TOKEN=` a
   `/opt/myvilla/.env`, **rimuovere** la riga `ANTHROPIC_API_KEY=`, riavviare
   `lead-api chat-api`.
5. **Mac**: rimuovere `ANTHROPIC_API_KEY=` da `~/Code/myvilla-la/.env` (o
   rinominarla `ANTHROPIC_API_KEY_DISABLED=` per l'emergenza); verificare
   `python3 _system/scripts/llm_client.py --status` → `api_key_in_env: false`.
6. **Console Anthropic**: revocare/disabilitare la chiave API vecchia, così
   nessun processo dimenticato può spendere.
7. Dopo il primo run cloud: controllare nel log del workflow lo step
   "Claude Code CLI status" e, sul Mac, `_system/logs/llm_calls.jsonl`
   (`backend: claude_code`, `billed: 0`).
8. Il token `setup-token` scade (circa un anno) o può essere revocato dal
   logout: se la CI logga "Claude Code non autenticata", rigenerarlo e
   aggiornare secret + `.env` del VPS.

## ci-and-deploy — cosa è cambiato

- `.github/workflows/daily-publish.yml`, `lead-intake.yml`, `geo-monitor.yml`:
  via `ANTHROPIC_API_KEY` (dal `.env` ricostruito e dagli `env:`); nuovo
  step **Install Claude Code CLI** (`actions/setup-node@v4` node 20 +
  `npm install -g @anthropic-ai/claude-code`) e step **Claude Code CLI
  status** (`llm_client.py --status`, non bloccante); `CLAUDE_CODE_OAUTH_TOKEN`
  esportato a livello di job dal secret omonimo. Con il secret assente la
  CLI risulta non autenticata → `LLMUnavailable` → gli script saltano.
- `weekly-backlinks.yml`, `linkedin-publish.yml`: nessun modello coinvolto;
  solo una nota in testa.
- `_system/scripts/daily_publish.sh`: `PATH` esteso a `~/.npm-global/bin`,
  log dello stato adapter allo START (CLI, auth, backend), avviso se
  `MYVILLA_LLM_BACKEND=api` senza emergenza dichiarata, conteggio chiamate
  LLM del giorno all'END. Nessuna logica di pipeline toccata.
- `_system/deploy/README.md`: sezione "Claude via abbonamento sul VPS"
  (node + CLI su Ubuntu, token in `/opt/myvilla/.env`, rimozione
  `ANTHROPIC_API_KEY`, `chat_api`/`lead_api` senza chiave API).
- `_system/deploy/*.service`: `Environment=IS_SANDBOX=1` (i servizi girano
  come root: la CLI rifiuta `bypassPermissions` da root senza questo flag,
  necessario alle chiamate `web_search=True`) + commento sul token.
- `_system/logs/.gitignore`: `llm_calls*.jsonl`.

## Modulo `journal-content` (migrato 2026-09-16)

File: `_system/scripts/generate_journal.py`, `_system/scripts/validate.py`, `_system/scripts/editorial_generator.py`, `_system/scripts/generate_evergreen.py`. Verificati e lasciati intatti (non usano Claude): `editorial_planner.py`, `social_guidelines.py`.

- Tutti e quattro importano `from llm_client import complete as _llm_complete, LLMUnavailable, LLMRefused` con un fallback `LLM_OK=False` se l'adapter manca: in quel caso lo step LLM viene saltato (return `None` / `{"error": ...}`), mai crash del run. Nessun file importa più `anthropic` né legge `ANTHROPIC_API_KEY`; rimosso anche `model_resolver` da questi quattro (il tier lo risolve l'adapter).
- **`generate_journal.generate_article`** → `tier="writer"`, `timeout=1500`, `max_tokens=20000` (informativo). Il vecchio retry su `stop_reason == "max_tokens"` (che la CLI non espone) è diventato: se la risposta non contiene un oggetto JSON completo (`json.loads` + `raw_decode` dal primo `{` falliscono) si ritenta una volta con la `_shorten_note`, poi si salta il candidato. Post-processing invariato (fences, raw_decode, slug sanitize, `sanitize_sources`, `verify_sources_live`). Il parametro `model=` resta: `approve.py` continua a passare il suo `_WRITER_MODEL` (id API), che l'adapter mappa per famiglia → `opus`. Default `_WRITER_MODEL = None` (= tier). `--model` resta come override.
- **`validate.ai_validate`** → `tier="balanced"`, `timeout=300`; stesso parsing (fences + `json.loads`), `None` su `LLMUnavailable/LLMRefused`. Help di `--ai` aggiornato (nessun credito API).
- **`editorial_generator.generate_post_for_slot`** → `tier="balanced"`, `system=EDITORIAL_SYSTEM_PROMPT`, `timeout=600`; loop di retry con feedback di validazione invariato. Codici errore invariati (`no_api_key` solo se l'adapter non è importabile; `api_error` con `exception="llm_unavailable: ..."` su limite d'uso). `DEFAULT_MODEL = None`.
- **`generate_evergreen._complete`** → `tier="balanced"`, `timeout=300`, fallback Gemini invariato quando Claude non è disponibile (Gemini non è oggetto della migrazione).
- Test eseguiti: `py_compile` sui 4 file; 3 chiamate reali via adapter (`validate.ai_validate` su un testo breve, `generate_journal._llm_complete` tier writer con prompt corto, `generate_evergreen._complete`); import di `editorial_generator`. Verificato in `_system/logs/llm_calls.jsonl`: `backend=claude_code`, `billed=0`.
- Non eseguito: `generate_journal.py --dry-run` (articolo completo, troppo lungo) e una generazione reale di `editorial_generator` (idem). Il primo run di pipeline reale va osservato: con `opus` via CLI un articolo da 20k token può richiedere diversi minuti (timeout 1500 s).

## Modulo `outreach-chain` (migrazione 2026-09-16)

File migrati all'adapter `llm_client` (nessun `import anthropic`, nessuna lettura di `ANTHROPIC_API_KEY`, nessun HTTP verso `api.anthropic.com`):

| File | Call site | Prima | Ora |
|---|---|---|---|
| `_system/scripts/radar.py` | `api_health_check` → voce `anthropic` | POST `/v1/messages` (haiku, 4 token) | `_ping_claude_code`: `llm_client.status()` (CLI + `claude auth status`), **nessuna chiamata a modello**; `env_var=None` → mai "missing" |
| `_system/scripts/radar.py` | `ai_score_batch` | `client.messages.create` (sonnet) | `complete(..., tier="balanced")`, un batch da 20 per chiamata, sequenziale; su `LLMUnavailable/LLMRefused` il batch corrente e i successivi usano i punteggi preliminari (`_fallback_scores`) |
| `_system/scripts/generate_radar_report.py` | `estimate_unknown_reach`, `generate_drafts`, `generate_viral_reply_drafts` | `messages.create` (opus) | `complete(..., tier="heavy")`; `ANTHROPIC_OK` → `LLM_OK`; viral reply mantiene il fallback Gemini se Claude non è disponibile |
| `_system/scripts/reply_drafter.py` | `_call_claude` | `messages.create` + parse testo | `complete_json(..., REPLY_JSON_SCHEMA, tier="balanced")` — stesso dict (classification/confidence/reasoning/subject/body/include_attachments/suggested_next_step); `HAS_ANTHROPIC` resta come alias di `HAS_LLM` |
| `_system/scripts/followup_engine.py` | `_generate_body_with_claude`, `_generate_rescue_cold_body` | SDK `Anthropic` | helper `_llm_complete_or_none` (tier balanced) → template deterministico su qualunque errore |
| `_system/scripts/approve.py` | `_handle_revise`, `_handle_revise_radar` | SDK `Anthropic` (writer) | `complete(..., tier="writer")`; errore 500 con messaggio se `llm_client` non importabile |
| `_system/scripts/feature_pitch.py` | `_generate_pitch` | `urllib` POST `/v1/messages` | `complete(prompt, tier="heavy")` |
| `_system/scripts/model_resolver.py` | `_get_resolution` | GET `/v1/models` con chiave | `has_api_key()`: senza chiave nessuna rete, cache pulita o `FALLBACKS`, nessun errore (gli id servono solo come famiglia per `llm_client.model_for`) |

Gli id restituiti da `model_resolver.resolve(tier)` (`_BALANCED_MODEL`, `_HEAVY_MODEL`, `_WRITER_MODEL`, `CLAUDE_MODEL`, `MODEL`) sono ancora passati come `model=` per compatibilità con i flag `--model` e con il campo `model` dei draft JSON, ma l'adapter li mappa per famiglia sull'alias CLI (opus/sonnet/haiku): il `tier=` è la scelta effettiva.

Degradazione: nessuno script crasha se il modello non risponde. `radar.py` e `generate_radar_report.py` proseguono senza lo step AI (log `modello non disponibile`), `reply_drafter` scrive la bozza `needs_human`, `followup_engine` usa i template, `feature_pitch` salta l'outlet (`pitch gen fallita`), `approve.py` risponde HTTP 500 con il motivo.

Test eseguiti (2026-09-16): `py_compile` sui 7 file; import di tutti i moduli; `reply_drafter._call_claude` con messaggio finto (1 chiamata JSON, balanced); `radar.ai_score_batch` con 1 item (1 chiamata, balanced); `followup_engine._generate_body_with_claude` touch-3 (1 chiamata, balanced); `model_resolver` offline simulato; `radar._ping_claude_code` (nessuna chiamata a modello). Tutte le righe in `_system/logs/llm_calls.jsonl` con `backend=claude_code`, `billed=0`.

Residuo: il banner API health mantiene la chiave `anthropic` (etichetta "Anthropic" in `api_health_banner.py`, file non di questo modulo): ora significa "Claude Code CLI autenticata".

## Modulo `social-dormant` (2026-09-16)

Perimetro: `generate_social.py`, `generate_ig_companion.py`, `generate_x_companion.py`, `ig_viral_radar.py`, `partner_scraper.py` (flussi dormienti: produzione social spenta dal 2026-08-05, ma devono restare funzionanti per `approve.py` e `editorial_generator.py`).

Cosa è cambiato: rimossi `import anthropic`, `anthropic.Anthropic(...)`, `client.messages.create` e ogni lettura di `ANTHROPIC_API_KEY` come precondizione; rimosso anche `model_resolver` (chiamava `/v1/models` con la chiave API). Ogni file importa `from llm_client import complete, LLMUnavailable, LLMRefused`; i tre file batch (`generate_social`, `ig_viral_radar`, `partner_scraper`) hanno uno stub di fallback se l'adapter non è importabile (`LLM_OK=False` → step saltato). Tier: `heavy` per i post social reattivi/companion/Reddit, `balanced` per i companion IG/X, `cheap` per gli scoring (partner + viral). I parametri `model=` e i flag `--model` restano (default `None` = tier; un alias `opus/sonnet/haiku` o un id è un override). Le costanti `_HEAVY_MODEL`, `_BALANCED_MODEL`, `CLAUDE_SCORING_MODEL` valgono ora `None`; `ig_viral_radar.SCORING_MODEL = "haiku"`. Gli scoring usano `json_schema` (structured output) con fallback al parsing del testo. Degradazione: `LLMUnavailable`/`LLMRefused` → `[]` o `None` (stesso comportamento della vecchia "chiave assente"), i companion CLI ritornano exit 2; nessun crash.

Limite noto: la CLI headless non accetta immagini, quindi `ig_viral_radar.score_and_comment` è text-only (l'immagine viene scaricata solo per la thumbnail) e il prompt istruisce il modello a essere conservativo sulla RULE 2 (mai lodare come cemento ciò che la caption non dichiara). Test eseguiti: py_compile + import dei 5 file; 4 chiamate reali (X companion 7,8 s, partner scoring 11 s, viral scoring 26 s, IG companion via CLI 42 s) tutte loggate in `_system/logs/llm_calls.jsonl` con `backend=claude_code`, `billed=0`. Attenzione: `approve.py` lancia `generate_ig_companion.py` con `timeout=60`; con sonnet via CLI un companion ha impiegato 42 s, quindi il timeout va portato a ≥180 s (file fuori perimetro).

## Modulo `fase2-scripts` (lead_score, chat_api, geo_monitor, backlinklib e dipendenti) — 2026-09-16

**File migrati**: `_system/scripts/lead_score.py`, `_system/scripts/chat_api.py`, `_system/scripts/geo_monitor.py`, `_system/backlinks/backlinklib.py`. `journalist_requests.py`, `linkedin_founder_drafts.py` e `press_submit.py` non chiamavano l'API direttamente: i primi due passano da `backlinklib.claude_text` (ora su `llm_client`), `press_submit.py` non usa modelli. Nessuno di questi file importa più `anthropic` né legge `ANTHROPIC_API_KEY`.

- **lead_score.py** — `refine_with_llm()` usa `llm_client.complete_json(..., tier="balanced", timeout=180, retries=1)` con schema `{tier enum A/B/C, score, reasons[], needs_human, confidence, vendor}`; stessa firma/output di prima (`method: "rules+llm"`). `LLMUnavailable`/`LLMRefused` → si tengono le regole (come prima quando mancava la chiave). Nessuna PII al modello (solo campi progetto), invariato.
- **chat_api.py** — il turno a due chiamate con tool `capture_lead` è diventato **una sola chiamata** `llm_client.chat(messages, system, tier="cheap", json_schema=TURN_SCHEMA, timeout=120, retries=0)` con output `{reply: string, capture_lead: object|null}` (`anyOf` null/oggetto, `required: first_name, email`). Se `capture_lead` è valorizzato lo script chiama `capture_lead()` (ledger + score + ack/alert in thread) e, se l'email è rifiutata, sostituisce la risposta con la richiesta di ricontrollarla. Disclosure al primo turno, tetto 30 messaggi/sessione, rate-limit IP, fallback "write to info@myvilla.la" su qualsiasi eccezione: invariati. `BACKEND` vale ora `"claude_code"` (o `"fake"`); `/api/healthz` lo espone. `retries=0` perché la chat è interattiva: un limite d'uso produce subito il messaggio di fallback invece di far attendere il visitatore. Il self-test `--live` fa 2 turni reali + 1 scoring lead; il loop del tetto di sessione ora usa il backend finto (prima faceva 30 chiamate reali).
- **geo_monitor.py** — motore Claude: `complete(question + suffisso "cita le fonti con URL", system=SYSTEM_ANSWER, tier="balanced", web_search=True)`; le fonti vengono estratte dal testo con regex (`extract_urls`: link markdown con titolo + URL nudi, dedup). Fact-check: `complete_json(..., tier="cheap")` con schema `{wrong_claims: [{claim, issue}]}`. Firme interne cambiate (`ask_claude(question, model=None)`, `fact_check(answer_text, facts, model=None)`: nessun altro script le chiama). `cost_usd` Claude = **0.0** (abbonamento); `cost_reported_usd` della CLI è salvato nel run solo come informazione. Listino Claude rimosso da `PRICE` (resta Gemini). Se Claude Code non è disponibile (o va in `LLMUnavailable` a metà run) il motore claude e il fact-check vengono saltati per il resto del run con log, exit 0. Gemini invariato. Nuovo flag `--no-save` (test senza toccare `runs/` e `summary.json`).
- **backlinklib.py** — `claude_text(prompt, system, tier, max_tokens)` → `llm_client.complete`; stessa firma, ritorna testo o `None` (anche su `LLMUnavailable`/`LLMRefused`). `resolve_model()` resta per compatibilità ma non è più usata dal percorso di generazione.

**Test eseguiti** (tutti `backend=claude_code`, `billed=0` nel log): `py_compile` sui 7 file; `lead_score.py --no-llm` (0 chiamate) e con LLM (1 chiamata, tier A, confidence 0.97); `chat_api.py --self-test --live` (disclosure ok, lead catturato al 2° turno, tetto 429 ok); `geo_monitor.py --engine claude --limit 1 --dry-run` (0 chiamate) e `--no-save` reale (1 chiamata web, 9 URL estratti, run non scritto).

**Da sistemare fuori perimetro**: `.github/workflows/geo-monitor.yml` passa ancora `ANTHROPIC_API_KEY` come secret; in Actions non c'è la CLI Claude Code né il keychain, quindi il motore claude verrebbe saltato (Gemini continuerebbe). Opzioni: installare la CLI nel job e usare `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`), oppure spostare il run settimanale su launchd sul Mac. `_system/docs/fase2/seo-geo-journal.md` riga 17 cita ancora il secret `ANTHROPIC_API_KEY`.
