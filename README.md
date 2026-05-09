<div align="center">

# 📁 FileAgent

### AI File Intelligence Agent

**Personal AI-powered document management system**

Send documents via Telegram → get instant AI analysis, recommendations, and reminders.
Ask questions → get precise answers from your knowledge base.

[![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## What is FileAgent?

FileAgent is a personal AI agent that turns your documents into a searchable, intelligent knowledge base. Upload a passport photo — get expiry reminders. Send a blood test — get health recommendations. Drop an invoice — track deadlines automatically.

**Key idea:** Upload → Analyze → Act. Every document gets AI-powered classification, field extraction, recommendations, and automatic reminders.

### How it works

```
📱 Telegram / 🌐 Web / 🔌 API / 🤖 MCP / 🧠 Codex+Claude (cognee-mcp)
              ↓
    ┌─────────────────────────┐
    │   AI Ingest Pipeline    │
    │                         │
    │ 1. Receive & validate   │
    │ 2. Auto-crop (OpenCV)   │
    │ 3. Dedup (SHA-256)      │
    │ 4. Parse (OCR/PDF/DOCX) │
    │ 5. Classify (LLM)       │
    │ 6. Route to skill       │
    │ 7. Extract fields (LLM) │
    │ 8. Auto-reminder        │
    │ 9. Store on disk        │
    │ 10. Embed (Gemini 768d) │
    │ 11. Save metadata       │
    │ 12. Cognee ingest       │
    │ 13. Refresh insights    │
    └─────────────────────────┘
              ↓
    📊 SQLite + 🔍 Qdrant + 💾 Disk +
    🧠 Cognee sidecar (graph + memory)
```

**Memory layer** lives in a separate `cognee` process (Apache 2.0,
graph + vector knowledge memory). Files, voice notes, and substantive
chat messages all land in a unified `main_dataset` (the same default
cognee-mcp uses, so external agents writing via `remember` join the
same scope). Per-project `dev_<id>` datasets are isolated by ACL so
external coding agents can only see what they're scoped to. See
`docs/codex-claude-setup.md`.

---

## ✨ Features

### 📄 Smart Document Processing
- **Auto-classification** — AI identifies document type (passport, lab result, invoice, contract) and category
- **Field extraction** — Pulls out names, dates, amounts, diagnoses, document numbers
- **Detailed analysis** — Explains what the document is, why it matters, what to do next
- **Priority assessment** — High/medium/low with reasoning
- **Storage recommendations** — How to store, whether you need originals, related documents

### 📸 Multi-page Scanning
- **`/scan` command** — Send photos one by one, assemble into PDF
- **Media groups** — Send multiple photos at once → auto-assembled PDF
- **Auto-crop** — OpenCV detects document on desk/background, trims borders
- **Preview** — Thumbnail strip for page order verification before assembly

### 🔍 Semantic Search (RAG)
- **Multimodal embeddings** — Gemini Embedding 2 processes both text AND images
- **Vector search** — Qdrant kNN across all documents
- **LLM synthesis** — AI composes detailed answers from found documents
- **Chat context** — Remembers conversation history for follow-up questions

### 📊 Analytics
- **Trend analysis** — "Analyze hemoglobin over the past year" → chart + summary
- **Data extraction** — Pulls numerical metrics from documents with reference ranges
- **Chart generation** — Auto-generated PNG charts for visual analysis

### 💡 AI Insights
- **Category overview** — AI analyzes all documents per category, highlights issues
- **Motivational recommendations** — Not just "renew passport" but WHY and HOW it improves your life
- **Web research** — Optionally enriches recommendations with current info (via Tavily)
- **Daily advice** — Telegram messages at 9:00 AM and 8:00 PM with actionable tips

### ⏰ Auto-reminders
- **Expiry detection** — Extracts expiration dates from documents automatically
- **Smart intervals** — Passport: 6 months, driver license: 60 days, others: 2 weeks
- **Telegram notifications** — With "Done" and "Snooze" buttons
- **Dashboard widget** — Upcoming reminders with urgency color-coding

### 🎙 Voice Messages
- **Whisper transcription** — OpenAI Whisper speech-to-text
- **Action choice** — Search documents or save as Obsidian note
- **Smart notes** — LLM extracts title, tags, action items from voice

### 🖥 Web Dashboard
- **Glassmorphism dark UI** — Professional SaaS design, Tailwind CSS + HTMX
- **Real-time dashboard** — KPI cards, activity feed, pipeline health
- **Mobile-first** — Bottom navigation, touch-friendly, responsive
- **File preview** — Inline PDF viewer with zoom, image viewer
- **Settings** — API keys management, LLM model configuration, encrypted storage

### 🧩 Skills System
- **YAML-configurable** — Each document type defined in a simple YAML file
- **Custom extraction** — Define what fields to pull for each category
- **Response templates** — Format Telegram responses per document type
- **Hot reload** — Changes picked up every 30 seconds, no restart needed

### 🧠 Memory Layer (Cognee sidecar)
- **Graph + vector knowledge memory** — every file, voice note, and
  user chat message gets cognified into the unified `main_dataset`
- **Codex / Claude Code / ChatGPT integration** — register the
  `cognee-mcp` binary as an MCP server (stdio transport recommended for
  Codex) and use V2 `recall`/`remember`/`forget`
- **Per-project isolation** — register a dev project via the HTTP API,
  ingest its repo, and the agent only sees that project's data; cross-
  project access is blocked at the cognee permission layer
- **Sidecar architecture** — cognee runs in its own process and venv,
  so its dependency tree (lancedb, networkx, fastapi-users, ...) never
  touches FAG's main runtime
- See [docs/codex-claude-setup.md](docs/codex-claude-setup.md) for
  end-to-end setup

---

## 🚀 Quick Start

### Option 1: Docker Compose (recommended)

Works on **macOS, Linux, and Windows**.

```bash
# Clone
git clone https://github.com/sskorohod/file-agent.git
cd file-agent

# Configure
cp .env.example .env
# Edit .env with your API keys (see Configuration below)

# Run
docker compose up -d
```

Open **http://localhost:8000** — done!

### Option 2: Install Script (macOS/Linux)

```bash
curl -sSL https://raw.githubusercontent.com/sskorohod/file-agent/main/install.sh | bash
```

Interactive installer will guide you through API key setup.

### Option 3: Manual Installation

```bash
# Prerequisites: Python 3.12+, Docker (for Qdrant)
git clone https://github.com/sskorohod/file-agent.git
cd file-agent

# Start Qdrant
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant:latest

# Setup Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — add Telegram token, API keys

# Run
make dev
```

### Optional: Cognee memory sidecar

The cognee sidecar adds graph-based memory and the MCP entry point for
external coding agents. It is independent of the document pipeline —
FAG works without it; degrades gracefully when it's down.

```bash
# Provision .venv-cognee, infra/cognee/.env, install cognee + cognee-mcp
make cognee-install

# Edit infra/cognee/.env (LLM_API_KEY, OPENAI_API_KEY, etc.)
$EDITOR infra/cognee/.env

# FAG-facing API server :8765
make cognee-start

# (Optional) MCP server :8766 for Codex / Claude Code / ChatGPT
make cognee-mcp-start
```

See [docs/codex-claude-setup.md](docs/codex-claude-setup.md) for token
scopes (personal vs per-project) and client configuration snippets.

---

## ⚙️ Configuration

### Required API Keys

| Key | Purpose | Get it from |
|-----|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot | [@BotFather](https://t.me/BotFather) |
| `TELEGRAM__OWNER_ID` | Your Telegram user ID | [@userinfobot](https://t.me/userinfobot) |
| `GOOGLE_API_KEY` | Gemini Embedding (free) | [Google AI Studio](https://aistudio.google.com/apikey) |

### LLM Provider (one of)

| Key | Models | Pricing |
|-----|--------|---------|
| `ANTHROPIC_API_KEY` | Claude Sonnet/Haiku | Pay per token |
| `OPENAI_API_KEY` | GPT-4o/4o-mini | Pay per token |
| OAuth Proxy | ChatGPT subscription models | Subscription |

Configure in **Settings → LLM Models** in the web dashboard.

### Dashboard Auth

```bash
# Generate password hash
python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt()).decode())"
```

```env
WEB__SESSION_SECRET=your-random-32-char-string
WEB__LOGIN=your@email.com
WEB__PASSWORD_HASH=$2b$12$...
```

### Full .env Example

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM__OWNER_ID=169108358
GOOGLE_API_KEY=AIzaSy...
ANTHROPIC_API_KEY=sk-ant-...
WEB__SESSION_SECRET=change-me-random-string
WEB__LOGIN=admin@example.com
WEB__PASSWORD_HASH=$2b$12$...
```

---

## 📱 Telegram Commands

| Command | Description |
|---------|-------------|
| `/search <query>` | Semantic search across all documents |
| `/analytics <question>` | Data analytics with chart generation |
| `/insights` | AI overview and recommendations per category |
| `/scan [name]` | Start multi-page document scan |
| `/done` | Finish scan with page preview |
| `/cancel` | Cancel active scan session |
| `/files [category]` | Browse files with pagination |
| `/recent [N]` | Last N uploaded files |
| `/stats` | Database statistics |
| `/notes` | View saved notes |

**Upload:** Just send any file (PDF, photo, DOCX) — processing is automatic.

**Voice:** Send voice message → choose "Search" or "Save as note".

**Multi-photo:** Select multiple photos and send at once → auto-assembled PDF.

---

## 🏗 Architecture

```
app/
├── main.py              # FastAPI app, lifespan, background tasks
├── config.py            # Pydantic Settings (YAML + env)
├── pipeline.py          # 13-step document processor
├── bot/handlers.py      # Telegram commands & file handlers
├── llm/
│   ├── router.py        # litellm multi-provider wrapper
│   ├── classifier.py    # Document classification
│   ├── search.py        # RAG search + answer synthesis
│   ├── analytics.py     # Trend analysis + chart generation
│   └── insights.py      # Category AI analysis + daily advice
├── parser/
│   ├── pdf.py           # PyMuPDF + Tesseract OCR
│   └── image.py         # Vision API + OCR fallback
├── storage/
│   ├── db.py            # SQLite (12 tables, FTS5)
│   ├── files.py         # Categorized file storage
│   └── vectors.py       # Qdrant + Gemini Embedder
├── web/
│   ├── routes.py        # 25+ web endpoints
│   ├── auth.py          # Session-based auth middleware
│   └── templates/       # Jinja2 + Tailwind CSS + HTMX
├── api/routes.py        # REST API v1 (Bearer token)
├── mcp_server.py        # MCP tools for Claude Code/Desktop
└── utils/
    ├── pdf.py           # Auto-crop + PDF assembly (OpenCV)
    └── crypto.py        # Fernet encryption for secrets

skills/                  # YAML skill definitions
config.yaml              # Runtime configuration
docker-compose.yml       # App + Qdrant containers
```

---

## 🔌 Integrations

### REST API

```bash
# Create API key in Settings, then:
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8000/api/v1/search?q=passport
```

### MCP (Claude Code / Claude Desktop)

```json
{
  "mcpServers": {
    "file-agent": {
      "url": "http://localhost:8000/mcp/sse",
      "headers": {"Authorization": "Bearer YOUR_KEY"}
    }
  }
}
```

---

## 📖 Documentation

- [**PROJECT.md**](.docs/PROJECT.md) — Full project description and capabilities
- [**USER_GUIDE.md**](.docs/USER_GUIDE.md) — How to use (Telegram, Web, API)
- [**TECHNICAL.md**](.docs/TECHNICAL.md) — Architecture, database schema, configuration

---

## 🛠 Development

```bash
make dev       # Start with auto-reload
make test      # Run tests
make lint      # Ruff check
make format    # Ruff format
```

---

## License

MIT

---

<div align="center">
<sub>Built with ❤️ using Claude, FastAPI, and Gemini</sub>
</div>
