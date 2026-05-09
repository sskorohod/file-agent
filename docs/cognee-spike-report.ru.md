# Отчёт Cognee Spike (Phase 0)

**Ветка:** `spike/cognee-compat`  
**Дата:** 2026-05-07  
**Цель:** подтвердить, можно ли интегрировать Cognee 1.0.x в FAG и каким
способом (embedded library vs sidecar service).

Английская версия: [cognee-spike-report.md](cognee-spike-report.md).

## TL;DR

✅ **Решение: Cognee запускается как sidecar HTTP service в собственном venv.**

Embedded-интеграция невозможна: cognee 1.0.8 транзитивно требует
`starlette>=1.0.0`, что конфликтует с FAG-овским пином `starlette<1.0.0`
(Python 3.14 + Jinja2 LRU cache regression, см. CLAUDE.md). Sidecar
изолирует ~110 транзитивных deps от main FAG venv'а и использует
готовый production-ready FastAPI server cognee.

## Окружение

- macOS Darwin 25.3.0 (arm64)
- Python 3.14.3 (из `~/fag/.venv`)
- Изолированный venv: `.venv-spike/` (создан в worktree)
- Qdrant: SSH tunnel на `127.0.0.1:6333` (активен)

## Установка cognee

`pip install cognee` в `.venv-spike` отрезолвил **cognee 1.0.8** с
~110 транзитивными пакетами. Notable:

| Пакет | Установлено | FAG main pin | Конфликт |
|---|---|---|---|
| **starlette** | **1.0.0** | **<1.0.0** | 🔴 hard |
| litellm | 1.83.7 | 1.82.6 | minor (не из TeamPCP block list) |
| fastapi | 0.136.1 | 0.135.2 | minor |
| pydantic | 2.12.5 | 2.x | none |
| numpy | 2.4.4 | (transitive) | возможен (PyMuPDF/Tesseract) |
| Новые deps | lancedb 0.30.2, networkx 3.6.1, sqlalchemy 2.0.49, redis 7.4.0, alembic 1.18.4, fastapi-users 15.0.5, instructor 1.15.1, fakeredis 2.35.1, ladybug 0.16.0, pylance 0.36.0, langdetect 1.0.9, datamodel-code-generator 0.57.0, cbor2 6.0.1, ... (~30 новых top-level) | — | — |

`import cognee` работает на Python 3.14.3.

## Cognee runtime warnings (на import)

```
Cognee 1.0 changes: New API — remember/recall/forget/improve
  (V1 add/cognify/search still work).
Session memory enabled by default (CACHING=false to disable).
Multi-user access control on by default
  (ENABLE_BACKEND_ACCESS_CONTROL=false to disable).
```

Default state path: `<site-packages>/cognee/.cognee_system/databases` —
**обязательно переопределять** через `COGNEE_SYSTEM_ROOT_DIRECTORY`
(state внутри site-packages теряется при upgrade пакета).

## FastAPI server (кандидат на sidecar)

Cognee поставляется с `cognee.api.client:app` — настоящим FastAPI
приложением с 73 зарегистрированными endpoint'ами.

Запущен через:

```bash
HTTP_API_HOST=127.0.0.1 \
HTTP_API_PORT=8765 \
ENABLE_BACKEND_ACCESS_CONTROL=false \
CACHING=false \
.venv-spike/bin/python -m cognee.api.client
```

Probe'ы:

- `GET /` → 200 `{"message":"Hello, World, I am alive!"}`
- `GET /docs` → отрисовался Swagger UI
- `GET /openapi.json` → 73 paths

Ключевые группы endpoint'ов (по префиксам):

```
/api/v1/auth/{login,logout,me,register,api-keys,...}
/api/v1/add
/api/v1/cognify
/api/v1/memify
/api/v1/search
/api/v1/recall
/api/v1/remember
/api/v1/improve
/api/v1/forget
/api/v1/datasets/...
/api/v1/permissions/...   (multi-tenant ACL — отключён в single-user)
/api/v1/notebooks/...
/api/v1/sessions/...
```

CLI `cognee-cli` принимает `--api-url http://localhost:8765`, чтобы
делегировать команды на запущенный сервер — это и есть канонический
паттерн client → sidecar.

## Что НЕ было проверено в этом spike'е

- **End-to-end** `add → cognify → search` против реальных Anthropic /
  Gemini / Qdrant. Требует API keys, стоит LLM-токенов и пишет в
  Qdrant collection. Отложено в **spike-2 в Phase 1** (запускается
  через `scripts/spike2_cognee_e2e.py` против работающего sidecar'а).
- Подтверждение, что cognee реально использует `GEMINI_API_KEY` для
  embeddings (имя env var и поддерживаемый model id надо подтверждать
  по cognee config; fallback — OpenAI `text-embedding-3-small` 1536-dim).
  Отложено на spike-2.
- Latency `cognify` на реальных размерах текста (1 KB, 10 KB, 100 KB).
  Отложено на spike-2.
- Установка cognee-mcp + wiring в API-mode. Отложено на **Phase 6**.

## Архитектурные решения, принятые в этом spike'е

1. Cognee запускается отдельным процессом от FAG, в `.venv-cognee/`,
   на `127.0.0.1:8765`.
2. Main FAG venv не трогаем — никаких `cognee` import'ов, никаких
   транзитивных upgrade'ов starlette.
3. `app/memory/cognee_client.py` будет async wrapper на `httpx.AsyncClient`,
   а не Python wrapper над cognee-библиотекой.
4. Single-user posture: `ENABLE_BACKEND_ACCESS_CONTROL=false`,
   `CACHING=false`, bind только на loopback.
5. Cognee владеет своим Qdrant collection и dim-моделью embedding —
   шарить `file_agent_v2` не пытаемся.
6. Каталог состояния перекрыт через `COGNEE_SYSTEM_ROOT_DIRECTORY`
   на `infra/cognee/data/`.

## Cleanup

`.venv-spike/` добавлен в `.gitignore` и остаётся на диске для
итераций spike-2. Будет удалён, когда Phase 1 запустит
`infra/cognee/setup.sh`, который провижнит канонический
`.venv-cognee/`.
