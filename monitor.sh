#!/usr/bin/env bash
# monitor.sh — Health check complet SneakerBot
# Usage : ./monitor.sh [--quiet]   (--quiet : affiche seulement si problème)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

QUIET=false
[[ "${1:-}" == "--quiet" ]] && QUIET=true

PORT=$(grep -oP '(?<=--port )\d+' /etc/systemd/system/sneaker_bot.service 2>/dev/null | head -1)
PORT="${PORT:-5003}"
HEALTH_URL="http://localhost:${PORT}/health"
CSV_MAX_AGE_MIN=90   # alerte si CSV plus vieux que 90 min
ISSUES=()

ok()   { $QUIET || echo "  OK  $*"; }
warn() { echo "  WARN $*"; ISSUES+=("$*"); }
fail() { echo "  FAIL $*"; ISSUES+=("$*"); }

$QUIET || echo "=== SneakerBot Monitor — $(date '+%Y-%m-%d %H:%M:%S') ==="

# ── 1. Services systemd ───────────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ Services ]"

for svc in sneaker_bot sneaker_celery; do
  STATE=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
  if [[ "$STATE" == "active" ]]; then
    ok "$svc = active"
  else
    fail "$svc = $STATE"
  fi
done

# ── 2. Redis ──────────────────────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ Redis ]"
if redis-cli ping > /dev/null 2>&1; then
  ok "Redis répond (PONG)"
else
  fail "Redis ne répond pas"
fi

# ── 3. Health endpoint ────────────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ Health endpoint ]"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "000")
if [[ "$HTTP" == "200" ]]; then
  ok "GET /health → 200"
else
  fail "GET /health → $HTTP (attendu 200)"
fi

HEALTH_JSON=""
if [[ "$HTTP" == "200" ]]; then
  HEALTH_JSON=$(curl -sf "$HEALTH_URL" 2>/dev/null || echo "{}")
  HEALTH_OK=$(echo "$HEALTH_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ok','?'))" 2>/dev/null || echo "?")
  if [[ "$HEALTH_OK" == "True" ]]; then
    ok "health.ok = true"
  else
    warn "health.ok = $HEALTH_OK"
  fi
fi

# ── 4. CSV fraîcheur ─────────────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ CSV data ]"
if [[ -n "$HEALTH_JSON" ]]; then
  CSV_DATA=$(echo "$HEALTH_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
fr=d.get('data_freshness',{}).get('market_fr_csv',{})
print(fr.get('rows','?'), fr.get('age_minutes','?'), fr.get('stale','?'))
" 2>/dev/null || echo "? ? ?")
  ROWS=$(echo "$CSV_DATA" | awk '{print $1}')
  AGE=$(echo "$CSV_DATA" | awk '{print $2}')
  STALE=$(echo "$CSV_DATA" | awk '{print $3}')

  if [[ "$ROWS" == "148" ]]; then
    ok "market_fr.csv = $ROWS rows"
  else
    warn "market_fr.csv = $ROWS rows (attendu 148)"
  fi

  if [[ "$STALE" == "False" ]]; then
    ok "CSV fraîcheur OK (age=${AGE}min)"
  else
    AGE_INT=${AGE%.*}
    if [[ "$AGE_INT" -gt "$CSV_MAX_AGE_MIN" ]] 2>/dev/null; then
      warn "CSV trop ancien : ${AGE}min > ${CSV_MAX_AGE_MIN}min"
    else
      ok "CSV age = ${AGE}min"
    fi
  fi
fi

# ── 5. Celery worker vivant ───────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ Celery ]"
CELERY_PID=$(systemctl show sneaker_celery -p MainPID --value 2>/dev/null || echo "0")
if [[ "$CELERY_PID" -gt 1 ]] 2>/dev/null; then
  RSS_KB=$(cat /proc/"$CELERY_PID"/status 2>/dev/null | grep VmRSS | awk '{print $2}' || echo "0")
  RSS_MB=$((RSS_KB / 1024))
  ok "Celery PID=$CELERY_PID RSS=${RSS_MB}MB"
  if [[ "$RSS_MB" -gt 1400 ]]; then
    warn "Celery mémoire élevée : ${RSS_MB}MB (limite 1500MB)"
  fi
else
  fail "Celery PID introuvable"
fi

# ── 6. Permissions critiques ──────────────────────────────────────────────────
$QUIET || echo ""
$QUIET || echo "[ Permissions ]"
for f in .env data/users.db data/sneakerbot.db; do
  if [[ -f "$f" ]]; then
    PERM=$(stat -c "%a" "$f" 2>/dev/null)
    if [[ "$PERM" == "600" ]]; then
      ok "$f = 600"
    else
      warn "$f = $PERM (doit être 600)"
      chmod 600 "$f"
      warn "  → corrigé automatiquement"
    fi
  fi
done

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
if [[ ${#ISSUES[@]} -eq 0 ]]; then
  echo "╔══════════════════════════════════════════╗"
  echo "║  ALL OK — SneakerBot opérationnel       ║"
  echo "╚══════════════════════════════════════════╝"
  exit 0
else
  echo "╔══════════════════════════════════════════╗"
  echo "║  PROBLÈMES DÉTECTÉS                     ║"
  for issue in "${ISSUES[@]}"; do
    printf "║  → %-38s║\n" "$issue"
  done
  echo "╚══════════════════════════════════════════╝"
  exit 1
fi
