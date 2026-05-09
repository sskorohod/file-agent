# Отчёт Cognee Spike-2 (Phase 1 — end-to-end)

Прогон против `http://127.0.0.1:8765` 2026-05-07 23:56:27 PDT.

Английская версия: [cognee-spike2-report.md](cognee-spike2-report.md).

## Dataset
- Имя: `spike2`
- Fixture-текст: `'Note about Fixar CRM scheduling rules. The user prefers a 3-hour scheduling window when offering appointment slots to clients. The primary location is the Vancouver office. Reminders should be sent the day before at 9 AM Pacific.'`
- Запрос: `'What is the preferred scheduling window in Fixar CRM?'`

## Результаты

### root
```json
{
  "status_code": 200,
  "body": "{\"message\":\"Hello, World, I am alive!\"}"
}
```

### openapi
```json
{
  "total_paths": 73,
  "memory_paths": [
    "/api/v1/add",
    "/api/v1/cognify",
    "/api/v1/search",
    "/api/v1/permissions/datasets/{principal_id}",
    "/api/v1/datasets",
    "/api/v1/datasets/{dataset_id}",
    "/api/v1/datasets/{dataset_id}/data/{data_id}",
    "/api/v1/datasets/{dataset_id}/graph",
    "/api/v1/datasets/{dataset_id}/data",
    "/api/v1/datasets/status",
    "/api/v1/datasets/{dataset_id}/data/{data_id}/raw",
    "/api/v1/datasets/{dataset_id}/schema",
    "/api/v1/recall",
    "/api/v1/forget"
  ]
}
```

### add
```json
{
  "status_code": 200,
  "elapsed_s": 3.82957075,
  "payload_sample": "{'status': 'PipelineRunCompleted', 'pipeline_run_id': '38131dec-...'}"
}
```

### cognify
```json
{
  "status_code": 200,
  "elapsed_s": 8.845595,
  "payload_sample": "{'3d300c1b-...': {'status': 'PipelineRunCompleted', ...}}"
}
```

### search
```json
{
  "status_code": 200,
  "elapsed_s": 1.87994825,
  "payload_sample": "['Based on the knowledge graph, the preferred scheduling window in Fixar CRM is a **3-hour window** when offering appointment slots to clients.']"
}
```

### recall
```json
{
  "status_code": 200,
  "elapsed_s": 1.8897426250000002,
  "payload_sample": "[{'kind': 'graph_completion', 'search_type': 'GRAPH_COMPLETION', 'text': 'The preferred scheduling window in Fixar CRM is a **3-hour window' ...}]"
}
```

### qdrant
```json
{
  "error": "HTTP 401"
}
```

## Заметки

- `cognify` latency: 8.85s на 229-char фикстуру
- Cleanup: не запускался. State остаётся в cognee для инспекции.

## Lessons (применять в Phase 2+)

Путь от «свежей установки» до «первого 200 OK на `/recall`» вскрыл
несколько контрактных деталей, которые README и llms.txt не зафиксировали:

1. **Vector backend.** `pip install cognee` поставляет адаптеры только
   к `lancedb`, `pgvector` и `chromadb`. Qdrant упомянут в маркетинге,
   но **не** default-extra. Используем `VECTOR_DB_PROVIDER=lancedb`;
   собственный Qdrant FAG'а (`file_agent_v2`) не трогаем.
2. **Два флага для отключения auth.** И `ENABLE_BACKEND_ACCESS_CONTROL=false`,
   и `REQUIRE_AUTHENTICATION=false` должны быть выставлены. Только
   первый — FastAPI Users dependency отвергает запросы с 401. С обоими —
   cognee fallback'ится на `default_user@example.com`, созданного при
   первом старте.
3. **`/add` — multipart, не JSON.** Body schema —
   `Body_add_api_v1_add_post`: `data` — список бинарных файлов-аплоадов,
   `datasetName` — form field. Plain-text идёт как фейковый
   `text/plain` upload.
4. **Все остальные request-bodies — camelCase**, не snake_case. `topK`,
   `searchType`, `runInBackground`, `datasetName`, `nodeName`. Послать
   `dataset_name` или `top_k` — тихий 400.
5. **Anthropic-провайдеру нужен `anthropic` SDK, установленный в
   cognee-venv.** `pip install cognee` его не тянет. Cognee использует
   SDK через `instructor` (`Mode.ANTHROPIC_TOOLS`), обходя litellm — так
   что модели должны быть Anthropic-native id (`claude-sonnet-4-6`),
   не litellm-style (`anthropic/claude-sonnet-4-...`).
6. **Embedding через `gemini/text-embedding-004` упал с timeout
   соединения 30s.** Cognee при старте probe'ит embedding-endpoint и
   bail'ится, если не отвечает. Откатились на
   `openai/text-embedding-3-small` (1536 dim), которая работает с
   существующим `OPENAI_API_KEY`. Повторно пробовать Gemini — known
   unknown на потом — вероятно нужен правильный `EMBEDDING_ENDPOINT`
   или model id ближе к litellm-овским ожиданиям.
7. **Default user создаётся при первом старте**
   (`default_user@example.com / default_password`) внутри
   сконфигурированного system root directory. Пока этот каталог жив,
   следующие рестарты используют того же юзера.
8. **Cognee state path.** Установка `COGNEE_SYSTEM_ROOT_DIRECTORY` в
   `infra/cognee/data/` (относительно корня проекта) держит state
   вне site-packages. start.sh делает `cd` в infra/cognee перед exec,
   так что относительный путь резолвится корректно.
9. **Профиль латентности (маленькая фикстура, ~250 байт):** `add`
   ~3.8s, `cognify` ~8.8s, `search`/`recall` ~1.9s. Для Phase 2 ingest
   это значит, что `cognify` — доминирующая стоимость; нетривиальные
   документы стоит группировать в batch или гонять в background.

Эти уроки приводят к немедленным правкам в
`app/memory/cognee_client.py` (multipart для аналога `/add`,
camelCase в JSON-ключах), `infra/cognee/setup.sh` (плюс
`pip install anthropic`) и `infra/cognee/.env.example` (реальные
имена env-vars и model id'ы).
