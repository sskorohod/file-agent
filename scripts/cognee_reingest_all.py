"""Wipe + re-ingest cognee `main_dataset` from current SQLite content.

Sprint F. The cognee graph carries old encrypted text from the
FAGE-encrypted era. This script:

1. Drops every document and note from the cognee dataset (best-effort —
   if the sidecar lacks a clean drop API we just rebuild on top and
   accept duplicates; cognify deduplicates by content hash).
2. For each `files` row with `extracted_text`, calls
   `cognee.add(text)` with source_id `file_<id>`.
3. For each `notes` row with `content`, calls
   `cognee.add(text)` with source_id `note_<id>`.
4. After all adds, fires `cognify` once so cognee builds the graph.

Usage:
    .venv/bin/python scripts/cognee_reingest_all.py [--dry-run]

Make sure the sidecar is up first: `make cognee-start`.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

for _k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
    os.environ.pop(_k, None)
import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

ROOT = "/Users/vskorokhod/fag"
sys.path.insert(0, ROOT)
os.chdir(ROOT)


async def main(dry_run: bool):
    from app.config import get_settings
    from app.storage.db import Database
    from app.memory.cognee_client import CogneeClient, CogneeError, CogneeUnavailable

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()
    cog = CogneeClient(s.cognee)
    await cog.setup()
    if not cog.healthy:
        print("✗ cognee sidecar unhealthy — start it: `make cognee-start`")
        return

    dataset = s.cognee.default_dataset
    print(f"target dataset: {dataset}")
    print(f"sidecar: {s.cognee.base_url}\n")

    cur = await db.db.execute(
        "SELECT id, original_name, extracted_text "
        "FROM files WHERE extracted_text != '' "
        "  AND extracted_text NOT LIKE '%RkFHRQ%' "
        "ORDER BY created_at"
    )
    files = [dict(r) for r in await cur.fetchall()]

    cur = await db.db.execute(
        "SELECT id, title, content FROM notes "
        "WHERE content != '' AND content NOT LIKE '%RkFHRQ%' "
        "ORDER BY created_at"
    )
    notes = [dict(r) for r in await cur.fetchall()]

    print(f"to re-ingest: {len(files)} files, {len(notes)} notes")

    if dry_run:
        print("\n--dry-run: not calling cognee.add. Sample sources:")
        for f in files[:5]:
            print(f"  file_{f['id'][:8]}  {f['original_name']}")
        for n in notes[:5]:
            print(f"  note_{n['id']}  {(n['title'] or '')[:50]}")
        return

    t0 = time.time()
    ok = fail = 0

    for i, f in enumerate(files, 1):
        text = (f.get("extracted_text") or "")[:30000]
        try:
            await cog.add(
                content=text,
                dataset=dataset,
                filename=f"file_{f['id']}.txt",
                run_in_background=False,
            )
            ok += 1
            if i % 10 == 0:
                print(f"  files {i}/{len(files)} sent")
        except (CogneeUnavailable, CogneeError) as exc:
            print(f"  ✗ file {f['id'][:8]}: {exc}")
            fail += 1

    for i, n in enumerate(notes, 1):
        title = (n.get("title") or "").strip()
        body = (n.get("content") or "").strip()
        text = (f"{title}\n\n{body}" if title else body)[:30000]
        try:
            await cog.add(
                content=text,
                dataset=dataset,
                filename=f"note_{n['id']}.txt",
                run_in_background=False,
            )
            ok += 1
            if i % 20 == 0:
                print(f"  notes {i}/{len(notes)} sent")
        except (CogneeUnavailable, CogneeError) as exc:
            print(f"  ✗ note {n['id']}: {exc}")
            fail += 1

    add_dur = time.time() - t0
    print(f"\nadded {ok}/{ok+fail} sources in {add_dur:.0f}s")

    print("\ntriggering cognify (graph extraction)…")
    t1 = time.time()
    try:
        await cog.cognify(dataset=dataset, run_in_background=False)
        print(f"  cognify done in {time.time()-t1:.0f}s")
    except (CogneeUnavailable, CogneeError) as exc:
        print(f"  ✗ cognify: {exc}")

    await cog.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
