# CLAUDE.md — AI File Intelligence Agent

## Project Overview

Personal AI agent for document processing via Telegram Bot + Web Dashboard.
Receives files → parses → classifies (LLM) → stores → embeds (Gemini multimodal) → semantic search.

## Tech Stack

- **Backend:** Python 3.14, FastAPI, uvicorn
- **Telegram:** python-telegram-bot (polling mode)
- **LLM:** litellm (Anthropic Claude, Google Gemini, OpenAI GPT)
- **Embedding:** Gemini Embedding 2 (`gemini-embedding-2-preview`, 768-dim multimodal)
- **Vector DB:** Qdrant (remote on ugreen `192.168.1.244`, accessed via SSH tunnel)
- **Metadata DB:** SQLite + aiosqlite (WAL mode, FTS5)
- **Parsers:** PyMuPDF (PDF), pytesseract (OCR), python-docx, Vision API (images)
- **Web UI:** Jinja2 + HTMX (no JS build step)
- **Config:** YAML + Pydantic Settings + .env
- **Memory layer (sidecar):** cognee 1.0.8 + cognee-mcp 0.5.4 in `.venv-cognee/`,
  graph + vector knowledge memory, separate process on `:8765`; cognee-mcp
  proxy on `:8766` for external agents (Codex / Claude Code / ChatGPT)

## Quick Start

```bash
# 1. SSH tunnel to Qdrant (required before starting)
ssh -L 6333:localhost:6333 -L 6334:localhost:6334 -N ugreen &

# 2. Run
source .venv/bin/activate
make dev  # uvicorn with reload on :8000

# 3. (Optional) cognee memory sidecar — see infra/cognee/README.md
make cognee-install      # one-time, provisions .venv-cognee + .env
make cognee-start        # FAG-facing API on :8765
make cognee-mcp-start    # MCP server for external agents on :8766
```

## Key Commands

```bash
make dev                 # Start FAG with auto-reload
make test                # pytest tests/ -v
make lint                # ruff check
make format              # ruff format

make cognee-install      # provision .venv-cognee + infra/cognee/.env
make cognee-start        # start cognee FastAPI sidecar (8765)
make cognee-stop         # stop cognee sidecar
make cognee-status       # is the sidecar up?
make cognee-logs         # tail infra/cognee/logs/cognee.log
make cognee-spike2       # one-shot e2e probe (writes Phase-1 fixture)

make cognee-mcp-start    # start cognee-mcp (8766) for Codex / Claude
make cognee-mcp-stop     # stop cognee-mcp
make cognee-mcp-logs     # tail infra/cognee/logs/cognee-mcp.log
```

## Architecture

```
Telegram/Web/HTTP/MCP → FAG (port 8000) → Pipeline → Storage + Cognee
                                                          ↑
                Codex / Claude Code / ChatGPT ─────► cognee-mcp (8766)
                                                          ↓
                                           cognee FastAPI sidecar (8765)
                                                          ↓
                                           SQLite + LanceDB + NetworkX
```

### Pipeline data flow (file ingest)
- Steps 1-2: raw bytes → temp file
- Step 3: temp file → ParseResult (text, language, pages)
- Step 4: text → LLM → ClassificationResult (category, document_type, tags)
- Step 5: classification → SkillEngine → routing rules
- Step 6: bytes → FileStorage → permanent path + SHA-256
- Step 7: bytes + text → Gemini Embedding → Qdrant (FAG `file_agent_v2`)
- Step 8: all metadata → SQLite
- Step 9.5: extracted_text → cognee `main_dataset` (graph + vector via sidecar)

Voice notes and substantive Telegram chat queries reuse the cognee
ingest path through `app/ingestion/text_ingest.py` — same `main_dataset`
dataset, no document pipeline needed. The default name `main_dataset`
matches cognee-mcp's hardcoded default, so external agents (Codex,
Claude Code) writing via `remember` land in the same scope.

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
  storage/db.py    # SQLite async (FTS5, processing_log, dev_projects)
  storage/files.py # File storage with categorization
  storage/vectors.py # Qdrant + GeminiEmbedder
  memory/cognee_client.py  # async httpx client for cognee sidecar
  memory/dev_ingest.py     # per-project repo + decisions ingest
  ingestion/text_ingest.py # voice note + chat → cognee shim
  skills/engine.py # YAML skill loader + matcher
  api/routes.py    # /api/v1/* (files + dev/projects/*)
  web/routes.py    # Dashboard (11 endpoints)
config.yaml        # Main config (models, qdrant, embedding, cognee)
skills/*.yaml      # User-editable classification rules

infra/cognee/
  setup.sh         # provision .venv-cognee + cognee + cognee-mcp
  start.sh         # cognee FastAPI sidecar :8765
  stop.sh
  start-mcp.sh     # cognee-mcp on :8766 (proxies into the sidecar)
  stop-mcp.sh
  .env.example     # full env contract (LLM_*, EMBEDDING_*, VECTOR_DB_*, COGNEE_MCP_*)
  README.md        # operator-facing
docs/
  cognee-spike-report.md       # Phase 0 architecture decision
  cognee-spike2-report.md      # Phase 1 e2e + lessons
  codex-claude-setup.md        # Phase 6 operator guide
```

## Configuration

- **Config priority:** env vars > config.yaml > defaults
- **env_prefix:** empty (no prefix — `TELEGRAM_BOT_TOKEN`, not `APP_TELEGRAM_BOT_TOKEN`)
- **Nested env delimiter:** `__` (e.g., `QDRANT__HOST=localhost`)
- **API keys:** .env file, exported to os.environ via `setup_env_keys()`
- **Gemini:** Both `GOOGLE_API_KEY` and `GEMINI_API_KEY` exported (litellm uses one, google-genai the other)

## External Services

- **Qdrant:** Docker on ugreen (192.168.1.244), API key required, accessed via SSH tunnel to localhost:6333
- **Anthropic:** Claude for classification/extraction/search
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
- **Cognee sidecar isolation:** the cognee process has its own `.venv-cognee/`
  because `cognee 1.0.8` requires `starlette>=1.0.0` (conflicts with FAG's
  `<1.0.0` pin). FAG main runtime never imports cognee — it talks to the
  sidecar over HTTP via `app/memory/cognee_client.py`
- **Cognee dataset isolation needs ACL:** with `ENABLE_BACKEND_ACCESS_CONTROL=false`
  the `datasets` filter on `recall` does not actually isolate graphs.
  Phase 5b sets it to `true` and provisions a per-project cognee user
  so each `dev_<id>` is genuinely scoped
- **Cognee API request shapes:** `/api/v1/add` is multipart/form-data;
  every other JSON body uses **camelCase** (`datasetName`, `topK`,
  `runInBackground`, `searchType`)
- **cognee-mcp deps on Python 3.14 arm64:** psycopg2-binary has no
  wheel — `setup.sh` installs `cognee-mcp --no-deps` and pins the
  runtime essentials (fastmcp, mcp) explicitly
