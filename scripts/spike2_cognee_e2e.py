"""Phase 1 spike-2: end-to-end probe of the Cognee sidecar.

Run AFTER `make cognee-start`. Hits the live sidecar over HTTP — does NOT
import cognee. Costs a small amount of LLM/embedding tokens (~ a few cents).

Steps:
  1. GET / — sanity
  2. GET /openapi.json — record available endpoints
  3. POST /api/v1/add — small fixture text in dataset "spike2"
  4. POST /api/v1/cognify — measure latency
  5. POST /api/v1/search and /api/v1/recall — verify retrieval
  6. Inspect Qdrant for the cognee collection (dim, count)
  7. Write findings to docs/cognee-spike2-report.md

Output: prints structured progress, writes the report file.

Usage:
    make cognee-start
    python3 scripts/spike2_cognee_e2e.py [--cleanup]

The --cleanup flag attempts to forget the spike2 dataset at the end. Off by
default so you can inspect cognee state after the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "cognee-spike2-report.md"
SIDECAR_URL = os.environ.get("COGNEE_BASE_URL", "http://127.0.0.1:8765")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
DATASET = "spike2"

FIXTURE = (
    "Note about Fixar CRM scheduling rules. The user prefers a 3-hour "
    "scheduling window when offering appointment slots to clients. The "
    "primary location is the Vancouver office. Reminders should be sent "
    "the day before at 9 AM Pacific."
)
QUERY = "What is the preferred scheduling window in Fixar CRM?"


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


async def probe_root(client: httpx.AsyncClient) -> dict[str, Any]:
    section("1. GET /")
    r = await client.get("/")
    print(f"  status={r.status_code} body={r.text[:200]}")
    r.raise_for_status()
    return {"status_code": r.status_code, "body": r.text[:200]}


async def probe_openapi(client: httpx.AsyncClient) -> dict[str, Any]:
    section("2. GET /openapi.json")
    r = await client.get("/openapi.json")
    r.raise_for_status()
    spec = r.json()
    paths = list(spec.get("paths", {}).keys())
    print(f"  total endpoints: {len(paths)}")
    interesting = [
        p for p in paths
        if any(k in p for k in ("/add", "/cognify", "/search", "/recall", "/forget", "/datasets"))
    ]
    for p in interesting:
        methods = list(spec["paths"][p].keys())
        print(f"  {','.join(m.upper() for m in methods):15s} {p}")
    return {"total_paths": len(paths), "memory_paths": interesting}


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, Any, float]:
    t0 = time.perf_counter()
    r = await client.request(method, path, json=body, timeout=timeout)
    elapsed = time.perf_counter() - t0
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:500]
    return r.status_code, payload, elapsed


async def step_add(client: httpx.AsyncClient) -> dict[str, Any]:
    section("3. POST /api/v1/add")
    body = {"data": FIXTURE, "dataset_name": DATASET}
    code, payload, elapsed = await call(client, "POST", "/api/v1/add", body)
    print(f"  status={code} elapsed={elapsed:.2f}s")
    if isinstance(payload, dict):
        print(f"  payload keys: {list(payload.keys())}")
    elif isinstance(payload, list):
        print(f"  list len={len(payload)}")
    else:
        print(f"  payload: {str(payload)[:200]}")
    return {"status_code": code, "elapsed_s": elapsed, "payload_sample": str(payload)[:500]}


async def step_cognify(client: httpx.AsyncClient) -> dict[str, Any]:
    section("4. POST /api/v1/cognify")
    body = {"datasets": [DATASET]}
    code, payload, elapsed = await call(client, "POST", "/api/v1/cognify", body, timeout=300.0)
    print(f"  status={code} elapsed={elapsed:.2f}s")
    print(f"  payload: {str(payload)[:300]}")
    return {"status_code": code, "elapsed_s": elapsed, "payload_sample": str(payload)[:500]}


async def step_search(client: httpx.AsyncClient) -> dict[str, Any]:
    section("5a. POST /api/v1/search")
    body = {"query": QUERY, "query_type": "GRAPH_COMPLETION", "datasets": [DATASET], "top_k": 5}
    code, payload, elapsed = await call(client, "POST", "/api/v1/search", body, timeout=120.0)
    print(f"  status={code} elapsed={elapsed:.2f}s")
    print(f"  payload sample: {str(payload)[:400]}")
    return {"status_code": code, "elapsed_s": elapsed, "payload_sample": str(payload)[:800]}


async def step_recall(client: httpx.AsyncClient) -> dict[str, Any]:
    section("5b. POST /api/v1/recall")
    body = {"query": QUERY, "dataset_name": DATASET, "top_k": 5}
    code, payload, elapsed = await call(client, "POST", "/api/v1/recall", body, timeout=120.0)
    print(f"  status={code} elapsed={elapsed:.2f}s")
    print(f"  payload sample: {str(payload)[:400]}")
    return {"status_code": code, "elapsed_s": elapsed, "payload_sample": str(payload)[:800]}


async def step_qdrant() -> dict[str, Any]:
    section("6. Inspect Qdrant for cognee collection(s)")
    api_key = os.environ.get("QDRANT_API_KEY", "")
    headers = {"api-key": api_key} if api_key else {}
    async with httpx.AsyncClient(base_url=QDRANT_URL, headers=headers, timeout=10.0) as q:
        r = await q.get("/collections")
        if r.status_code != 200:
            print(f"  qdrant /collections -> {r.status_code} {r.text[:200]}")
            return {"error": f"HTTP {r.status_code}"}
        collections = r.json().get("result", {}).get("collections", [])
        print(f"  collections: {[c['name'] for c in collections]}")
        info: dict[str, Any] = {"collections": []}
        for c in collections:
            name = c["name"]
            ri = await q.get(f"/collections/{name}")
            if ri.status_code == 200:
                detail = ri.json().get("result", {})
                config = detail.get("config", {}).get("params", {}).get("vectors", {})
                size = (
                    config.get("size")
                    if isinstance(config, dict) and "size" in config
                    else (config if isinstance(config, int) else "?")
                )
                points = detail.get("points_count", "?")
                print(f"    {name}: dim={size} points={points}")
                info["collections"].append({"name": name, "dim": size, "points": points})
        return info


async def step_forget(client: httpx.AsyncClient) -> dict[str, Any]:
    section("7. POST /api/v1/forget (cleanup)")
    code, payload, elapsed = await call(
        client, "POST", "/api/v1/forget", {"dataset_name": DATASET}
    )
    print(f"  status={code} elapsed={elapsed:.2f}s payload={str(payload)[:200]}")
    return {"status_code": code, "elapsed_s": elapsed}


def render_report(results: dict[str, Any], cleanup_ran: bool) -> str:
    lines = [
        "# Cognee Spike-2 Report (Phase 1 — end-to-end)",
        "",
        f"Run against `{SIDECAR_URL}` on {time.strftime('%Y-%m-%d %H:%M:%S %Z')}.",
        "",
        "## Dataset",
        f"- Name: `{DATASET}`",
        f"- Fixture text: `{FIXTURE!r}`",
        f"- Query: `{QUERY!r}`",
        "",
        "## Results",
        "",
    ]
    for step, payload in results.items():
        lines.append(f"### {step}")
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    add = results.get("add", {})
    cognify = results.get("cognify", {})
    if cognify:
        lines.append(f"- `cognify` latency: {cognify.get('elapsed_s', '?')}s on {len(FIXTURE)}-char fixture")
    if add and add.get("status_code", 0) >= 400:
        lines.append("- `add` returned non-2xx — verify env vars / API keys in infra/cognee/.env")
    qd = results.get("qdrant", {})
    if isinstance(qd, dict) and qd.get("collections"):
        cogn_collections = [c for c in qd["collections"] if "cognee" in c["name"].lower() or c["name"] != "file_agent_v2"]
        if cogn_collections:
            lines.append(f"- Cognee Qdrant collections detected: {[c['name'] for c in cogn_collections]}")
            for c in cogn_collections:
                lines.append(f"  - `{c['name']}`: dim={c.get('dim')} points={c.get('points')}")
    if cleanup_ran:
        lines.append("- Cleanup: `forget` was called on the spike2 dataset.")
    else:
        lines.append("- Cleanup: not run. State remains in cognee for inspection.")
    return "\n".join(lines) + "\n"


async def main(cleanup: bool) -> int:
    api_key = os.environ.get("COGNEE_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient(base_url=SIDECAR_URL, headers=headers, timeout=60.0) as client:
        try:
            results: dict[str, Any] = {}
            results["root"] = await probe_root(client)
            results["openapi"] = await probe_openapi(client)
            results["add"] = await step_add(client)
            results["cognify"] = await step_cognify(client)
            results["search"] = await step_search(client)
            results["recall"] = await step_recall(client)
            results["qdrant"] = await step_qdrant()
            cleanup_ran = False
            if cleanup:
                results["forget"] = await step_forget(client)
                cleanup_ran = True
        except httpx.HTTPError as exc:
            print(f"\nFATAL: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            print("Is the sidecar running? `make cognee-start` then retry.", file=sys.stderr)
            return 2

    section("Writing report")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(results, cleanup_ran), encoding="utf-8")
    print(f"  wrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spike-2 end-to-end probe of the Cognee sidecar")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Call /api/v1/forget on the spike2 dataset at the end of the run",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(cleanup=args.cleanup)))
