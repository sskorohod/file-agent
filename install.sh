#!/usr/bin/env bash
# Smart Storage — VPS Installer
# Usage: curl -sSL https://raw.githubusercontent.com/sskorohod/file-agent/main/install.sh | bash
#
# Supports: Ubuntu 20+, Debian 11+, CentOS/RHEL 8+, Fedora, Arch
# Installs: Python 3.12+, venv, app, systemd service, nginx reverse proxy, SSL

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
APP_NAME="smart-storage"
APP_DIR="/opt/$APP_NAME"
APP_USER="smartstorage"
REPO_URL="https://github.com/sskorohod/file-agent.git"
BRANCH="main"
DOMAIN=""
EMAIL=""
PORT=8000

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

# ── Pre-flight checks ──────────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Run as root: sudo bash install.sh"

echo -e "${BLUE}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║       Smart Storage Installer        ║"
echo "  ║   AI Document Intelligence Agent     ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── Interactive config ─────────────────────────────────────────────
read -rp "Domain name (e.g. files.example.com, empty for IP only): " DOMAIN
if [[ -n "$DOMAIN" ]]; then
    read -rp "Email for SSL certificate: " EMAIL
fi
read -rp "Telegram Bot Token: " TG_TOKEN
read -rp "Master Password for encryption: " MASTER_PWD

# ── Detect package manager ─────────────────────────────────────────
install_pkg() {
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq "$@"
    elif command -v dnf &>/dev/null; then
        dnf install -y -q "$@"
    elif command -v pacman &>/dev/null; then
        pacman -S --noconfirm "$@"
    else
        err "No supported package manager found (apt/dnf/pacman)"
    fi
}

# ── Install system deps ───────────────────────────────────────────
log "Installing system dependencies..."
install_pkg python3 python3-venv python3-pip git nginx curl

# Install certbot if domain provided
if [[ -n "$DOMAIN" ]]; then
    install_pkg certbot python3-certbot-nginx 2>/dev/null || \
    install_pkg certbot 2>/dev/null || \
    warn "Certbot not available in repos, skipping SSL auto-setup"
fi

# Install tesseract for OCR (optional)
install_pkg tesseract-ocr 2>/dev/null || warn "Tesseract OCR not available, OCR disabled"

# ── Check Python version ──────────────────────────────────────────
PYTHON=$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3)
PY_VER=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
log "Python: $PYTHON ($PY_VER)"

# ── Create app user ───────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -m -d "$APP_DIR" -s /bin/bash "$APP_USER"
    log "Created user: $APP_USER"
fi

# ── Clone/update repo ─────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    log "Updating existing installation..."
    cd "$APP_DIR"
    sudo -u "$APP_USER" git pull origin "$BRANCH"
else
    log "Cloning repository..."
    git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
    chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
fi

cd "$APP_DIR"

# ── Python venv + deps ────────────────────────────────────────────
log "Setting up Python virtual environment..."
sudo -u "$APP_USER" $PYTHON -m venv .venv
sudo -u "$APP_USER" .venv/bin/pip install -q --upgrade pip
sudo -u "$APP_USER" .venv/bin/pip install -q -r requirements.txt

# ── Node + Tailwind CSS build ─────────────────────────────────────
if command -v npm &>/dev/null; then
    log "Building CSS..."
    sudo -u "$APP_USER" npm install --silent 2>/dev/null || true
    sudo -u "$APP_USER" npx tailwindcss -i app/web/static/src/input.css -o app/web/static/css/styles.css --minify 2>/dev/null || true
else
    warn "npm not found, skipping CSS build (using pre-built CSS)"
fi

# ── Configure .env ────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    log "Creating .env configuration..."
    SESSION_SECRET=$(openssl rand -base64 32)
    PW_HASH=$(.venv/bin/python -c "import bcrypt; print(bcrypt.hashpw(b'changeme', bcrypt.gensalt()).decode())")
    cat > .env << ENVEOF
# Telegram
TELEGRAM_BOT_TOKEN=$TG_TOKEN

# LLM Providers (fill in your keys)
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
OPENAI_API_KEY=

# Qdrant (if using remote vector DB)
QDRANT__HOST=localhost
QDRANT__PORT=6333
QDRANT__API_KEY=

# Web Auth
WEB__SESSION_SECRET=$SESSION_SECRET
WEB__LOGIN=admin
WEB__PASSWORD_HASH=$PW_HASH

# Encryption
MASTER_PASSWORD=$MASTER_PWD
ENCRYPTION__FILES=true
ENCRYPTION__DATABASE=true

# Telegram owner (auto-detected on first /start)
TELEGRAM__OWNER_ID=
ENVEOF
    chown "$APP_USER":"$APP_USER" .env
    chmod 600 .env
    warn "Edit .env to add API keys: nano $APP_DIR/.env"
else
    log ".env already exists, keeping current config"
fi

# ── Create data directories ───────────────────────────────────────
sudo -u "$APP_USER" mkdir -p data
sudo -u "$APP_USER" mkdir -p /home/$APP_USER/ai-agent-files

# ── Systemd service ───────────────────────────────────────────────
log "Setting up systemd service..."
cat > /etc/systemd/system/$APP_NAME.service << SVCEOF
[Unit]
Description=Smart Storage - AI Document Intelligence
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=no
ReadWritePaths=$APP_DIR /home/$APP_USER/ai-agent-files
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$APP_NAME"

# ── Nginx reverse proxy ──────────────────────────────────────────
log "Configuring nginx..."

if [[ -n "$DOMAIN" ]]; then
    SERVER_NAME="$DOMAIN"
else
    SERVER_NAME="_"
fi

# Handle both sites-available (Debian/Ubuntu) and conf.d (RHEL/Fedora)
if [[ -d /etc/nginx/sites-available ]]; then
    NGINX_CONF="/etc/nginx/sites-available/$APP_NAME"
    NGINX_LINK="/etc/nginx/sites-enabled/$APP_NAME"
else
    NGINX_CONF="/etc/nginx/conf.d/$APP_NAME.conf"
    NGINX_LINK=""
fi

cat > "$NGINX_CONF" << 'NGXEOF'
server {
    listen 80;
    server_name SERVER_NAME_PLACEHOLDER;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:PORT_PLACEHOLDER;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE/WebSocket support (MCP, HTMX)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGXEOF

sed -i "s/SERVER_NAME_PLACEHOLDER/$SERVER_NAME/g" "$NGINX_CONF"
sed -i "s/PORT_PLACEHOLDER/$PORT/g" "$NGINX_CONF"

if [[ -n "$NGINX_LINK" ]]; then
    ln -sf "$NGINX_CONF" "$NGINX_LINK"
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
fi

nginx -t && systemctl reload nginx

# ── SSL certificate ───────────────────────────────────────────────
if [[ -n "$DOMAIN" && -n "$EMAIL" ]] && command -v certbot &>/dev/null; then
    log "Obtaining SSL certificate..."
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive || \
        warn "SSL setup failed — configure manually: certbot --nginx -d $DOMAIN"
fi

# ── Firewall ──────────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow 'Nginx Full' 2>/dev/null || true
    log "Firewall: allowed Nginx Full"
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-service=http --add-service=https 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    log "Firewall: allowed HTTP/HTTPS"
fi

# ── Start the service ─────────────────────────────────────────────
log "Starting Smart Storage..."
systemctl start "$APP_NAME"
sleep 3

if systemctl is-active --quiet "$APP_NAME"; then
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Smart Storage installed successfully!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    if [[ -n "$DOMAIN" ]]; then
        echo -e "  Web UI:  ${BLUE}https://$DOMAIN${NC}"
        echo -e "  MCP:     ${BLUE}https://$DOMAIN/mcp${NC}"
    else
        IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "YOUR_IP")
        echo -e "  Web UI:  ${BLUE}http://$IP${NC}"
    fi
    echo ""
    echo -e "  Config:  ${YELLOW}$APP_DIR/.env${NC}"
    echo -e "  Logs:    ${YELLOW}journalctl -u $APP_NAME -f${NC}"
    echo -e "  Status:  ${YELLOW}systemctl status $APP_NAME${NC}"
    echo -e "  Restart: ${YELLOW}systemctl restart $APP_NAME${NC}"
    echo ""
    warn "Next steps:"
    echo "  1. Edit .env — add API keys: nano $APP_DIR/.env"
    echo "  2. Change web password:"
    echo "     cd $APP_DIR && .venv/bin/python -c \"import bcrypt; print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt()).decode())\""
    echo "  3. Restart: systemctl restart $APP_NAME"
    echo "  4. Open Telegram — send /start to your bot"
    echo ""
else
    err "Service failed to start. Check: journalctl -u $APP_NAME -n 50"
fi
