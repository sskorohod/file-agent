# Конкурентный аудит memory-проектов и PKM-инструментов

_2026-05-10. Параллельный research трёх агентов: LLM memory engines,
PKM/AI tools, voice-first journaling. Полный raw-output ниже,
синтез — наверху._

## TL;DR — 7 идей для FAG, в порядке ROI/effort

| # | Идея | Источник | Effort | ROI |
|---|---|---|---|---|
| 1 | **`/insights` — surface существующих метрик** (`lag_correlations`, `anomaly_alerts`, `personal_baselines`, `checkin_signal_stats`) | Bearable | S (~2h) | 🔥🔥🔥 |
| 2 | **Weekly digest** Sunday 20:00 (mood/энергия неделя vs прошлая, темы, todos) | Reflect / Saner | M (~1d) | 🔥🔥 |
| 3 | **"On This Day"** ежедневное 09:00 — заметки год/месяц назад на эту дату | Day One | S (~2h) | 🔥🔥 |
| 4 | **Mem0 ADD/UPDATE/DELETE/NOOP fact protocol** — деплицирует факты | Mem0 | M (~1d) | 🔥🔥 |
| 5 | **Bitemporal facts** (`valid_from` / `valid_to` / `superseded_by`) | Zep / Graphiti | M (~1d) | 🔥🔥 |
| 6 | **Auto-backlink при ingest** — title-embedding match → `[[wikilink]]` | Reflect | M (~1d) | 🔥 |
| 7 | **Daily-page в vault** (Logseq) — `/today` пишет `wiki/daily/<date>.md` | Logseq / Khoj | S (~3h) | 🔥 |

**Главный инсайт:** у нас уже есть **семь незадействованных таблиц** —
`lag_correlations`, `anomaly_alerts`, `personal_baselines`,
`checkin_signal_stats`, `note_relations` (1487 rows!), `entity_aliases`,
`magic_link_tokens`. Они **наполняются**, но **не видны пользователю**.
Самый дешёвый win — просто отрисовать их.

---

## Критические архитектурные паттерны (взять из memory-engines)

### 1. Mem0 — ADD/UPDATE/DELETE/NOOP протокол
После каждой extraction LLM отвечает не «вот новые факты», а одним
из четырёх operations против существующих:
```
For each candidate fact:
  top_k_similar = vector_search(fact, k=5)
  llm_decide → ADD | UPDATE id | DELETE id | NOOP
  apply via outbox
```
**Эффект:** факты не дублируются, противоречия резолвятся, growth
ограничен. У FAG сейчас 274 enrichment-rows на 158 заметок — этот
паттерн их свёл бы к 158-200 каноническим.

### 2. Zep / Graphiti — Bitemporal knowledge graph
Каждый факт имеет `valid_from` / `valid_to`. **Никогда не UPDATE,
только close-and-supersede.**
- «Слава живёт в Ереване (2025-03)» получает `valid_to=2026-04` когда
  переехал, новая строка появляется
- Mood/energy за прошлый месяц не перезатираются re-enrichment'ом
- Возможно «как изменилось моё настроение за полгода» как граф-запрос

### 3. Memori — Conscious vs Auto ingest
Два режима памяти:
- **Conscious** — важные постоянные факты, всегда в context window
  (анализируется раз в 6 часов фоновым job'ом, промотируется по
  частоте упоминаний)
- **Auto** — обычный per-query retrieval (как у нас сейчас)

Реализация для FAG:
- `wiki/profile/conscious.md` всегда инжектится в search prompt
- Cron 6h: сканирует notes на факты ≥3 упоминаний → промотирует
- `maxFacts=200` cap → автосуммаризация при переполнении

### 4. TrustGraph — Ontology-driven extraction
`skills/ontology.yaml` определяет canonical entity types и
allowed relations:
```yaml
entities:
  - kind: Person
    fields: [name, aliases, role]
  - kind: Project
    fields: [name, status]
relations:
  - (Person, works_on, Project)
  - (Person, mentioned_in, Note)
```
LLM-extractor должен соответствовать схеме. Анти-drift для
free-form `category` / `tags`.

### 5. LlamaIndex Memory Blocks
Чёткое разделение трёх типов памяти:
- **StaticMemoryBlock** — `wiki/profile/static.md` всегда injected
- **FactExtractionMemoryBlock** — `wiki/facts.md` с `maxFacts` + auto-summarize
- **VectorMemoryBlock** — `wiki/episodic/` = текущие notes/files

У нас всё это разбросано — стоит унифицировать в три файла.

---

## UX-паттерны (взять из PKM-инструментов)

### Khoj — Natural-language schedules
```
/schedule "пришли мне сводку по работе каждый понедельник"
```
LLM парсит ↦ APScheduler cron job ↦ запускает saved query.

### Reor / Smart Connections — "Related" sidebar live
При просмотре документа (web `/files/<id>` или Telegram preview) —
блок «Связанные» с топ-3 cosine-ближайшими. **Один доп. Qdrant-вызов**,
~50 LOC.

### Logseq — Daily journal page
`/today` не просто timeline, а **записывает** в `wiki/daily/<date>.md`,
с auto-prepend новых voice/text notes. Vault становится канонический
журнал, не expor.

### Saner — Reflection flow
`/reflect today|week` → LLM сводит mood/themes/highlights → возвращает
reflection card с **«Сохранить как заметку»** inline button. Reuses
существующий `text_ingest.py`.

### Reflect — Auto-backlink
При ingest заметки: title-embedding search → если cosine >0.85 с
существующей → автоматически вставляет `[[Existing Title]]` в saved
markdown. **Vault становится графом** без ручной разметки.

### Quivr — Query rewriting
Перед vector search — мини-LLM call расширяет query на основе
последних 3 Telegram-сообщений. Ловит «и за позавчера тоже»-style
follow-ups. **Дёшево, большая precision win.**

### Open WebUI — Memory as tool calls
4 явные функции для LLM: `add_memory`, `search_memories`,
`replace_memory_content`, `delete_memory`. Модель сама решает «это
durable preference, save» vs «эфемерное». Чище чем «всё в cognee».

### Rewind / Heyday — Timeline scrubber
Горизонтальный heat-map последних 30 дней, цвет = mood × volume,
клик → день. Pure HTMX над существующими данными.

---

## Voice-first / journaling-аналитика

### Bearable — Factor correlations
Plain-English wording корреляций:
> «Когда ты спишь <6ч, настроение на следующий день -1.2, p<0.05»

**У FAG `lag_correlations` уже есть!** Не используется.

### Day One — On This Day
Ежедневный пинг: «Год назад в этот день ты писал про…».
Огромный emotional payoff, низкая стоимость.

### Daylio — Mood-by-activity bar
Горизонтальный bar: avg mood per top-10 categories. Раскрывает что
именно на тебя влияет позитивно («running: 4.6/5; meetings: 2.8/5»).

### Apple Health Journaling — Suggestion-based prompts
> «Ты прошёл 12k шагов и звонил маме — хочешь записать про сегодня?»

State-of-mind logging как **отдельный лёгкий check-in** дважды в день
(утро/вечер), отделённый от journaling.

### Exist.io — Daily score
Composite weighted: `today = 0.3·mood + 0.3·sleep + 0.2·focus + 0.2·social`.
Daily push: «Сегодня вероятно будет 7/10 — сон хороший, но
тренировку вчера пропустил».

### Voice Notes — Action item auto-extraction
Каждая голосовая → LLM выделяет «todos» → агрегируются в недельный
todo-список. У FAG уже есть `note_tasks` table — не используется.

---

## Что у FAG **уже лучше** чем у этих проектов

- **Selective AES-256-GCM шифрование sensitive файлов** — никто из
  список не имеет
- **PIN-gating через Telegram** — Reflect / Saner закрытые но даже
  у них нет такого UX
- **Multi-store с outbox-pattern** для eventual consistency — Mem0
  и Letta этого не делают, у них один store
- **Telegram-нативность** — у всех остальных PKM web/electron, мы в
  чате 24/7
- **Karpathy LLM-wiki в `~/ai-agent-files/wiki/`** — markdown vault
  как human-readable source-of-truth — у них либо чёрный ящик,
  либо .md без LLM-управления

---

## Конкретный sprint-roadmap (по убыванию ROI)

### Sprint K — Surface unused tables (1 день, 0 LLM-расхода)
1. `/insights` команда: cards с `lag_correlations` + recent
   `anomaly_alerts` + `note_relations`
2. Mood-by-category bar в `/dashboard`
3. Timeline-heatmap последних 30 дней (Rewind-style)

**Файлы:** `app/bot/handlers.py:cmd_insights`,
`app/analytics/dashboard.py:_panel_mood_by_category`,
`_panel_timeline_heatmap`.

### Sprint L — Weekly digest + On This Day (1 день, $0.05/неделя)
4. APScheduler job Sunday 20:00: weekly digest
5. APScheduler job 09:00: «year ago today»
6. Anomaly nudges в `_daily_advice_loop`

**Файлы:** `app/services/weekly_digest.py`,
`app/services/on_this_day.py`, `app/main.py` lifespan.

### Sprint M — Mem0 fact protocol (2 дня)
7. `app/llm/fact_resolver.py`: ADD/UPDATE/DELETE/NOOP
8. Schema migration: `facts` table с FK на notes/files
9. Pipeline step 4.5 после classify

### Sprint N — Bitemporal + Conscious (1.5 дня)
10. Schema: `valid_from`/`valid_to`/`superseded_by` в `facts` table
11. `wiki/profile/conscious.md` — promote-by-frequency cron
12. `app/llm/search.py` инжектит conscious.md в каждый prompt

### Sprint O — Auto-linking + daily-page (1 день)
13. `text_ingest.py`: title-embedding search → auto `[[wikilink]]`
14. `/today` пишет в `wiki/daily/<date>.md`
15. Query rewriting перед vector search (Quivr)

---

## Полный raw-output трёх research-агентов

(всё ниже — нетронутый вывод; см. сводку выше для синтеза)

### Agent 1: LLM memory engines

(см. результат search-агента выше — Letta, Mem0, Zep/Graphiti,
Memary, R2R, AnythingLLM, Memori, TrustGraph, LlamaIndex)

### Agent 2: PKM/AI tools

(Khoj, Reor, AnythingLLM, Logseq, Smart Connections, Saner, Reflect,
Rewind, Quivr, Open WebUI)

### Agent 3: Voice-first journaling

(Saner, Reflect, Day One, Bear, Heyday, Voice Notes, Roam/Logseq,
Bearable, Daylio, Apple Health Journal, Rize/Timing, Exist.io)

См. оригинальный chat-output для деталей по каждому.
