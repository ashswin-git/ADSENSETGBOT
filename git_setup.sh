#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  ALEXADS BOT — GITHUB TOKEN SETUP
#  Ek baar chalaao, phir auto-sync kaam karega
# ═══════════════════════════════════════════════════════════

BOT_DIR="$HOME/ADSENSETGBOT"
CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'; BOLD='\033[1m'

echo -e "${CYAN}${BOLD}"
echo "╔══════════════════════════════════════╗"
echo "║    🔑 GITHUB TOKEN SETUP              ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

echo -e "${YELLOW}Step 1: GitHub pe token banao:${NC}"
echo "  1. github.com pe login karo"
echo "  2. Top-right profile → Settings"
echo "  3. Left sidebar → Developer settings"
echo "  4. Personal access tokens → Tokens (classic)"
echo "  5. Generate new token (classic)"
echo "  6. Note: 'alexads-bot'"
echo "  7. Expiration: No expiration"
echo "  8. Scopes: ✅ repo (tick karo)"
echo "  9. Generate token → Copy karo"
echo ""

read -p "$(echo -e ${CYAN}Token paste karo: ${NC})" TOKEN

if [ -z "$TOKEN" ]; then
    echo "❌ Token empty! Dobara chalaao."
    exit 1
fi

# Save token
echo "$TOKEN" > "$BOT_DIR/.git_token"
chmod 600 "$BOT_DIR/.git_token"

# Update git remote with token
cd "$BOT_DIR"
REPO_URL=$(git config --get remote.origin.url)
USERNAME=$(echo "$REPO_URL" | sed 's|https://github.com/||' | cut -d'/' -f1)
REPONAME=$(echo "$REPO_URL" | sed 's|https://github.com/||' | cut -d'/' -f2 | sed 's/.git//')

NEW_URL="https://$TOKEN@github.com/$USERNAME/$REPONAME.git"
git remote set-url origin "$NEW_URL"
git config user.email "alexadsbot@backup.com"
git config user.name "AlexAds AutoSync"

# Create data folder + .gitkeep
mkdir -p "$BOT_DIR/data"
touch "$BOT_DIR/data/.gitkeep"

# Make sure data/bot_data.db is tracked (not in gitignore)
# Remove *.db from gitignore for data/ folder only
if grep -q "^\*.db" "$BOT_DIR/.gitignore" 2>/dev/null; then
    # Add exception
    echo "!data/bot_data.db" >> "$BOT_DIR/.gitignore"
fi

# Test push
echo -e "\n${YELLOW}GitHub se test sync ho raha hai...${NC}"
git add data/ .gitignore 2>/dev/null
git commit -m "🔧 Setup: data folder aur gitignore update" --quiet 2>/dev/null || true
if git push origin main --quiet 2>/dev/null || git push origin master --quiet 2>/dev/null; then
    echo -e "${GREEN}✅ Token kaam kar raha hai! GitHub sync ready!${NC}"
else
    echo -e "${RED}❌ Push failed. Token check karo.${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}${BOLD}✅ Setup Complete!${NC}"
echo ""
echo -e "${BOLD}Ab bot chalao:${NC}"
echo -e "  ${GREEN}bash ~/ADSENSETGBOT/run.sh${NC}"
echo ""
echo -e "${BOLD}Manual sync:${NC}"
echo -e "  ${GREEN}bash ~/ADSENSETGBOT/git_sync.sh${NC}"
