# Memory layer audit — 2026-05-10

After two days of sweeping repairs (decrypt FAGE residue across `files`,
`note_enrichments`, `note_items`, `chat_history`; full reindex of 50 docs
+ 159 notes; multi-doc disambiguation; LLM-wiki materialisation), this is
my honest read of where the FAG memory layer actually stands and what
still needs fixing for it to behave **deterministically, without
hallucination, dedup-clean and consistent across all stores**.

## TL;DR

We have **four memory stores**, each with their own truth:

| Store | What lives there | Source of truth? |
|---|---|---|
| **SQLite** (`agent.db`) | files, notes, secrets, chat history, processing log, schema rows | ✅ canonical |
| **Qdrant** (`file_agent_v2`) | embeddings — file chunks (text + multimodal) + note chunks | derived from SQLite |
| **Cognee** (sidecar `:8765`, dataset `main_dataset`) | LanceDB vectors + NetworkX graph from cognify-ed text | derived from SQLite |
| **Markdown vault** (`~/ai-agent-files/`) | `notes/*.md` (manual-friendly), `wiki/{docs,notes,entities,index,log,CLAUDE}.md` (autogen) | derived from SQLite |

When everything is healthy, it works. The problem is they drift apart
silently — and silent drift is what causes hallucinations and "model
contradicts itself" symptoms.

## Critical gaps (fix before claiming "memory layer is done")

### 1. **No write-time fan-out invariant**

Today the only deterministic write path is the SQLite insert. Qdrant /
cognee / wiki updates are best-effort, can fail without breaking
SQLite, and there's no transactional reconciliation. Symptoms we
already hit:

- 163 orphan note vectors in Qdrant after I deleted notes from SQLite
- `metadata_json` carrying FAGE-encrypted base64 for 9 files because
  encryption-at-rest writes succeeded into SQLite while the rest of the
  pipeline crashed silently
- Cognee got a passport image's encrypted ciphertext because text was
  shipped before the encrypt step

**Fix:** an outbox pattern. Pipeline writes ONLY to SQLite + an
`outbox` table; a single sweeper applies the row to Qdrant + cognee +
wiki + obsidian-md, and only marks `applied_at` when ALL succeed. Any
half-applied row gets retried. Same model on delete.

### 2. **Cognee is configured but disabled by default**

`config.yaml` has `cognee.enabled: true` AND `cognee.use_for_search: true`,
yet the sidecar wasn't running today (had to `make cognee-install` just
now). Search transparently falls through to Qdrant — fine for relevance
but we lose the graph-completion path that's supposed to anchor answers
to entity-relation triples and prevent "the LLM made it up". So:

- We have RAG, not knowledge-graph-grounded answers
- Multi-hop questions ("какие документы у меня связаны с Inha
  Smelova") can't traverse the graph

**Fix:** make cognee a hard requirement of `make dev` (fail closed
if the sidecar's not up), surface its health in `/settings`,
re-ingest **decrypted** notes + files from scratch (the existing
ingest had encrypted text), wire the LLM search system prompt to
quote its source via `[doc-id:chunk]`.

### 3. **No dedup across (text, embedding) pairs**

A single document gets embedded twice today: once as multimodal
(image bytes → 768-dim) and once as text-chunk (extracted text →
768-dim). They collide in the same collection but on different
points. For users who upload near-duplicates (same passport scanned
twice), we have no SHA-based dedup at the vector level, only at the
files table (sha256 unique). Symptom: search returns "two passports"
when the user actually has one and a re-scan.

**Fix:** in pipeline `_step_store`, when `sha256` matches an existing
row, refuse-and-link instead of allowing both rows to coexist (today
we have a duplicate-detection flow but it's dialog-driven; should be
hard-default + override).

### 4. **No reconciliation job**

There's no daily/hourly task that walks every store and confirms:

- every `files.id` has at least one Qdrant point (and vice versa)
- every `notes.id` with non-empty content has a vector
- every `processing_log` "store" success has a paired Qdrant write
- wiki `entities/*.md` exists for every entity referenced in a doc
- cognee sees every file/note SQLite has

Today we discover divergence by accident (user complains "вылезает I-94
в поиск паспортов"). We need a `make memory-doctor` target that prints
divergence and a `--fix` flag to re-emit missing pieces.

### 5. **Entity layer is regex, not LLM**

`build_wiki.py` uses a heuristic Python regex for "entities" (capitalised
multi-word + known org hints). That's fragile:

- catches false positives like "После Визита" as an entity
- misses lower-case names like "клиент Виктор"
- can't link aliases ("Vyacheslav Skorokhod" / "Скороход Вячеслав" /
  "Слава" are 3 separate pages today)

**Fix:** entity extraction via cognee's existing graph (`recall(query,
search_type='entities')`) once the sidecar is alive, OR via a dedicated
LLM pass with explicit alias resolution. Then the wiki gets ONE
canonical entity page per real person/org and all aliases redirect.

### 6. **No source-of-truth for "what the user's archive contains"**

We have:

- `files` table (SQLite) — 50 rows
- `wiki/index.md` — autogen mirror
- `_DOCS.md` (legacy from PR #9) — also autogen mirror
- Settings → Files page (HTML) — yet another mirror

**Fix:** delete the legacy `_DOCS.md`, keep only the wiki index.
Settings page should be a thin view over the same SQLite query, no
secondary store.

### 7. **Note enrichment is over-eager and partly stale**

`note_enrichments` has 274 rows for 158 notes — multiple enrichments
per note from re-runs. We just decrypted 211 enrichment summaries that
were generated from **encrypted** transcripts (LLM saw FAGE base64) so
those are noise. Even after decrypt, the structured fields
(`structured_json`, `mood_score`, `sentiment`, `energy`) are stale for
155 notes.

**Fix:** wipe `note_enrichments` and re-enrich every note from current
content (fresh LLM pass). Drop the multi-row history — keep one
canonical enrichment per note in a single column on `notes` itself.

### 8. **PIN gating and data lineage**

Sensitive files are AES-GCM-encrypted on disk + need PIN to open.
Good. But:

- `extracted_text` for sensitive files is **plaintext** in SQLite —
  whoever has the DB has the passport content
- Qdrant stores the same text in chunks
- Cognee will mirror it
- The `wiki/docs/<slug>.md` summary leaks the same content in plain
  Markdown

**Fix:** make a deliberate decision and document it. Either
"sensitive metadata is plaintext-by-design (search needs it) and the
binary file is the only secret" — which is what we're doing, but
nowhere stated. Or: encrypt the text columns in DB too (we have a
master-key path for that, abandoned during Sprint A) and accept the
search regression.

## Quality-of-life gaps

- **No cross-document navigation in Telegram.** Search returns docs;
  user can't ask "show me everything that mentions USCIS" without
  cognee-graph or full-text-FTS path.
- **`processing_log` is write-only.** No retention policy, no UI.
  Useful for `memory-doctor` but unused.
- **Auto-delete TTL is hardcoded to 15 min.** Should be a per-category
  setting (paystubs maybe permanent; SSN 5 minutes).
- **No "this changed" log surfaced to the user.** When the bot
  reclassifies a file in the background, the user has no way to see
  it without reading `log.md`.
- **Cognee dataset is shared with external agents** (Codex, Claude
  Code via cognee-mcp). That's a feature today but means a
  misbehaving external agent can poison our personal scope. Should be
  scoped to a `fag_personal` dataset, with `main_dataset` left for
  cross-agent shared memory.

## What's already strong

- SQLite is well-designed: WAL mode, FTS5, processing_log + schema
  versioning, 8 migrations applied cleanly
- Embeddings are multimodal + text, 768-dim cosine, single collection,
  consistent
- Skill engine (YAML-driven classification + force-encrypt flags) is
  the right abstraction
- Sprint B's selective AES-256-GCM at-rest with FAGB magic is sound
- The `data/.session_key` recovery path saved us 173 files + 155 notes
  worth of plaintext that would have been lost
- Auto-delete loop is persisted (survives restart) — most chat-bot
  TTL implementations hold state in process

## Verdict

**The memory layer is ~70% of where it needs to be.** The big
foundation (SQLite + Qdrant + cognee + skills + selective encryption)
is solid. The remaining 30% — and the part that prevents
hallucinations and silent drift — is **invariant enforcement**:
outbox pattern, reconciliation job, dedup-by-sha hard default, entity
canonicalisation, cognee being mandatory not best-effort.

If we ship the outbox pattern (item 1) and the reconciliation job
(item 4), most of the rest can be incremental. Without those, every
new feature adds another way for the four stores to diverge.

## Recommended next sprints

| Sprint | Item | Estimate |
|---|---|---|
| D | outbox table + sweeper for SQLite → Qdrant/cognee/wiki | 1 day |
| E | `make memory-doctor` reconciliation job + `--fix` | 0.5 day |
| F | wipe + redo `note_enrichments` from clean transcripts | 0.5 day |
| G | LLM entity extraction (replace regex) + alias resolution | 1 day |
| H | per-category auto-delete TTL + scope cognee dataset | 0.5 day |
| I | sensitive-text policy decision + docs | 0.5 day |

Total: ~4 days of focused work to take memory from "works most of the
time" to "deterministic, dedup-clean, no silent divergence".
