"""Sprint Q — re-embed image-only files via Gemini multimodal + add a
header chunk so they show up in search regardless of OCR quality.

The SSN card, passport scans, driver's licence photos etc. landed in
Qdrant with only 1-2 text chunks of garbled OCR output (e.g. "WITH 'DHS
AUTHORIZATIONS 52129750812 THIS won Thee FOR" instead of "VALID FOR
WORK ONLY WITH DHS AUTHORIZATION"). Vector search for "SSN" or
"паспорт" therefore misses the actual card and surfaces tax forms with
the literal phrase in their template.

This script:

1. Walks every ``files`` row with ``mime_type LIKE 'image/%'`` or PDFs
   that look image-only (no extracted_text or very short text).
2. Reads the file bytes from `stored_path` (decrypting if `encrypted=1`
   via the existing FAGB helper).
3. Calls ``vector_store.upsert_document(... file_bytes=bytes ...)`` —
   which now emits a header chunk (-3) + multimodal point (-1) + the
   existing text chunks. Idempotent: uuid5 keys mean re-runs overwrite.

Usage:
    .venv/bin/python scripts/reembed_images.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import dotenv

ROOT = "/Users/vskorokhod/fag"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
dotenv.load_dotenv(f"{ROOT}/.env", override=True)


async def main(dry_run: bool, limit: int | None):
    from app.config import get_settings
    from app.storage.db import Database
    from app.storage.vectors import VectorStore
    from app.llm.classifier import ClassificationResult

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()
    vs = VectorStore(s.qdrant, s.embedding, google_api_key=s.google_api_key)
    await vs.connect()

    cur = await db.db.execute(
        "SELECT id, original_name, stored_path, mime_type, category, summary, "
        "       tags, extracted_text, encrypted, "
        "       (SELECT json_extract(metadata_json, '$.document_type') "
        "        FROM files f2 WHERE f2.id = f.id) AS document_type "
        "FROM files f "
        "WHERE mime_type LIKE 'image/%' "
        "   OR (mime_type LIKE 'application/pdf' "
        "       AND length(COALESCE(extracted_text, '')) < 200) "
        "ORDER BY created_at"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if limit:
        rows = rows[:limit]
    print(f"image-like files to re-embed: {len(rows)}")

    ok = fail = 0
    for i, f in enumerate(rows, 1):
        path = f.get("stored_path") or ""
        if not path or not os.path.exists(path):
            print(f"  [{i}] {f['original_name']}: stored_path missing — skip")
            fail += 1
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if f.get("encrypted"):
                try:
                    from app.utils.crypto import decrypt_bytes, load_or_create_system_key
                    key = load_or_create_system_key()
                    data = decrypt_bytes(data, key)
                except Exception as e:
                    print(f"  [{i}] {f['original_name']}: decrypt failed: {e}")
                    fail += 1
                    continue

            if dry_run:
                print(f"  [{i}] {f['original_name']} ({f['mime_type']}, "
                      f"{len(data)} bytes) — would re-embed")
                continue

            tags = []
            try:
                import json as _json
                tags = _json.loads(f.get("tags") or "[]")
            except Exception:
                pass
            chunks = await vs.upsert_document(
                file_id=f["id"],
                text=f.get("extracted_text") or "",
                metadata={
                    "category": f.get("category") or "",
                    "filename": f.get("original_name") or "",
                    "original_name": f.get("original_name") or "",
                    "document_type": f.get("document_type") or "",
                    "summary": f.get("summary") or "",
                    "tags": tags,
                },
                file_bytes=data,
                mime_type=f.get("mime_type") or "",
            )
            ok += 1
            if i % 5 == 0:
                print(f"  ✓ {i}/{len(rows)} sent (last: {f['original_name']}, "
                      f"{chunks} points)")
        except Exception as exc:
            fail += 1
            print(f"  ✗ [{i}] {f['original_name']}: {exc}")

    print(f"\nrebuilt {ok}/{ok + fail} ({fail} failed)")
    await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.dry_run, args.limit))
