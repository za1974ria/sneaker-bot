#!/usr/bin/env bash
# rollback.sh — Rollback d'urgence SneakerBot
# Usage : ./rollback.sh [--from-backup ARCHIVE.tar.gz]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT=$(grep -oP '(?<=--port )\d+' /etc/systemd/system/sneaker_bot.service 2>/dev/null | head -1)
PORT="${PORT:-5003}"
HEALTH_URL="http://localhost:${PORT}/health"
BDIR="/root/backups/pre_deploy"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] ERREUR : $*" >&2; exit 1; }

log "=== ROLLBACK START ==="

# ── Option 1 : rollback depuis archive tar ────────────────────────────────────
if [[ "${1:-}" == "--from-backup" && -n "${2:-}" ]]; then
  ARCHIVE="$2"
  [[ -f "$ARCHIVE" ]] || fail "Archive introuvable : $ARCHIVE"
  log "Extraction depuis $ARCHIVE..."
  tar -xzf "$ARCHIVE" -C / --overwrite 2>&1 | tail -5
  log "Extraction OK"
fi

# ── Option 2 : restore DB depuis pre_deploy (défaut) ─────────────────────────
if [[ "${1:-}" != "--from-backup" ]]; then
  log "Recherche derniers backups DB dans $BDIR..."
  for f in users sneakerbot; do
    LATEST=$(ls -t "$BDIR/${f}_"*.db 2>/dev/null | head -1 || true)
    if [[ -n "$LATEST" ]]; then
      log "Restore $f.db depuis $(basename "$LATEST")"
      cp "$LATEST" "data/${f}.db"
    else
      log "Pas de backup pour $f — ignoré"
    fi
  done
  LATEST_CSV=$(ls -t "$BDIR/market_fr_"*.csv 2>/dev/null | head -1 || true)
  if [[ -n "$LATEST_CSV" ]]; then
    log "Restore market_fr.csv depuis $(basename "$LATEST_CSV")"
    cp "$LATEST_CSV" data/market_fr.csv
  fi
fi

# ── Permissions ───────────────────────────────────────────────────────────────
chmod 600 .env data/*.db 2>/dev/null || true

# ── Restart services ──────────────────────────────────────────────────────────
log "Restart sneaker_bot..."
systemctl restart sneaker_bot

log "Restart sneaker_celery..."
systemctl restart sneaker_celery

# ── Attente health (max 30s) ──────────────────────────────────────────────────
log "Attente health sur port ${PORT}..."
for i in $(seq 1 30); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    log "Health OK (${i}s)"
    break
  fi
  [[ $i -eq 30 ]] && fail "Health toujours KO après 30s — intervention manuelle requise"
  sleep 1
done

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ROLLBACK OK                            ║"
printf "║  sneaker_bot  : %-24s║\n" "$(systemctl is-active sneaker_bot)"
printf "║  sneaker_celery: %-23s║\n" "$(systemctl is-active sneaker_celery)"
echo "╚══════════════════════════════════════════╝"
