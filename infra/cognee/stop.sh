#!/usr/bin/env bash
# Stop the cognee sidecar identified by infra/cognee/cognee.pid.
# No-op if the pid file is missing or the process has already exited.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra/cognee"
PID_FILE="${INFRA_DIR}/cognee.pid"

if [[ ! -f "${PID_FILE}" ]]; then
    echo "[cognee-stop] no pid file, nothing to stop"
    exit 0
fi

PID="$(cat "${PID_FILE}")"
if ! kill -0 "${PID}" 2>/dev/null; then
    echo "[cognee-stop] pid ${PID} not running, removing stale pid file"
    rm -f "${PID_FILE}"
    exit 0
fi

echo "[cognee-stop] sending SIGTERM to ${PID}"
kill -TERM "${PID}"

for _ in $(seq 1 15); do
    if ! kill -0 "${PID}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        echo "[cognee-stop] stopped"
        exit 0
    fi
    sleep 1
done

echo "[cognee-stop] still alive after 15s, sending SIGKILL"
kill -KILL "${PID}" 2>/dev/null || true
rm -f "${PID_FILE}"
