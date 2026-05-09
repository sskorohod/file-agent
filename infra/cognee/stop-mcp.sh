#!/usr/bin/env bash
# Stop cognee-mcp identified by infra/cognee/cognee-mcp.pid.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PID_FILE="${ROOT_DIR}/infra/cognee/cognee-mcp.pid"

if [[ ! -f "${PID_FILE}" ]]; then
    echo "[cognee-mcp-stop] no pid file, nothing to stop"
    exit 0
fi

PID="$(cat "${PID_FILE}")"
if ! kill -0 "${PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    echo "[cognee-mcp-stop] pid ${PID} not running"
    exit 0
fi

echo "[cognee-mcp-stop] sending SIGTERM to ${PID}"
kill -TERM "${PID}"

for _ in $(seq 1 15); do
    if ! kill -0 "${PID}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        echo "[cognee-mcp-stop] stopped"
        exit 0
    fi
    sleep 1
done

kill -KILL "${PID}" 2>/dev/null || true
rm -f "${PID_FILE}"
echo "[cognee-mcp-stop] forced"
