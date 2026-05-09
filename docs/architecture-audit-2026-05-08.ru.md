# Аудит архитектуры памяти FAG — 2026-05-08

Честный обзор миграции на cognee после Phase 0–7b. Проверено по
реальному коду на ветке `spike/cognee-compat`, не по памяти.

Английская версия: [architecture-audit-2026-05-08.md](architecture-audit-2026-05-08.md).

## TL;DR

Миграция **работает**, но накручена поверх легаси без удаления
дублирующих путей. Найдено 9 проблем уровня critical/serious и
ещё 8 quality-of-life. Все решаемые, это последствия принципа
«мигрируем сейчас, чистим потом».

## Critical (целостность данных, тихие сбои)

### C1. Dev-проект блокируется при истечении JWT

`DevIngestor.register_project` создаёт нового cognee-юзера со случайным
паролем ([dev_ingest.py:104-119](../app/memory/dev_ingest.py)) и хранит
**только JWT** в `dev_projects.cognee_token`. Пароль выкидывается. JWT
истекает (FastAPI Users default ≈ 1 час). После этого мы не можем
залогиниться снова, и данные проекта оказываются недоступными для FAG
(cognee-юзер всё ещё их владеет, но у нас нет credentials).

**Фикс:** хранить пароль (зашифрованным в `secrets` или рядом с
токеном) и автоматически пере-логиниваться при 401.

### C2. Нет связи `file_id` ↔ cognee `data_id`

`_step_cognee_ingest` ([pipeline.py:670-688](../app/pipeline.py))
шлёт текст + `filename=file_record.original_name` и больше ничего.
FAG UUID (`file_record.id`) в cognee не попадает. Поэтому когда
`cognee.recall` возвращает hit — мы не можем перейти к оригинальному
документу на диске, отрисовать inline-preview или прицепить к
ответу в Telegram.

Именно из-за этого Phase 4 Telegram-ответ остался без кнопок файлов
— `file_id` не существует, нечего рисовать.

**Фикс:** передавать `file_id` (и `source_type=file`) как cognee
metadata в каждом `add`, хранить это в нодах cognee, прокидывать
обратно через `recall` в FAG'овский `LLMSearch.answer`.

### C3. Двойной embedding — Gemini Qdrant И OpenAI lancedb

Каждый документ:

- Шаг 7 [`_step_embed`](../app/pipeline.py) пишет 768-dim Gemini-вектор
  в Qdrant collection `file_agent_v2`.
- Шаг 9.5 `_step_cognee_ingest` запускает cognify, который пишет
  1536-dim OpenAI-вектор в lancedb.

Два LLM-вызова на embedding на каждый документ. Два хранилища надо
поддерживать. Коллекция `file_agent_v2` сейчас читается только
fallback'ом в `LLMSearch.answer` (`use_for_search=false`) и из
`handlers.py:1231` (быстрый non-RAG поиск). В остальном —
write-only мёртвый груз.

**Фикс:** удалить Шаг 7. Дропнуть `file_agent_v2` (или оставить
пустым для fallback'а). Убрать секцию embedding из `config.yaml`.

### C4. Skill-extracted поля не доходят до cognee

Шаг 5.5 ([`_step_extract`](../app/pipeline.py)) использует
`custom_prompt` подходящего skill'а, чтобы извлечь структурированные
поля (priority, expiry_date, amount, parties, и т.д.) и кладёт их
в `files.metadata_json`. Затем `_step_cognee_ingest` шлёт в cognee
**только `parse_result.text`** — структурированные поля в payload
не попадают. Cognee'шный graph-extractor вынужден заново
выводить их из сырого текста — это и дубль работы, и потеря точности.

**Фикс:** включать `extracted_fields` в cognee metadata, чтобы они
ложились на ноду данных и были доступны для запросов без
переизвлечения.

### C5. Spike/canary данные лежат в реальной памяти пользователя

В `main_dataset` сейчас сидят `Sunfish-7392`, `axolotl Mochi`,
`spike2_fixture` и другие фикстуры из Phase 1/3/6. Плюс старый
dataset `personal` всё ещё содержит 9 элементов из ранних
смок-тестов (мигрировали в main_dataset, но не удалили).

**Фикс:** одноразовый скрипт `scripts/cleanup_spike_data.py`,
который удаляет канарики по filename / dataset name и дропает
неиспользуемый `personal`.

## Serious (UX, корректность, стоимость)

### S1. Латентность cognify при upload'е в Telegram

`_step_cognee_ingest` запускает `await cognify(...)` inline в
pipeline'е. На реальном документе это 8–30 секунд. Telegram-юзер
видит «бот думает» намного дольше, чем до миграции — потому что
ждём cognify до ответа.

**Фикс:** превратить Шаг 9.5 в fire-and-forget — `asyncio.create_task`
после Шага 8 (save_meta). `processing_log` будет фиксировать
success/failure асинхронно. Pipeline возвращает ответ
пользователю немедленно.

### S2. Регрессия search_prompt'а в FAG

Phase 4 переключила `/search` через `cognee.recall` при
`use_for_search=true`. Это обходит наш аккуратно настроенный
`search_prompt` в `config.yaml` («respond in same language»,
«cite which document», «give detailed answers»). Cognee'шный
graph_completion имеет свой внутренний промпт — короче и менее
FAG-aware.

Симптом: ответы из Telegram-бота стилистически другие — больше
«фактологический extract», меньше «разговор о твоём архиве».

**Фикс:** использовать cognee с `searchType="CHUNKS"`, чтобы получить
сырой контекст, потом пропускать через свой `LLMSearch.llm.search_answer`
с FAG-промптом. +1 LLM-вызов на каждый поиск, но восстанавливает UX.

### S3. JWT истекает — Codex MCP отваливается

Cognee-mcp child-процесс держит JWT, который мы передали при старте.
При истечении (~1 час) каждый recall возвращает 401. Пользователь
должен пере-минтить и перезапустить Codex.

**Фикс:** поддержка JWT refresh в CogneeClient — на 401 пытаться
`login_as_user(default_user, default_password)` один раз, повторить
запрос. Для Codex'овского child-процесса принимать email/password,
не только JWT.

### S4. Нет memory_type / authority_level

Изначальный план Phase 5 имел `memory_type` (fact / preference /
rule / decision / event / task) и `authority_level` (личная_мысль /
черновик / утверждённое). Не было реализовано. Каждая память в
cognee «плоская» — `axolotl named Mochi` и `legal contract clause N`
выглядят одинаково при retrieval'е. Аналитика («покажи все мои
preferences» или «только durable rules») невозможна.

**Фикс:** добавить `memory_type` и `authority_level` как cognee
metadata (передавать на `add`, выводить через `recall`). На стороне
FAG: тип выводится из skill/extraction (notes → preference / fact,
files → fact, chat → mostly fact). AGENTS.md инструктирует Codex
передавать `memory_type` на `remember`.

### S5. Нет conflict detection / supersession

В cognee нет понятия «этот факт заменяет тот». Скажешь сегодня
«User uses VS Code», а завтра «User switched to Cursor» — оба
останутся в графе, оба придут на recall. Пользователь получит
старый факт примерно так же часто, как новый.

**Фикс:** перед каждым `add` делать `recall(content)` для поиска
похожих. Если найден и LLM-судья считает противоречивым — пометить
старый superseded'ом (атрибут ноды), флагнуть relation. Было в
плане Phase 5, но не построено.

### S6. Нет temporal awareness / `expires_at`

«Vanya is driving home from Las Vegas» сохранилось 8 мая. После
того как он приехал — факт устарел. Cognee никогда не expire'ит
записи. Любой recall в будущем вытащит устаревший факт, пока
пользователь явно не forget'нет.

**Фикс:** хранить `valid_from` / `expires_at` (тоже из исходного
плана) при add. Codex извлекает из формулировки пользователя
(«сегодня», «на этой неделе», «до пятницы»). Recall фильтрует
expired по умолчанию; опциональный `include_expired=true` для
analytics.

### S7. Потерянные file-кнопки в Telegram (регресс)

Связано с C2. Без `file_id` в cognee-ответах наш Telegram-бот не
может отрисовать inline-кнопки скачивания исходного документа.
Пользователь получает текстовый ответ без возможности clickthrough.

**Фикс:** тот же что и C2 — как `file_id` начнёт прокидываться,
восстановить рендер кнопок в `handlers.py`.

### S8. `default_user` — суперюзер by design

Cognee создаёт `default_user@example.com` с `is_superuser=True`.
Главный процесс FAG логинится как этот юзер, поэтому любой
`recall` из Telegram или web видит ВСЕ datasets — включая
`dev_<id>` для проектов, которые должны были быть изолированными.

В мульти-юзер пробе (Phase 5b) мы доказали, что *non-superuser*
юзеры изолированы. Но default_user, через которого ходит FAG для
своих основных потоков, видит всё. Если пользователь подключал
Codex через токен default_user'а (а это и было во время теста) —
агент тоже видит всё, разрушая весь смысл Phase 5.

**Фикс:** создать non-superuser personal-юзера (`fag_personal@local`)
на старте, дать ему ownership над `main_dataset`, использовать ЕГО
токен для основных потоков FAG. Default_user оставить только для
admin-операций (cross-dataset forget, dataset cleanup).

## Quality-of-life

### Q1. cognee-mcp spawn'ится на каждую Codex-сессию

Codex stdio-конфиг spawn'ит свежий `cognee-mcp` child на каждую
сессию. Открыл 5 сессий — 5 cognee-mcp процессов, каждый ~300 MB
RAM. Все они говорят с тем же sidecar'ом, но overhead подпроцессов
складывается.

**Фикс:** держать cognee-mcp как один долгоживущий daemon (HTTP или
Unix socket); Codex подключается, не spawn'ит. Требует чтобы Codex
CLI поддерживал `transport: streamable_http` надёжно с FastMCP —
сейчас не поддерживает (см. Phase 6 spike). Откладываем до апдейта
Codex'а.

### Q2. Голосовые заметки лежат в трёх местах

`_save_smart_note`:

- пишет в SQLite `notes`
- пишет Obsidian `.md` под `storage/notes/`
- ингестит в cognee

Плюс оригинальное Telegram-аудио может остаться кэшированным на диске.

**Фикс:** выбрать один canonical store. Если cognee — это THE memory
layer, таблица `notes` может быть удалена; Obsidian-export становится
опциональным (и генерится из cognee по запросу). Сейчас это дубль
без явной причины.

### Q3. Нет FAG-дашборда для cognee-памяти

Web dashboard (`/files`, `/insights`, `/search`) показывает файлы и
LLM-derived insights. Нет ни одной view для «что в моём memory графе»,
«сколько у меня памяти», «какие топики самые связные», «что забыть».
Cognee работает невидимо для пользователя.

**Фикс:** добавить страницу `/memory` со списком datasets, последними
фактами, графовой визуализацией (у cognee есть свой `/visualize`
endpoint, можно встроить) и UI для forget-by-content. Возможно UI-сервер
самой cognee-mcp.

### Q4. Чат-ингест порог heuristic и неправильный

`ingest_text_to_cognee` срабатывает только для сообщений ≥40 chars.
Пропускает короткие важные («я ушёл из Acme», «купил Tesla», «вышел
на пенсию»). И принимает длинные но нерелевантные размышления.

**Фикс:** убрать chars-порог, заменить на дешёвый LLM-судья
(«это факт о пользователе, стоит сохранять? yes/no») или просто
ингестить всё и пусть cognee-extractor сам решает.

### Q5. Старый dataset `personal` остался, забытый

Мигрировали 9 элементов в `main_dataset`, но source не удалили.
Сидит и путает будущих операторов.

**Фикс:** `cognee.forget(dataset="personal", everything=True)` и
убрать из всех документов.

### Q6. Нет retention policy

cognee SQLite + lancedb растут безгранично. Нет дедупа, нет TTL,
нет «забыть после N лет если не использовалось». Для одного юзера
это нормально лет на 5, но рано или поздно место будет иметь значение.

**Фикс:** background-task `_memory_gc_loop`, который проходит по
редко-используемым нодам и либо сжимает (downgrade в summary), либо
expire'ит. Откладываем до момента, когда место реально начнёт
заканчиваться.

### Q7. Нет backups / recovery

Если `infra/cognee/data/` повреждён — вся derived-память
испаряется. Raw truth (`files`/`notes`/`chat_history` в FAG SQLite)
выживет, но восстановление cognee = re-cognify всего = дорого
(LLM-стоимость).

**Фикс:** scheduled `tar -czf` от `infra/cognee/data/` в локальный
бэкап, restore-скрипт `scripts/restore_cognee_backup.sh`. Для
параноика: ещё пушить в S3.

### Q8. Нет integration-тестов

В `tests/` только старые unit-тесты (db, errors, parser, skills,
storage). Ни одного, который бы прогонял cognee-путь end-to-end.
Каждую регрессию мы ловили вручную через smoke. Следующая регрессия
будет пропущена, пока кто-нибудь не заметит в живую.

**Фикс:** добавить `tests/test_cognee_pipeline.py`, который гонит
полный flow upload → cognee → recall против моков (или одноразового
real-instance в CI).

## Что бы я делал, в порядке

| Sprint | Зачем | Ставка |
|---|---|---|
| **Sprint 1: Quality core** — C1 + C2 + C3 + C4 + C5 | Critical корректность — auth, навигация, дедуп, потерянные данные | 1–2 дня, ~300 LoC |
| **Sprint 2: UX restoration** — S1 + S2 + S7 | Восстановить UX Telegram-бота до уровня до-миграции | 1 день, ~150 LoC |
| **Sprint 3: Memory richness** — S4 + S5 + S6 | Сделать память реально queryable как данные, не плоский мешок | 2–3 дня, ~400 LoC |
| **Sprint 4: Auth & safety** — S3 + S8 + Q5 | Стабильные Codex-сессии, безопасные scopes | 1 день |
| **Sprint 5: Visibility** — Q3 | Пользователь видит свою память и может курировать | 2 дня, UI-работа |
| **Позже** — Q1, Q2, Q4, Q6, Q7, Q8 | Quality-of-life, мониторинг, retention | Откладываем |

Итого core-фиксы: ~4–5 дней сфокусированной работы, чтобы добраться
до качественного продукта. Каждый Sprint мёрджабельный независимо.
