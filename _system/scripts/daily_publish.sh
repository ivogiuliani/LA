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

TODAY="$(date +%Y-%m-%d)"
RADAR_FILE="_system/radar/reports/radar_${TODAY}.json"
LOG_DIR="_system/logs"
LOG_FILE="${LOG_DIR}/daily_publish_${TODAY}.log"
LOCK_FILE="${LOG_DIR}/.daily_publish.lock"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Lock semplice (no flock su macOS by default — uso mkdir, atomico).
if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    log "Pipeline già in esecuzione (lock presente). Esco."
    exit 0
fi
trap 'rmdir "$LOCK_FILE" 2>/dev/null' EXIT

log "=== daily_publish START ==="
log "PWD: $(pwd)"
log "Python: $(which python3) ($(python3 --version 2>&1))"

# Step 1: radar di oggi?
if [ ! -f "$RADAR_FILE" ]; then
    log "Radar di oggi non trovato: $RADAR_FILE — skip pipeline."
    log "=== daily_publish END (no radar) ==="
    exit 0
fi
log "Radar trovato: $RADAR_FILE ($(stat -f '%z' "$RADAR_FILE") bytes)"

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

log "=== daily_publish END ==="

# Exit code: 0 se entrambi 0, altrimenti il primo non-zero
if [ $GEN_EXIT -ne 0 ]; then
    exit $GEN_EXIT
fi
exit $PUB_EXIT
