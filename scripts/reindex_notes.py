"""Atomic chunking + Qdrant re-embedding for every note in the DB.

After the FAGE decrypt sweep, the `notes` table contains real plaintext
again, but the vectors that used to live in Qdrant under `type='note'`
were wiped earlier today. This script:

1. Drops any leftover note-typed points in Qdrant.
2. For each note: chunks the content (300-word atomic windows with 50-word
   overlap), embeds each chunk through Gemini Embedding-2 (768-dim cosine,
   matching the existing collection schema), upserts with payload
       { type: 'note', note_id, chunk_index, text, source, category,
         tags, created_at }
3. Updates `notes.embedded` to mirror what's now in Qdrant.

Notes with empty content (e.g. the 2 unrecoverable rows from the
decrypt sweep) are skipped.

Usage:
    .venv/bin/python scripts/reindex_notes.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

for _k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
    os.environ.pop(_k, None)
import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

ROOT = Path("/Users/vskorokhod/fag")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

CHUNK_WORDS = 300
CHUNK_OVERLAP = 50


def _chunks(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP):
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [text]
    out = []
    i = 0
    while i < len(words):
        out.append(" ".join(words[i:i + size]))
        i += size - overlap
    return out


async def main(dry_run: bool):
    from app.config import get_settings
    from app.storage.db import Database
    from app.storage.vectors import VectorStore
    from qdrant_client.models import (Filter, FieldCondition, MatchValue,
                                      PointStruct)

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()
    vs = VectorStore(s.qdrant, embedding_config=s.embedding,
                     google_api_key=os.environ.get("GOOGLE_API_KEY", ""))
    await vs.connect()
    coll = s.qdrant.collection_name

    # 1. Wipe leftover note points (filter by payload.type='note')
    if not dry_run:
        try:
            vs._client.delete(
                collection_name=coll,
                points_selector=Filter(must=[FieldCondition(
                    key="type", match=MatchValue(value="note"),
                )]),
                wait=True,
            )
            print("[reindex_notes] wiped existing note points")
        except Exception as exc:
            print(f"[reindex_notes] wipe warning: {exc}")

    # 2. Pull all notes with non-empty plaintext content
    cur = await db.db.execute(
        "SELECT id, title, content, source, category, subcategory, "
        "tags, created_at FROM notes "
        "WHERE content != '' AND content NOT LIKE 'RkFHRQ%' "
        "ORDER BY id"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    print(f"[reindex_notes] processing {len(rows)} notes\n")

    embedder = vs._get_gemini_embedder()
    points: list[PointStruct] = []
    BATCH = 32
    stats = {"notes_ok": 0, "notes_skip": 0, "chunks": 0, "embed_fail": 0}
    t0 = time.time()

    async def _flush():
        nonlocal points
        if not points or dry_run:
            return
        try:
            vs._client.upsert(collection_name=coll, points=points, wait=True)
        except Exception as e:
            print(f"  [embed_fail flush] {e}")
            stats["embed_fail"] += len(points)
        points = []

    for i, n in enumerate(rows, 1):
        nid = n["id"]
        text = (n.get("content") or "").strip()
        if not text:
            stats["notes_skip"] += 1
            continue

        title = (n.get("title") or "").strip()
        # Glue title in front so embeddings carry the topic.
        body = f"{title}\n\n{text}" if title else text
        chunks = _chunks(body)
        if not chunks:
            stats["notes_skip"] += 1
            continue

        try:
            tags = json.loads(n.get("tags") or "[]")
        except Exception:
            tags = []

        try:
            vectors = embedder.embed_texts(chunks, task_type="RETRIEVAL_DOCUMENT")
        except Exception as e:
            print(f"  [{i}/{len(rows)}] note {nid}: embed FAIL {e}")
            stats["embed_fail"] += 1
            continue

        for ci, (chunk_text, vec) in enumerate(zip(chunks, vectors)):
            pid = str(uuid.uuid5(
                uuid.NAMESPACE_DNS, f"note:{nid}:chunk:{ci}",
            ))
            points.append(PointStruct(
                id=pid,
                vector=vec,
                payload={
                    "type": "note",
                    "note_id": nid,
                    "chunk_index": ci,
                    "text": chunk_text[:5000],
                    "source": n.get("source") or "",
                    "category": n.get("category") or "",
                    "subcategory": n.get("subcategory") or "",
                    "tags": tags,
                    "created_at": n.get("created_at") or "",
                    "title": title[:200],
                },
            ))
            stats["chunks"] += 1

        stats["notes_ok"] += 1

        if not dry_run and stats["notes_ok"] % 10 == 0:
            print(f"  [{i}/{len(rows)}] note {nid}: {len(chunks)} chunks "
                  f"(running total: {stats['chunks']})")

        if len(points) >= BATCH:
            await _flush()

    await _flush()

    if not dry_run:
        # mirror to notes.embedded so the column doesn't lie
        try:
            await db.db.execute(
                "UPDATE notes SET embedded='1' WHERE content != '' "
                "AND content NOT LIKE 'RkFHRQ%'"
            )
            await db.db.execute(
                "UPDATE notes SET embedded='0' WHERE content = '' "
                "OR content LIKE 'RkFHRQ%'"
            )
            await db.db.commit()
        except Exception as e:
            print(f"  [warn] notes.embedded update: {e}")

    print(f"\n=== SUMMARY ({time.time()-t0:.0f}s) ===")
    for k, v in stats.items():
        print(f"  {k:15s} {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
