#!/usr/bin/env bash
# ============================================
# FileAgent Installer
# ============================================
# Usage:
#   curl -sSL https://raw.githubusercontent.com/YOUR_USER/file-agent/main/install.sh | bash
#   or
#   git clone ... && cd file-agent && bash install.sh
# ============================================

set -e

REPO_URL="https://github.com/YOUR_USER/file-agent.git"
INSTALL_DIR="${FILEAGENT_DIR:-$HOME/file-agent}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     FileAgent Installer v1.0         ║${NC}"
echo -e "${BLUE}║     AI File Intelligence Agent       ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Detect OS ──────────────────────────────────
OS="unknown"
if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macos"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="linux"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    OS="windows"
fi
info "Detected OS: $OS"

# ── Check Docker ───────────────────────────────
if command -v docker &>/dev/null && command -v docker compose &>/dev/null; then
    ok "Docker found: $(docker --version | head -1)"
    HAS_DOCKER=1
else
    warn "Docker not found. Install from https://docs.docker.com/get-docker/"
    HAS_DOCKER=0
fi

# ── Choose installation method ─────────────────
echo ""
echo "Installation methods:"
echo "  1) Docker Compose (recommended — works everywhere)"
echo "  2) Native (macOS/Linux only — Python + venv)"
echo ""
read -p "Choose [1/2]: " METHOD
METHOD=${METHOD:-1}

# ── Clone repository ───────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repository exists at $INSTALL_DIR, pulling latest..."
    cd "$INSTALL_DIR"
    git pull --ff-only 2>/dev/null || warn "Could not pull (offline?)"
elif [[ -f "app/main.py" ]]; then
    info "Already in project directory"
    INSTALL_DIR="$(pwd)"
else
    info "Cloning repository to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR" || fail "Failed to clone repository"
    cd "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Create .env ────────────────────────────────
if [[ ! -f .env ]]; then
    info "Creating .env from template..."
    cp .env.example .env

    echo ""
    echo -e "${YELLOW}Configure your API keys:${NC}"
    echo ""

    read -p "Telegram Bot Token (from @BotFather): " TG_TOKEN
    read -p "Telegram Owner ID (your user ID): " TG_OWNER
    read -p "Google API Key (for Gemini Embedding): " GOOGLE_KEY
    read -p "Anthropic API Key (optional, press Enter to skip): " ANTHROPIC_KEY
    read -p "OpenAI API Key (optional, press Enter to skip): " OPENAI_KEY
    read -p "Dashboard password: " -s DASH_PASS
    echo ""
    read -p "Dashboard email: " DASH_EMAIL

    # Generate session secret
    SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32)

    # Generate password hash
    PASS_HASH=""
    if [[ -n "$DASH_PASS" ]]; then
        PASS_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'$DASH_PASS', bcrypt.gensalt()).decode())" 2>/dev/null || echo "")
    fi

    # Write to .env
    sed -i.bak "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$TG_TOKEN|" .env
    sed -i.bak "s|^TELEGRAM__OWNER_ID=.*|TELEGRAM__OWNER_ID=$TG_OWNER|" .env
    sed -i.bak "s|^GOOGLE_API_KEY=.*|GOOGLE_API_KEY=$GOOGLE_KEY|" .env
    [[ -n "$ANTHROPIC_KEY" ]] && sed -i.bak "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ANTHROPIC_KEY|" .env
    [[ -n "$OPENAI_KEY" ]] && sed -i.bak "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$OPENAI_KEY|" .env
    sed -i.bak "s|^WEB__SESSION_SECRET=.*|WEB__SESSION_SECRET=$SESSION_SECRET|" .env
    sed -i.bak "s|^WEB__LOGIN=.*|WEB__LOGIN=$DASH_EMAIL|" .env
    [[ -n "$PASS_HASH" ]] && sed -i.bak "s|^WEB__PASSWORD_HASH=.*|WEB__PASSWORD_HASH=$PASS_HASH|" .env
    rm -f .env.bak

    ok ".env configured"
else
    ok ".env already exists"
fi

# ── Method 1: Docker Compose ──────────────────
if [[ "$METHOD" == "1" ]]; then
    if [[ "$HAS_DOCKER" != "1" ]]; then
        fail "Docker is required for this method. Install from https://docs.docker.com/get-docker/"
    fi

    info "Building and starting containers..."
    docker compose up -d --build

    echo ""
    ok "FileAgent is running!"
    echo ""
    echo -e "  ${GREEN}Web Dashboard:${NC}  http://localhost:8000"
    echo -e "  ${GREEN}Qdrant:${NC}         http://localhost:6333"
    echo ""
    echo "Commands:"
    echo "  docker compose logs -f app     # View logs"
    echo "  docker compose restart app     # Restart"
    echo "  docker compose down            # Stop all"
    echo ""
    exit 0
fi

# ── Method 2: Native Install ──────────────────
info "Installing natively..."

# Check Python
PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3; do
    if command -v $cmd &>/dev/null; then
        PY_VER=$($cmd --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
        PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
        if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 12 ]]; then
            PYTHON=$cmd
            break
        fi
    fi
done
[[ -z "$PYTHON" ]] && fail "Python 3.12+ required. Install from https://python.org"
ok "Python: $($PYTHON --version)"

# Install system dependencies
if [[ "$OS" == "macos" ]]; then
    if command -v brew &>/dev/null; then
        info "Installing system dependencies via Homebrew..."
        brew install tesseract tesseract-lang 2>/dev/null || true
    else
        warn "Homebrew not found. Install tesseract manually: brew install tesseract"
    fi
    # Start Qdrant in Docker if available
    if [[ "$HAS_DOCKER" == "1" ]]; then
        info "Starting Qdrant in Docker..."
        docker run -d --name fileagent-qdrant -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant:latest 2>/dev/null || true
        ok "Qdrant running on localhost:6333"
    fi
elif [[ "$OS" == "linux" ]]; then
    if command -v apt-get &>/dev/null; then
        info "Installing system dependencies via apt..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libgl1-mesa-glx
    fi
    if [[ "$HAS_DOCKER" == "1" ]]; then
        info "Starting Qdrant in Docker..."
        docker run -d --name fileagent-qdrant -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant:latest 2>/dev/null || true
        ok "Qdrant running on localhost:6333"
    fi
fi

# Create virtual environment
if [[ ! -d .venv ]]; then
    info "Creating virtual environment..."
    $PYTHON -m venv .venv
fi
source .venv/bin/activate
ok "Virtual environment activated"

# Install Python dependencies
info "Installing Python dependencies (this may take a few minutes)..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
ok "Dependencies installed"

# Create directories
mkdir -p data
mkdir -p ~/ai-agent-files

# Initialize database
info "Initializing database..."
$PYTHON -c "
import asyncio
from app.storage.db import Database
async def init():
    db = Database('data/agent.db')
    await db.connect()
    await db.close()
    print('Database initialized')
asyncio.run(init())
"
ok "Database ready"

echo ""
ok "FileAgent installed successfully!"
echo ""
echo -e "  ${GREEN}Start:${NC}          source .venv/bin/activate && make dev"
echo -e "  ${GREEN}Dashboard:${NC}      http://localhost:8000"
echo ""
echo "Quick start:"
echo "  cd $INSTALL_DIR"
echo "  source .venv/bin/activate"
echo "  make dev"
echo ""
