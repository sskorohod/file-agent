#!/usr/bin/env bash
# Provision the cognee sidecar venv (.venv-cognee) and seed its .env file.
#
# Idempotent: re-running upgrades cognee but never overwrites infra/cognee/.env.
# The sidecar runs as a separate process from FAG (see docs/cognee-spike-report.md
# for why embedded integration is blocked).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA_DIR="${ROOT_DIR}/infra/cognee"
VENV_DIR="${ROOT_DIR}/.venv-cognee"
COGNEE_VERSION="${COGNEE_VERSION:-1.0.8}"

# Find a usable Python — prefer the same interpreter that runs FAG.
PYTHON_BIN="${COGNEE_PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
        PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3.11 || true)"
    fi
fi
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: no Python 3.11+ found. Set COGNEE_PYTHON=/path/to/python and retry." >&2
    exit 1
fi

echo "[cognee-setup] using ${PYTHON_BIN} ($("${PYTHON_BIN}" --version))"

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[cognee-setup] creating ${VENV_DIR}"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
echo "[cognee-setup] installing cognee==${COGNEE_VERSION} (this may take a few minutes)"
"${VENV_DIR}/bin/pip" install --quiet "cognee==${COGNEE_VERSION}"

# Cognee uses the Anthropic SDK directly via instructor (not litellm) when
# LLM_PROVIDER=anthropic, so the SDK must be installed alongside cognee.
echo "[cognee-setup] installing anthropic SDK (used when LLM_PROVIDER=anthropic)"
"${VENV_DIR}/bin/pip" install --quiet anthropic

# Phase 6: cognee-mcp lets external agents (Codex / Claude Code / ChatGPT)
# talk to the sidecar via the Model Context Protocol. We install with
# --no-deps to avoid the postgres-binary build chain (psycopg2-binary
# does not have wheels for Python 3.14 on arm64) and pull in only what
# cognee-mcp actually needs at runtime.
echo "[cognee-setup] installing cognee-mcp (--no-deps) + fastmcp"
"${VENV_DIR}/bin/pip" install --quiet --no-deps cognee-mcp
"${VENV_DIR}/bin/pip" install --quiet "fastmcp>=3.2.0" "mcp>=1.12.0"

mkdir -p "${INFRA_DIR}/data"
mkdir -p "${INFRA_DIR}/logs"

if [[ ! -f "${INFRA_DIR}/.env" ]]; then
    cp "${INFRA_DIR}/.env.example" "${INFRA_DIR}/.env"
    echo "[cognee-setup] created ${INFRA_DIR}/.env from .env.example"
    echo "[cognee-setup] EDIT IT — set ANTHROPIC_API_KEY / GOOGLE_API_KEY / VECTOR_DB_KEY"
else
    echo "[cognee-setup] ${INFRA_DIR}/.env already exists, leaving as-is"
fi

INSTALLED_VERSION="$("${VENV_DIR}/bin/pip" show cognee 2>/dev/null | awk '/^Version:/ {print $2}')"
echo "[cognee-setup] done. cognee ${INSTALLED_VERSION} ready in ${VENV_DIR}"
echo "[cognee-setup] next: edit infra/cognee/.env, then 'make cognee-start'"
