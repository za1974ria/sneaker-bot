#!/usr/bin/env bash
# deploy.sh — Déploiement SneakerBot (restart propre avec checks)
# Usage : ./deploy.sh [--no-celery] [--skip-backup]
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

NO_CELERY=false
SKIP_BACKUP=false
for arg in "$@"; do
  [[ "$arg" == "--no-celery" ]]    && NO_CELERY=true
  [[ "$arg" == "--skip-backup" ]]  && SKIP_BACKUP=true
done

PORT=$(grep -oP '(?<=--port )\d+' /etc/systemd/system/sneaker_bot.service 2>/dev/null | head -1)
PORT="${PORT:-5003}"
HEALTH_URL="http://localhost:${PORT}/health"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] ERREUR : $*" >&2; exit 1; }

# ── 1. Pré-checks ─────────────────────────────────────────────────────────────
log "=== DEPLOY START ==="

[[ -f .env ]] || fail ".env manquant — abandon"

log "Syntaxe Python..."
venv/bin/python -c "import ast; ast.parse(open('app/app.py').read())" \
  || fail "app/app.py syntaxe invalide — abandon"

log "Redis..."
redis-cli ping > /dev/null 2>&1 || fail "Redis ne répond pas — abandon"

# ── 2. Backup rapide avant toute modification ──────────────────────────────────
if [[ "$SKIP_BACKUP" == false ]]; then
  log "Backup DB pré-déploiement..."
  BDIR="/root/backups/pre_deploy"
  mkdir -p "$BDIR"
  TS=$(date '+%Y%m%d_%H%M%S')
  cp data/users.db       "$BDIR/users_${TS}.db"       2>/dev/null || true
  cp data/sneakerbot.db  "$BDIR/sneakerbot_${TS}.db"  2>/dev/null || true
  cp data/market_fr.csv  "$BDIR/market_fr_${TS}.csv"  2>/dev/null || true
  # Garder seulement les 5 derniers pre-deploy backups par fichier
  for f in users sneakerbot market_fr; do
    ls -t "$BDIR/${f}_"*.* 2>/dev/null | tail -n +6 | xargs -r rm -f
  done
  log "Backup OK → $BDIR"
fi

# ── 3. Permissions fichiers sensibles ─────────────────────────────────────────
log "Permissions..."
chmod 600 .env data/*.db 2>/dev/null || true
chmod 700 deploy.sh rollback.sh monitor.sh backup.sh 2>/dev/null || true

# ── 4. Restart services ───────────────────────────────────────────────────────
log "Restart sneaker_bot..."
systemctl restart sneaker_bot

if [[ "$NO_CELERY" == false ]]; then
  log "Restart sneaker_celery..."
  systemctl restart sneaker_celery
fi

# ── 5. Attente health (max 30s) ────────────────────────────────────────────────
log "Attente health sur port ${PORT}..."
for i in $(seq 1 30); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    log "Health OK (${i}s)"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  DEPLOY FAILED — health KO après 30s    ║"
    echo "║  Lancer : ./rollback.sh                 ║"
    echo "╚══════════════════════════════════════════╝"
    journalctl -u sneaker_bot -n 20 --no-pager >&2
    exit 1
  fi
  sleep 1
done

# ── 6. Validation finale ───────────────────────────────────────────────────────
HEALTH_JSON=$(curl -sf "$HEALTH_URL" 2>/dev/null)
echo "$HEALTH_JSON" | grep -q '"ok":true' || fail "health endpoint retourne ok=false"

ROWS=$(echo "$HEALTH_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['data_freshness']['market_fr_csv']['rows'])" 2>/dev/null || echo "?")
log "CSV rows = $ROWS"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  DEPLOY OK                              ║"
printf "║  sneaker_bot  : %-24s║\n" "$(systemctl is-active sneaker_bot)"
$NO_CELERY || printf "║  sneaker_celery: %-24s║\n" "$(systemctl is-active sneaker_celery)"
printf "║  CSV rows     : %-24s║\n" "$ROWS"
echo "╚══════════════════════════════════════════╝"
