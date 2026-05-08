# Connecting Codex / Claude Code / ChatGPT to FAG memory

This is the operator's path from "FAG is running" to "Codex can read and write
the memory graph". It assumes Phases 0–6 of the cognee migration are done.

## Architecture (recap)

```
external agent (Codex, Claude Code, ChatGPT)
        ↓ MCP over Streamable HTTP
cognee-mcp on 127.0.0.1:8766
        ↓ HTTP REST (API mode), Bearer = cognee user JWT
cognee FastAPI sidecar on 127.0.0.1:8765
        ↓
local SQLite + LanceDB graph
```

The agent only ever talks to **cognee-mcp**. It never touches the FAG
process. The user a JWT belongs to determines what scope the agent can
read and write.

## One-time setup

```bash
make cognee-install          # creates .venv-cognee, .env, installs cognee-mcp too
$EDITOR infra/cognee/.env    # fill in LLM_API_KEY, OPENAI_API_KEY, etc.
make cognee-start            # FAG-facing API on :8765
```

You'll get a JWT for whichever scope you want the agent to see. There are
two ways to pick the scope.

### Scope A: personal (default user, sees everything)

`default_user@example.com / default_password` is created automatically by
the sidecar at startup. It's a superuser, which means it can read across
all datasets — convenient for a single-user FAG owner.

```bash
TOKEN=$(curl -sS -X POST \
    -F username=default_user@example.com \
    -F password=default_password \
    http://127.0.0.1:8765/api/v1/auth/login | jq -r .access_token)

# Persist into infra/cognee/.env so make cognee-mcp-start picks it up.
sed -i.bak "s|^COGNEE_MCP_API_TOKEN=.*|COGNEE_MCP_API_TOKEN=${TOKEN}|" infra/cognee/.env
```

### Scope B: a single dev project (isolated)

Register the project (HTTP API needs a `mode=full` API key from FAG —
generate one in the dashboard or via `db.create_api_key`):

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/dev/projects \
    -H "Authorization: Bearer $FAG_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"name":"FixarCRM","repo_path":"/Users/me/code/fixar-crm"}'
```

Look up the project's cognee bearer:

```bash
sqlite3 data/agent.db \
    "SELECT cognee_token FROM dev_projects WHERE name='FixarCRM';"
```

Paste that JWT into `COGNEE_MCP_API_TOKEN` in `infra/cognee/.env`. The
agent will now only see `dev_<id>` data — `personal` and other dev
projects return 404 by ACL.

## Start cognee-mcp

```bash
make cognee-mcp-start
make cognee-mcp-logs   # in another terminal
```

A successful start prints `ready at http://127.0.0.1:8766/mcp`.

## Codex configuration

Codex's MCP config lives at `~/.codex/config.toml` (or per-workspace
`.codex/config.toml`). Add:

```toml
[mcp_servers.fag-memory]
type = "http"
url = "http://127.0.0.1:8766/mcp"
```

Codex authenticates implicitly: cognee-mcp owns the JWT, so the Codex
process needs no token of its own.

## Claude Code / Claude Desktop configuration

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

Or via the Claude CLI:

```bash
claude mcp add fag-memory -t http http://127.0.0.1:8766/mcp
claude mcp list
```

## Tools the agent gets

Listed by `mcp.list_tools()`:

| Tool | Purpose |
|---|---|
| `remember` | session-aware write (V2 API) |
| `recall` | session-aware read (V2 API, recommended) |
| `forget_memory` | delete by dataset |
| `improve` | enrich the graph |
| `cognify` | V1 write (background pipeline) |
| `search` | V1 read (multiple search types) |
| `save_interaction` | log Q/A pairs |
| `delete` / `list_data` / `delete_dataset` | data management |
| `prune` | wipe everything (only in direct mode, not API mode) |
| `cognify_status` / `codify_status` | pipeline state (only in direct mode) |

Recommended: `recall` for read, `remember` for write. The V1 tools work
but skew toward direct-mode features.

## Troubleshooting

**`Recall failed: 401 Unauthorized`** — the JWT in `COGNEE_MCP_API_TOKEN`
expired (default JWT lifetime is short). Re-run the login curl from
Scope A and restart `make cognee-mcp-stop && make cognee-mcp-start`.

**`Cognify failed`** in direct mode — the sidecar is unhealthy or the
LLM call ran out of budget. Check `infra/cognee/logs/cognee.log`.

**Recall returns "knowledge graph context is empty"** — write succeeded
but cognify hasn't finished. With cognee-mcp's API mode, cognify runs
in background and there's no status tool to poll; wait 10–30 seconds
and retry, or watch the sidecar log for `Pipeline run completed`.

**Two agents on the same project** — give them the same token. The
session_id field on remember/recall lets them share working state.

**Switching projects** — overwrite `COGNEE_MCP_API_TOKEN` and
`make cognee-mcp-stop && make cognee-mcp-start`. cognee-mcp does not
support multi-tenant per request; one process serves one user/scope.
