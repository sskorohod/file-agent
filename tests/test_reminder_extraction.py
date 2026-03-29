"""Tests for ReminderExtractionService + DB writes."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from app.notes.reminders import ReminderExtractionService


class TestReminderExtraction:
    """Extraction service: auto-create, inferred, dedup, task linking."""

    @pytest.fixture
    def svc(self, db):
        return ReminderExtractionService(db)

    async def _create_note_with_task(self, db, content, task_desc, due_date=""):
        """Helper: create note + task, return (note_id, task_id)."""
        note_id = await db.save_note(content=content, source="telegram")
        task_id = await db.save_task(note_id, task_desc, due_date=due_date)
        return note_id, task_id

    # --- Auto-create explicit reminder ---

    @pytest.mark.asyncio
    async def test_explicit_creates_reminder(self, db, svc):
        """Note with 'напомни' -> auto-creates reminder."""
        note_id, task_id = await self._create_note_with_task(
            db, "Напомни завтра купить масло", "Купить масло", due_date="завтра",
        )
        count = await svc.extract(note_id)
        assert count == 1

        reminders = await db.list_note_reminders()
        assert len(reminders) == 1
        r = reminders[0]
        assert r["note_id"] == note_id
        assert r["task_id"] == task_id
        assert r["source"] == "explicit"
        assert r["status"] == "pending"
        assert r["confidence"] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_explicit_defaults_tomorrow_when_no_due(self, db, svc):
        """Explicit intent without due phrase -> defaults to tomorrow 09:00."""
        note_id, _ = await self._create_note_with_task(
            db, "Напомни купить молоко", "Купить молоко",
        )
        count = await svc.extract(note_id)
        assert count == 1

        reminders = await db.list_note_reminders()
        remind_at = datetime.fromisoformat(reminders[0]["remind_at"])
        assert remind_at.hour == 9
        assert remind_at.minute == 0

    # --- Inferred task creates no reminder automatically ---

    @pytest.mark.asyncio
    async def test_inferred_no_auto_create(self, db, svc):
        """Note without explicit marker -> no auto-created reminder."""
        note_id, _ = await self._create_note_with_task(
            db, "Заказать фильтр до пятницы", "Заказать фильтр", due_date="пятница",
        )
        count = await svc.extract(note_id)
        assert count == 0

        reminders = await db.list_note_reminders()
        assert len(reminders) == 0

    # --- Reprocess does not duplicate ---

    @pytest.mark.asyncio
    async def test_reprocess_no_duplicate(self, db, svc):
        """Processing same note twice -> no duplicate reminders."""
        note_id, _ = await self._create_note_with_task(
            db, "Напомни купить молоко", "Купить молоко",
        )
        count1 = await svc.extract(note_id)
        count2 = await svc.extract(note_id)
        assert count1 == 1
        assert count2 == 0  # dedup

        reminders = await db.list_note_reminders()
        assert len(reminders) == 1

    # --- Reminder linked to task ---

    @pytest.mark.asyncio
    async def test_reminder_linked_to_task(self, db, svc):
        """Reminder has task_id populated when task match is clear."""
        note_id, task_id = await self._create_note_with_task(
            db, "Не забыть оплатить счёт завтра", "Оплатить счёт", due_date="завтра",
        )
        await svc.extract(note_id)

        reminders = await db.list_note_reminders()
        assert reminders[0]["task_id"] == task_id

    # --- create_for_inferred ---

    @pytest.mark.asyncio
    async def test_create_for_inferred(self, db, svc):
        """create_for_inferred creates reminders for tasks with due_date."""
        note_id, _ = await self._create_note_with_task(
            db, "Заказать масло до среды", "Заказать масло", due_date="среда",
        )
        count = await svc.create_for_inferred(note_id)
        assert count == 1

        reminders = await db.list_note_reminders()
        assert reminders[0]["source"] == "inferred"
        assert reminders[0]["confidence"] == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_create_for_inferred_skips_no_due(self, db, svc):
        """create_for_inferred skips tasks without due_date."""
        note_id, _ = await self._create_note_with_task(
            db, "Потом разберусь", "Разобраться",
        )
        count = await svc.create_for_inferred(note_id)
        assert count == 0

    @pytest.mark.asyncio
    async def test_create_for_inferred_dedup(self, db, svc):
        """create_for_inferred respects dedup (no double-create)."""
        note_id, _ = await self._create_note_with_task(
            db, "Позвонить до пятницы", "Позвонить", due_date="пятница",
        )
        count1 = await svc.create_for_inferred(note_id)
        count2 = await svc.create_for_inferred(note_id)
        assert count1 == 1
        assert count2 == 0

    # --- Edge cases ---

    @pytest.mark.asyncio
    async def test_nonexistent_note(self, db, svc):
        """Extract for missing note_id returns 0."""
        count = await svc.extract(99999)
        assert count == 0

    @pytest.mark.asyncio
    async def test_note_without_tasks(self, db, svc):
        """Note with no tasks -> 0 reminders."""
        note_id = await db.save_note(content="Напомни что-то", source="telegram")
        count = await svc.extract(note_id)
        assert count == 0
