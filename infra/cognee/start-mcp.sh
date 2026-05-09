#!/usr/bin/env bash
# Start cognee-mcp in API mode, proxying to the cognee sidecar at $HTTP_API_PORT.
#
# Reads the per-project (or default) bearer token from infra/cognee/.env via
# COGNEE_MCP_API_TOKEN. Codex / Claude Code etc. talk to this server, NOT the
# raw cognee FastAPI server, so we keep their access scoped to whatever user
# the token belongs to.
#
# Pid: infra/cognee/cognee-mcp.pid. Log: infra/cognee/logs/cognee-mcp.log.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra/cognee"
VENV_DIR="${ROOT_DIR}/.venv-cognee"
PID_FILE="${INFRA_DIR}/cognee-mcp.pid"
LOG_FILE="${INFRA_DIR}/logs/cognee-mcp.log"
ENV_FILE="${INFRA_DIR}/.env"

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "ERROR: ${VENV_DIR} missing. Run 'make cognee-install' first." >&2
    exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} missing." >&2
    exit 1
fi
if [[ ! -x "${VENV_DIR}/bin/cognee-mcp" ]]; then
    echo "ERROR: cognee-mcp not installed in ${VENV_DIR}. Run 'make cognee-install'." >&2
    exit 1
fi

# Reuse the sidecar pid file's slot if stale.
if [[ -f "${PID_FILE}" ]]; then
    OLD_PID="$(cat "${PID_FILE}")"
    if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "[cognee-mcp-start] already running (pid ${OLD_PID})"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

mkdir -p "$(dirname "${LOG_FILE}")"

# Load .env for COGNEE_MCP_*. Direct source (process substitution does not
# propagate exports reliably in bash 5+).
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

API_URL="${COGNEE_MCP_API_URL:-http://${HTTP_API_HOST:-127.0.0.1}:${HTTP_API_PORT:-8765}}"
MCP_HOST="${COGNEE_MCP_HOST:-127.0.0.1}"
MCP_PORT="${COGNEE_MCP_PORT:-8766}"
MCP_PATH="${COGNEE_MCP_PATH:-/mcp}"
# Codex's rmcp client and FastMCP's streamable HTTP transport have a
# protocol mismatch — tool calls arrive at the server but the response
# never makes it back. SSE works for both Codex and Claude Code.
MCP_TRANSPORT="${COGNEE_MCP_TRANSPORT:-sse}"

if [[ -z "${COGNEE_MCP_API_TOKEN:-}" ]]; then
    echo "WARNING: COGNEE_MCP_API_TOKEN is empty. The MCP server will not be able"
    echo "         to authenticate against the cognee sidecar. Set it to a JWT"
    echo "         (default user, or a per-project dev_<id> user)."
fi

echo "[cognee-mcp-start] proxying to ${API_URL}"
echo "[cognee-mcp-start] transport: ${MCP_TRANSPORT}"
echo "[cognee-mcp-start] listening on http://${MCP_HOST}:${MCP_PORT}${MCP_PATH}"
echo "[cognee-mcp-start] log: ${LOG_FILE}"

cd "${INFRA_DIR}"
# SSE transport ignores --path; URL clients use is /sse.
START_ARGS=(
    --transport "${MCP_TRANSPORT}"
    --host "${MCP_HOST}"
    --port "${MCP_PORT}"
    --api-url "${API_URL}"
    --api-token "${COGNEE_MCP_API_TOKEN:-}"
    --no-migration
)
if [[ "${MCP_TRANSPORT}" == "http" ]]; then
    START_ARGS+=(--path "${MCP_PATH}")
fi

nohup "${VENV_DIR}/bin/cognee-mcp" "${START_ARGS[@]}" \
    >>"${LOG_FILE}" 2>&1 &

NEW_PID=$!
echo "${NEW_PID}" >"${PID_FILE}"
echo "[cognee-mcp-start] pid ${NEW_PID}"

PROBE_PATH="${MCP_PATH}"
[[ "${MCP_TRANSPORT}" == "sse" ]] && PROBE_PATH="/sse"
for _ in $(seq 1 30); do
    if curl -sS -m 1 "http://${MCP_HOST}:${MCP_PORT}${PROBE_PATH}" -H 'accept: text/event-stream' --max-time 1 >/dev/null 2>&1; then
        echo "[cognee-mcp-start] ready at http://${MCP_HOST}:${MCP_PORT}${PROBE_PATH}"
        exit 0
    fi
    sleep 1
done
echo "[cognee-mcp-start] WARNING: not responding after 30s. Check ${LOG_FILE}." >&2
exit 1
