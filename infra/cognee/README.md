# Cognee Sidecar

Cognee 1.0 runs as a separate process from FAG. The main FAG venv stays clean; cognee
gets its own `.venv-cognee/` and writes state to `infra/cognee/data/`.

See `docs/cognee-spike-report.md` for why this is a sidecar and not an embedded library.

## Quick start

```bash
make cognee-install     # creates .venv-cognee, seeds infra/cognee/.env
$EDITOR infra/cognee/.env  # set ANTHROPIC_API_KEY, GOOGLE_API_KEY, VECTOR_DB_KEY
make cognee-start       # launches FastAPI on 127.0.0.1:8765
make cognee-logs        # tails infra/cognee/logs/cognee.log
make cognee-stop        # SIGTERM the pid in infra/cognee/cognee.pid
```

Smoke check: `curl http://127.0.0.1:8765/` should return
`{"message":"Hello, World, I am alive!"}`. Swagger UI at `/docs`.

## What lives where

| Path | Tracked in git | Purpose |
|---|---|---|
| `infra/cognee/setup.sh` | yes | provisions `.venv-cognee/`, seeds `.env` |
| `infra/cognee/start.sh` | yes | starts cognee.api.client, writes pid + log |
| `infra/cognee/stop.sh` | yes | stops via pid file |
| `infra/cognee/.env.example` | yes | env template (no secrets) |
| `infra/cognee/.env` | NO (gitignored) | real secrets |
| `infra/cognee/data/` | NO | cognee state (SQLite, graph dbs, file cache) |
| `infra/cognee/logs/` | NO | cognee server log |
| `infra/cognee/cognee.pid` | NO | running pid |
| `.venv-cognee/` | NO | sidecar venv (not the main `.venv`) |

## Troubleshooting

**Sidecar not responding.** Check `infra/cognee/logs/cognee.log`. The most common issue
is missing/invalid `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY` in `infra/cognee/.env`.

**Port 8765 already in use.** Change `HTTP_API_PORT` in `infra/cognee/.env` AND the
matching `cognee.base_url` in FAG `config.yaml`.

**Stale pid file after a crash.** `rm infra/cognee/cognee.pid` then `make cognee-start`.

**Embedding/LLM env var names don't match what cognee actually reads.** This is the
known unknown — `.env.example` reflects intent, not verified contract. Spike-2
(Phase 1, see plan) confirms exact var names against `cognee.api.client`.
