# Cognee Spike-2 Report (Phase 1 — end-to-end)

Run against `http://127.0.0.1:8765` on 2026-05-07 23:56:27 PDT.

## Dataset
- Name: `spike2`
- Fixture text: `'Note about Fixar CRM scheduling rules. The user prefers a 3-hour scheduling window when offering appointment slots to clients. The primary location is the Vancouver office. Reminders should be sent the day before at 9 AM Pacific.'`
- Query: `'What is the preferred scheduling window in Fixar CRM?'`

## Results

### root
```json
{
  "status_code": 200,
  "body": "{\"message\":\"Hello, World, I am alive!\"}"
}
```

### openapi
```json
{
  "total_paths": 73,
  "memory_paths": [
    "/api/v1/add",
    "/api/v1/cognify",
    "/api/v1/search",
    "/api/v1/permissions/datasets/{principal_id}",
    "/api/v1/datasets",
    "/api/v1/datasets/{dataset_id}",
    "/api/v1/datasets/{dataset_id}/data/{data_id}",
    "/api/v1/datasets/{dataset_id}/graph",
    "/api/v1/datasets/{dataset_id}/data",
    "/api/v1/datasets/status",
    "/api/v1/datasets/{dataset_id}/data/{data_id}/raw",
    "/api/v1/datasets/{dataset_id}/schema",
    "/api/v1/recall",
    "/api/v1/forget"
  ]
}
```

### add
```json
{
  "status_code": 200,
  "elapsed_s": 3.82957075,
  "payload_sample": "{'status': 'PipelineRunCompleted', 'pipeline_run_id': '38131dec-3bc5-5d2f-a10f-eb6d9307c6de', 'dataset_id': '3d300c1b-688e-51c3-8f78-3e8e833fbfd7', 'dataset_name': 'spike2', 'payload': None, 'data_ingestion_info': [{'run_info': {'status': 'PipelineRunAlreadyCompleted', 'pipeline_run_id': '38131dec-3bc5-5d2f-a10f-eb6d9307c6de', 'dataset_id': '3d300c1b-688e-51c3-8f78-3e8e833fbfd7', 'dataset_name': 'spike2', 'payload': None, 'data_ingestion_info': None}, 'data_id': 'a94a6ba1-a3fc-5b0f-a613-fa49817b"
}
```

### cognify
```json
{
  "status_code": 200,
  "elapsed_s": 8.845595,
  "payload_sample": "{'3d300c1b-688e-51c3-8f78-3e8e833fbfd7': {'status': 'PipelineRunCompleted', 'pipeline_run_id': '9bc4957d-efbc-5c18-819e-be4dcf78661b', 'dataset_id': '3d300c1b-688e-51c3-8f78-3e8e833fbfd7', 'dataset_name': 'spike2', 'payload': None, 'data_ingestion_info': [{'run_info': {'status': 'PipelineRunCompleted', 'pipeline_run_id': '9bc4957d-efbc-5c18-819e-be4dcf78661b', 'dataset_id': '3d300c1b-688e-51c3-8f78-3e8e833fbfd7', 'dataset_name': 'spike2', 'payload': None, 'data_ingestion_info': None}, 'data_id':"
}
```

### search
```json
{
  "status_code": 200,
  "elapsed_s": 1.87994825,
  "payload_sample": "['Based on the knowledge graph, the preferred scheduling window in Fixar CRM is a **3-hour window** when offering appointment slots to clients.']"
}
```

### recall
```json
{
  "status_code": 200,
  "elapsed_s": 1.8897426250000002,
  "payload_sample": "[{'kind': 'graph_completion', 'search_type': 'GRAPH_COMPLETION', 'text': 'The preferred scheduling window in Fixar CRM is a **3-hour window** when offering appointment slots to clients.', 'score': None, 'dataset_id': None, 'dataset_name': None, 'metadata': {}, 'raw': {'value': 'The preferred scheduling window in Fixar CRM is a **3-hour window** when offering appointment slots to clients.'}, 'structured': None, 'source': 'graph'}]"
}
```

### qdrant
```json
{
  "error": "HTTP 401"
}
```

## Notes

- `cognify` latency: 8.85s on 229-char fixture
- Cleanup: not run. State remains in cognee for inspection.

## Lessons (apply to Phase 2+)

The path from "fresh install" to "first 200 OK on `/recall`" surfaced several
contract details that the README and llms.txt had not pinned down:

1. **Vector backend.** `pip install cognee` ships only `lancedb`, `pgvector`,
   and `chromadb` adapters. Qdrant is mentioned in marketing copy but is **not**
   a default extra. We use `VECTOR_DB_PROVIDER=lancedb`; FAG's own Qdrant
   collection (`file_agent_v2`) is untouched.
2. **Two flags to disable auth.** Both `ENABLE_BACKEND_ACCESS_CONTROL=false`
   and `REQUIRE_AUTHENTICATION=false` must be set. The first alone leaves the
   FastAPI Users dependency rejecting requests with 401. With both set, cognee
   falls back to a `default_user@example.com` account it created on first
   startup.
3. **`/add` is multipart, not JSON.** Body schema is
   `Body_add_api_v1_add_post`: `data` is a list of binary file uploads,
   `datasetName` is a form field. Plain text goes in as a fake
   `text/plain` upload.
4. **All other request bodies use camelCase**, not snake_case. `topK`,
   `searchType`, `runInBackground`, `datasetName`, `nodeName`. Sending
   `dataset_name` or `top_k` returns 400 silently.
5. **Anthropic provider needs the `anthropic` SDK installed in the cognee
   venv.** `pip install cognee` does not pull it. Cognee uses the SDK via
   `instructor` (`Mode.ANTHROPIC_TOOLS`), bypassing litellm — so models must
   be Anthropic-native ids (`claude-sonnet-4-6`), not litellm-style
   (`anthropic/claude-sonnet-4-...`).
6. **Embedding via `gemini/text-embedding-004` failed with a 30s connection
   timeout.** Cognee's startup probes the embedding endpoint and bails if it
   doesn't respond. We fell back to `openai/text-embedding-3-small` (1536
   dims), which works against the existing `OPENAI_API_KEY`. Re-trying Gemini
   is a known-unknown for a follow-up — likely needs the right
   `EMBEDDING_ENDPOINT` value or a model id closer to litellm's expectations.
7. **Default user is created during first startup** (`default_user@example.com`
   / `default_password`) inside the configured system root directory. As long
   as the directory persists, subsequent restarts use the same user.
8. **Cognee state path.** Setting `COGNEE_SYSTEM_ROOT_DIRECTORY` to
   `infra/cognee/data/` (relative to the project root) keeps state out of
   site-packages. start.sh `cd`s into infra/cognee before exec, so the
   relative path resolves correctly.
9. **Latency profile (small fixture, ~250 bytes):** `add` ~3.8s,
   `cognify` ~8.8s, `search`/`recall` ~1.9s. For Phase 2 ingest, this means
   `cognify` is the dominant cost — consider batching or background runs for
   non-trivial documents.

These lessons drive immediate updates to `app/memory/cognee_client.py`
(use multipart for the equivalent of `/add`, switch JSON keys to camelCase),
to `infra/cognee/setup.sh` (also `pip install anthropic`), and to
`infra/cognee/.env.example` (real env-var names, real model ids).
