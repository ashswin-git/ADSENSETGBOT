#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  ALEXADS BOT — AUTO RESTART + AUTO GIT SYNC RUNNER
#  Features:
#  - Bot crash pe auto restart
#  - Har 6 ghante GitHub pe DB push
#  - Crash pe bhi backup
# ═══════════════════════════════════════════════════════════

BOT_DIR="$HOME/ADSENSETGBOT"
LOG="$BOT_DIR/bot.log"
SYNC_INTERVAL=21600   # 6 ghante (seconds)
RESTART_COUNT=0
LAST_SYNC=0

cd "$BOT_DIR" || exit 1

# Load .env
if [ -f "$BOT_DIR/.env" ]; then
    set -a; source "$BOT_DIR/.env"; set +a
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

sync_to_github() {
    log "🔄 GitHub sync ho raha hai..."
    if bash "$BOT_DIR/git_sync.sh" 2>&1 | tee -a "$LOG"; then
        log "✅ GitHub sync done!"
    else
        log "⚠️ GitHub sync failed (check token)"
    fi
    LAST_SYNC=$(date +%s)
}

# First sync on start
sync_to_github

while true; do
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "🚀 Bot start ho raha hai... (Restart #$RESTART_COUNT)"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    python bot.py 2>&1 | tee -a "$LOG" &
    BOT_PID=$!

    # Monitor loop — check bot + auto sync
    while kill -0 $BOT_PID 2>/dev/null; do
        NOW=$(date +%s)
        ELAPSED=$((NOW - LAST_SYNC))
        if [ $ELAPSED -ge $SYNC_INTERVAL ]; then
            sync_to_github
        fi
        sleep 30
    done

    # Bot ne exit kiya
    wait $BOT_PID
    EXIT_CODE=$?
    log "⚠️ Bot stopped (exit: $EXIT_CODE)"

    # Crash pe GitHub sync
    sync_to_github

    RESTART_COUNT=$((RESTART_COUNT + 1))
    if [ $RESTART_COUNT -ge 100 ]; then
        log "❌ 100 restarts ho gaye. Manual check karo!"
        break
    fi

    log "🔄 5 second mein restart..."
    sleep 5
done
