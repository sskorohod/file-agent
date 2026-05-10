"""Re-classify every file in SQLite, refreshing summary / category /
tags / sensitive / owner / display_label using the current LLM prompt.

Lighter than reindex_all.py — does NOT touch Qdrant or rebuild FTS;
re-uses the `extracted_text` already stored in the DB. Use this when
you only changed the classification prompt and want fresh summaries
+ button labels without paying for re-parsing or re-embedding.

Usage:
    .venv/bin/python scripts/reclassify_only.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

for _k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
          "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

ROOT = "/Users/vskorokhod/fag"
sys.path.insert(0, ROOT)
os.chdir(ROOT)


async def main():
    from app.config import get_settings
    from app.storage.db import Database
    from app.skills.engine import SkillEngine
    from app.llm.router import LLMRouter
    from app.llm.classifier import Classifier

    s = get_settings()
    db = Database(s.database.path)
    await db.connect()
    skills = SkillEngine(s.skills.directory)
    await skills.load_all()
    router = LLMRouter(s.llm, db=db)
    classifier = Classifier(llm=router, skills=skills)

    cur = await db.db.execute(
        "SELECT id, original_name, mime_type, extracted_text, metadata_json "
        "FROM files ORDER BY created_at"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    print(f"reclassifying {len(rows)} files (no Qdrant, no FTS)\n")

    ok = 0
    fail = 0
    skip = 0
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        text = (row.get("extracted_text") or "").strip()
        name = row["original_name"]
        if not text:
            print(f"[{i}/{len(rows)}] {name} — skip (no extracted_text)")
            skip += 1
            continue
        try:
            cl = await classifier.classify(
                text=text[:3000], filename=name,
                mime_type=row.get("mime_type", "") or "", language="",
            )
        except Exception as e:
            print(f"[{i}/{len(rows)}] {name} — FAIL: {e}")
            fail += 1
            continue

        # Merge with existing metadata so we don't lose chunks_embedded etc.
        try:
            old = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            old = {}
        old.update({
            "document_type": cl.document_type,
            "owner": cl.owner,
            "display_label": cl.display_label,
            "expiry_date": cl.expiry_date,
            "skill": cl.skill_name,
            "reclassified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        await db.update_file(
            row["id"],
            category=cl.category,
            summary=cl.summary,
            tags=cl.tags,
            sensitive=cl.sensitive,
            metadata_json=old,
        )
        label = cl.display_label or cl.document_type or "—"
        print(f"[{i}/{len(rows)}] {name[:32]:<32}  →  {cl.category:8s}  «{label}»")
        ok += 1

    # Wipe search cache
    try:
        await db.db.execute("DELETE FROM search_cache")
        await db.db.commit()
    except Exception:
        pass

    print(f"\nDone in {time.time()-t0:.0f}s — ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    asyncio.run(main())
