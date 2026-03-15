#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  ALEXADS BOT — GIT AUTO SYNC
#  DB ko GitHub pe push karta hai
#  Bot isko automatically call karta hai + manually bhi chala sakte ho
# ═══════════════════════════════════════════════════════════

BOT_DIR="$HOME/ADSENSETGBOT"
DB_SRC="$HOME/bot_data.db"
DB_DEST="$BOT_DIR/data/bot_data.db"
LOG="$BOT_DIR/sync.log"
STAMP=$(date -u '+%Y-%m-%d %H:%M UTC')

cd "$BOT_DIR" || exit 1

# ── Git token check ────────────────────────────────────────
if [ -f "$BOT_DIR/.git_token" ]; then
    GIT_TOKEN=$(cat "$BOT_DIR/.git_token")
    REPO_URL=$(git config --get remote.origin.url 2>/dev/null)
    # Inject token in URL
    if [[ "$REPO_URL" == https://* ]] && [[ "$REPO_URL" != *"@"* ]]; then
        REPO_WITH_TOKEN=$(echo "$REPO_URL" | sed "s|https://|https://$GIT_TOKEN@|")
        git remote set-url origin "$REPO_WITH_TOKEN" 2>/dev/null
    fi
fi

# ── Copy DB to data/ folder ────────────────────────────────
mkdir -p "$BOT_DIR/data"
if [ ! -f "$DB_SRC" ]; then
    echo "[$STAMP] ⚠️ DB nahi mili: $DB_SRC" | tee -a "$LOG"
    exit 1
fi

cp "$DB_SRC" "$DB_DEST"
DB_SIZE=$(du -sh "$DB_DEST" | cut -f1)

# ── Stats gather ───────────────────────────────────────────
USERS="?"
ACCOUNTS="?"
CODES="?"
TASKS="?"
if command -v sqlite3 &>/dev/null; then
    USERS=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM users;" 2>/dev/null || echo "?")
    ACCOUNTS=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM user_accounts;" 2>/dev/null || echo "?")
    CODES=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM access_codes;" 2>/dev/null || echo "?")
    TASKS=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM scheduled_tasks WHERE is_active=1;" 2>/dev/null || echo "?")
fi

# ── Git add + commit + push ────────────────────────────────
git add data/bot_data.db

# Check if there's anything to commit
if git diff --cached --quiet; then
    echo "[$STAMP] ℹ️ No changes in DB — skip." | tee -a "$LOG"
    exit 0
fi

COMMIT_MSG="🗄 Auto backup | $STAMP | Users:$USERS Acc:$ACCOUNTS Codes:$CODES Tasks:$TASKS | $DB_SIZE"
git commit -m "$COMMIT_MSG" --quiet 2>/dev/null

# Push
if git push origin main --quiet 2>/dev/null || git push origin master --quiet 2>/dev/null; then
    echo "[$STAMP] ✅ Pushed to GitHub | $COMMIT_MSG" | tee -a "$LOG"
    
    # Keep only last 50 log lines
    tail -50 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    exit 0
else
    echo "[$STAMP] ❌ Push failed! Token check karo." | tee -a "$LOG"
    echo ""
    echo "Fix karne ke liye:"
    echo "  1. GitHub pe Personal Access Token banao"
    echo "     github.com → Settings → Developer settings → Personal access tokens"
    echo "  2. Token save karo:"
    echo "     echo 'YOUR_TOKEN' > ~/ADSENSETGBOT/.git_token"
    echo "  3. Dobara chalaao: bash ~/ADSENSETGBOT/git_sync.sh"
    exit 1
fi
