# AI File Intelligence Agent — PRD v2.0

**AI File Intelligence Agent**

Personal AI-powered document processing, classification & knowledge base

Platform: Telegram Bot + Web Dashboard

Stack: Python / FastAPI / SQLite / Qdrant

LLM Providers: Anthropic · OpenAI · Google Gemini

Deployment: Local (macOS)

Version 2.0 · March 2026

## 1. Executive Summary

### 1.1 Product Vision

AI File Intelligence Agent — это персональный AI-агент, который принимает файлы через Telegram, автоматически их анализирует, классифицирует, сохраняет в структурированном виде и строит из них доступную базу знаний с семантическим поиском.

### 1.2 Key Differentiators

| Свойство | Описание |
| --- | --- |
| Мульти-LLM | Выбор модели на лету: Claude, GPT, Gemini. Настройка через UI. |
| Skill Engine | Пользователь создаёт правила обработки (skills) и редактирует их через дашборд. |
| Локальный деплой | Всё работает на Mac. Данные не уходят на сторонние серверы (LLM API вызовы — единственное исключение). |
| Web Dashboard | Минимальный UI для поиска, настроек, просмотра файлов и управления скиллами. |

### 1.3 Target User

Solo-пользователь (владелец системы), который хочет организовать свои документы (медицинские, финансовые, бизнес-документы) и получать к ним быстрый доступ через семантический поиск.

## 2. User Scenarios (MVP)

### 2.1 Загрузка файла

**Actor:** Пользователь в Telegram

**Trigger:** Отправка PDF, фото или DOCX в бот

**Flow:** 1) Пользователь отправляет файл + опциональный комментарий → 2) Бот парсит файл → 3) LLM классифицирует → 4) Skill определяет папку → 5) Бот отвечает с summary

**Result:** Файл сохранён, вектор создан, метаданные записаны.

### 2.2 Поиск по базе

**Actor:** Пользователь в Telegram или Dashboard

**Trigger:** Текстовый запрос (например: "Покажи мои анализы за 2024")

**Flow:** 1) Embedding запроса → 2) Vector search → 3) LLM формирует ответ с ссылками на файлы

### 2.3 Управление Skills

**Actor:** Пользователь в Dashboard

**Trigger:** Создание / редактирование YAML-скилла через веб-интерфейс

**Flow:** 1) Открыть редактор → 2) Изменить правила → 3) Save & Reload → 4) Новая логика применяется к следующему файлу

### 2.4 Настройка LLM-провайдеров

**Actor:** Пользователь в Dashboard → Settings

**Flow:** 1) Ввести API-ключ → 2) Выбрать модель (напр. claude-sonnet-4-20250514) → 3) Назначить роль: classification / extraction / search / default

## 3. System Architecture

### 3.1 High-Level Pipeline

Telegram Bot ────────────────────────────────────────→ Web Dashboard

│ │

└──────────────┐ FastAPI Backend ┌───────────────┘

│ │

└────────┬────────┘

│

┌──────────┼───────────┐

│ │ │

Ingestion LLM Router Skill Engine

│ │ │

Parser Providers Rules (YAML)

│ (Claude/ │

│ GPT/ │

│ Gemini) │

│ │ │

└──────────┼───────────┘

│

Storage Layer

┌──────┼───────┐

│ │ │

Files SQLite Qdrant

### 3.2 Tech Stack (MVP)

| Компонент | Технология | Почему |
| --- | --- | --- |
| Backend | Python 3.11 + FastAPI | Асинхронный, лёгкий, опыт с Fixar |
| Telegram | python-telegram-bot (async) | Официальная библиотека, webhook-моде |
| PDF Parser | PyMuPDF (fitz) | Быстрый, OCR через Tesseract fallback |
| OCR | Tesseract + pytesseract | Бесплатный, работает локально |
| DOCX | python-docx | Надёжный парсинг Word |
| Metadata DB | SQLite + aiosqlite | Ноль конфигурации, локально |
| Vector DB | Qdrant (embedded) | Локальный, мощная фильтрация, Rust-производительность |
| LLM Router | litellm | Единый API для Anthropic/OpenAI/Google |
| Web UI | FastAPI + Jinja2 + HTMX | Минимальный JS, SSR, быстрый дев |
| Config | YAML + Pydantic Settings | Редактирование руками + валидация |

## 4. Component Specifications

### 4.1 Telegram Bot

Входная точка системы. Принимает файлы и текстовые команды, отправляет ответы.

**Commands:**

- /start — приветствие и инструкция

- /search <query> — поиск по базе

- /recent — последние 10 файлов

- /stats — статистика базы

**Supported file types:** PDF, JPEG/PNG, DOCX. Размер до 20MB.

**Authentication:** Whitelist по Telegram user_id в config.yaml.

### 4.2 Ingestion Layer

Принимает файл от Telegram, генерирует UUID, сохраняет во временную папку.

**Output contract:**

```
{

"file_id": "uuid",

"original_name": "report.pdf",

"temp_path": "/tmp/ingestion/uuid.pdf",

"mime_type": "application/pdf",

"size_bytes": 245760,

"user_comment": "lab results from Jan"

}
```

### 4.3 Parser

Извлекает текст из файла. Стратегия зависит от типа:

| Тип | Библиотека | Fallback |
| --- | --- | --- |
| PDF (текстовый) | PyMuPDF | Tesseract OCR если текст пустой |
| PDF (скан) | Tesseract OCR | Gemini Vision если OCR низкого качества |
| Image | Tesseract OCR | Gemini Vision / GPT-4o Vision |
| DOCX | python-docx | — |

**Важно:** Для изображений и сканов низкого качества — использовать LLM Vision API как fallback. Это дороже, но точнее.

### 4.4 LLM Router (Provider Abstraction)

Центральный компонент для работы с LLM. Оборачивает все провайдеры через litellm.

**Конфигурация провайдеров (config.yaml):**

```
providers:

anthropic:

api_key: "sk-ant-..."

models:

- id: "claude-sonnet-4-20250514"

roles: [classification, extraction, search]

- id: "claude-haiku-4-5-20251001"

roles: [classification] # дешевле

openai:

api_key: "sk-..."

models:

- id: "gpt-4o"

roles: [extraction, search]

- id: "gpt-4o-mini"

roles: [classification]

google:

api_key: "..."

models:

- id: "gemini-2.0-flash"

roles: [classification, vision_ocr]

defaults:

classification: "claude-sonnet-4-20250514"

extraction: "gpt-4o"

search: "claude-sonnet-4-20250514"

vision_ocr: "gemini-2.0-flash"
```

**Роли моделей:**

- classification — определение категории и типа документа

- extraction — извлечение сущностей и генерация summary

- search — ответы на поисковые запросы пользователя

- vision_ocr — распознавание изображений и сканов

### 4.5 Classification Engine

Классификатор использует structured output (JSON mode) для предсказуемого результата.

**Output schema:**

```
{

"category": "Health",

"subcategory": "Labs",

"document_type": "lab_result",

"confidence": 0.94,

"tags": ["blood_test", "CBC"],

"entities": {

"date": "2024-01-15",

"provider": "Quest Diagnostics",

"patient": "John Doe"

},

"summary": "CBC blood test results...",

"language": "en"

}
```

**Категории (MVP):**

| Категория | Подкатегории | Примеры типов |
| --- | --- | --- |
| Health | Labs, Doctors, Imaging, Insurance | lab_result, doctor_note, xray, insurance_eob |
| Business | Invoices, Taxes, Contracts, Receipts | invoice, tax_form, contract, receipt |
| Personal | ID, Legal, Education, Other | passport, lease, diploma, letter |

### 4.6 Skill Engine

Skills — это YAML-файлы, которые определяют поведение системы для каждой категории. Пользователь может редактировать их через Dashboard.

**Пример skill (health.yaml):**

```
name: health

description: "Medical document processing"

routing:

- match:

document_type: "lab_result"

folder: "Health/Labs"

naming: "{date}_{provider}_{type}"

- match:

document_type: "doctor_note"

folder: "Health/Doctors"

naming: "{date}_{doctor}_{type}"

extraction:

required_entities:

- date

- provider

optional_entities:

- diagnosis

- doctor

- medications

prompts:

classification: |

You are classifying medical documents.

Focus on document type and date extraction.
```

**Skill жизненный цикл:**

1. Skill Engine загружает все YAML из /skills/ при старте

2. После классификации — находит матчинг skill по category

3. Применяет routing rules для определения папки и имени файла

4. Пользователь редактирует через UI → hot reload без рестарта

### 4.7 Storage Layer

#### 4.7.1 File Storage

\~/ai-agent-data/

files/

Health/

Labs/

Doctors/

Imaging/

Business/

Invoices/

Taxes/

Personal/

ID/

Legal/

temp/ # временные файлы (авто-очистка 24h)

db/

metadata.db # SQLite

qdrant/ # Qdrant storage data

skills/

health.yaml

business.yaml

personal.yaml

config.yaml # основная конфигурация

#### 4.7.2 Metadata DB (SQLite)

```
CREATE TABLE documents (

id TEXT PRIMARY KEY,

original_name TEXT NOT NULL,

file_path TEXT NOT NULL,

mime_type TEXT,

size_bytes INTEGER,

category TEXT,

subcategory TEXT,

document_type TEXT,

confidence REAL,

summary TEXT,

tags TEXT, -- JSON array

entities TEXT, -- JSON object

user_comment TEXT,

language TEXT DEFAULT 'en',

created_at TEXT DEFAULT (datetime('now')),

updated_at TEXT DEFAULT (datetime('now'))

);

CREATE TABLE processing_log (

id INTEGER PRIMARY KEY AUTOINCREMENT,

document_id TEXT REFERENCES documents(id),

step TEXT, -- ingestion|parsing|classification|storage

status TEXT, -- success|error

model_used TEXT,

duration_ms INTEGER,

error_message TEXT,

created_at TEXT DEFAULT (datetime('now'))

);
```

#### 4.7.3 Vector DB (Qdrant)

Qdrant работает в embedded режиме через qdrant-client (Python, без отдельного сервера). Преимущества: мощная фильтрация по payload, высокая производительность (Rust), лёгкий переход на client-server режим при масштабировании. Документы разбиваются на чанки для лучшего поиска.

**Chunking strategy:**

- Текст до 500 слов → 1 чанк

- Текст > 500 слов → чанки по 400 слов с overlap 50

- Каждый чанк хранит payload: document_id, category, tags, chunk_index

**Embedding model:** all-MiniLM-L6-v2 (sentence-transformers) — локальный, бесплатный, 384 размерность.

**Режим работы:** qdrant-client в режиме :memory: для тестов, path=./db/qdrant для прода. При масштабировании — переключение на Qdrant server (Docker) без изменения кода.

**Payload-фильтрация (ключевое преимущество Qdrant):**

```
# Поиск с фильтром по категории и дате:

client.search(

collection_name="documents",

query_vector=embedding,

query_filter=Filter(must=[

FieldCondition(key="category", match=MatchValue(value="Health")),

FieldCondition(key="date", range=Range(gte="2024-01-01")),

]),

limit=10

)
```

### 4.8 Web Dashboard

Минимальный веб-интерфейс для управления системой. FastAPI + Jinja2 + HTMX (минимальный JS).

| Страница | Функционал |
| --- | --- |
| Dashboard | Статистика: кол-во файлов по категориям, последние обработки, ошибки |
| Files | Просмотр всех файлов, фильтры по категории, поиск |
| Search | Семантический поиск по базе с превью результатов |
| Skills | YAML-редактор с валидацией и hot reload |
| Settings | API-ключи, выбор моделей, назначение ролей |
| Logs | Логи обработки с фильтрами по статусу |

## 5. Processing Pipeline (Detail)

| Шаг | Компонент | Вход | Выход | Async |
| --- | --- | --- | --- | --- |
| 1. Receive | Telegram Bot | File + comment | IngestPayload | ✔ |
| 2. Ingest | Ingestion Layer | IngestPayload | TempFile + UUID | ✔ |
| 3. Parse | Parser | TempFile | ExtractedText | ✔ |
| 4. Classify | LLM Router | ExtractedText | Classification JSON | ✔ |
| 5. Route | Skill Engine | Classification | Folder + filename | ✖ |
| 6. Store file | File Storage | TempFile + route | Final path | ✖ |
| 7. Embed | Qdrant | ExtractedText | Vector(s) | ✔ |
| 8. Save meta | SQLite | All above | DB record | ✖ |
| 9. Respond | Telegram Bot | Summary | Message to user | ✔ |

**Важно:** Шаги 2--8 выполняются в background task (asyncio.create_task). Пользователь сразу получает "Файл принят, обрабатываю...", а потом готовый ответ.

## 6. Error Handling & Retry

| Ошибка | Стратегия | Max retries |
| --- | --- | --- |
| LLM API timeout | Retry с exponential backoff | 3 |
| LLM API rate limit | Переключиться на fallback модель | 1 |
| Parser failure | Попробовать fallback метод (OCR → Vision) | 1 |
| File too large | Отклонить с сообщением | 0 |
| Invalid JSON from LLM | Re-prompt с инструкцией вернуть только JSON | 2 |
| Qdrant write error | Retry, записать в лог и продолжить | 2 |

Все ошибки логируются в таблицу processing_log. Пользователь видит ошибки в Dashboard → Logs.

## 7. Non-Functional Requirements

| Требование | Значение | Комментарий |
| --- | --- | --- |
| Обработка файла | < 10 сек (полный pipeline) | Включая LLM call |
| Поиск | < 1 сек (векторный поиск) | Без LLM обработки |
| Макс. файл | 20 MB | Telegram лимит |
| Одноврем. обработка | 3 файла | asyncio semaphore |
| Надёжность | Нет потери файлов | Файл сохраняется первым |
| Безопасность | User whitelist | По user_id в config |

## 8. Repository Structure

```
ai-file-agent/

├── app/

│ ├── main.py # FastAPI app + startup

│ ├── config.py # Pydantic Settings

│ ├── bot/

│ │ ├── handlers.py # Telegram handlers

│ │ └── commands.py # /start, /search, /recent

│ ├── ingestion/

│ │ └── ingest.py # File download + UUID

│ ├── parser/

│ │ ├── base.py # Parser interface

│ │ ├── pdf_parser.py # PyMuPDF + Tesseract

│ │ ├── image_parser.py # OCR + Vision fallback

│ │ └── docx_parser.py # python-docx

│ ├── llm/

│ │ ├── router.py # LLM provider routing

│ │ ├── classifier.py # Classification prompts

│ │ └── search.py # RAG search logic

│ ├── skills/

│ │ ├── engine.py # Skill loader + matcher

│ │ └── validator.py # YAML schema validation

│ ├── storage/

│ │ ├── files.py # File move + naming

│ │ ├── db.py # SQLite operations

│ │ └── vectors.py # Qdrant operations

│ ├── web/

│ │ ├── routes.py # Dashboard routes

│ │ └── templates/ # Jinja2 templates

│ │ ├── base.html

│ │ ├── dashboard.html

│ │ ├── files.html

│ │ ├── search.html

│ │ ├── skills.html

│ │ ├── settings.html

│ │ └── logs.html

│ └── pipeline.py # Orchestrator

├── skills/ # User-editable skills

│ ├── health.yaml

│ ├── business.yaml

│ └── personal.yaml

├── config.yaml # Main config

├── requirements.txt

├── Makefile # make run, make test

└── README.md
```

## 9. MVP Scope

### 9.1 Included

| Компонент | Статус | Комментарий |
| --- | --- | --- |
| Telegram Bot | ✔ MVP | Приём файлов, команды, ответы |
| Parser (PDF + OCR + DOCX) | ✔ MVP | 3 типа файлов |
| LLM Router | ✔ MVP | Все 3 провайдера, роли моделей |
| Classification | ✔ MVP | Structured JSON output |
| Skill Engine | ✔ MVP | YAML routing + hot reload |
| File Storage | ✔ MVP | Категоризованное хранение |
| SQLite Metadata | ✔ MVP | Метаданные + логи |
| Qdrant Vectors | ✔ MVP | Embedding + поиск + фильтрация |
| Web Dashboard | ✔ MVP | 6 страниц |
| Settings UI | ✔ MVP | API ключи + модели |

### 9.2 Excluded (Future)

- Knowledge Graph (граф связей между документами)

- Long Chain Engine (глубокий анализ цепочкой вызовов)

- Auto-learning skills (автоматическое создание правил)

- Мульти-юзер поддержка

- Аналитика здоровья / финансов

- Мобильное приложение

## 10. Roadmap

| Фаза | Срок | Деливери |
| --- | --- | --- |
| Phase 1: MVP | 2--3 недели | Полный pipeline + Dashboard + Skills |
| Phase 2: Search+ | 1--2 недели | RAG-ответы, улучшенный поиск, фильтры |
| Phase 3: Intelligence | 2--4 недели | Memory Layer, связи между документами, auto-skills |
| Phase 4: Analytics | 3—4 недели | Мед. аналитика, финансовые отчёты, рекомендации |

## 11. Success Criteria

| Метрика | Target | Как измерять |
| --- | --- | --- |
| Точность классификации | > 85% | processing_log + ручная проверка |
| Корректная папка | > 90% | Проверка file_path в DB |
| Скорость поиска | < 1 сек | Логирование времени |
| Скорость pipeline | < 10 сек | duration_ms в логах |
| Error rate | < 5% | processing_log ошибки / всего |
| Обучаемость | скиллы работают | Тест нового skill на 5 файлах |

## 12. Рекомендации по улучшению

### 12.1 Архитектурные

**1. litellm вместо прямых SDK.** Оригинальный PRD не описывал как именно переключаться между провайдерами. litellm даёт единый API для всех трёх + fallback из коробки.

**2. Роли моделей.** Не все задачи требуют мощную модель. Classification можно делать на Haiku/Mini (дешевле), а extraction — на полных моделях. Это экономит до 70% на API.

**3. HTMX вместо React.** Для dashboard из 6 страниц React — оверкилл. HTMX + Jinja2 даёт интерактивность без сборки фронта, и всё остаётся в Python экосистеме.

**4. SQLite вместо PostgreSQL.** Для локального single-user агента PostgreSQL избыточен. SQLite с aiosqlite работает из коробки, zero config, и отлично справляется с тысячами документов.

### 12.2 Функциональные

**5. Vision API как OCR fallback.** Tesseract плохо справляется со сканами низкого качества и фотографиями. Gemini Flash или GPT-4o-mini как fallback для vision OCR значительно повысит точность распознавания.

**6. Chunking + overlap.** Оригинальный PRD не описывал стратегию разбивки текста. Для точного семантического поиска нужен chunking (400 слов, overlap 50) — иначе длинные документы плохо ищутся.

**7. Processing log.** Добавлена таблица processing_log для отслеживания каждого шага pipeline. Это критично для отладки и Dashboard → Logs.

**8. Naming templates в skills.** Skills теперь определяют не только папку, но и шаблон имени файла ({date}_{provider}_{type}). Это делает файлы читаемыми без открытия.

### 12.3 UX

**9. Мгновенный ответ + follow-up.** Пользователь сразу получает "Файл принят, обрабатываю..." и не ждёт 10 секунд. Результат приходит вторым сообщением. Это важно для Telegram UX.

**10. YAML-редактор с валидацией.** Вместо ручного редактирования файлов — веб-редактор с подсветкой YAML и real-time валидацией схемы. Ошибки видны до сохранения.

### 12.4 Будущее (Phase 2+)

**11. Feedback loop.** Когда пользователь исправляет категорию — сохранять correction в отдельную таблицу. Потом использовать для few-shot примеров в промптах классификации.

**12. MCP Server.** Выставить агента как MCP server (ты уже знаешь формат по Fixar). Это позволит Claude Code и другим агентам работать с базой документов.

**13. Дедупликация.** Перед сохранением — проверять hash файла. Если файл уже есть — спрашивать пользователя вместо дублирования.

**14. Bulk import.** Возможность загрузить папку с файлами (через Dashboard) для начального наполнения базы. Без этого старт с нуля может быть утомительным.

## 13. Constraints & Guardrails

- LLM НЕ принимает решения о хранении — только классифицирует. Skill Engine решает куда положить файл.

- Long Chain Engine НЕ используется в основном pipeline — только по запросу.

- Данные НЕ хранятся только в vector DB — всегда есть file + SQLite как source of truth.

- API ключи хранятся в config.yaml с ограниченными правами. Не передаются через Telegram.

- Все LLM вызовы логируются с model_used и duration для контроля затрат.
