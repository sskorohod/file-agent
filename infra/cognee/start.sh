#!/usr/bin/env bash
# Start the cognee sidecar (FastAPI server, cognee.api.client:app).
#
# Reads infra/cognee/.env, writes pid to infra/cognee/cognee.pid,
# logs to infra/cognee/logs/cognee.log. Skips if already running.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra/cognee"
VENV_DIR="${ROOT_DIR}/.venv-cognee"
PID_FILE="${INFRA_DIR}/cognee.pid"
LOG_FILE="${INFRA_DIR}/logs/cognee.log"
ENV_FILE="${INFRA_DIR}/.env"

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "ERROR: ${VENV_DIR} missing. Run 'make cognee-install' first." >&2
    exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} missing. Run 'make cognee-install' to seed it." >&2
    exit 1
fi

# If pid is stale or alive, decide what to do.
if [[ -f "${PID_FILE}" ]]; then
    OLD_PID="$(cat "${PID_FILE}")"
    if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "[cognee-start] already running (pid ${OLD_PID})"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

mkdir -p "$(dirname "${LOG_FILE}")"

# Load .env (export every assignment to children). Process substitution
# (source <(grep ...)) does not propagate exports reliably in bash 5+, so
# we source the file directly with `set -a` around it.
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

# Default state directory — keep cognee state INSIDE the repo (gitignored),
# not inside site-packages.
export COGNEE_SYSTEM_ROOT_DIRECTORY="${COGNEE_SYSTEM_ROOT_DIRECTORY:-${INFRA_DIR}/data}"

echo "[cognee-start] launching cognee.api.client on ${HTTP_API_HOST:-127.0.0.1}:${HTTP_API_PORT:-8765}"
echo "[cognee-start] state dir: ${COGNEE_SYSTEM_ROOT_DIRECTORY}"
echo "[cognee-start] log: ${LOG_FILE}"

cd "${INFRA_DIR}"
nohup "${VENV_DIR}/bin/python" -m cognee.api.client \
    >>"${LOG_FILE}" 2>&1 &

NEW_PID=$!
echo "${NEW_PID}" >"${PID_FILE}"
echo "[cognee-start] pid ${NEW_PID}"

# Brief readiness probe.
HOST="${HTTP_API_HOST:-127.0.0.1}"
PORT="${HTTP_API_PORT:-8765}"
for _ in $(seq 1 30); do
    if curl -sS -m 1 "http://${HOST}:${PORT}/" >/dev/null 2>&1; then
        echo "[cognee-start] ready at http://${HOST}:${PORT}/"
        exit 0
    fi
    sleep 1
done
echo "[cognee-start] WARNING: not responding after 30s. Check ${LOG_FILE}." >&2
exit 1
