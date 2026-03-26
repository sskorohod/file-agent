# AI File Intelligence Agent

Personal AI agent for document processing, classification, and knowledge base management. Send files via Telegram → get them parsed, classified, stored, and searchable.

## Architecture

```
Telegram Bot ──→ Pipeline (9 steps) ──→ File Storage (~/ai-agent-files/)
     │                │                       │
     │          Parse → Classify → Route      │
     │                │                       │
     ▼                ▼                       ▼
  Commands       LLM Router              SQLite (metadata)
  /search        (litellm)               Qdrant (vectors)
  /recent        Anthropic/OpenAI/Gemini
  /stats
     │
     ▼
  Web Dashboard (localhost:8000)
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker (on ugreen server for Qdrant)
- Telegram Bot Token (via @BotFather)
- At least one LLM API key (Anthropic, OpenAI, or Google)

### 2. Setup Qdrant on ugreen

```bash
scp infra/docker-compose.qdrant.yml ugreen:~/qdrant/docker-compose.yml
scp infra/qdrant_config.yaml ugreen:~/qdrant/qdrant_config.yaml
ssh ugreen "cd ~/qdrant && docker compose up -d"
```

See `infra/README-qdrant.md` for details.

### 3. Install & Configure

```bash
git clone <repo> && cd ai-file-agent
cp .env.example .env
# Edit .env with your keys:
#   TELEGRAM_BOT_TOKEN=...
#   ANTHROPIC_API_KEY=...
#   QDRANT_HOST=<ugreen-ip>

make install
```

### 4. Run

```bash
make dev
# Opens: http://localhost:8000 (dashboard)
# Telegram bot starts automatically
```

## Configuration

All settings in `config.yaml`, overridable by environment variables:

| Setting | Default | Description |
|---------|---------|-------------|
| `llm.models.classification.model` | `anthropic/claude-3-haiku` | Cheap model for classification |
| `llm.models.extraction.model` | `anthropic/claude-sonnet-4` | Full model for extraction/search |
| `qdrant.host` | `localhost` | Qdrant server address |
| `embedding.model` | `all-MiniLM-L6-v2` | Local embedding model |
| `embedding.chunk_size_words` | `400` | Chunk size for vectorization |

## Skills

Skills are YAML files in `skills/` that define document classification rules:

```yaml
name: health
category: health
routing_rules:
  keywords: [diagnosis, patient, blood test]
  patterns: ['\b(WBC|RBC)\b']
naming_template: "{date}_{document_type}"
extraction:
  fields:
    - name: document_type
      required: true
```

Create new skills by copying `skills/TEMPLATE.yaml`. Skills hot-reload — no restart needed.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/search <query>` | Semantic search across all documents |
| `/recent [N]` | Last N processed files |
| `/stats` | Database statistics |
| `/skills` | List active skills |
| Send file | Process, classify, store |
| Send text | Q&A over knowledge base |

## Web Dashboard

Available at `http://localhost:8000`:

- **Dashboard** — stats, recent files, LLM costs
- **Files** — browse, filter, search, view details
- **Search** — semantic search with AI answers
- **Skills** — manage classification skills
- **Settings** — API keys status, Qdrant health
- **Logs** — processing pipeline logs

## Project Structure

```
ai-file-agent/
├── app/
│   ├── main.py          # FastAPI entry + lifespan
│   ├── config.py         # Pydantic settings
│   ├── pipeline.py       # 9-step processing pipeline
│   ├── bot/handlers.py   # Telegram bot
│   ├── parser/           # PDF, Image, DOCX, Text parsers
│   ├── llm/              # Router, Classifier, RAG Search
│   ├── skills/engine.py  # YAML skill engine
│   ├── storage/          # Files, SQLite DB, Qdrant vectors
│   └── web/              # Dashboard routes + templates
├── skills/               # YAML skill definitions
├── infra/                # Qdrant Docker setup
├── tests/                # pytest suite
├── config.yaml           # Default configuration
└── Makefile              # Dev commands
```

## Testing

```bash
make test          # Run all tests
make lint          # Check code style
```

## Tech Stack

- **Backend**: Python 3.11, FastAPI, asyncio
- **Telegram**: python-telegram-bot (async)
- **LLM**: litellm (Anthropic + OpenAI + Gemini)
- **Parsing**: PyMuPDF, Tesseract OCR, python-docx
- **Vectors**: Qdrant (remote), sentence-transformers
- **Database**: SQLite + aiosqlite
- **Web UI**: Jinja2 + HTMX
- **Embedding**: all-MiniLM-L6-v2 (local)
