.PHONY: install dev test lint format clean \
	cognee-install cognee-start cognee-stop cognee-logs cognee-status cognee-spike2 \
	cognee-mcp-start cognee-mcp-stop cognee-mcp-logs

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest tests/ -v --tb=short

lint:
	ruff check app/ tests/
	ruff format --check app/ tests/

format:
	ruff check --fix app/ tests/
	ruff format app/ tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache

# ── Cognee sidecar (see infra/cognee/README.md) ────────────────────────────
cognee-install:
	bash infra/cognee/setup.sh

cognee-start:
	bash infra/cognee/start.sh

cognee-stop:
	bash infra/cognee/stop.sh

cognee-logs:
	tail -f infra/cognee/logs/cognee.log

cognee-status:
	@if [ -f infra/cognee/cognee.pid ] && kill -0 $$(cat infra/cognee/cognee.pid) 2>/dev/null; then \
		echo "running (pid $$(cat infra/cognee/cognee.pid))"; \
		curl -sS http://127.0.0.1:8765/ || true; echo; \
	else \
		echo "not running"; \
	fi

cognee-spike2:
	@# Runs against a live sidecar via HTTP — no cognee import.
	@# Use the same Python that runs FAG so httpx is available.
	python3 scripts/spike2_cognee_e2e.py

# ── cognee-mcp (Phase 6: external agents — Codex, Claude, ChatGPT) ─────────
cognee-mcp-start:
	bash infra/cognee/start-mcp.sh

cognee-mcp-stop:
	bash infra/cognee/stop-mcp.sh

cognee-mcp-logs:
	tail -f infra/cognee/logs/cognee-mcp.log
