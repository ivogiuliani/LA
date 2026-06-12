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

# ── Sync + cross-rail guard ────────────────────────────────────────
# 1) pull --rebase --autostash: l'autostash è ESSENZIALE — i run
#    precedenti possono lasciare file di stato modificati (cache
#    scraper ecc.) e senza autostash il pull fallisce, il marker
#    resta stale e si rischia la doppia digest (successo il 1/6).
# 2) Il marker viene letto ANCHE da origin/main via git show: immune
#    a qualunque problema del working tree locale.
log "git pull --rebase --autostash per sincronizzare lo stato..."
if ! git pull --rebase --autostash origin main >> "$LOG_FILE" 2>&1; then
    log "  ⚠ git pull fallito — leggo comunque il marker da origin"
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

# Step 1: radar di oggi. Se manca lo lanciamo noi (auto-sufficiente).
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

# Step 1b: generate_radar_report — draft pitch + contatti editoriali +
# dashboard HTML. Non-bloccante.
log "--- generate_radar_report.py (draft + contatti + dashboard) ---"
python3 _system/scripts/generate_radar_report.py --radar "$RADAR_FILE" \
    >> "$LOG_FILE" 2>&1 || log "generate_radar_report errore non bloccante (continuo)"

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

# Step 2: generate_journal
log "--- generate_journal.py ---"
python3 _system/scripts/generate_journal.py \
    --radar "$RADAR_FILE" \
    --min-score 14 \
    --max-articles 3 \
    >> "$LOG_FILE" 2>&1
GEN_EXIT=$?
log "generate_journal.py exit code: $GEN_EXIT"

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

# Step 3b: generate_social — proposte social del giorno dal radar
# (max 2 set reactive = 2 IG + 2 X, con immagini auto). NON pubblica:
# crea solo le card da approvare nel pannello. Non-bloccante.
log "--- generate_social.py (proposte del giorno) ---"
python3 _system/scripts/generate_social.py --radar "$RADAR_FILE" \
    --max-posts 2 >> "$LOG_FILE" 2>&1 || \
    log "generate_social errore non bloccante (continuo)"

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
        log "=== daily_publish END ==="
    fi
    exit 0
fi

log "=== daily_publish END (failed gen=$GEN_EXIT pub=$PUB_EXIT) ==="
if [ $GEN_EXIT -ne 0 ]; then
    exit $GEN_EXIT
fi
exit $PUB_EXIT
