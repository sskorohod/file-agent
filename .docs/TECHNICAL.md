# FileAgent — Техническая документация

## Архитектура

```
Telegram Bot ──┐
Web Dashboard ─┤
REST API ──────┼──→ FastAPI ──→ Pipeline (13 шагов) ──→ Storage
MCP Server ────┘         │                                  │
                         ↓                                  ↓
                    LLM Router                    SQLite + Qdrant + Disk
                    (litellm)
                         │
              ┌──────────┼──────────┐
              ↓          ↓          ↓
          Anthropic   OpenAI    Google
          Claude      GPT       Gemini
```

## Стек технологий

| Компонент | Технология | Версия |
|-----------|-----------|--------|
| Runtime | Python | 3.14 |
| Web Framework | FastAPI + uvicorn | latest |
| Telegram | python-telegram-bot | polling mode |
| LLM Routing | litellm | multi-provider |
| Embeddings | google-genai (Gemini) | gemini-embedding-2-preview |
| Vector DB | Qdrant | remote, SSH tunnel |
| Metadata DB | SQLite + aiosqlite | WAL mode, FTS5 |
| PDF Parsing | PyMuPDF (fitz) | + Tesseract OCR |
| Image Processing | OpenCV + Pillow | auto-crop, PDF assembly |
| Web UI | Jinja2 + HTMX + Tailwind CSS | CDN, no build step |
| Config | Pydantic Settings | YAML + .env |

---

## Pipeline — 13-step document processing

```
1. receive       Validate file (size, extension)
2. auto-crop     Detect document edges in images (OpenCV Otsu threshold)
3. dedup         SHA-256 hash check → skip if exact duplicate
4. ingest        Write to temp directory
5. parse         Extract text (PyMuPDF / Tesseract / python-docx)
6. classify      LLM → category, document_type, tags, summary
7. route         Match skill by category + keywords
8. extract       Skill custom_prompt → structured JSON fields
9. auto-remind   If expiry_date → schedule reminder (180/60/14 days)
10. store        Save to categorized path: ~/ai-agent-files/{category}/{YYYY-MM}/{filename}
11. embed        Gemini multimodal embeddings → Qdrant (text chunks + file bytes)
                 + semantic dedup check (>94% similarity)
12. save_meta    Write to SQLite (file + FTS + processing_log + metadata_json)
13. done         Invalidate search cache, refresh category insight
```

### Auto-crop алгоритм

```python
1. Проверить контраст: edge_mean vs center_mean (порог >25)
2. Otsu's threshold → binary image
3. Morphological close (25x25 kernel) → заполнить текст
4. Connected components → найти самый большой белый регион (бумага)
5. Bounding box → crop с padding 0.3%
```

Работает для: белый документ на тёмном столе, фото с рамками, сканы.

### Embedding Flow

```
Файл → Gemini multimodal embedding (raw bytes)  → point в Qdrant
     → Текст → chunk (400 слов, 50 overlap)     → points в Qdrant

Поиск → query → Gemini text embedding           → kNN search в Qdrant
```

- Модель: `gemini-embedding-2-preview`, 768 dimensions, Cosine distance
- Мультимодальный: PDF, изображения, аудио, видео — из raw bytes
- Текстовый: чанки с `task_type=RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`

---

## База данных — SQLite Schema

### Основные таблицы

| Таблица | Назначение | Ключ |
|---------|-----------|------|
| `files` | Метаданные документов | id (UUID) |
| `processing_log` | Логи pipeline | auto-increment |
| `llm_usage` | Трекинг LLM вызовов | auto-increment |
| `search_cache` | Кеш поисковых запросов | query_hash (MD5) |
| `reminders` | Напоминания о сроках | auto-increment |
| `insights` | AI-аналитика по категориям | category |
| `folders` | Пользовательские папки | auto-increment |
| `file_folders` | Связь файлов и папок | file_id + folder_id |
| `notes` | Заметки и голосовые | auto-increment |
| `chat_history` | История чата | auto-increment |
| `api_keys` | API ключи | key |
| `files_fts` | Полнотекстовый поиск (FTS5) | rowid |

### files — ключевые поля

```sql
id TEXT PRIMARY KEY,
original_name TEXT,
stored_path TEXT,
sha256 TEXT,
size_bytes INTEGER,
mime_type TEXT,
category TEXT DEFAULT 'uncategorized',
tags TEXT DEFAULT '[]',           -- JSON array
summary TEXT,
source TEXT DEFAULT 'telegram',   -- telegram/api/mcp/web
extracted_text TEXT,              -- до 50K символов
metadata_json TEXT DEFAULT '{}',  -- document_type, confidence, skill,
                                  -- chunks_embedded, language, parser,
                                  -- pages, extracted_fields
priority TEXT DEFAULT '',         -- high/medium/low
created_at TEXT,
updated_at TEXT
```

### metadata_json структура

```json
{
  "document_type": "passport",
  "confidence": 0.95,
  "skill": "personal",
  "chunks_embedded": 3,
  "language": "ru",
  "parser": "pymupdf+tesseract",
  "pages": 2,
  "extracted_fields": {
    "full_name": "Иванов Иван",
    "expiry_date": "2028-06-28",
    "document_type": "загранпаспорт",
    "priority": "medium",
    "summary": "...",
    "importance": "...",
    "action_required": "...",
    "related_documents": "...",
    "storage_advice": "..."
  }
}
```

---

## API Reference

### REST API v1

Все эндпоинты требуют `Authorization: Bearer <key>`.

| Method | Endpoint | Mode | Описание |
|--------|---------|------|----------|
| GET | `/api/v1/stats` | lite | Статистика архива |
| GET | `/api/v1/files` | lite | Список файлов (?category, ?limit, ?offset) |
| GET | `/api/v1/files/{id}` | lite | Метаданные файла |
| GET | `/api/v1/files/{id}/text` | lite | Извлечённый текст (?max_chars) |
| GET | `/api/v1/files/{id}/download` | lite | Скачать файл |
| GET | `/api/v1/search` | lite/full | Поиск (?q, ?top_k) |
| POST | `/api/v1/files/upload` | full | Загрузить {filename, data: base64} |
| DELETE | `/api/v1/files/{id}` | full | Каскадное удаление |

### MCP Tools

| Tool | Описание |
|------|----------|
| `get_archive_overview()` | Полная сводка архива |
| `find_and_get(query)` | Поиск + полный текст за 1 вызов |
| `search_documents(query, top_k)` | Лёгкий поиск (чанки) |
| `list_files(category, limit)` | Обзор файлов |
| `get_file_text(file_id, max_chars)` | Текст документа |
| `get_file_metadata(file_id)` | Метаданные |
| `upload_file(filename, data_base64)` | Загрузка |
| `delete_file(file_id)` | Удаление |
| `save_note(content, file_id)` | Сохранить заметку |
| `get_notes(limit)` | Читать заметки |

### MCP Resources

| URI | Описание |
|-----|----------|
| `documents://overview` | Сводка архива |
| `documents://recent` | Последние 10 файлов |
| `documents://notes` | Все заметки |

---

## Конфигурация

### Файлы конфигурации

| Файл | Приоритет | Описание |
|------|-----------|----------|
| `.env` | Высший | Секреты: API ключи, токены |
| `config.yaml` | Средний | Основные настройки |
| `app/config.py` | Низший | Defaults в Pydantic моделях |

### Переменные окружения (.env)

```bash
# Обязательные
TELEGRAM_BOT_TOKEN=...
GOOGLE_API_KEY=...           # Gemini Embedding

# LLM (один из двух)
ANTHROPIC_API_KEY=sk-ant-... # Anthropic Claude
OPENAI_API_KEY=sk-...        # OpenAI GPT

# Qdrant
QDRANT__HOST=localhost
QDRANT__PORT=6333
QDRANT__API_KEY=...

# Web Auth
WEB__SESSION_SECRET=random-string-32-chars
WEB__LOGIN=user@email.com
WEB__PASSWORD_HASH=$2b$12$...  # bcrypt hash

# Telegram
TELEGRAM__OWNER_ID=123456789   # Telegram user ID

# Опциональные
TAVILY_API_KEY=...             # Web research для Insights
```

### Вложенные переменные

Разделитель: `__`. Например:
```
QDRANT__HOST=localhost         → config.qdrant.host
TELEGRAM__OWNER_ID=123        → config.telegram.owner_id
WEB__PORT=8000                → config.web.port
```

---

## Безопасность

| Мера | Реализация |
|------|-----------|
| Web Auth | Email + bcrypt password, session cookie (7 дней) |
| Telegram Auth | `owner_only` декоратор на всех хендлерах |
| API Auth | Bearer token (SQLite, lite/full modes) |
| MCP | Только localhost (allowed_hosts) |
| Rate Limiting | slowapi, 5 попыток/мин на /login |
| SQL Injection | Параметризованные запросы |
| CORS | Ограничен origin |
| HTTPS | Через reverse proxy (nginx/caddy) |

---

## Skills System

### Структура skill YAML

```yaml
name: personal
display_name: "Личные / Personal"
description: "IDs, passports, certificates"
category: personal
priority: 5
enabled: true
tags: [personal, identity]

routing_rules:
  keywords: [passport, driver license, виза]
  patterns: ['\b\d{3}-\d{2}-\d{4}\b']
  mime_types: [application/pdf, image/jpeg]
  min_confidence: 0.3

response_template: |
  🆔 {document_type}
  👤 {full_name}
  📅 Выдан: {issue_date} · Истекает: {expiry_date}
  📝 {summary}
  ✅ {action_required}

extraction:
  fields:
    - name: full_name
      description: "ФИО"
      required: false
    - name: expiry_date
      description: "Дата окончания YYYY-MM-DD"
      required: false
  custom_prompt: >
    Извлеки данные из документа...
```

### Reminder intervals

| Тип документа | Интервал |
|--------------|----------|
| passport, паспорт, загранпаспорт | 180 дней (6 мес) |
| driver, водительск, license, права | 60 дней |
| Все остальные | 14 дней |

---

## Background Tasks

| Task | Интервал | Описание |
|------|----------|----------|
| `_skill_reload_loop` | 30 сек | Перезагрузка изменённых YAML |
| `_orphan_cleanup_loop` | 5 мин | Удаление осиротевших файлов |
| `_reminder_loop` | 1 час | Проверка и отправка напоминаний |
| `_daily_advice_loop` | 9:00 + 20:00 | Ежедневные мотивационные советы |

---

## Файловая структура

```
app/
  main.py              FastAPI app, lifespan, background tasks
  config.py            Pydantic Settings (YAML + env)
  pipeline.py          13-step document processor
  bot/handlers.py      Telegram commands & file handlers
  llm/
    router.py          litellm wrapper, role-based model selection
    classifier.py      Hybrid rule + LLM classification
    search.py          RAG: vector search → LLM answer
    analytics.py       Multi-document trend analysis
    insights.py        Category AI analysis + daily advice
  parser/
    factory.py         Parser routing by file type
    pdf.py             PyMuPDF + Tesseract OCR
    image.py           Vision API + Tesseract
  storage/
    db.py              SQLite async (FTS5, processing_log)
    files.py           File storage with categorization
    vectors.py         Qdrant + GeminiEmbedder
  skills/engine.py     YAML skill loader + matcher
  utils/
    pdf.py             Auto-crop + images → PDF assembly
    errors.py          Error handling utilities
  web/
    routes.py          Dashboard (20+ endpoints)
    auth.py            AuthMiddleware (session-based)
    templates/         Jinja2 + Tailwind CSS templates
  api/routes.py        REST API v1
  mcp_server.py        MCP tools & resources

config.yaml            Main configuration
skills/*.yaml          Document classification rules
data/agent.db          SQLite database
.env                   Environment secrets
Makefile               Dev commands
```

---

## Запуск в production

```bash
# 1. SSH tunnel
ssh -L 6333:localhost:6333 -N ugreen &

# 2. Запуск через gunicorn
gunicorn app.main:app -w 2 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000

# 3. Reverse proxy (nginx)
# → HTTPS termination + static files
```

## Мониторинг

- **Health check:** `GET /health` — статус DB, Qdrant, Telegram, Skills
- **Pipeline logs:** `/logs` в web UI или `processing_log` таблица
- **LLM usage:** `llm_usage` таблица, dashboard AI Usage виджет
- **Errors:** Telegram уведомления через error handler
