# Memory system map — 11 stations

A navigable reference for the memory architecture. Each station is a
discrete decision area we can discuss, refine, and commit independently.
Use this as a checklist while iterating.

Companion docs:

- [architecture-audit-2026-05-08.md](architecture-audit-2026-05-08.md) — known problems, by severity
- [codex-claude-setup.md](codex-claude-setup.md) — operator path
- [cognee-spike-report.md](cognee-spike-report.md) — Phase 0 architecture decision
- [cognee-spike2-report.md](cognee-spike2-report.md) — Phase 1 e2e + lessons

---

## 1. Sources of memory (what creates entries)

**Currently working:** Telegram documents, Telegram voice notes
("save as note" branch), Telegram chat (long user messages),
web/HTTP upload, Codex/Claude via MCP, dev-project repos via
`POST /api/v1/dev/projects/{id}/ingest_repo`.

**Open decisions:**

- Which sources should auto-save vs require an explicit trigger?
- Voice "search" branch — currently NOT saved. Should it be?
- Assistant turns in chat — currently NOT saved. Worth keeping?
- Future sources: email, calendar, Slack, screenshots?

**Code:** [bot/handlers.py](../app/bot/handlers.py),
[api/routes.py](../app/api/routes.py),
[pipeline.py](../app/pipeline.py)

---

## 2. Pre-processing (before a fact reaches cognee)

**Currently working:** files → parse (PyMuPDF/Tesseract/Vision)
→ classify LLM → skill extract; voice → Whisper transcribe → LLM
extract title/tags; chat → length filter ≥40 chars; Codex — no
preprocessing.

**Open decisions:**

- Should an LLM judge gate "is this worth remembering at all?"
  before cognee (Mem0-style)?
- Skill-extracted fields (priority, expiry_date, amount) — should
  they ride along structurally to cognee, not just stay in SQLite?
- Should Codex inputs be normalized (paraphrase, date parsing)?

**Code:** [parser/factory.py](../app/parser/factory.py),
[llm/classifier.py](../app/llm/classifier.py),
[pipeline.py:_step_extract](../app/pipeline.py)

---

## 3. Metadata that travels with the fact

**Currently:** only `filename`. Cognee knows just the upload's name —
no link to FAG `file_id`, no source type, no date, no priority.

**Open decisions (richer metadata = richer queries later):**

- `file_id` (FAG UUID) — required for navigating back to the
  original. **Critical.**
- `source_type` (file / note / chat / codex_remember / dev_repo) —
  for recall filtering.
- `memory_type` (fact / preference / rule / decision / event /
  task) — to query "show me all my rules".
- `authority_level` (personal_thought / draft / approved /
  official_doc) — distinguishes "I think X" from "the contract
  says X".
- `valid_from` / `expires_at` — temporal awareness.
- `tags` (we already compute these for files) — propagate to
  cognee.

**Code:** [memory/cognee_client.py:add](../app/memory/cognee_client.py),
[pipeline.py:_step_cognee_ingest](../app/pipeline.py)

---

## 4. Storage (where things physically live)

**Currently:**

- Raw (source of truth): FAG SQLite [data/agent.db](../data/agent.db)
  — `files`, `notes`, `chat_history` + disk `~/ai-agent-files/`.
- Derived (for retrieval): cognee SQLite + lancedb + NetworkX
  graph in `infra/cognee/data/`.
- **Duplicate:** Qdrant `file_agent_v2` (Gemini embeddings) —
  written but barely read.

**Open decisions:**

- Drop Qdrant `file_agent_v2` entirely? (Sprint 1, audit C3)
- Where should voice-note Obsidian markdowns live — keep
  duplicating, or pick one canonical store?
- Retention policy for cognee state? (audit Q6)

**Code:** [storage/db.py](../app/storage/db.py),
[storage/vectors.py](../app/storage/vectors.py),
[storage/files.py](../app/storage/files.py)

---

## 5. Cognify (how cognee builds the graph)

**Currently:** every `add` is followed by an inline `cognify`.
Cognee internally calls its own LLM (Anthropic Claude Sonnet),
extracts entities + relations, writes to lancedb (vectors) +
NetworkX (graph).

**Open decisions:**

- Inline cognify (current, +10–30s blocking) vs background task
  (fast user reply, "memory available in a minute")? (Sprint 2,
  audit S1)
- Batched cognify (every N seconds, drain a queue) vs per-document?
- Custom cognify prompt tuned to our document categories?

**Code:** [memory/cognee_client.py:cognify](../app/memory/cognee_client.py)

---

## 6. Conflict / supersession (what to do with contradictions)

**Currently:** nothing. "I use VS Code" + "I switched to Cursor" —
both stay as facts. Recall returns whichever the graph likes.

**Open decisions:**

- Before each `add`, search similar via `recall(content)` — yes/no?
- When a similar one is found — who decides "duplicate / supersede
  / contradict": rules, or an LLM judge?
- On supersede, what happens to the old fact — delete, mark, keep
  with a `superseded_by` relation?
- Are "stale" facts visible in normal recall, or only in an
  analytics view?

**Code:** not written yet. Will live in
`memory/cognee_client.py:insert_with_conflict_check` (Sprint 3,
audit S5).

---

## 7. Temporal (lifetime of a fact)

**Currently:** nothing. "Vanya is driving home from Las Vegas"
will remain true forever.

**Open decisions:**

- Who sets `expires_at` — the user explicitly, an LLM parser
  reading "сегодня"/"до пятницы", or rules per type (event =
  7 days, decision = forever)?
- Expired facts — hide in recall or surface with a stale flag?
- Need a "what expired this week, anything to refresh?" query?

**Code:** not written yet. Sprint 3, audit S6.

---

## 8. Retrieval / search (how things come back out)

**Currently:**

- Telegram / web `/search` → cognee.recall (graph_completion) —
  but **lost our custom `search_prompt`** along the way.
- Codex / Claude via MCP → cognee.recall.
- HTTP API `/api/v1/search` → cognee (full mode) or Qdrant (lite mode).

**Open decisions:**

- Restore FAG's search_prompt (Sprint 2, audit S2): pull CHUNKS
  from cognee, run them through our own LLM with the FAG prompt.
- Default `searchType` (GRAPH_COMPLETION / RAG_COMPLETION /
  CHUNKS / SUMMARIES)?
- Recall must return `file_id` so Telegram can render inline file
  buttons again (audit S7).
- Need an "advanced search" with filters by `memory_type`,
  `source_type`, dataset?

**Code:** [llm/search.py](../app/llm/search.py),
[api/routes.py](../app/api/routes.py)

---

## 9. Access scopes (who sees what)

**Currently:**

- `main_dataset` — personal scope. Owned by `default_user`, who is
  a **superuser** — sees everything, including dev projects.
- `dev_<id>` — per-project, separate non-superuser cognee user,
  isolated by ACL.
- `personal` — old dataset, no longer used, not deleted.

**Open decisions:**

- Create a non-superuser `fag_personal` to own main_dataset
  (Sprint 4, audit S8) — so FAG doesn't accidentally see dev
  projects?
- Should `dev_<id>` projects expose read-only sub-tokens for
  safer Codex handoff?
- Need shared "commercial" scopes (e.g. a `family` dataset shared
  with another user)?

**Code:** [memory/cognee_client.py](../app/memory/cognee_client.py),
[memory/dev_ingest.py](../app/memory/dev_ingest.py),
[main.py](../app/main.py)

---

## 10. Codex / Claude integration

**Currently:** `~/.codex/config.toml` runs `cognee-mcp` over stdio
with the default_user JWT. `~/.codex/AGENTS.md` instructs Codex
when to call remember / recall. JWT expires in ~1 hour, no auto
refresh.

**Open decisions:**

- `remember` trigger: explicit "запомни" only (current) / LLM
  judge "is this a fact worth keeping" / auto-save every user turn?
- JWT refresh — handled by cognee-mcp child or by FAG, which would
  store email/password? (Sprint 4, audit S3)
- Should Codex see a separate per-session memory section?
- Same questions for Claude Code, ChatGPT — which configuration
  do we recommend?

**Code:** `~/.codex/AGENTS.md`, `~/.codex/config.toml`,
[docs/codex-claude-setup.md](codex-claude-setup.md)

---

## 11. Hygiene / lifecycle (keeping memory healthy)

**Currently:** spike fixtures (Sunfish-7392, axolotl Mochi, etc.)
sit in real memory; no UI to view/delete a fact; no backups; no
integration tests for the cognee path.

**Open decisions:**

- Cleanup script for seed/canary data (Sprint 1, audit C5).
- Web page `/memory` — list facts, "forget" button, graph
  visualization (Sprint 5, audit Q3).
- Backup `infra/cognee/data/` — where (local / S3), how often?
- Integration tests — yes/no, and what minimum coverage?

**Code:** scripts/ for cleanup, web/routes.py for UI, infra/ for
backups, tests/ for tests.

---

## How to use this doc

When we discuss a station:

1. Pick a number (1–11).
2. I expand the current code state + present options.
3. We agree on the decision.
4. Decision becomes acceptance criteria for the next commit.
5. Mark it ✅ here.

Recommended order (but you can jump anywhere):

1. **Foundation:** stations 1 → 3 → 4 (input + metadata + storage)
2. **Cognee internals:** stations 5 → 6 → 7 (cognify + conflicts + time)
3. **Surface:** stations 8 → 9 → 10 (retrieval + scopes + agents)
4. **Maintenance:** station 11 (hygiene)
