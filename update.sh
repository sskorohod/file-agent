#!/usr/bin/env bash
# Smart Storage — Updater
# Usage: sudo bash /opt/smart-storage/update.sh

set -euo pipefail

APP_NAME="smart-storage"
APP_DIR="/opt/$APP_NAME"
APP_USER="smartstorage"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

[[ $EUID -ne 0 ]] && err "Run as root: sudo bash update.sh"
[[ ! -d "$APP_DIR/.git" ]] && err "App not found at $APP_DIR"

cd "$APP_DIR"

log "Backing up database..."
cp -f data/agent.db "data/agent.db.bak.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

log "Pulling latest code..."
sudo -u "$APP_USER" git pull origin main

log "Updating Python dependencies..."
sudo -u "$APP_USER" .venv/bin/pip install -q -r requirements.txt

log "Rebuilding CSS..."
if command -v npx &>/dev/null; then
    sudo -u "$APP_USER" npx tailwindcss -i app/web/static/src/input.css -o app/web/static/css/styles.css --minify 2>/dev/null || true
fi

log "Restarting service..."
systemctl restart "$APP_NAME"
sleep 2

if systemctl is-active --quiet "$APP_NAME"; then
    log "Update complete! Smart Storage is running."
    echo -e "  Logs: ${YELLOW}journalctl -u $APP_NAME -f${NC}"
else
    err "Service failed to restart. Check: journalctl -u $APP_NAME -n 50"
fi
