#!/usr/bin/env bash
# backup.sh — Backup SneakerBot avec vérification intégrité SQLite
# Usage : ./backup.sh [--verify]   (--verify : integrity_check sur chaque DB)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VERIFY=false
[[ "${1:-}" == "--verify" ]] && VERIFY=true

BACKUP_DIR=/root/backups
APP_DIR=/root/sneaker_bot
TIMESTAMP=$(date '+%Y%m%d_%H%M')
ARCHIVE="${BACKUP_DIR}/sneakerbot_${TIMESTAMP}.tar.gz"
LOG="${BACKUP_DIR}/backup.log"

mkdir -p "$BACKUP_DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

log "START backup → $ARCHIVE"

# ── Intégrité SQLite avant backup ─────────────────────────────────────────────
if [[ "$VERIFY" == true ]]; then
  log "Vérification intégrité SQLite..."
  ALL_OK=true
  for db in data/*.db; do
    [[ -f "$db" ]] || continue
    RESULT=$(sqlite3 "$db" "PRAGMA integrity_check;" 2>/dev/null || echo "ERROR")
    if [[ "$RESULT" == "ok" ]]; then
      log "  OK  $db"
    else
      log "  WARN $db : $RESULT"
      ALL_OK=false
    fi
  done
  $ALL_OK && log "Toutes les DB intègres" || log "WARNING : certaines DB ont des anomalies"
fi

# ── Archive complète ───────────────────────────────────────────────────────────
tar -czf "$ARCHIVE" \
    --exclude="${APP_DIR}/venv" \
    --exclude="${APP_DIR}/__pycache__" \
    --exclude="${APP_DIR}/.git" \
    --exclude="${APP_DIR}/data/*.tmp" \
    "$APP_DIR" 2>> "$LOG"

SIZE=$(du -sh "$ARCHIVE" | cut -f1)
log "OK archive=$ARCHIVE size=$SIZE"

# ── Rotation : garder 7 dernières archives ────────────────────────────────────
cd "$BACKUP_DIR"
ls -t sneakerbot_*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
KEPT=$(ls sneakerbot_*.tar.gz 2>/dev/null | wc -l)
log "ROTATION kept=$KEPT archives"

log "DONE"
