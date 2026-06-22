#!/bin/bash
#
# daily_publish.sh — wrapper invocato da launchd (poll ogni 30 min).
#
# Pipeline:
#   1.  radar.py (se il radar di oggi manca)
#   1b. generate_radar_report.py (draft pitch + contatti + dashboard HTML)
#   2.  generate_journal.py (articoli draft)
#   3.  publish_all_drafts.py (publish + pitch + follow-up + digest mail)
#   4.  feature_pitch.py (proposte feature alle testate locali)
#   5.  commit+push finale dello stato (log outreach, cache, dedup)
#
# Idempotenza giornaliera:
#   - no-op prima delle TARGET_HOUR
#   - no-op se il log di oggi contiene la riga canonica
#     "=== daily_publish END ===" (scritta SOLO a run completato con
#     successo — i fallimenti scrivono "END (failed ...)" che NON
#     matcha, così il poll successivo ritenta)
#   - cross-rail: marker _system/outreach/.last_digest_date condiviso
#     via git con GitHub Actions; letto da origin/main (immune a
#     working tree sporco), chi arriva primo vince.
#
# Log:  _system/logs/daily_publish_YYYY-MM-DD.log
# Lock: mkdir atomico — una sola istanza alla volta.

set -uo pipefail

PROJECT_ROOT="/Users/ivogiuliani/Code/myvilla-la"
cd "$PROJECT_ROOT" || exit 1

# Ora-target: la pipeline gira solo a partire da questa ora locale.
# Pattern poll-based (come io.giuliani.fallingknife): robusto allo
# sleep del Mac — launchd esegue l'intervallo perso appena sveglio.
TARGET_HOUR=8

# Tetto di tempo per il radar. Una singola API lenta (es. x.ai/Grok che
# manda dati col contagocce oltre il read-timeout per-richiesta, o rete
# degradata con decine di chiamate) può tenere il radar appeso per ORE e
# bloccare TUTTA la pipeline prima della digest — è quello che è successo
# il 14/06 (report non arrivato). Oltre il tetto il radar viene ucciso e
# si prosegue: la digest deve partire comunque.
RADAR_MAX_SECONDS="${RADAR_MAX_SECONDS:-1500}"  # 25 min

TODAY="$(date +%Y-%m-%d)"
CURRENT_HOUR="$(date +%-H)"
RADAR_FILE="_system/radar/reports/radar_${TODAY}.json"
LOG_DIR="_system/logs"
LOG_FILE="${LOG_DIR}/daily_publish_${TODAY}.log"
LOCK_FILE="${LOG_DIR}/.daily_publish.lock"
MARKER_FILE="_system/outreach/.last_digest_date"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Esegue un comando con un tetto di tempo (macOS non ha `timeout`).
# Alla scadenza: SIGTERM, poi SIGKILL dopo 8s. La verifica del file
# prodotto a valle decide se proseguire o saltare gli step dipendenti.
run_with_timeout() {
    local secs="$1"; shift
    "$@" &
    local cmd_pid=$!
    ( sleep "$secs"; kill -TERM "$cmd_pid" 2>/dev/null
      sleep 8; kill -KILL "$cmd_pid" 2>/dev/null ) &
    local watch_pid=$!
    wait "$cmd_pid" 2>/dev/null
    local rc=$?
    kill -TERM "$watch_pid" 2>/dev/null   # disarma il watchdog
    wait "$watch_pid" 2>/dev/null
    return $rc
}

# ── No-op #1: troppo presto (silenzioso) ───────────────────────────
if [ "$CURRENT_HOUR" -lt "$TARGET_HOUR" ]; then
    exit 0
fi

# ── No-op #2: già completato oggi (silenzioso) ─────────────────────
# Matcha SOLO la riga canonica di successo. "END (failed ...)" /
# "END (dry-run)" / "END (radar failed)" non matchano → retry al
# prossimo poll. È sicuro ritentare: radar esistente viene riusato,
# journal ha i cooldown, i pitch hanno il dedup URL, la digest ha il
# marker cross-rail.
if [ -f "$LOG_FILE" ] && grep -q "=== daily_publish END ===" "$LOG_FILE"; then
    exit 0
fi

# ── Lock: una sola istanza alla volta ──────────────────────────────
if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK_FILE" 2>/dev/null' EXIT

log "=== daily_publish START ==="
log "PWD: $(pwd)"
log "Python: $(which python3) ($(python3 --version 2>&1))"

# ── Self-heal git + Sync + cross-rail guard ────────────────────────
# CAUSA RADICE della doppia digest (21-22/6): un rebase rimasto APPESO da un
# run precedente (.git/rebase-merge, da un pull --rebase interrotto su un
# conflitto di file di stato) blocca OGNI pull/push successivo → il marker
# .last_digest_date non raggiunge più origin → l'altro rail (cloud) invia una
# SECONDA digest. Lo abortiamo subito, tornando a uno stato pulito.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "  ⚠ rebase git appeso da un run precedente — git rebase --abort"
    git rebase --abort >> "$LOG_FILE" 2>&1 || rm -rf .git/rebase-merge .git/rebase-apply
fi
# pull --rebase --autostash: l'autostash è ESSENZIALE — i run precedenti
# lasciano file di stato modificati e senza autostash il pull fallisce.
# Il marker viene letto ANCHE da origin/main via git show (sotto): immune
# a problemi del working tree locale.
log "git pull --rebase --autostash per sincronizzare lo stato..."
if ! git pull --rebase --autostash origin main >> "$LOG_FILE" 2>&1; then
    log "  ⚠ git pull fallito — abort rebase + fetch del marker da origin"
    git rebase --abort >> "$LOG_FILE" 2>&1 || true
    git fetch origin main >> "$LOG_FILE" 2>&1 || log "  ⚠ anche il fetch è fallito (offline?)"
fi

REMOTE_MARKER="$(git show "origin/main:${MARKER_FILE}" 2>/dev/null | tr -d '[:space:]')"
LOCAL_MARKER="$(cat "$MARKER_FILE" 2>/dev/null | tr -d '[:space:]')"
if [ "$REMOTE_MARKER" = "$TODAY" ] || [ "$LOCAL_MARKER" = "$TODAY" ]; then
    log "Digest di oggi già inviato dall'altro rail (marker=$TODAY) — skip."
    # Riga canonica così i poll successivi diventano no-op silenziosi.
    log "=== daily_publish END ==="
    exit 0
fi

# Step 1: radar di oggi. Se manca lo lanciamo noi (auto-sufficiente),
# con tetto di tempo. Se non lo produce (timeout/rete) NON usciamo:
# proseguiamo saltando solo gli step che dipendono dal radar — la
# digest e l'outreach devono partire comunque.
RADAR_OK=1
if [ ! -f "$RADAR_FILE" ]; then
    log "Radar di oggi non trovato — lancio radar.py (tetto ${RADAR_MAX_SECONDS}s)..."
    run_with_timeout "$RADAR_MAX_SECONDS" python3 _system/scripts/radar.py >> "$LOG_FILE" 2>&1
    RADAR_EXIT=$?
    log "radar.py exit code: $RADAR_EXIT"
    if [ ! -f "$RADAR_FILE" ]; then
        RADAR_OK=0
        log "⚠ radar non ha prodotto $RADAR_FILE (timeout o rete)."
        log "  Proseguo SENZA gli step che dipendono dal radar; digest e"
        log "  outreach partono comunque (un radar appeso non deve più"
        log "  bloccare il report giornaliero)."
    fi
fi
[ "$RADAR_OK" = "1" ] && \
    log "Radar trovato: $RADAR_FILE ($(stat -f '%z' "$RADAR_FILE") bytes)"

# Step 1b: generate_radar_report — draft pitch + contatti editoriali +
# dashboard HTML. Non-bloccante. Dipende dal radar.
if [ "$RADAR_OK" = "1" ]; then
    log "--- generate_radar_report.py (draft + contatti + dashboard) ---"
    python3 _system/scripts/generate_radar_report.py --radar "$RADAR_FILE" \
        >> "$LOG_FILE" 2>&1 || log "generate_radar_report errore non bloccante (continuo)"
else
    log "--- generate_radar_report.py — SKIP (radar non disponibile) ---"
fi

# Step 1c: reply_monitor — rileva risposte e bounce dei giornalisti.
# GAP storico: non era schedulato da NESSUNA parte → bounce/risposte
# fermi al 23/4, blacklist stantia, dashboard "Risposte: 0" cieca.
# --since limita la scansione Gmail agli ultimi 60 giorni (quota).
# Non-bloccante.
log "--- reply_monitor.py (risposte + bounce) ---"
SINCE_DATE="$(date -v-60d +%Y-%m-%d 2>/dev/null || date -d '60 days ago' +%Y-%m-%d)"
python3 _system/scripts/reply_monitor.py --since "$SINCE_DATE" \
    >> "$LOG_FILE" 2>&1 || log "reply_monitor errore non bloccante (continuo)"

# DRY_RUN=1 → simula senza spedire mail né pubblicare (smoke test)
DRY_FLAGS=""
FP_DRY=""
if [ "${DRY_RUN:-0}" = "1" ]; then
    log "DRY_RUN=1 — modalità simulazione, niente email/push"
    DRY_FLAGS="--dry-run --no-email --no-push"
    FP_DRY="--dry-run"
fi

# Step 1d: ig_publisher — pubblica su Instagram i post APPROVATI da
# Ivo (max IG_DAILY_CAP/giorno, default 1 — ramp account nuovo).
# Gira PRIMA di publish_all_drafts così la digest riporta l'esito.
# Senza credenziali Meta esce pulito (fase pre-setup). Non-bloccante.
IG_DRY=""
[ "${DRY_RUN:-0}" = "1" ] && IG_DRY="--dry-run"
log "--- ig_publisher.py $IG_DRY ---"
python3 _system/scripts/ig_publisher.py $IG_DRY >> "$LOG_FILE" 2>&1 || \
    log "ig_publisher errore non bloccante (continuo)"

# Step 1d-bis: x_publisher — pubblica su X i tweet APPROVATI (status:
# approved in _drafts/social/), max X_DAILY_CAP/giorno (default 4).
# Mirror di ig_publisher. Posta SOLO gli approvati (mai i draft), li
# sposta in social/posts/published/ (niente doppio post). No-op se la
# coda approvati è vuota. In DRY_RUN fa solo preview. Non-bloccante.
X_DRY=""
[ "${DRY_RUN:-0}" = "1" ] && X_DRY="--dry-run"
SD_DRY=""
[ "${DRY_RUN:-0}" = "1" ] && SD_DRY="--dry-run"
log "--- x_publisher.py --dir --status approved $X_DRY ---"
python3 _system/scripts/x_publisher.py --dir --status approved --publish-live $X_DRY \
    >> "$LOG_FILE" 2>&1 || \
    log "x_publisher errore non bloccante (continuo)"

# Step 2: generate_journal — dipende dal radar. Se saltato, GEN_EXIT=0
# (non è un fallimento: la digest può comunque chiudere con successo).
GEN_EXIT=0
if [ "$RADAR_OK" = "1" ]; then
    log "--- generate_journal.py ---"
    python3 _system/scripts/generate_journal.py \
        --radar "$RADAR_FILE" \
        --min-score 14 \
        --max-articles 3 \
        >> "$LOG_FILE" 2>&1
    GEN_EXIT=$?
    log "generate_journal.py exit code: $GEN_EXIT"
else
    log "--- generate_journal.py — SKIP (radar non disponibile) ---"
fi

# Step 3: publish_all_drafts (gestisce coda vuota senza problemi)
log "--- publish_all_drafts.py $DRY_FLAGS ---"
python3 _system/scripts/publish_all_drafts.py $DRY_FLAGS \
    >> "$LOG_FILE" 2>&1
PUB_EXIT=$?
log "publish_all_drafts.py exit code: $PUB_EXIT"

# Step 3a: pulizia proposte social stantie (>7 giorni) → _archive.
# Il pannello deve mostrare solo contenuto attuale: un post reattivo
# su una notizia vecchia è rumore per chi gestisce i social.
mkdir -p _archive/social
find _drafts/social _system/social/posts/reactive -name "*.md" -mtime +7 \
    -exec mv {} _archive/social/ \; 2>/dev/null || true

# Step 3a-bis: rotazione magazzino proposte — tieni solo le 3 reactive
# IG + 2 X più recenti, archivia le eccedenti (il pannello ne mostra
# comunque max 3/2: il magazzino deve rispecchiare la vetrina).
python3 - << 'PRUNE'
from pathlib import Path
import shutil
arch = Path("_archive/social"); arch.mkdir(parents=True, exist_ok=True)
for pattern, keep in (("-ig-", 3), ("-x-", 2)):
    files = []
    for d in (Path("_drafts/social"), Path("_system/social/posts/reactive")):
        if d.exists():
            files += [f for f in d.glob("*.md") if pattern in f.name]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        shutil.move(str(f), str(arch / f.name))
        print(f"  [rotazione] archiviato {f.name}")
PRUNE

# Step 3b: generate_social — proposte social del giorno dal radar
# (max 2 set reactive = 2 IG + 2 X, con immagini auto). NON pubblica:
# crea solo le card da approvare nel pannello. Dipende dal radar.
if [ "$RADAR_OK" = "1" ]; then
    log "--- generate_social.py (proposte del giorno) ---"
    python3 _system/scripts/generate_social.py --radar "$RADAR_FILE" \
        --max-posts 2 >> "$LOG_FILE" 2>&1 || \
        log "generate_social errore non bloccante (continuo)"
else
    log "--- generate_social.py — SKIP (radar non disponibile) ---"
fi

# Step 3c: ig_viral_radar — scopre post IG virali altrui in nicchia e
# prepara un commento pronto (copia-incolla) per ognuno. NON dipende dal
# radar (usa Apify direttamente). NON auto-pubblica nulla: produce solo
# opportunità da consultare nel pannello (sezione "📷 Instagram —
# Commenti a post virali"). Salta se il token Apify manca. Non-bloccante.
if grep -q "^APIFY_API_TOKEN=" .env 2>/dev/null; then
    log "--- ig_viral_radar.py (commenti a post virali IG) ---"
    run_with_timeout 600 python3 _system/scripts/ig_viral_radar.py \
        --min-relevance 5 >> "$LOG_FILE" 2>&1 || \
        log "ig_viral_radar errore non bloccante (continuo)"
else
    log "--- ig_viral_radar.py — SKIP (APIFY_API_TOKEN assente) ---"
fi

# Step 3d: generate_evergreen — flusso brand-awareness dal SITO (3
# proposte/giorno dai topic del sito + immagini originali). NON dipende
# dal radar, NON pubblica: solo proposte da approvare nel pannello
# (sezione "✨ Evergreen dal sito"). LLM con fallback Gemini. Non-bloccante.
log "--- generate_evergreen.py (proposte evergreen dal sito) ---"
python3 _system/scripts/generate_evergreen.py >> "$LOG_FILE" 2>&1 || \
    log "generate_evergreen errore non bloccante (continuo)"

# Step 3e: social_digest — email quotidiana a Ivo+Giana con l'abstract di
# TUTTI i contenuti social del pannello (proposte da approvare + evergreen +
# commenti ai post virali IG/X + coda di pubblicazione). Gira DOPO la
# generazione social (3c/3d) così fotografa i contenuti freschi. Solo
# formattazione, niente LLM. Idempotente: una volta/giorno (marker su data).
# Non-bloccante.
log "--- social_digest.py (abstract social a Ivo+Giana) $SD_DRY ---"
python3 _system/scripts/social_digest.py $SD_DRY >> "$LOG_FILE" 2>&1 || \
    log "social_digest errore non bloccante (continuo)"

# Step 4: feature_pitch — non-bloccante
log "--- feature_pitch.py $FP_DRY ---"
python3 _system/scripts/feature_pitch.py $FP_DRY >> "$LOG_FILE" 2>&1 || \
    log "feature_pitch.py errore non bloccante (continuo)"

# Step 5: commit+push finale dello stato. feature_pitch (e gli step
# precedenti) scrivono file TRACKED (feature_pitch_log.jsonl, cache
# scraper, send_log...) DOPO il push interno di publish_all_drafts:
# senza questo commit il working tree resta sporco, il pull del run
# successivo fallirebbe e i due rail divergerebbero (split-brain).
if [ "${DRY_RUN:-0}" != "1" ]; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        log "--- commit finale stato post-run ---"
        git add -A >> "$LOG_FILE" 2>&1
        git commit -m "Daily pipeline state ${TODAY} (post-run) [skip ci]" \
            >> "$LOG_FILE" 2>&1 || true
        git push origin main >> "$LOG_FILE" 2>&1 || \
            log "  ⚠ push stato finale fallito (il prossimo run farà pull --autostash)"
    fi
fi

# Riga canonica SOLO a successo: i poll successivi della giornata
# diventano no-op. Su fallimento il poll dopo 30 min ritenta.
if [ $GEN_EXIT -eq 0 ] && [ $PUB_EXIT -eq 0 ]; then
    if [ "${DRY_RUN:-0}" = "1" ]; then
        log "=== daily_publish END (dry-run) ==="
    else
        [ "$RADAR_OK" = "0" ] && \
            log "Nota: radar saltato (timeout/rete); digest e outreach inviati comunque."
        log "=== daily_publish END ==="
    fi
    exit 0
fi

log "=== daily_publish END (failed gen=$GEN_EXIT pub=$PUB_EXIT) ==="
if [ $GEN_EXIT -ne 0 ]; then
    exit $GEN_EXIT
fi
exit $PUB_EXIT
