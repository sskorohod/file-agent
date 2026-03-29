#!/usr/bin/env python3
"""Backfill document_date from extracted_fields in existing metadata_json."""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


DATE_KEYS = ("date", "issue_date", "date_of_service", "measurement_date")
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y")


def extract_date(fields: dict) -> str | None:
    for key in DATE_KEYS:
        val = fields.get(key)
        if not val or not isinstance(val, str):
            continue
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        try:
            datetime.fromisoformat(val)
            return val[:10]
        except ValueError:
            continue
    return None


async def main():
    from app.config import get_settings
    settings = get_settings()
    db_path = settings.storage.resolved_path / "fileagent.db"

    import aiosqlite
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, metadata_json FROM files WHERE document_date IS NULL"
        )
        rows = await cursor.fetchall()
        updated = 0
        for row in rows:
            try:
                meta = json.loads(row["metadata_json"] or "{}")
                fields = meta.get("extracted_fields", {})
                doc_date = extract_date(fields)
                if doc_date:
                    await db.execute(
                        "UPDATE files SET document_date=? WHERE id=?",
                        (doc_date, row["id"]),
                    )
                    updated += 1
            except Exception as e:
                print(f"  Skip {row['id'][:12]}: {e}")
        await db.commit()
        print(f"Backfilled document_date for {updated}/{len(rows)} files")


if __name__ == "__main__":
    asyncio.run(main())
