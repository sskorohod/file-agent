# FAG memory architecture audit — 2026-05-08

Honest review of the cognee migration after Phases 0–7b. Verified against
the actual code on `spike/cognee-compat`, not against my memory of it.

Russian version: [architecture-audit-2026-05-08.ru.md](architecture-audit-2026-05-08.ru.md).

## TL;DR

The migration **works**, but it's been bolted on top of the legacy stack
without retiring duplicate paths. There are 9 issues I'd call serious or
critical, and 8 more worth fixing soon. No design problem is unfixable;
they're all consequences of "ship the migration, defer cleanup."

## Critical (data integrity, silent failure modes)

### C1. dev-project users get locked out when their JWT expires

`DevIngestor.register_project` mints a fresh cognee user with a random
password ([dev_ingest.py:104-119](app/memory/dev_ingest.py)) and stores
**only the JWT** in `dev_projects.cognee_token`. The password is thrown
away. JWTs expire (FastAPI Users default = ~1 hour). After that we
cannot log in again, and the project's data is permanently inaccessible
to FAG (the cognee user still owns it, but we have no credentials).

**Fix:** persist the password (encrypted in `secrets` table or alongside
the token), and re-login automatically when a 401 comes back.

### C2. No `file_id` ↔ cognee `data_id` link

`_step_cognee_ingest` ([pipeline.py:670-688](app/pipeline.py)) sends
text + `filename=file_record.original_name` and nothing else. The FAG
UUID (`file_record.id`) never lands in cognee. So when `cognee.recall`
returns a hit, we cannot navigate back to the original document on
disk, render an inline preview, or attach it to a Telegram reply.

This is why Phase 4 gave Telegram answers without file buttons: there
is no `file_id` to put on the button.

**Fix:** pass `file_id` (and source_type=`file`) as cognee metadata
on every `add`, store it in cognee data nodes, surface it back through
`recall` results into FAG's `LLMSearch` answer dict.

### C3. Duplicate embedding — Gemini Qdrant AND OpenAI lancedb

For every document:
- Step 7 [`_step_embed`](app/pipeline.py) writes a 768-dim Gemini
  vector to Qdrant collection `file_agent_v2`.
- Step 9.5 `_step_cognee_ingest` triggers cognee's cognify which writes
  a 1536-dim OpenAI vector to its lancedb.

Two LLM/embedding API calls per document. Two stores to keep alive.
The `file_agent_v2` collection is read by `LLMSearch.answer` only as a
fallback path (`use_for_search=false`) and from `handlers.py:1231` (a
non-RAG quick search). Otherwise it's write-only — pure dead weight.

**Fix:** delete Step 7. Drop `file_agent_v2` (or keep empty for
fallback). Remove the embedding section from `config.yaml`.

### C4. Skill-extracted fields never reach cognee

Step 5.5 ([`_step_extract`](app/pipeline.py)) uses the matched skill's
`custom_prompt` to pull structured fields (priority, expiry_date,
amount, parties, etc.) and stores them in `files.metadata_json`.
`_step_cognee_ingest` later sends only `parse_result.text` to cognee —
the structured fields are NOT in the payload. Cognee's graph
extraction has to re-derive them from raw text, which is both
duplicate work and lossy.

**Fix:** include `extracted_fields` in cognee metadata, so they appear
on the data node and can be queried directly without re-extraction.

### C5. Spike/canary data lives in the user's real memory

`main_dataset` currently contains `Sunfish-7392`, `axolotl Mochi`,
`spike2_fixture`, and other Phase-1/3/6 fixtures. Plus the orphaned
`personal` dataset still has 9 items from earlier smoke tests
(migrated to main_dataset but not deleted).

**Fix:** one-shot script `scripts/cleanup_spike_data.py` that deletes
the canaries by filename / dataset name and drops the unused `personal`
dataset.

## Serious (UX, correctness, cost)

### S1. Cognify latency on Telegram upload

`_step_cognee_ingest` runs `await cognify(...)` inline in the pipeline.
On a real document this takes 8–30s. The Telegram user sees the bot
"thinking" much longer than before because we wait for cognify before
replying.

**Fix:** make Step 9.5 fire-and-forget — `asyncio.create_task` after
Step 8 (save_meta) returns. `processing_log` will record success/failure
async. Pipeline returns to user immediately.

### S2. FAG search prompt regression

Phase 4 routed `/search` through `cognee.recall` when `use_for_search=true`.
This bypasses our carefully-tuned `search_prompt` in `config.yaml`
("respond in same language", "cite which document", "give detailed
answers"). Cognee's graph completion has its own internal prompt that's
shorter and less FAG-aware.

Symptom: replies from Telegram bot are stylistically different — more
"factual extract", less "conversation about your archive".

**Fix:** use cognee with `searchType="CHUNKS"` to get raw context, then
run our own `LLMSearch.llm.search_answer` over those chunks with the
FAG prompt. Costs +1 LLM call per search, but restores the UX.

### S3. JWT expiry breaks Codex MCP

cognee-mcp child process holds the JWT we passed at startup. When it
expires (~1 hour), every `recall` returns 401. The user has to re-mint
and restart Codex.

**Fix:** support JWT refresh in CogneeClient — on 401, attempt
`login_as_user(default_user, default_password)` once, retry the request.
For Codex's child process, accept email/password instead of just JWT.

### S4. No memory_type / authority_level

Original Phase 5 plan had `memory_type` (fact / preference / rule /
decision / event / task) and `authority_level` (personal_thought /
draft / approved). Neither was ever wired. Every memory in cognee is
"flat" — an `axolotl named Mochi` and `legal contract clause N` look
the same to retrieval. Analytics ("show me all my preferences" or
"only durable rules") are impossible.

**Fix:** add `memory_type` and `authority_level` as cognee metadata
(passed at `add` time, surfaced through `recall`). FAG-side: derive
type from skill/extraction (notes → preference / fact, files → fact,
chat → mostly fact). Codex AGENTS.md instruct it to pass `memory_type`
on `remember`.

### S5. No conflict detection or supersession

Cognee has no concept of "this fact replaces that fact". Tell it
"User uses VS Code" today and "User switched to Cursor" tomorrow —
both stay in the graph, both come back on recall. The user gets the
older fact roughly as often as the newer one.

**Fix:** before each `add`, do `recall(content)` to find similar
existing memories. If found and the LLM judges them contradictory,
mark the older as superseded (cognee node attribute) and flag the
relation. This was specced in the original Phase 5 plan but never
built.

### S6. No temporal awareness / `expires_at`

"Vanya is driving home from Las Vegas" was stored on May 8. After he
arrives, the fact is stale. Cognee never expires entries. Every recall
in the future will surface the outdated fact unless the user explicitly
forgets it.

**Fix:** store `valid_from` / `expires_at` (also from the original
plan) at add time. Codex extracts from the user phrasing ("сегодня",
"на этой неделе", "до пятницы"). Recall filters out expired by default;
optional `include_expired=true` for analytics.

### S7. Lost Telegram file buttons (regression)

Tied to C2. Without `file_id` in cognee responses, our Telegram bot
can't render inline buttons to download the source document. The user
gets a textual answer with no way to click through.

**Fix:** same as C2 — once `file_id` flows through, restore button
rendering in `handlers.py`.

### S8. `default_user` is a superuser by design

Cognee creates `default_user@example.com` with `is_superuser=True`. The
FAG main process logs in as this user, so any `recall` from Telegram
or web sees ALL datasets — including `dev_<id>` for projects that were
supposed to be isolated.

In the multi-user probe (Phase 5b) we proved that *non-superuser* users
are isolated. But the default_user, which is what FAG uses for its own
flows, sees everything. If the user has connected Codex through the
default_user's token (they did during testing), the agent also sees
across all projects — defeating Phase 5's whole point.

**Fix:** create a non-superuser personal user (`fag_personal@local`) at
startup, give it ownership of `main_dataset`, use ITS token for FAG's
own flows. Reserve `default_user` for admin-only ops (forget across
datasets, dataset cleanup).

## Quality-of-life issues

### Q1. cognee-mcp spawned per Codex session

Codex stdio config spawns a fresh `cognee-mcp` child for every Codex
session. Open 5 Codex sessions = 5 cognee-mcp processes, each ~300 MB
RAM. They all talk to the same sidecar, but the subprocess overhead
adds up.

**Fix:** keep cognee-mcp as a single long-lived daemon (HTTP or Unix
socket); Codex connects to it instead of spawning. Requires Codex CLI
to support `transport: streamable_http` reliably with FastMCP — which
right now it does not (see Phase 6 spike). Defer until Codex updates.

### Q2. Voice notes stored in three places

`_save_smart_note`:
- writes to SQLite `notes` table
- writes Obsidian `.md` file under `storage/notes/`
- ingests into cognee

Plus the original Telegram audio file might still be cached on disk.

**Fix:** decide one canonical store. If cognee is THE memory layer,
notes table can be dropped; Obsidian export becomes optional (and
generated from cognee on demand). Today this is duplication for no
clear reason.

### Q3. No FAG dashboard for cognee memory

Web dashboard (`/files`, `/insights`, `/search`) shows files and
LLM-derived insights. There is no view for "what's in my memory graph",
"how much memory do I have", "what topics are most-connected", "what
should I forget". Cognee runs blind from the user's perspective.

**Fix:** add `/memory` page with: dataset list, recent memories, a
graph visualization (cognee has its own `/visualize` endpoint we can
embed), and a forget-by-content UI. Possibly the cognee-mcp UI server.

### Q4. Chat ingest threshold is heuristic and wrong

`ingest_text_to_cognee` only runs for messages ≥40 chars. Misses
short, useful statements ("я ушёл из Acme", "купил Tesla", "вышел
на пенсию"). And it accepts long but irrelevant rambles.

**Fix:** drop the chars threshold, replace with a cheap LLM-judge
("is this a fact about the user worth remembering? yes/no") or just
ingest everything and let cognee's own extractor decide.

### Q5. `personal` dataset still exists, abandoned

Migrated 9 items to `main_dataset` but never deleted the source. Sits
there confusing future operators.

**Fix:** `cognee.forget(dataset="personal", everything=True)` and
remove from any docs.

### Q6. No retention policy

cognee SQLite + lancedb grow without bound. No dedup, no TTL, no
"forget after N years if not accessed". For a single user this is
fine for years, but eventually disk usage matters.

**Fix:** background task `_memory_gc_loop` that walks rarely-accessed
nodes and either compresses (downgrade to summary) or expires.
Defer until disk usage actually matters.

### Q7. No backups / recovery story

If `infra/cognee/data/` corrupts, all derived memory is gone. Raw
truth (`files`/`notes`/`chat_history` in FAG SQLite) survives, but
rebuilding cognee = re-cognify everything = expensive (LLM cost).

**Fix:** scheduled `tar -czf` of `infra/cognee/data/` to local backup,
restore script `scripts/restore_cognee_backup.sh`. For paranoia: also
push to S3.

### Q8. No integration tests

`tests/` has only old unit tests (db, errors, parser, skills, storage).
None exercise the cognee path end-to-end. Every regression we caught
was via manual smoke. The next regression will be missed until a real
user notices.

**Fix:** add `tests/test_cognee_pipeline.py` that runs the full
upload→cognee→recall flow against a mock cognee server (or against a
disposable real one in CI).

## What I'd do, in order

| Sprint | Why | Stake |
|---|---|---|
| **Sprint 1: Quality core** — C1 + C2 + C3 + C4 + C5 | Critical correctness — auth, navigability, dedup, lost data | 1–2 days, ~300 LoC |
| **Sprint 2: UX restoration** — S1 + S2 + S7 | Re-restore Telegram UX to pre-migration parity | 1 day, ~150 LoC |
| **Sprint 3: Memory richness** — S4 + S5 + S6 | Make memory actually queryable as data, not just a bag | 2–3 days, ~400 LoC |
| **Sprint 4: Auth & safety** — S3 + S8 + Q5 | Stable Codex sessions, safe scopes | 1 day |
| **Sprint 5: Visibility** — Q3 | User can SEE their memory and curate it | 2 days, UI work |
| **Later** — Q1, Q2, Q4, Q6, Q7, Q8 | Quality-of-life, monitoring, retention | Defer |

Total core fixes: about 4–5 days of focused work to get to a quality
product. Each Sprint is mergeable independently.
