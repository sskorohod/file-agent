# CLAUDE.md — AI File Intelligence Agent + Life OS

## Project Overview

Personal AI agent: document processing + smart notes + health tracking + life analytics.
Telegram Bot + Web Dashboard (Life OS). Receives files/voice/text → processes → classifies (LLM) → stores → embeds → semantic search. Smart Notes with enrichment, habit tracking, anomaly detection, morning briefings, weekly reports.

## Tech Stack

- **Backend:** Python 3.14, FastAPI, uvicorn
- **Telegram:** python-telegram-bot (polling/webhook mode)
- **LLM:** litellm (Anthropic Claude, Google Gemini, OpenAI GPT)
- **Embedding:** Gemini Embedding 2 (`gemini-embedding-2-preview`, 768-dim multimodal)
- **Vector DB:** Qdrant (remote on ugreen `192.168.1.244`, accessed via SSH tunnel)
- **Metadata DB:** SQLite + aiosqlite (WAL mode, FTS5, ~29 tables)
- **Parsers:** PyMuPDF (PDF), pytesseract (OCR), python-docx, Vision API (images)
- **Web UI:** Jinja2 + HTMX + Tailwind 3 (CLI build, no CDN) + inline SVG charts
- **CSS:** Tailwind 3 CLI → `app/web/static/css/styles.css` (run `make css`)
- **Config:** YAML + Pydantic Settings + .env
- **Security:** AES-256-GCM encryption, CSRF middleware, bcrypt auth, rate limiting, CSP headers
- **MCP:** Streamable HTTP + SSE transports

## Quick Start

```bash
# 1. SSH tunnel to Qdrant (required before starting)
ssh -L 6333:localhost:6333 -L 6334:localhost:6334 -N ugreen &

# 2. Run
source .venv/bin/activate
make css   # Build Tailwind CSS (first time / after template changes)
make dev   # uvicorn with reload on :8000
```

## Key Commands

```bash
make dev        # Start with auto-reload
make test       # pytest tests/ -v
make lint       # ruff check
make format     # ruff format
make css        # Build Tailwind CSS (minified)
make css-watch  # Tailwind watch mode for dev
```

## Architecture

```
Telegram/Web → FastAPI → Pipeline (9 steps) → Storage
                              ↓
              1.receive → 2.ingest → 3.parse → 4.classify → 5.route
              → 6.store → 7.embed → 8.save_meta → 9.done

Smart Notes:  Capture → Enrich (LLM) → Relate (Qdrant) → Project (Obsidian)
```

### Smart Notes Pipeline (v1.5)
- **Capture:** instant save, never blocks on LLM (voice/text/web/checkin)
- **Enrich:** LLM categorization (12 categories), entity/fact/task extraction, mood/sentiment/energy
- **Relate:** semantic search (Qdrant) + tag overlap + entity matching
- **Project:** Obsidian vault export with MOCs, backlinks, YAML frontmatter

### Life OS Dashboard (`/notes`)
- **Hero State Panel:** weighted day score → "Хороший ритм"/"Нужен фокус"/"Режим восстановления"
- **KPI Row:** mood/sleep/calories/weight with sparklines, deltas, state labels, target bands
- **Insight Strip:** max 3 explainable cards (warning/win/pattern/action)
- **Domain Charts:** SVG area/line charts with target zones, tooltips, draw-in animation
- **Focus:** SVG donut chart (category distribution) with legend
- **Fuel:** nutrition widget (macro bar, meal completion strip)
- **Correlations:** rich cards with strength bars
- **Heatmap:** 90-day activity calendar
- **Action Layer:** due tasks, review queue, reminders
- **Journal Stream:** search/filter/note list (below fold)
- **Maturity States:** empty → early → mature (different UX per state)

## File Structure (key files)

```
app/
  main.py              # FastAPI app, lifespan, middleware, background loops
  config.py            # Pydantic Settings (YAML + env)
  pipeline.py          # 9-step document orchestrator
  bot/handlers.py      # Telegram: 20 commands, 8 callback handlers
  llm/router.py        # litellm wrapper, role-based model selection
  llm/classifier.py    # Hybrid rule+LLM classification
  llm/search.py        # RAG: vector search → LLM answer
  parser/factory.py    # Parser routing by file type
  storage/db.py        # SQLite async (29 tables, ~150 methods)
  storage/files.py     # FileStorage with pluggable backends
  storage/backends/    # local.py, s3.py, gdrive.py
  storage/vectors.py   # Qdrant + GeminiEmbedder
  skills/engine.py     # YAML skill loader + matcher
  utils/crypto.py      # AES-256-GCM, Fernet, Argon2id key derivation
  web/
    routes.py          # 90+ endpoints (dashboard, files, notes, settings)
    auth.py            # Session auth middleware
    csrf.py            # CSRF protection middleware
    limiter.py         # Rate limiting
    static/css/        # Compiled Tailwind CSS
    templates/         # 35+ Jinja2 templates
    templates/partials/ # 12 HTMX partials (charts, food, heatmap)
  notes/
    capture.py         # Instant note capture
    processor.py       # Async enrichment queue
    enrichment.py      # LLM categorization + entity/fact/task extraction
    categorizer.py     # 12 categories with subcategories
    relations.py       # Semantic + tag + entity linking
    projection.py      # Obsidian vault projection
    vault.py           # Obsidian vault interface
    food.py            # Food analytics, calorie extraction
    morning.py         # Morning briefing engine
    checkin.py         # Evening check-in (signal-based, adaptive)
    reminders.py       # Reminder extraction + recurring reminders
    weekly.py          # Weekly analysis reports
    analytics.py       # Trends, correlations (Pearson r)
    anomaly.py         # Proactive anomaly detection
    habits.py          # Habit tracking + streaks
    export.py          # CSV/JSON export
    watcher.py         # Inbox file watcher
config.yaml            # Main config (models, qdrant, embedding)
tailwind.config.js     # Tailwind 3 config (custom dark theme)
skills/*.yaml          # User-editable classification rules
```

## Telegram Bot Commands

```
/start    — Начать работу         /notes    — Заметки сегодня
/help     — Список команд         /note     — Быстрая заметка
/search   — Семантический поиск   /today    — Метрики дня
/files    — Список файлов         /missing  — Что не заполнено
/scan     — Сканирование          /morning  — Утренний брифинг
/recent   — Последние файлы       /habits   — Привычки и стрики
/stats    — Статистика            /export   — Экспорт заметок
/insights — AI обзор              /analyze  — Недельный анализ
/unlock   — Разблокировать шифрование
```

## Configuration

- **Config priority:** env vars > config.yaml > defaults
- **env_prefix:** empty (no prefix — `TELEGRAM_BOT_TOKEN`, not `APP_TELEGRAM_BOT_TOKEN`)
- **Nested env delimiter:** `__` (e.g., `QDRANT__HOST=localhost`)
- **API keys:** .env file, exported to os.environ via `setup_env_keys()`
- **Secrets:** webhook_secret, pin_code, owner_id — in .env only, NOT in config.yaml

## Security

- **Encryption at rest:** AES-256-GCM (files) + encrypted DB columns, master password + optional key file (2FA)
- **Auth:** bcrypt password hash, session cookies, rate-limited login (5/min)
- **CSRF:** per-session token, auto-injected into forms via JS, X-CSRF-Token header for HTMX
- **CSP:** script-src 'self' unpkg.com; style-src 'self' fonts.googleapis.com; frame-src 'self'; object-src 'self'
- **Path traversal:** validated in LocalBackend._resolve_path()
- **Static files:** `/static/` exempt from auth (CSS/JS must load on login page)
- **Callback safety:** all Telegram query.answer/edit wrapped in _safe_answer/_safe_edit (no "query too old" crashes)

## Background Loops

| Loop | Interval | Purpose |
|------|----------|---------|
| `_note_reminder_loop` | 300s | Send due note reminders + create recurring |
| `_reminder_loop` | 3600s | File-based reminders |
| `_anomaly_check_loop` | 14400s | Detect anomalies (mood drop, sleep deficit, missed food) |
| `_daily_advice_loop` | scheduled | Morning briefing (9:00) + evening advice (20:00) |
| `_orphan_cleanup_loop` | 300s | Clean orphaned DB records + disk files |
| `_skill_reload_loop` | 30s | Hot-reload YAML skills |
| Note processor | continuous | Drain enrichment queue + DB scan for captured notes |
| Evening checkin | scheduled | Signal-based adaptive check-in |
| Daily/weekly summary | scheduled | Auto-generate summaries and reports |
| Inbox watcher | 10s | Watch vault inbox for .md files |

## Gotchas

- **Python 3.14 + Starlette:** Must use `starlette<1.0.0` (Jinja2 LRU cache breaks with unhashable dict-in-tuple)
- **Qdrant gRPC:** Port 6334 blocked by ugreen firewall; use `prefer_grpc=False` (HTTP only)
- **Qdrant direct IP:** Python sockets can't connect directly to 192.168.1.244 (macOS sandbox); must use SSH tunnel
- **FTS5 UPDATE trigger:** Must use `INSERT INTO fts(fts, rowid, ...) VALUES('delete', ...)` syntax
- **datetime.utcnow():** Deprecated in Python 3.14; use `datetime.now()`
- **qdrant-client 1.17+:** `client.search()` removed; use `client.query_points()`
- **Route ordering:** Static `/notes/habits`, `/notes/graph`, etc. MUST be registered before `/notes/{note_id}` (FastAPI matches `{note_id}` first otherwise)
- **Tailwind CSS:** Built via CLI (`make css`), NOT CDN. After template changes run `make css` to rebuild
- **PDF preview:** iframe must NOT have sandbox attribute; download endpoint must NOT have CSP headers
- **Static files auth:** `/static/` path exempt from auth middleware (CSS must load on login page)
- **Embed step is non-fatal:** If Qdrant is down, file still gets processed and stored, just not searchable
