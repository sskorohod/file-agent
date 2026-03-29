"""Habit Tracking — define habits, auto-detect completion, track streaks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Mapping of metric_key → fact key in note_facts / special check
METRIC_CHECKS = {
    "calories": "calories",
    "sleep_hours": "sleep_hours",
    "weight_kg": "weight_kg",
    "exercise_min": "exercise_min",
    "mood_score": "mood_score",
    "water_ml": "water_ml",
    "steps": "steps",
}


class HabitTracker:
    """Track habits via auto-detection from note_facts and food_entries."""

    def __init__(self, db):
        self.db = db

    async def create_habit(
        self, name: str, frequency: str = "daily",
        target_value: float = 1, metric_key: str = "",
        description: str = "",
    ) -> int:
        cursor = await self.db.db.execute(
            """INSERT INTO habits (name, description, frequency, target_value, metric_key)
               VALUES (?, ?, ?, ?, ?)""",
            (name, description, frequency, target_value, metric_key),
        )
        await self.db.db.commit()
        return cursor.lastrowid

    async def list_habits(self, active_only: bool = True) -> list[dict]:
        where = "WHERE active=1" if active_only else ""
        cursor = await self.db.db.execute(
            f"SELECT * FROM habits {where} ORDER BY created_at",
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_habit(self, habit_id: int):
        await self.db.db.execute("DELETE FROM habits WHERE id=?", (habit_id,))
        await self.db.db.commit()

    async def check_habits_for_date(self, date: str) -> list[dict]:
        """Auto-detect habit completion for a given date. Returns habit statuses."""
        habits = await self.list_habits()
        results = []

        for h in habits:
            completed = False
            value = 0.0

            if h["metric_key"] == "food_log":
                # Special: check if any food entries exist
                entries = await self.db.get_food_entries_by_date(date)
                completed = len(entries) > 0
                value = len(entries)
            elif h["metric_key"] == "note_any":
                # Special: check if any notes exist
                notes = await self.db.get_daily_notes(date)
                completed = len(notes) > 0
                value = len(notes)
            elif h["metric_key"] in METRIC_CHECKS:
                # Check note_facts for this metric
                fact_key = METRIC_CHECKS[h["metric_key"]]
                cursor = await self.db.db.execute(
                    "SELECT AVG(value_num), MAX(value_num) FROM note_facts WHERE key=? AND date=? AND value_num IS NOT NULL",
                    (fact_key, date),
                )
                row = await cursor.fetchone()
                if row and row[0] is not None:
                    value = row[0]
                    completed = value >= h["target_value"]
            elif h["metric_key"].startswith("category:"):
                # Check if notes exist in this category
                cat = h["metric_key"].split(":", 1)[1]
                cursor = await self.db.db.execute(
                    "SELECT COUNT(*) FROM notes WHERE category=? AND created_at LIKE ?",
                    (cat, f"{date}%"),
                )
                row = await cursor.fetchone()
                value = row[0] if row else 0
                completed = value >= h["target_value"]

            # Save entry (upsert)
            await self.db.db.execute(
                """INSERT INTO habit_entries (habit_id, date, completed, value, auto_detected)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(habit_id, date) DO UPDATE SET
                     completed=excluded.completed, value=excluded.value, auto_detected=1""",
                (h["id"], date, int(completed), value),
            )

            results.append({
                **h,
                "completed": completed,
                "value": value,
                "streak": await self.get_streak(h["id"], date),
            })

        await self.db.db.commit()
        return results

    async def get_streak(self, habit_id: int, from_date: str = "") -> int:
        """Count consecutive completed days ending at from_date."""
        if not from_date:
            from_date = datetime.now().strftime("%Y-%m-%d")

        streak = 0
        current = datetime.strptime(from_date, "%Y-%m-%d")

        for _ in range(365):
            date_str = current.strftime("%Y-%m-%d")
            cursor = await self.db.db.execute(
                "SELECT completed FROM habit_entries WHERE habit_id=? AND date=?",
                (habit_id, date_str),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                streak += 1
            else:
                break
            current -= timedelta(days=1)

        return streak

    async def get_habit_history(self, habit_id: int, days: int = 30) -> list[dict]:
        """Get habit entries for the last N days."""
        cursor = await self.db.db.execute(
            """SELECT * FROM habit_entries
               WHERE habit_id=? AND date >= date('now', ?)
               ORDER BY date DESC""",
            (habit_id, f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def toggle_habit_entry(self, habit_id: int, date: str) -> bool:
        """Manually toggle habit completion for a date."""
        cursor = await self.db.db.execute(
            "SELECT completed FROM habit_entries WHERE habit_id=? AND date=?",
            (habit_id, date),
        )
        row = await cursor.fetchone()
        new_val = 0 if (row and row[0]) else 1

        await self.db.db.execute(
            """INSERT INTO habit_entries (habit_id, date, completed, auto_detected)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(habit_id, date) DO UPDATE SET completed=?, auto_detected=0""",
            (habit_id, date, new_val, new_val),
        )
        await self.db.db.commit()
        return bool(new_val)
