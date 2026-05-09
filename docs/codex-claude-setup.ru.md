# Подключение Codex / Claude Code / ChatGPT к памяти FAG

Путь оператора от «FAG запущен» до «Codex может читать и писать в
memory-граф». Подразумевает, что Phase 0–6 миграции на cognee
выполнены.

Английская версия: [codex-claude-setup.md](codex-claude-setup.md).

## Архитектура (напоминание)

```
Codex CLI / IDE       Claude Code / Desktop      Другие MCP-клиенты
       |                        |                        |
       | stdio                  | stdio или HTTP/SSE     |
       v                        v                        v
              cognee-mcp child-процесс(ы)
                          |
                          | HTTP REST (API mode)
                          | Bearer = JWT cognee-юзера
                          v
              cognee FastAPI sidecar (:8765)
                          |
                          v
              локальные SQLite + LanceDB graph
```

Codex запускает свой `cognee-mcp` child через stdio. У Codex CLI'я
HTTP MCP-транспорт (`streamable_http`) **не совместим** со
streamable HTTP сервером FastMCP в cognee-mcp 0.5.4 (запрос долетает,
но ответ не возвращается). stdio — единственный рабочий путь.

Claude Code счастливее с HTTP/SSE — это документировано и работает
против `make cognee-mcp-start` на `:8766`.

## Одноразовая настройка

```bash
make cognee-install           # создаёт .venv-cognee, .env, ставит cognee-mcp
$EDITOR infra/cognee/.env     # заполни LLM_API_KEY, OPENAI_API_KEY и т.д.
make cognee-start             # FAG-facing API на :8765
```

## Default scope: `default_user` + `main_dataset`

`default_user@example.com / default_password` создаётся автоматически
при старте sidecar'а как superuser — удобно для single-user FAG-владельца.
Дефолтный cognee dataset — `main_dataset`, это же дефолт инструмента
`remember` в cognee-mcp, поэтому записи из Codex попадают в тот же
scope, который читает FAG.

Минти JWT для этого юзера:

```bash
TOKEN=$(curl -sS -X POST \
    -F username=default_user@example.com \
    -F password=default_password \
    http://127.0.0.1:8765/api/v1/auth/login | jq -r .access_token)
```

## Codex (stdio — рекомендовано)

Добавь в `~/.codex/config.toml`:

```toml
[mcp_servers.fag-memory]
command = "/абсолютный/путь/к/.venv-cognee/bin/cognee-mcp"
args = [
    "--api-url", "http://127.0.0.1:8765",
    "--api-token", "<JWT из шага выше>",
    "--no-migration",
]
```

Перезапусти Codex. Проверь:

```bash
codex mcp get fag-memory   # transport: stdio
codex mcp list             # fag-memory должен быть enabled
```

В Codex-чате `recall("...")` теперь вызываемый tool. В
sandboxed-вызове `codex exec` пиши `--dangerously-bypass-approvals-and-sandbox`
или `--full-auto`, чтобы auto-approval не отверг tool call.

Помогает ещё положить хинт в `~/.codex/AGENTS.md`, чтобы Codex знал,
когда тянуться к этому tool — пример с рабочей конфигурацией см. в
`~/.codex/AGENTS.md` этого репо.

## Claude Code / Claude Desktop (HTTP — работает с HTTP-сервером cognee-mcp)

```bash
make cognee-mcp-start         # spawn'ит cognee-mcp на :8766 в API mode
```

`~/.claude.json`:

```json
{
  "mcpServers": {
    "fag-memory": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

Или через Claude CLI:

```bash
claude mcp add fag-memory -t http http://127.0.0.1:8766/mcp
claude mcp list
```

## Per-project dev scope (изолированный)

Зарегистрируй dev-проект — нужен `mode=full` API key от FAG:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/dev/projects \
    -H "Authorization: Bearer $FAG_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"name":"FixarCRM","repo_path":"/Users/me/code/fixar-crm"}'
```

Достань cognee JWT проекта:

```bash
sqlite3 data/agent.db \
    "SELECT cognee_token FROM dev_projects WHERE name='FixarCRM';"
```

Подмени этот JWT в `--api-token` Codex-конфига (или замени
`COGNEE_MCP_API_TOKEN` в `infra/cognee/.env`, если ты на HTTP-режиме
для Claude). Перезапусти MCP-вход. Агент теперь видит только данные
`dev_<id>`; `main_dataset` и другие dev-проекты возвращают HTTP 404
по ACL.

## Tools, которые получает агент

| Tool | Назначение |
|---|---|
| `remember` | durable write (без `session_id`) — запускает add+cognify |
| `recall` | durable read |
| `forget_memory` | удаление по dataset; никогда не передавай `everything=true` |
| `improve` | обогащение графа |
| `cognify` | V1 write (background pipeline) |
| `search` | V1 read (CHUNKS, SUMMARIES, GRAPH_COMPLETION, …) |
| `save_interaction` | логирование Q/A пар |
| `delete` / `list_data` / `delete_dataset` | управление данными |
| `prune` | стереть всё (только direct mode, не API mode) |
| `cognify_status` / `codify_status` | статус pipeline'а (только direct mode) |

Рекомендованные: `recall` для чтения, `remember` для записи. V1-tools —
fallback, если нужен сырой chunk-поиск вместо graph-completion'а.

## Troubleshooting

**`mcp: fag-memory/recall (failed) — user cancelled MCP tool call`** в
`codex exec` — auto-approval Codex'а отверг tool call. Добавь
`--dangerously-bypass-approvals-and-sandbox` (или `--full-auto`) для
non-interactive прогонов. В интерактивной сессии нажми Allow.

**`Recall failed: 401 Unauthorized`** — JWT истёк. Пере-минти через
login curl и обнови `--api-token` в Codex-конфиге. Перезапусти Codex.

**Recall возвращает «knowledge graph context is empty»** — `cognify`
ещё в процессе (5–30 секунд на extraction entities и обновление
графа). Подожди, повтори. Смотри `infra/cognee/logs/cognee.log` на
`Pipeline run completed`.

**Codex выставил transport=streamable_http для HTTP-URL и tool calls
тихо падают** — переключайся на stdio (см. секцию Codex). У rmcp
Codex'а и FastMCP-сервера несовместимость в streamable HTTP.

**Переключение scope'ов** — поменяй JWT в Codex-конфиге и перезапусти
Codex. cognee-mcp не поддерживает multi-tenant per request; один
процесс обслуживает один юзер/scope.
