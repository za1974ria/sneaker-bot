#!/bin/bash
# SneakerBot Weekly Cleanup — purge logs, cache Playwright, tmp orphelins

LOG=/root/sneaker_bot/cleanup.log
APP_DIR=/root/sneaker_bot

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

log "START weekly cleanup"

# 1. Vieux logs uvicorn (> 14 jours)
find /var/log -name "sneaker*" -mtime +14 -delete 2>/dev/null
find "$APP_DIR" -name "*.log" -mtime +14 -delete 2>/dev/null

# 2. Cache Playwright chromium > 7 jours
find /root/.cache/ms-playwright -name "*.tmp" -mtime +7 -delete 2>/dev/null
find /tmp -name ".org.chromium.*" -mtime +1 -delete 2>/dev/null
find /tmp -name "playwright*" -mtime +1 -delete 2>/dev/null

# 3. Fichiers __pycache__ orphelins
find "$APP_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# 4. Truncate watchdog log si > 1000 lignes
WLOG="$APP_DIR/watchdog.log"
if [ -f "$WLOG" ] && [ "$(wc -l < "$WLOG")" -gt 1000 ]; then
    tail -n 300 "$WLOG" > "${WLOG}.tmp" && mv "${WLOG}.tmp" "$WLOG"
    log "watchdog.log truncated"
fi

log "DONE"
