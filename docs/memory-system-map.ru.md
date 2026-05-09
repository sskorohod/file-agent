# Карта памяти — 11 станций

Навигационная карта по архитектуре памяти. Каждая станция — отдельная
зона решений, которую мы обсуждаем, дорабатываем и коммитим независимо.
Используется как чек-лист по ходу работы.

Сопутствующие документы:

- [architecture-audit-2026-05-08.md](architecture-audit-2026-05-08.md) — известные проблемы по серьёзности
- [codex-claude-setup.md](codex-claude-setup.md) — инструкция оператору
- [cognee-spike-report.md](cognee-spike-report.md) — Phase 0, выбор архитектуры
- [cognee-spike2-report.md](cognee-spike2-report.md) — Phase 1, end-to-end и lessons

Английская версия: [memory-system-map.md](memory-system-map.md).

---

## 1. Источники памяти (что создаёт записи)

**Сейчас работает:** Telegram-документы, Telegram-голосовые (опция
«сохранить как заметку»), Telegram-чат (длинные user-сообщения),
web/HTTP-аплоад, Codex/Claude через MCP, dev-проект через
`POST /api/v1/dev/projects/{id}/ingest_repo`.

**Открытые решения:**

- Какие источники должны быть авто-сохранениями, а какие — explicit?
- Голосовое «поиск» — сейчас НЕ сохраняется. Должно ли?
- Assistant turn в чате — сейчас НЕ сохраняется. Хочешь иметь?
- Будут ли новые источники: email, календарь, Slack, скриншоты?

**Код:** [bot/handlers.py](../app/bot/handlers.py),
[api/routes.py](../app/api/routes.py),
[pipeline.py](../app/pipeline.py)

---

## 2. Pre-processing (до того, как факт попадает в cognee)

**Сейчас работает:** файлы → парсинг (PyMuPDF/Tesseract/Vision) →
классификация LLM → skill extract; голосовые → Whisper транскрипция
→ LLM extract заголовка/тегов; чат → фильтр по длине ≥40 chars;
Codex — без preprocessing'а.

**Открытые решения:**

- Должен ли LLM-судья решать «стоит ли вообще запоминать?» перед
  cognee (как у Mem0)?
- Skill-extracted поля (priority, expiry_date, amount) — должны ли
  структурно передаваться в cognee, а не только в SQLite?
- Должен ли быть отдельный preprocessing для Codex-ввода
  (нормализация дат, перефраз)?

**Код:** [parser/factory.py](../app/parser/factory.py),
[llm/classifier.py](../app/llm/classifier.py),
[pipeline.py:_step_extract](../app/pipeline.py)

---

## 3. Метаданные, которые идут вместе с фактом

**Сейчас:** только `filename`. Cognee знает лишь имя загруженного
файла — без связи с FAG `file_id`, без типа источника, без даты,
без приоритета.

**Открытые решения (чем богаче метаданные — тем богаче запросы потом):**

- `file_id` (UUID FAG'а) — обязательно для возврата к оригиналу.
  **Критично.**
- `source_type` (file / note / chat / codex_remember / dev_repo) —
  для фильтрации recall.
- `memory_type` (fact / preference / rule / decision / event /
  task) — для запросов «покажи все мои правила».
- `authority_level` (личная_мысль / черновик / утверждённое /
  официальный_документ) — различать «я подумал X» от «в договоре
  написано X».
- `valid_from` / `expires_at` — temporal awareness.
- `tags` (мы их уже считаем для files) — пробросить в cognee.

**Код:** [memory/cognee_client.py:add](../app/memory/cognee_client.py),
[pipeline.py:_step_cognee_ingest](../app/pipeline.py)

---

## 4. Storage (где физически лежит)

**Сейчас:**

- Raw (источник истины): FAG SQLite [data/agent.db](../data/agent.db)
  — `files`, `notes`, `chat_history` + диск `~/ai-agent-files/`.
- Derived (для поиска): cognee SQLite + lancedb + NetworkX graph
  в `infra/cognee/data/`.
- **Дубль:** Qdrant `file_agent_v2` (Gemini embeddings) — идёт
  запись, чтения почти нет.

**Открытые решения:**

- Убираем ли Qdrant `file_agent_v2` совсем? (Sprint 1, audit C3)
- Где должны жить voice-note Obsidian-markdown'ы — оставить
  дублирование или один canonical store?
- Нужна ли retention policy для cognee state'а? (audit Q6)

**Код:** [storage/db.py](../app/storage/db.py),
[storage/vectors.py](../app/storage/vectors.py),
[storage/files.py](../app/storage/files.py)

---

## 5. Cognify (как cognee строит граф)

**Сейчас:** на каждый `add` отдельный inline `cognify`. Cognee
внутри дёргает свой LLM (Anthropic Claude Sonnet), извлекает
entities + relations, кладёт в lancedb (vectors) + NetworkX (graph).

**Открытые решения:**

- Cognify inline в pipeline (как сейчас, +10–30 сек блокировки)
  или background task (быстрый ответ, но «память появится через
  минуту»)? (Sprint 2, audit S1)
- Batched cognify (раз в N секунд, обрабатывая накопленную очередь)
  или per-document?
- Кастомный prompt для cognify, чтобы извлечение было точнее под
  наши категории?

**Код:** [memory/cognee_client.py:cognify](../app/memory/cognee_client.py)

---

## 6. Conflict / Supersession (что делать с противоречиями)

**Сейчас:** ничего. «Использую VS Code» + «перешёл на Cursor» —
оба остаются как факты. Recall может вернуть любой.

**Открытые решения:**

- Перед каждым `add` искать похожие через `recall(content)` — да/нет?
- Если найден похожий — кто решает «duplicate / supersede /
  contradict»: правила или LLM-судья?
- На supersede что делать со старым — удалить, пометить, оставить
  с relation `superseded_by`?
- Видны ли «протухшие» факты в обычном recall, или только в
  analytics-view?

**Код:** не написано. Будет в
`memory/cognee_client.py:insert_with_conflict_check` (Sprint 3,
audit S5).

---

## 7. Temporal (срок жизни факта)

**Сейчас:** ничего. «Ваня едет домой из Лас-Вегаса» останется
правдой навсегда.

**Открытые решения:**

- Кто проставляет `expires_at` — пользователь явно, LLM-парсер из
  фразы («сегодня»/«до пятницы»), правила по типу (event = 7 дней,
  decision = бессрочно)?
- Истёкшие факты — скрывать в recall или возвращать с пометкой?
- Нужен ли запрос «что протухло на этой неделе, надо обновить?»?

**Код:** не написано. Sprint 3, audit S6.

---

## 8. Retrieval / Search (как достаётся обратно)

**Сейчас:**

- Telegram/web `/search` → cognee.recall (graph_completion) — но
  **потерял наш кастомный `search_prompt`**.
- Codex/Claude через MCP → cognee.recall.
- HTTP API `/api/v1/search` → cognee (full mode) или Qdrant (lite mode).

**Открытые решения:**

- Восстановить FAG-овский search_prompt (Sprint 2, audit S2):
  cognee возвращает CHUNKS, мы пропускаем через свой LLM с нашим
  промптом.
- Какой `searchType` по умолчанию (GRAPH_COMPLETION / RAG_COMPLETION /
  CHUNKS / SUMMARIES)?
- Recall обязан возвращать `file_id`, чтобы Telegram снова
  рисовал inline-кнопки файлов (audit S7).
- Нужен ли «advanced search» с фильтрами по `memory_type`,
  `source_type`, dataset?

**Код:** [llm/search.py](../app/llm/search.py),
[api/routes.py](../app/api/routes.py)

---

## 9. Access scopes (кто что видит)

**Сейчас:**

- `main_dataset` — личное (всё). Через default_user, который
  **superuser** — видит всё, включая dev-проекты.
- `dev_<id>` — изолированный per-project, через отдельного
  non-superuser cognee-юзера.
- `personal` — старый dataset, не используется, не удалён.

**Открытые решения:**

- Создать non-superuser `fag_personal` для main_dataset (Sprint 4,
  audit S8) — чтобы FAG не видел dev-проекты случайно?
- Должны ли `dev_<id>` иметь read-only sub-tokens для безопасной
  выдачи Codex'у?
- Нужны ли «коммерческие» scopes (например `family`, разделяемый
  с другим пользователем)?

**Код:** [memory/cognee_client.py](../app/memory/cognee_client.py),
[memory/dev_ingest.py](../app/memory/dev_ingest.py),
[main.py](../app/main.py)

---

## 10. Codex / Claude integration

**Сейчас:** `~/.codex/config.toml` запускает `cognee-mcp` через
stdio с JWT default_user'а. `~/.codex/AGENTS.md` инструктирует
Codex'а, когда вызывать remember/recall. JWT истекает через ~1 час,
без авто-обновления.

**Открытые решения:**

- Триггер для `remember`: только explicit «запомни» (как сейчас) /
  LLM-судья «это факт стоит сохранять» / автомат на каждый user
  message?
- Refresh JWT'а — на стороне cognee-mcp child или на стороне FAG,
  который хранит email/password? (Sprint 4, audit S3)
- Нужно ли Codex'у видеть отдельную секцию его собственной памяти
  (per-codex-session)?
- Аналогично для Claude Code, ChatGPT — какую конфигурацию
  рекомендуем?

**Код:** `~/.codex/AGENTS.md`, `~/.codex/config.toml`,
[docs/codex-claude-setup.md](codex-claude-setup.md)

---

## 11. Hygiene / lifecycle (поддержание формы)

**Сейчас:** spike-фикстуры (Sunfish-7392, axolotl Mochi, и т.д.)
лежат в реальной памяти; нет UI чтобы посмотреть/удалить факт;
нет backup'ов; нет integration-тестов для cognee-пути.

**Открытые решения:**

- Cleanup-скрипт для seed/canary данных (Sprint 1, audit C5).
- Веб-страница `/memory` — список фактов, кнопка «forget»,
  граф-визуализация (Sprint 5, audit Q3).
- Backup `infra/cognee/data/` — куда (локально / S3) и как часто?
- Integration-тесты — да/нет, какой минимальный набор?

**Код:** scripts/ для cleanup, web/routes.py для UI, infra/ для
backups, tests/ для тестов.

---

## Как пользоваться этим документом

Когда обсуждаем станцию:

1. Называешь номер (1–11).
2. Я разворачиваю текущий код + варианты.
3. Договариваемся о решении.
4. Решение становится acceptance-критерием следующего коммита.
5. Помечаешь здесь ✅.

Естественный порядок (но можно идти как угодно):

1. **Фундамент:** станции 1 → 3 → 4 (input + metadata + storage)
2. **Внутренности cognee:** станции 5 → 6 → 7 (cognify + конфликты + время)
3. **Поверхность:** станции 8 → 9 → 10 (retrieval + scopes + агенты)
4. **Поддержка:** станция 11 (hygiene)
