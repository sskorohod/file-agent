"""Full reindex: drop Qdrant collection, re-parse + re-classify + re-embed
every file in SQLite. Run after major changes to extraction or summary
prompts so the archive matches the new algorithm everywhere.

Usage:
    .venv/bin/python scripts/reindex_all.py [--dry-run] [--id <fid>]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Strip shell-injected proxy/key vars before loading .env
for _k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
          "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

ROOT = Path("/Users/vskorokhod/fag")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


async def main(dry_run: bool, only_id: str | None):
    from app.config import get_settings
    from app.storage.db import Database
    from app.storage.files import FileStorage
    from app.storage.vectors import VectorStore
    from app.skills.engine import SkillEngine
    from app.llm.router import LLMRouter
    from app.llm.classifier import Classifier
    from app.parser.factory import ParserFactory
    from app.utils.crypto import load_or_create_system_key, decrypt_bytes, is_encrypted_blob

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()

    storage = FileStorage(
        base_path=s.storage.base_path,
        allowed_extensions=s.storage.allowed_extensions,
    )
    vs = VectorStore(s.qdrant, embedding_config=s.embedding,
                     google_api_key=os.environ.get("GOOGLE_API_KEY", ""))
    await vs.connect()
    skills = SkillEngine(s.skills.directory)
    await skills.load_all()
    router = LLMRouter(s.llm, db=db)
    classifier = Classifier(llm=router, skills=skills)
    parser = ParserFactory()

    try:
        system_key = load_or_create_system_key()
    except Exception as e:
        print(f"[!] system_key: {e}; sensitive files will be skipped")
        system_key = None

    # ── 1. Wipe Qdrant collection (recreate with same schema) ────────────
    if not dry_run and only_id is None:
        print("[reindex] dropping Qdrant collection file_agent_v2 …")
        try:
            vs._client.delete_collection(s.qdrant.collection_name)
        except Exception as e:
            print(f"   warning: {e}")
        # Recreate via VectorStore.connect — which auto-creates if missing
        # We already called connect() above; need to re-trigger creation.
        from qdrant_client.models import Distance, VectorParams
        vs._client.create_collection(
            collection_name=s.qdrant.collection_name,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )
        print("[reindex] qdrant collection recreated (768-dim cosine)")

    # ── 2. For every file: read → parse → classify → update → embed ──────
    if only_id:
        rows = [{"id": only_id}]
    else:
        cur = await db.db.execute("SELECT id FROM files ORDER BY created_at")
        rows = [dict(r) for r in await cur.fetchall()]
    print(f"[reindex] processing {len(rows)} files\n")

    stats = {"ok": 0, "no_text": 0, "no_disk": 0, "decrypt_fail": 0,
             "classify_fail": 0, "embed_fail": 0}
    t0 = time.time()

    for i, row in enumerate(rows, 1):
        # Re-fetch full row
        f = await db.get_file(row["id"])
        if not f:
            continue
        fid = f["id"]
        name = f["original_name"]
        path = Path(f["stored_path"]) if f.get("stored_path") else None
        was_sensitive = bool(f.get("sensitive"))
        print(f"[{i}/{len(rows)}] {name}  ({fid[:8]})", flush=True)

        if not path or not path.exists():
            print("   [no_disk] file missing on disk")
            stats["no_disk"] += 1
            continue

        # Read + decrypt if FAGB
        try:
            raw = path.read_bytes()
            if is_encrypted_blob(raw):
                if system_key is None:
                    print("   [decrypt_fail] FAGB blob, no system_key")
                    stats["decrypt_fail"] += 1
                    continue
                plain = decrypt_bytes(raw, system_key)
            else:
                plain = raw
        except Exception as e:
            print(f"   [decrypt_fail] {e}")
            stats["decrypt_fail"] += 1
            continue

        # Parse — write decrypted bytes to a tmp file because parser expects path
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=path.suffix, delete=False) as tmp:
            tmp.write(plain)
            tmp_path = Path(tmp.name)
        try:
            parsed = await parser.parse(tmp_path)
        except Exception as e:
            print(f"   [parse_fail] {e}")
            tmp_path.unlink(missing_ok=True)
            stats["classify_fail"] += 1
            continue
        finally:
            tmp_path.unlink(missing_ok=True)

        text = (parsed.text or "").strip()
        if not text:
            print("   [no_text] parser returned empty; skipping classify+embed")
            stats["no_text"] += 1
            continue

        # Classify
        try:
            cl = await classifier.classify(
                text=text[:3000], filename=name,
                mime_type=f.get("mime_type", "") or "", language="",
            )
        except Exception as e:
            print(f"   [classify_fail] {e}")
            stats["classify_fail"] += 1
            continue

        # Persist updates
        if not dry_run:
            await db.update_file(
                fid,
                category=cl.category,
                summary=cl.summary,
                tags=cl.tags,
                extracted_text=text,
                sensitive=cl.sensitive,
                metadata_json={
                    "document_type": cl.document_type,
                    "language": parsed.language,
                    "parser": parsed.parser_used,
                    "pages": parsed.pages,
                    "expiry_date": cl.expiry_date,
                    "skill": cl.skill_name,
                    "owner": cl.owner,
                    "display_label": cl.display_label,
                    "reindexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

        # Re-embed: text chunks + multimodal where applicable
        if not dry_run:
            try:
                await vs.upsert_document(
                    file_id=fid,
                    text=text,
                    file_bytes=plain,
                    mime_type=f.get("mime_type", "") or "",
                    metadata={
                        "filename": name,
                        "original_name": name,
                        "category": cl.category,
                        "document_type": cl.document_type or "",
                        "tags": cl.tags,
                        "summary": cl.summary or "",
                    },
                )
            except Exception as e:
                print(f"   [embed_fail] {e}")
                stats["embed_fail"] += 1
                continue

        print(f"   ✓ {cl.category} (sens={cl.sensitive}) :: {cl.summary[:80]}")
        stats["ok"] += 1

    elapsed = time.time() - t0

    # ── 3. Wipe search_cache + rebuild FTS ───────────────────────────────
    if not dry_run and only_id is None:
        await db.db.execute("DELETE FROM search_cache")
        # Rebuild FTS to match fresh extracted_text/summary/tags
        try:
            await db.db.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
            print("\n[reindex] FTS rebuilt")
        except Exception as e:
            print(f"\n[reindex] FTS rebuild warning: {e}")
        await db.db.commit()

    print()
    print("=" * 60)
    print(f"REINDEX SUMMARY ({elapsed:.0f}s)")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:15s} {v}")
    print(f"  total          {sum(stats.values())} of {len(rows)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + classify, but don't write to DB or Qdrant")
    ap.add_argument("--id", default=None,
                    help="only reindex this single file_id (skip Qdrant wipe)")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run, args.id))
