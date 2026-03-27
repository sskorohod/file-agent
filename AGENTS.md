# AGENTS.md — AI File Intelligence Agent

## Project Overview

Personal AI agent for document processing via Telegram Bot + Web Dashboard.
Receives files → parses → classifies (LLM) → stores → embeds (Gemini multimodal) → semantic search.

## Tech Stack

- **Backend:** Python 3.14, FastAPI, uvicorn
- **Telegram:** python-telegram-bot (polling mode)
- **LLM:** litellm (Anthropic Codex, Google Gemini, OpenAI GPT)
- **Embedding:** Gemini Embedding 2 (`gemini-embedding-2-preview`, 768-dim multimodal)
- **Vector DB:** Qdrant (remote on ugreen `192.168.1.244`, accessed via SSH tunnel)
- **Metadata DB:** SQLite + aiosqlite (WAL mode, FTS5)
- **Parsers:** PyMuPDF (PDF), pytesseract (OCR), python-docx, Vision API (images)
- **Web UI:** Jinja2 + HTMX (no JS build step)
- **Config:** YAML + Pydantic Settings + .env

## Quick Start

```bash
# 1. SSH tunnel to Qdrant (required before starting)
ssh -L 6333:localhost:6333 -L 6334:localhost:6334 -N ugreen &

# 2. Run
source .venv/bin/activate
make dev  # uvicorn with reload on :8000
```

## Key Commands

```bash
make dev       # Start with auto-reload
make test      # pytest tests/ -v
make lint      # ruff check
make format    # ruff format
```

## Architecture

```
Telegram/Web → FastAPI → Pipeline (9 steps) → Storage
                              ↓
              1.receive → 2.ingest → 3.parse → 4.classify → 5.route
              → 6.store → 7.embed → 8.save_meta → 9.done
```

### Pipeline data flow
- Steps 1-2: raw bytes → temp file
- Step 3: temp file → ParseResult (text, language, pages)
- Step 4: text → LLM → ClassificationResult (category, document_type, tags)
- Step 5: classification → SkillEngine → routing rules
- Step 6: bytes → FileStorage → permanent path + SHA-256
- Step 7: bytes + text → Gemini Embedding → Qdrant (multimodal + text chunks)
- Step 8: all metadata → SQLite

### Embedding (Gemini 2 multimodal)
- Provider: `gemini-embedding-2-preview` via `google-genai` SDK
- Multimodal: images, PDFs, audio, video embedded from raw bytes
- Text: chunked (400 words, 50 overlap) and embedded separately
- Both stored in same Qdrant collection (`file_agent_v2`, 768-dim, Cosine)
- Search queries use `task_type=RETRIEVAL_QUERY`
- Local fallback: `all-MiniLM-L6-v2` (384-dim) — dimension mismatch, use only if switching collection

## File Structure (key files)

```
app/
  main.py          # FastAPI app, lifespan, global state
  config.py        # Pydantic Settings (YAML + env)
  pipeline.py      # 9-step orchestrator
  bot/handlers.py  # Telegram commands & file handlers
  llm/router.py    # litellm wrapper, role-based model selection
  llm/classifier.py # Hybrid rule+LLM classification
  llm/search.py    # RAG: vector search → LLM answer
  parser/factory.py # Parser routing by file type
  parser/pdf.py    # PyMuPDF + Tesseract OCR
  parser/image.py  # Vision API + Tesseract fallback
  storage/db.py    # SQLite async (FTS5, processing_log)
  storage/files.py # File storage with categorization
  storage/vectors.py # Qdrant + GeminiEmbedder
  skills/engine.py # YAML skill loader + matcher
  web/routes.py    # Dashboard (11 endpoints)
config.yaml        # Main config (models, qdrant, embedding)
skills/*.yaml      # User-editable classification rules
```

## Configuration

- **Config priority:** env vars > config.yaml > defaults
- **env_prefix:** empty (no prefix — `TELEGRAM_BOT_TOKEN`, not `APP_TELEGRAM_BOT_TOKEN`)
- **Nested env delimiter:** `__` (e.g., `QDRANT__HOST=localhost`)
- **API keys:** .env file, exported to os.environ via `setup_env_keys()`
- **Gemini:** Both `GOOGLE_API_KEY` and `GEMINI_API_KEY` exported (litellm uses one, google-genai the other)

## External Services

- **Qdrant:** Docker on ugreen (192.168.1.244), API key required, accessed via SSH tunnel to localhost:6333
- **Anthropic:** Codex for classification/extraction/search
- **Google:** Gemini for embedding + vision OCR fallback
- **Telegram:** Bot polling mode

## Gotchas

- **Python 3.14 + Starlette:** Must use `starlette<1.0.0` (Jinja2 LRU cache breaks with unhashable dict-in-tuple)
- **Qdrant gRPC:** Port 6334 blocked by ugreen firewall; use `prefer_grpc=False` (HTTP only)
- **Qdrant direct IP:** Python sockets can't connect directly to 192.168.1.244 (macOS sandbox); must use SSH tunnel
- **FTS5 UPDATE trigger:** Must use `INSERT INTO fts(fts, rowid, ...) VALUES('delete', ...)` syntax, not `DELETE FROM fts`
- **datetime.utcnow():** Deprecated in Python 3.14; use `datetime.now()`
- **qdrant-client 1.17+:** `client.search()` removed; use `client.query_points()` (returns `.points` list)
- **Skills auto-reload:** Every 30s via background asyncio task; TEMPLATE.yaml is skipped during loading
- **Embed step is non-fatal:** If Qdrant is down, file still gets processed and stored, just not searchable
