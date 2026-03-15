#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  ALEXADS BOT — ONE COMMAND INSTALLER
#  Naye RDP/System pe sirf yeh ek command:
#  bash <(curl -sL https://raw.githubusercontent.com/ashswin-git/ADSENSETGBOT/main/install.sh)
# ═══════════════════════════════════════════════════════════

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

REPO="https://github.com/ashswin-git/ADSENSETGBOT.git"
BOT_DIR="$HOME/ADSENSETGBOT"

echo -e "${CYAN}${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║      🍂 ALEXADS BOT — AUTO INSTALLER     ║"
echo "║      Naye System pe Complete Setup       ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Detect OS ──────────────────────────────────────────────
if command -v apt-get &>/dev/null; then
    OS="ubuntu"
elif command -v pkg &>/dev/null; then
    OS="termux"
elif command -v yum &>/dev/null; then
    OS="centos"
else
    OS="unknown"
fi
echo -e "${CYAN}[INFO] OS: $OS${NC}"

# ── Step 1: Install Dependencies ───────────────────────────
echo -e "\n${YELLOW}[1/6] Dependencies install ho rahe hain...${NC}"
if [ "$OS" = "termux" ]; then
    pkg update -y -q 2>/dev/null
    pkg install -y python git sqlite screen curl 2>/dev/null
    pip install telethon --quiet --break-system-packages 2>/dev/null
elif [ "$OS" = "ubuntu" ]; then
    sudo apt-get update -qq 2>/dev/null
    sudo apt-get install -y python3 python3-pip git curl screen sqlite3 2>/dev/null
    pip3 install telethon --quiet 2>/dev/null
    # Alias python → python3 if needed
    if ! command -v python &>/dev/null; then
        sudo ln -sf /usr/bin/python3 /usr/bin/python 2>/dev/null || true
    fi
elif [ "$OS" = "centos" ]; then
    sudo yum install -y python3 git curl screen sqlite 2>/dev/null
    pip3 install telethon --quiet 2>/dev/null
fi
echo -e "${GREEN}✅ Dependencies ready!${NC}"

# ── Step 2: Clone/Update Repo ──────────────────────────────
echo -e "\n${YELLOW}[2/6] GitHub se code le raha hoon...${NC}"
if [ -d "$BOT_DIR/.git" ]; then
    cd "$BOT_DIR" && git pull origin main --quiet 2>/dev/null || git pull origin master --quiet 2>/dev/null
    echo -e "${GREEN}✅ Code updated!${NC}"
else
    git clone "$REPO" "$BOT_DIR" --quiet 2>/dev/null
    echo -e "${GREEN}✅ Repo cloned!${NC}"
fi
cd "$BOT_DIR"

# ── Step 3: Restore DB from GitHub ─────────────────────────
echo -e "\n${YELLOW}[3/6] GitHub se data restore ho raha hai...${NC}"
DB_DEST="$HOME/bot_data.db"

if [ -f "$BOT_DIR/data/bot_data.db" ]; then
    cp "$BOT_DIR/data/bot_data.db" "$DB_DEST"
    echo -e "${GREEN}✅ DB restored from GitHub!${NC}"
    # Show stats
    if command -v sqlite3 &>/dev/null; then
        USERS=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM users;" 2>/dev/null || echo "?")
        CODES=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM access_codes;" 2>/dev/null || echo "?")
        TASKS=$(sqlite3 "$DB_DEST" "SELECT COUNT(*) FROM scheduled_tasks;" 2>/dev/null || echo "?")
        echo -e "   ${CYAN}👥 Users: $USERS | 🔑 Codes: $CODES | ⏰ Tasks: $TASKS${NC}"
    fi
else
    echo -e "${YELLOW}⚠️ GitHub pe koi DB nahi mili — fresh start hoga.${NC}"
    echo -e "   ${CYAN}Bot chalane ke baad /backup se pehla backup karo.${NC}"
fi

# ── Step 4: Setup .env ─────────────────────────────────────
echo -e "\n${YELLOW}[4/6] Config setup...${NC}"
if [ -f "$BOT_DIR/.env" ]; then
    echo -e "${GREEN}✅ .env already hai (GitHub se aaya)!${NC}"
    cat "$BOT_DIR/.env"
else
    echo -e "${RED}❌ .env nahi mila!${NC}"
    echo -e "${YELLOW}Apna config enter karo:${NC}"
    read -p "API_ID: " API_ID
    read -p "API_HASH: " API_HASH
    read -p "BOT_TOKEN: " BOT_TOKEN
    read -p "ADMIN_ID: " ADMIN_ID
    cat > "$BOT_DIR/.env" << ENVEOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
ADMIN_ID=$ADMIN_ID
ENVEOF
    echo -e "${GREEN}✅ .env bana diya!${NC}"
fi

# ── Step 5: Git Config for auto-push ───────────────────────
echo -e "\n${YELLOW}[5/6] Git auto-sync setup...${NC}"
cd "$BOT_DIR"
git config user.email "alexadsbot@auto.com" 2>/dev/null || true
git config user.name "AlexAds Bot" 2>/dev/null || true

# ── Step 6: Make scripts executable ────────────────────────
echo -e "\n${YELLOW}[6/6] Scripts ready kar raha hoon...${NC}"
chmod +x "$BOT_DIR"/*.sh 2>/dev/null || true
echo -e "${GREEN}✅ Done!${NC}"

# ── Summary ────────────────────────────────────────────────
echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║        ✅ SETUP COMPLETE!                 ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "${BOLD}▶️  Bot chalao:${NC}"
echo -e "   ${GREEN}cd ~/ADSENSETGBOT && bash run.sh${NC}"
echo ""
echo -e "${BOLD}📤 Manual GitHub sync:${NC}"
echo -e "   ${GREEN}bash ~/ADSENSETGBOT/git_sync.sh${NC}"
echo ""

read -p "$(echo -e ${CYAN}Ab bot start karein? [y/n]: ${NC})" ANS
if [[ "$ANS" == "y" || "$ANS" == "Y" ]]; then
    cd "$BOT_DIR"
    source .env 2>/dev/null || export $(cat .env | xargs) 2>/dev/null || true
    bash run.sh
fi
