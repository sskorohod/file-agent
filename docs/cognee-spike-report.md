# Cognee Spike Report (Phase 0)

**Branch:** `spike/cognee-compat`
**Date:** 2026-05-07
**Goal:** confirm whether Cognee 1.0.x can be integrated into FAG and how (embedded library vs sidecar service).

Russian version: [cognee-spike-report.ru.md](cognee-spike-report.ru.md).

## TL;DR

✅ **Decision: Cognee runs as a sidecar HTTP service in its own venv.**

Embedded integration is not viable: cognee 1.0.8 transitively requires `starlette>=1.0.0`, while FAG pins `starlette<1.0.0` (Python 3.14 + Jinja2 LRU cache regression, see CLAUDE.md). Sidecar isolates ~110 transitive deps from the main FAG venv and uses cognee's built-in production-ready FastAPI server.

## Environment

- macOS Darwin 25.3.0 (arm64)
- Python 3.14.3 (from `~/fag/.venv`)
- Isolated venv: `.venv-spike/` (created in worktree)
- Qdrant: SSH tunnel on `127.0.0.1:6333` (active)

## Cognee install

`pip install cognee` in `.venv-spike` resolved to **cognee 1.0.8** with ~110 transitive packages. Notable:

| Package | Version installed | FAG main pin | Conflict |
|---|---|---|---|
| **starlette** | **1.0.0** | **<1.0.0** | 🔴 hard |
| litellm | 1.83.7 | 1.82.6 | minor (not in TeamPCP block list) |
| fastapi | 0.136.1 | 0.135.2 | minor |
| pydantic | 2.12.5 | 2.x | none |
| numpy | 2.4.4 | (transitive) | possible (PyMuPDF/Tesseract) |
| New deps pulled | lancedb 0.30.2, networkx 3.6.1, sqlalchemy 2.0.49, redis 7.4.0, alembic 1.18.4, fastapi-users 15.0.5, instructor 1.15.1, fakeredis 2.35.1, ladybug 0.16.0, pylance 0.36.0, langdetect 1.0.9, datamodel-code-generator 0.57.0, cbor2 6.0.1, ... (~30 new top-levels) | — | — |

`import cognee` works on Python 3.14.3.

## Cognee runtime warnings (on import)

```
Cognee 1.0 changes: New API — remember/recall/forget/improve
  (V1 add/cognify/search still work).
Session memory enabled by default (CACHING=false to disable).
Multi-user access control on by default
  (ENABLE_BACKEND_ACCESS_CONTROL=false to disable).
```

Default state path: `<site-packages>/cognee/.cognee_system/databases` — **must be overridden** via `COGNEE_SYSTEM_ROOT_DIRECTORY` (state inside site-packages is lost on package upgrade).

## FastAPI server (the sidecar candidate)

Cognee ships `cognee.api.client:app` — a real FastAPI application with 73 endpoints registered.

Started with:

```bash
HTTP_API_HOST=127.0.0.1 \
HTTP_API_PORT=8765 \
ENABLE_BACKEND_ACCESS_CONTROL=false \
CACHING=false \
.venv-spike/bin/python -m cognee.api.client
```

Probes:
- `GET /` → 200 `{"message":"Hello, World, I am alive!"}`
- `GET /docs` → Swagger UI rendered
- `GET /openapi.json` → 73 paths

Key endpoint groups (path prefixes seen via OpenAPI):

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
/api/v1/permissions/...   (multi-tenant ACL — disabled in single-user mode)
/api/v1/notebooks/...
/api/v1/sessions/...
```

The CLI `cognee-cli` accepts `--api-url http://localhost:8765` to delegate against a running server, which is the canonical client → sidecar pattern.

## What was NOT verified in this spike

- **End-to-end** `add → cognify → search` against real Anthropic/Gemini/Qdrant. This requires API keys, costs LLM tokens, and writes a Qdrant collection. Deferred to **spike-2 in Phase 1** (driven from `scripts/spike2_cognee_e2e.py` against the running sidecar).
- Verification that cognee actually consumes `GEMINI_API_KEY` for embeddings (the env var name and supported model id need to be confirmed against cognee's config; fallback is OpenAI `text-embedding-3-small` 1536-dim). Deferred to spike-2.
- Latency of `cognify` on real text sizes (1 KB, 10 KB, 100 KB). Deferred to spike-2.
- cognee-mcp installation + API-mode wiring. Deferred to **Phase 6**.

## Architectural decisions locked in by this spike

1. Cognee runs in a separate process from FAG, in `.venv-cognee/`, on `127.0.0.1:8765`.
2. FAG main venv stays untouched — no `cognee` import, no transitive starlette upgrade.
3. `app/memory/cognee_client.py` will be an `httpx.AsyncClient`-based wrapper, not a Python wrapper around the cognee library.
4. Single-user posture: `ENABLE_BACKEND_ACCESS_CONTROL=false`, `CACHING=false`, loopback bind only.
5. Cognee owns its own Qdrant collection and its own embedding model dim — no attempt to share `file_agent_v2`.
6. State directory is overridden via `COGNEE_SYSTEM_ROOT_DIRECTORY` to `infra/cognee/data/`.

## Cleanup

`.venv-spike/` is added to `.gitignore` and stays on disk for spike-2 iteration. It will be removed when Phase 1 lands `infra/cognee/setup.sh` which provisions the canonical `.venv-cognee/`.
