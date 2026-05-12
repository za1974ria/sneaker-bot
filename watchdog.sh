#!/bin/bash
# SneakerBot Watchdog — vérifie le service toutes les 5 min via cron
# Redémarre si /health répond autre chose que 200

LOG=/root/sneaker_bot/watchdog.log
MAX_LOG_LINES=500
HEALTH_URL="http://127.0.0.1:5003/health"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

# Rotation légère du log
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$MAX_LOG_LINES" ]; then
    tail -n 200 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi

# Check HTTP
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ]; then
    # Service OK — log silencieux (1 ligne/heure max pour éviter spam)
    MINUTE=$(date +%M)
    if [ "$MINUTE" = "00" ]; then
        log "OK health=200"
    fi
else
    log "WARN health=$HTTP_CODE — restart sneaker_bot"
    systemctl restart sneaker_bot
    sleep 8
    HTTP_CODE2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null)
    if [ "$HTTP_CODE2" = "200" ]; then
        log "RECOVERED after restart health=200"
    else
        log "CRITICAL restart failed health=$HTTP_CODE2"
    fi
fi
