.PHONY: install dev test lint format clean \
	cognee-install cognee-start cognee-stop cognee-logs cognee-status cognee-spike2 \
	cognee-mcp-start cognee-mcp-stop cognee-mcp-logs \
	reindex reindex-notes docs-wiki wiki-build wiki-clean notes-decrypt \
	memory-doctor memory-doctor-fix notes-reenrich \
	cognee-cognify cognee-cognify-foreground

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

# Trigger cognify on main_dataset (graph + vectors). Background by default.
cognee-cognify:
	.venv/bin/python scripts/cognee_cognify.py

# Same but block until done — useful for verification scripts.
cognee-cognify-foreground:
	.venv/bin/python scripts/cognee_cognify.py --foreground

# ── Maintenance ────────────────────────────────────────────────────────────
# Drop Qdrant collection and re-parse + re-classify + re-embed every file
# in SQLite. Run after major prompt or extraction-schema changes.
reindex:
	.venv/bin/python scripts/reindex_all.py

# Refresh the Obsidian-friendly per-document wiki under
# ~/ai-agent-files/docs/ (one .md per file + _DOCS.md index).
# Idempotent — safe to rerun after every reindex or new ingest.
docs-wiki:
	.venv/bin/python scripts/build_docs_wiki.py

# Re-embed every note into Qdrant (atomic chunks). Run after a notes
# decrypt or schema change.
reindex-notes:
	.venv/bin/python scripts/reindex_notes.py

# Decrypt any FAGE-encrypted residue in notes-related tables. Idempotent.
notes-decrypt:
	.venv/bin/python scripts/decrypt_legacy_notes.py --via-session-key

# Build the LLM-wiki vault (Karpathy pattern) under wiki.base_path —
# default ~/ai-agent-files/wiki/. One markdown page per document, per
# transcript, and per auto-extracted entity, with backlinks. Pure
# autogen — manual edits go under <vault>/manual/ instead.
# `wiki-build`     — LLM-driven entity extraction via proxy (Sprint G)
# `wiki-build-fast`— regex fallback, no LLM cost
wiki-build:
	.venv/bin/python scripts/build_wiki.py --use-llm

wiki-build-fast:
	.venv/bin/python scripts/build_wiki.py

# Wipe the autogen subdirs of the wiki vault (keeps manual/, raw/).
wiki-clean:
	@VAULT="$$(.venv/bin/python -c 'from app.config import get_settings; print(get_settings().wiki.resolved_path)')"; \
	echo "wiping $$VAULT/{docs,notes,entities,index.md,log.md,CLAUDE.md}"; \
	rm -rf "$$VAULT/docs" "$$VAULT/notes" "$$VAULT/entities" \
	       "$$VAULT/index.md" "$$VAULT/log.md" "$$VAULT/CLAUDE.md"

# Memory reconciliation — walks SQLite, Qdrant, cognee, wiki and
# prints divergence between the four. Run with --fix to enqueue
# outbox events that bring them back in sync.
memory-doctor:
	.venv/bin/python scripts/memory_doctor.py

memory-doctor-fix:
	.venv/bin/python scripts/memory_doctor.py --fix

# Wipe + redo every note enrichment (mood/energy/sentiment/tags).
# Run after a notes decrypt or a major prompt change.
notes-reenrich:
	.venv/bin/python scripts/redo_note_enrichments.py
