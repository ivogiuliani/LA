#!/bin/bash
#
# daily_publish.sh — wrapper invocato da launchd alle 08:00.
#
# Pipeline:
#   1. Cerca il radar JSON di oggi (radar_YYYY-MM-DD.json)
#   2. Lancia generate_journal.py per produrre gli articoli draft
#   3. Lancia publish_all_drafts.py per pubblicarli + spedire la mail digest
#
# Se il radar di oggi non esiste, skippa silenziosamente (esce 0):
# non ha senso generare un journal senza dati freschi.
#
# Log: _system/logs/daily_publish_YYYY-MM-DD.log
# Lock: solo un'istanza alla volta (evita doppia esecuzione se
#       launchd lancia mentre la precedente è ancora attiva).

set -uo pipefail

PROJECT_ROOT="/Users/ivogiuliani/Code/myvilla-la"
cd "$PROJECT_ROOT" || exit 1

# Ora-target: la pipeline gira solo a partire da questa ora locale.
# launchd ci invoca ogni 30 min (StartInterval), ma noi facciamo un
# near-instant no-op finché non sono almeno le TARGET_HOUR. Stesso
# pattern poll-based di Falling Knives (io.giuliani.fallingknife):
# robusto allo sleep del Mac perché launchd esegue l'intervallo
# perso APPENA il Mac si sveglia, e il primo poll dopo il wake (se
# è già passata l'ora target) fa scattare la pipeline.
TARGET_HOUR=8

TODAY="$(date +%Y-%m-%d)"
CURRENT_HOUR="$(date +%-H)"
RADAR_FILE="_system/radar/reports/radar_${TODAY}.json"
LOG_DIR="_system/logs"
LOG_FILE="${LOG_DIR}/daily_publish_${TODAY}.log"
LOCK_FILE="${LOG_DIR}/.daily_publish.lock"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ── No-op #1: troppo presto ────────────────────────────────────────
# Prima delle TARGET_HOUR non facciamo nulla e NON logghiamo (così
# il log non si riempie di righe a ogni poll notturno).
if [ "$CURRENT_HOUR" -lt "$TARGET_HOUR" ]; then
    exit 0
fi

# ── No-op #2: già fatto oggi ───────────────────────────────────────
# Se la pipeline di oggi è già stata completata con successo, esci
# SENZA loggare. Con un poll ogni 30 min, dopo il run delle ~08:00
# questo no-op scatta ~32 volte al giorno: niente rumore nel log.
# Criterio di "successo": il log di oggi contiene "=== daily_publish END ==="
if [ -f "$LOG_FILE" ] && grep -q "=== daily_publish END ===" "$LOG_FILE"; then
    exit 0
fi

# ── Lock: una sola istanza alla volta ──────────────────────────────
# (no flock su macOS by default — uso mkdir, atomico).
if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    # Un'altra istanza sta già girando — esci silenzioso.
    exit 0
fi
trap 'rmdir "$LOCK_FILE" 2>/dev/null' EXIT

log "=== daily_publish START ==="
log "PWD: $(pwd)"
log "Python: $(which python3) ($(python3 --version 2>&1))"

# ── Sync + cross-rail guard ────────────────────────────────────────
# Allinea il repo a origin/main così vediamo il marker .last_digest_date
# scritto dall'ALTRO scheduler (GitHub Actions). Se il cloud ha già
# fatto il run di oggi, il marker è la data di oggi → usciamo SENZA
# lanciare radar/journal/publish (niente spreco, niente mail doppia).
log "git pull --rebase per sincronizzare il marker cross-rail..."
git pull --rebase origin main >> "$LOG_FILE" 2>&1 || log "  (git pull fallito — proseguo con lo stato locale)"

MARKER_FILE="_system/outreach/.last_digest_date"
if [ -f "$MARKER_FILE" ] && [ "$(cat "$MARKER_FILE" 2>/dev/null | tr -d '[:space:]')" = "$TODAY" ]; then
    log "Digest di oggi già inviato da GitHub Actions (marker=$TODAY) — skip."
    log "=== daily_publish END (cloud already ran) ==="
    exit 0
fi

# Step 1: radar di oggi. Se manca, lo LANCIAMO noi — la pipeline è
# auto-sufficiente, non dipende più da scheduler esterni (Automator).
if [ ! -f "$RADAR_FILE" ]; then
    log "Radar di oggi non trovato — lancio radar.py..."
    python3 _system/scripts/radar.py >> "$LOG_FILE" 2>&1
    RADAR_EXIT=$?
    log "radar.py exit code: $RADAR_EXIT"
    if [ ! -f "$RADAR_FILE" ]; then
        log "radar.py non ha prodotto $RADAR_FILE — skip pipeline."
        log "=== daily_publish END (radar failed) ==="
        exit 1
    fi
fi
log "Radar trovato: $RADAR_FILE ($(stat -f '%z' "$RADAR_FILE") bytes)"

# Step 1b: generate_radar_report — arricchisce il radar JSON con i draft
# email + i contatti editoriali (via editorial scraper, solo verificati)
# e produce l'HTML dashboard. SENZA questo step publish_all_drafts non
# trova pitch da inviare (nessun giornalista nuovo contattato) e non si
# genera la dashboard. Non-bloccante: se fallisce, si prosegue con
# journal+publish (i follow-up e i feature pitch girano comunque).
log "--- generate_radar_report.py (draft + contatti + dashboard) ---"
python3 _system/scripts/generate_radar_report.py --radar "$RADAR_FILE" \
    >> "$LOG_FILE" 2>&1 || log "generate_radar_report errore non bloccante (continuo)"

# DRY_RUN=1 → simula senza spedire mail né pubblicare (solo per smoke test)
DRY_FLAGS=""
if [ "${DRY_RUN:-0}" = "1" ]; then
    log "DRY_RUN=1 — modalità simulazione, niente email/push"
    DRY_FLAGS="--dry-run --no-email --no-push"
fi

# Step 2: generate_journal
log "--- generate_journal.py ---"
python3 _system/scripts/generate_journal.py \
    --radar "$RADAR_FILE" \
    --min-score 14 \
    --max-articles 3 \
    >> "$LOG_FILE" 2>&1
GEN_EXIT=$?
log "generate_journal.py exit code: $GEN_EXIT"

# Anche se generate_journal non ha prodotto nulla (nessun candidato sopra
# soglia), publish_all_drafts gestisce la coda vuota: niente da fare,
# nessuna mail. Quindi continuo a prescindere.

# Step 3: publish_all_drafts
log "--- publish_all_drafts.py $DRY_FLAGS ---"
python3 _system/scripts/publish_all_drafts.py $DRY_FLAGS \
    >> "$LOG_FILE" 2>&1
PUB_EXIT=$?
log "publish_all_drafts.py exit code: $PUB_EXIT"

# Step 4: feature_pitch — proporre a 1-2 testate locali/giorno un
# articolo su My Villa. Self-contained (dedup via feature_pitch_log,
# rate-limit via send_email, solo email verificate auto-inviate).
# Un fallimento qui NON deve rompere la pipeline → || true.
log "--- feature_pitch.py $DRY_FLAGS ---"
FP_DRY=""
[ "${DRY_RUN:-0}" = "1" ] && FP_DRY="--dry-run"
python3 _system/scripts/feature_pitch.py $FP_DRY >> "$LOG_FILE" 2>&1 || \
    log "feature_pitch.py errore non bloccante (continuo)"

log "=== daily_publish END ==="

# Exit code: 0 se entrambi 0, altrimenti il primo non-zero
if [ $GEN_EXIT -ne 0 ]; then
    exit $GEN_EXIT
fi
exit $PUB_EXIT
