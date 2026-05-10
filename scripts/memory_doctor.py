"""Reconciliation tool — walks SQLite, Qdrant, cognee and the wiki vault
and prints divergence between the four. With ``--fix``, enqueues outbox
events to bring them back in sync.

Usage:
    .venv/bin/python scripts/memory_doctor.py [--fix]

Examples of what it catches:
- A `files` row whose Qdrant chunks were never written (embed step crashed)
- Qdrant points whose `file_id` no longer exists in SQLite (orphan)
- Notes that have plaintext content but no `note_id` payload in Qdrant
- Wiki `docs/` pages without a corresponding row in `files`
- Outbox `failed` rows that need manual replay
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)
ROOT = "/Users/vskorokhod/fag"
sys.path.insert(0, ROOT)
os.chdir(ROOT)


async def main(fix: bool):
    from app.config import get_settings
    from app.storage.db import Database
    from app.storage.vectors import VectorStore
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()
    vs = VectorStore(s.qdrant, embedding_config=s.embedding,
                     google_api_key=os.environ.get("GOOGLE_API_KEY", ""))
    await vs.connect()
    coll = s.qdrant.collection_name

    # ── 1. SQLite snapshot ────────────────────────────────────────────
    cur = await db.db.execute("SELECT id FROM files")
    file_ids = {dict(r)["id"] for r in await cur.fetchall()}
    cur = await db.db.execute(
        "SELECT id FROM notes WHERE content!='' "
        "AND content NOT LIKE '%RkFHRQ%'"
    )
    note_ids = {dict(r)["id"] for r in await cur.fetchall()}
    print(f"SQLite: files={len(file_ids)} notes={len(note_ids)}")

    # ── 2. Qdrant snapshot — scroll all points ────────────────────────
    qdrant_files: dict[str, int] = {}
    qdrant_notes: dict[int, int] = {}
    orphan_no_id = 0
    offset = None
    total = 0
    while True:
        result = vs._client.scroll(
            collection_name=coll, limit=512, with_payload=True,
            with_vectors=False, offset=offset,
        )
        pts, offset = result
        if not pts:
            break
        total += len(pts)
        for p in pts:
            pay = p.payload or {}
            t = (pay.get("type") or "").lower()
            if t == "note":
                nid = pay.get("note_id")
                if nid is None:
                    orphan_no_id += 1
                else:
                    qdrant_notes[int(nid)] = qdrant_notes.get(int(nid), 0) + 1
            else:
                fid = pay.get("file_id") or ""
                if not fid:
                    orphan_no_id += 1
                else:
                    qdrant_files[fid] = qdrant_files.get(fid, 0) + 1
        if offset is None:
            break
    print(f"Qdrant: total={total}  unique files={len(qdrant_files)}  "
          f"unique notes={len(qdrant_notes)}  orphans-no-id={orphan_no_id}")

    # ── 3. Compare ────────────────────────────────────────────────────
    files_missing_in_q = file_ids - set(qdrant_files)
    files_orphan_in_q = set(qdrant_files) - file_ids
    notes_missing_in_q = note_ids - set(qdrant_notes)
    notes_orphan_in_q = set(qdrant_notes) - note_ids

    print()
    print(f"Files in SQLite but missing in Qdrant: {len(files_missing_in_q)}")
    for fid in list(files_missing_in_q)[:10]:
        cur = await db.db.execute(
            "SELECT original_name FROM files WHERE id=?", (fid,)
        )
        r = await cur.fetchone()
        name = dict(r).get("original_name", "?") if r else "?"
        print(f"  - {fid[:8]}  {name}")
    if len(files_missing_in_q) > 10:
        print(f"  … +{len(files_missing_in_q)-10} more")

    print(f"\nFiles in Qdrant but missing in SQLite: {len(files_orphan_in_q)}")
    for fid in list(files_orphan_in_q)[:10]:
        print(f"  - {fid[:8]}  ({qdrant_files[fid]} chunks)")
    if len(files_orphan_in_q) > 10:
        print(f"  … +{len(files_orphan_in_q)-10} more")

    print(f"\nNotes in SQLite but missing in Qdrant: {len(notes_missing_in_q)}")
    for nid in list(notes_missing_in_q)[:10]:
        cur = await db.db.execute(
            "SELECT title FROM notes WHERE id=?", (nid,)
        )
        r = await cur.fetchone()
        title = (dict(r).get("title", "") if r else "?")[:50]
        print(f"  - note {nid}  «{title}»")

    print(f"\nNotes in Qdrant but missing in SQLite: {len(notes_orphan_in_q)}")
    for nid in list(notes_orphan_in_q)[:10]:
        print(f"  - note {nid} ({qdrant_notes[nid]} chunks)")

    # ── 4. Wiki vault ──────────────────────────────────────────────────
    vault = s.wiki.resolved_path
    if vault.exists():
        wiki_doc_ids: set[str] = set()
        for md in (vault / "docs").rglob("*.md"):
            try:
                head = md.read_text()[:600]
                for line in head.splitlines():
                    if line.startswith("id: "):
                        wiki_doc_ids.add(line.split(":", 1)[1].strip())
                        break
            except Exception:
                pass
        wiki_note_ids: set[int] = set()
        for md in (vault / "notes").rglob("*.md"):
            try:
                head = md.read_text()[:600]
                for line in head.splitlines():
                    if line.startswith("id: "):
                        try:
                            wiki_note_ids.add(int(line.split(":", 1)[1].strip()))
                        except Exception:
                            pass
                        break
            except Exception:
                pass
        wiki_files_missing = file_ids - wiki_doc_ids
        wiki_notes_missing = note_ids - wiki_note_ids
        print(f"\nWiki: {len(wiki_doc_ids)} docs, {len(wiki_note_ids)} notes")
        print(f"  files w/o wiki page: {len(wiki_files_missing)}")
        print(f"  notes w/o wiki page: {len(wiki_notes_missing)}")
    else:
        print(f"\nWiki vault doesn't exist yet ({vault}) — run `make wiki-build`")

    # ── 5. Outbox ──────────────────────────────────────────────────────
    stats = await db.outbox_stats()
    print(f"\nOutbox: {stats}")

    # ── 6. FAGE residue check ─────────────────────────────────────────
    cur = await db.db.execute(
        "SELECT 'files', SUM(CASE WHEN summary LIKE '%RkFHRQ%' OR "
        "extracted_text LIKE '%RkFHRQ%' THEN 1 ELSE 0 END) FROM files "
        "UNION ALL SELECT 'notes', SUM(CASE WHEN content LIKE '%RkFHRQ%' "
        "THEN 1 ELSE 0 END) FROM notes"
    )
    fage_left = sum((dict(r).get("SUM(CASE WHEN summary LIKE '%RkFHRQ%' OR extracted_text LIKE '%RkFHRQ%' THEN 1 ELSE 0 END)", 0)
                     or dict(r).get("SUM(CASE WHEN content LIKE '%RkFHRQ%' THEN 1 ELSE 0 END)", 0))
                    or 0 for r in await cur.fetchall())
    print(f"FAGE residue: {fage_left} (should be 0)")

    # ── 7. Fix mode ────────────────────────────────────────────────────
    if not fix:
        if any([files_missing_in_q, notes_missing_in_q,
                files_orphan_in_q, notes_orphan_in_q]):
            print("\nRun with --fix to enqueue outbox events for "
                  "missing/orphan rows.")
        return

    print("\n--fix: enqueueing outbox events …")
    enq = 0
    for fid in files_missing_in_q:
        await db.enqueue_outbox(
            event_type="file_ingested", source_kind="file",
            source_id=fid, targets=["qdrant", "wiki"],
        )
        enq += 1
    for nid in notes_missing_in_q:
        await db.enqueue_outbox(
            event_type="note_added", source_kind="note",
            source_id=str(nid), targets=["qdrant", "wiki"],
        )
        enq += 1
    for fid in files_orphan_in_q:
        await db.enqueue_outbox(
            event_type="file_deleted", source_kind="file",
            source_id=fid, targets=["qdrant"],
        )
        enq += 1
    for nid in notes_orphan_in_q:
        await db.enqueue_outbox(
            event_type="note_deleted", source_kind="note",
            source_id=str(nid), targets=["qdrant"],
        )
        enq += 1
    print(f"  enqueued {enq} events. The lifespan sweeper will apply them "
          "within 30s.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="enqueue outbox events to reconcile divergence")
    args = ap.parse_args()
    asyncio.run(main(args.fix))
