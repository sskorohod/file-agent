# Connecting Codex / Claude Code / ChatGPT to FAG memory

This is the operator's path from "FAG is running" to "Codex can read and write
the memory graph". It assumes Phases 0–6 of the cognee migration are done.

Russian version: [codex-claude-setup.ru.md](codex-claude-setup.ru.md).

## Architecture (recap)

```
Codex CLI / IDE       Claude Code / Desktop      Other MCP clients
       |                        |                        |
       | stdio                  | stdio or HTTP/SSE      |
       v                        v                        v
              cognee-mcp child process(es)
                          |
                          | HTTP REST (API mode)
                          | Bearer = cognee user JWT
                          v
              cognee FastAPI sidecar (:8765)
                          |
                          v
              local SQLite + LanceDB graph
```

Codex spawns its own `cognee-mcp` child via stdio. The Codex CLI's HTTP
MCP transport (`streamable_http`) is **not compatible** with FastMCP's
streamable HTTP server in cognee-mcp 0.5.4 (the request lands but the
response never makes it back). Stdio is the only working path.

Claude Code is happier with HTTP/SSE — it's documented and works against
`make cognee-mcp-start` on `:8766`.

## One-time setup

```bash
make cognee-install           # creates .venv-cognee, .env, installs cognee-mcp
$EDITOR infra/cognee/.env     # fill in LLM_API_KEY, OPENAI_API_KEY, etc.
make cognee-start             # FAG-facing API on :8765
```

## Default scope: `default_user` + `main_dataset`

`default_user@example.com / default_password` is created automatically by
the sidecar at startup as a superuser — convenient for the single-user
FAG owner. The default cognee dataset is `main_dataset`, which is also
the default in cognee-mcp's `remember` tool, so writes from Codex land
in the same scope FAG reads from.

Mint a JWT for this user:

```bash
TOKEN=$(curl -sS -X POST \
    -F username=default_user@example.com \
    -F password=default_password \
    http://127.0.0.1:8765/api/v1/auth/login | jq -r .access_token)
```

## Codex (stdio — recommended)

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.fag-memory]
command = "/absolute/path/to/.venv-cognee/bin/cognee-mcp"
args = [
    "--api-url", "http://127.0.0.1:8765",
    "--api-token", "<JWT from step above>",
    "--no-migration",
]
```

Restart Codex. Verify with:

```bash
codex mcp get fag-memory   # transport: stdio
codex mcp list             # fag-memory should be enabled
```

In a Codex chat, `recall("...")` should now be a callable tool. In a
sandboxed `codex exec` invocation pass `--dangerously-bypass-approvals-and-sandbox`
or `--full-auto` so the auto-approval prompt doesn't deny the tool call.

It also helps to put a hint in `~/.codex/AGENTS.md` so Codex knows when
to reach for the tool — see this repo's `~/.codex/AGENTS.md` for a
working example.

## Claude Code / Claude Desktop (HTTP — works with cognee-mcp's HTTP server)

```bash
make cognee-mcp-start         # spawns cognee-mcp on :8766 in API mode
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

Or with the Claude CLI:

```bash
claude mcp add fag-memory -t http http://127.0.0.1:8766/mcp
claude mcp list
```

## Per-project dev scope (isolated)

Register a dev project — needs a `mode=full` FAG API key:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/dev/projects \
    -H "Authorization: Bearer $FAG_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"name":"FixarCRM","repo_path":"/Users/me/code/fixar-crm"}'
```

Look up the project's cognee JWT:

```bash
sqlite3 data/agent.db \
    "SELECT cognee_token FROM dev_projects WHERE name='FixarCRM';"
```

Swap this JWT into the `--api-token` arg of your Codex config (or replace
`COGNEE_MCP_API_TOKEN` in `infra/cognee/.env` if you're using HTTP mode for
Claude). Restart the MCP entry. The agent now only sees `dev_<id>` data;
`main_dataset` and other dev projects return HTTP 404 by ACL.

## Tools the agent gets

| Tool | Purpose |
|---|---|
| `remember` | durable write (without `session_id`) — runs add+cognify |
| `recall` | durable read |
| `forget_memory` | delete by dataset; never pass `everything=true` |
| `improve` | enrich the graph |
| `cognify` | V1 write (background pipeline) |
| `search` | V1 read (CHUNKS, SUMMARIES, GRAPH_COMPLETION, …) |
| `save_interaction` | log Q/A pairs |
| `delete` / `list_data` / `delete_dataset` | data management |
| `prune` | wipe everything (direct mode only, not API mode) |
| `cognify_status` / `codify_status` | pipeline state (direct mode only) |

Recommended: `recall` for read, `remember` for write. V1 tools are
fallbacks if you need raw chunk search instead of graph completion.

## Troubleshooting

**`mcp: fag-memory/recall (failed) — user cancelled MCP tool call`** in
`codex exec` — Codex's auto-approval system denied the tool call. Add
`--dangerously-bypass-approvals-and-sandbox` (or `--full-auto`) for
non-interactive runs. In an interactive session, click Allow.

**`Recall failed: 401 Unauthorized`** — the JWT expired. Re-mint via
the login curl above and update `--api-token` in your Codex config.
Restart Codex.

**Recall returns "knowledge graph context is empty"** — `cognify` is
still in flight (it takes 5–30 seconds to extract entities and update
the graph). Wait, then retry. Watch `infra/cognee/logs/cognee.log` for
`Pipeline run completed`.

**Codex set transport=streamable_http for an HTTP URL and tool calls
silently fail** — switch to stdio (see Codex section). Codex's rmcp
client and FastMCP's streamable HTTP server have a protocol mismatch.

**Switching scopes** — change the JWT in your Codex config and restart
Codex. cognee-mcp does not support multi-tenant per request; one
process serves one user/scope.
