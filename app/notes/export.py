"""Export Service — CSV/JSON export for notes, facts, and food entries."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta


class ExportService:
    """Export notes data in various formats."""

    def __init__(self, db):
        self.db = db

    def _date_range(self, from_date: str, to_date: str) -> tuple[str, str]:
        """Normalize date range. Defaults: last 30 days."""
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if not from_date:
            from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        return from_date, to_date

    async def export_notes_csv(self, from_date: str = "", to_date: str = "") -> str:
        """Export notes as CSV string."""
        from_date, to_date = self._date_range(from_date, to_date)
        cursor = await self.db.db.execute(
            """SELECT id, created_at, source, status, category, subcategory,
                      user_title, content, raw_content, mood_score, sentiment, energy,
                      is_pinned
               FROM notes
               WHERE created_at >= ? AND created_at < date(?, '+1 day')
               ORDER BY created_at""",
            (from_date, to_date),
        )
        rows = await cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "created_at", "source", "status", "category", "subcategory",
            "title", "content", "raw_content", "mood_score", "sentiment", "energy",
            "is_pinned",
        ])
        for r in rows:
            writer.writerow(list(r))
        return output.getvalue()

    async def export_facts_csv(
        self, metrics: list[str] | None = None,
        from_date: str = "", to_date: str = "",
    ) -> str:
        """Export note facts as CSV string."""
        from_date, to_date = self._date_range(from_date, to_date)
        query = """SELECT f.note_id, n.created_at, f.fact_type, f.key,
                          f.value_num, f.value_text, f.unit, f.date
                   FROM note_facts f
                   JOIN notes n ON f.note_id = n.id
                   WHERE n.created_at >= ? AND n.created_at < date(?, '+1 day')"""
        params: list = [from_date, to_date]
        if metrics:
            placeholders = ",".join("?" * len(metrics))
            query += f" AND f.key IN ({placeholders})"
            params.extend(metrics)
        query += " ORDER BY n.created_at"

        cursor = await self.db.db.execute(query, params)
        rows = await cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "note_id", "date", "fact_type", "key", "value_num", "value_text",
            "unit", "fact_date",
        ])
        for r in rows:
            writer.writerow(list(r))
        return output.getvalue()

    async def export_food_csv(self, from_date: str = "", to_date: str = "") -> str:
        """Export food entries as CSV string."""
        from_date, to_date = self._date_range(from_date, to_date)
        cursor = await self.db.db.execute(
            """SELECT f.id, f.note_id, f.food_name, f.meal_type,
                      f.calories_kcal, f.protein_g, f.fat_g, f.carbs_g,
                      f.quantity, f.consumed_at
               FROM food_entries f
               JOIN notes n ON f.note_id = n.id
               WHERE n.created_at >= ? AND n.created_at < date(?, '+1 day')
               ORDER BY f.consumed_at""",
            (from_date, to_date),
        )
        rows = await cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "note_id", "food_name", "meal_type", "calories_kcal",
            "protein_g", "fat_g", "carbs_g", "quantity", "consumed_at",
        ])
        for r in rows:
            writer.writerow(list(r))
        return output.getvalue()

    async def export_all_json(self, from_date: str = "", to_date: str = "") -> str:
        """Export everything as a single JSON document."""
        from_date, to_date = self._date_range(from_date, to_date)

        # Notes
        cursor = await self.db.db.execute(
            """SELECT * FROM notes
               WHERE created_at >= ? AND created_at < date(?, '+1 day')
               ORDER BY created_at""",
            (from_date, to_date),
        )
        notes = [dict(r) for r in await cursor.fetchall()]

        # Facts
        cursor = await self.db.db.execute(
            """SELECT f.* FROM note_facts f
               JOIN notes n ON f.note_id = n.id
               WHERE n.created_at >= ? AND n.created_at < date(?, '+1 day')""",
            (from_date, to_date),
        )
        facts = [dict(r) for r in await cursor.fetchall()]

        # Food entries
        cursor = await self.db.db.execute(
            """SELECT f.* FROM food_entries f
               JOIN notes n ON f.note_id = n.id
               WHERE n.created_at >= ? AND n.created_at < date(?, '+1 day')""",
            (from_date, to_date),
        )
        food = [dict(r) for r in await cursor.fetchall()]

        # Tasks
        cursor = await self.db.db.execute(
            """SELECT t.* FROM note_tasks t
               JOIN notes n ON t.note_id = n.id
               WHERE n.created_at >= ? AND n.created_at < date(?, '+1 day')""",
            (from_date, to_date),
        )
        tasks = [dict(r) for r in await cursor.fetchall()]

        data = {
            "exported_at": datetime.now().isoformat(),
            "date_range": {"from": from_date, "to": to_date},
            "notes": notes,
            "facts": facts,
            "food_entries": food,
            "tasks": tasks,
        }
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
