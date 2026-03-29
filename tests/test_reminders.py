"""Tests for Reminder System v1 — parsing, extraction, DB queries, actions."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from app.notes.reminders import (
    parse_due_datetime,
    has_explicit_reminder_intent,
    ReminderExtractionService,
)


# ---------------------------------------------------------------------------
# Group 1: Due-date parsing (pure functions)
# ---------------------------------------------------------------------------

class TestParseDueDatetime:
    """Test natural language due-date parser."""

    # Reference: Wednesday 2026-03-25 at 14:00
    REF = datetime(2026, 3, 25, 14, 0, 0)

    def test_empty_returns_none(self):
        assert parse_due_datetime("", self.REF) is None
        assert parse_due_datetime(None, self.REF) is None

    def test_today(self):
        result = parse_due_datetime("сегодня", self.REF)
        assert result == self.REF.replace(hour=18, minute=0, second=0, microsecond=0)

    def test_tomorrow(self):
        result = parse_due_datetime("завтра", self.REF)
        expected = (self.REF + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_day_after_tomorrow(self):
        result = parse_due_datetime("послезавтра", self.REF)
        expected = (self.REF + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_in_n_days(self):
        result = parse_due_datetime("через 3 дня", self.REF)
        expected = (self.REF + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_in_n_hours(self):
        result = parse_due_datetime("через 2 часа", self.REF)
        expected = self.REF + timedelta(hours=2)
        assert result == expected

    def test_in_week(self):
        result = parse_due_datetime("через неделю", self.REF)
        expected = (self.REF + timedelta(weeks=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_weekday_forward(self):
        # REF is Wednesday (2). "в пятницу" → Friday = weekday 4 → +2 days
        result = parse_due_datetime("в пятницу", self.REF)
        assert result is not None
        assert result.weekday() == 4  # Friday
        assert result.hour == 9

    def test_weekday_before(self):
        # "до пятницы" → Thursday (day before Friday) at 18:00
        result = parse_due_datetime("до пятницы", self.REF)
        assert result is not None
        assert result.weekday() == 3  # Thursday
        assert result.hour == 18

    def test_weekday_same_day_wraps(self):
        # REF is Wednesday (2026-03-25). "в среду" → next Wednesday (2026-04-01)
        result = parse_due_datetime("в среду", self.REF)
        assert result is not None
        assert result.weekday() == 2  # Wednesday
        assert result.date() == (self.REF + timedelta(days=7)).date()

    def test_iso_date(self):
        result = parse_due_datetime("2026-04-05", self.REF)
        assert result == datetime(2026, 4, 5, 9, 0, 0)

    def test_unknown_returns_none(self):
        assert parse_due_datetime("когда-нибудь", self.REF) is None
        assert parse_due_datetime("потом", self.REF) is None


# ---------------------------------------------------------------------------
# Group 2: Explicit reminder intent detection
# ---------------------------------------------------------------------------

class TestExplicitIntent:

    def test_explicit_markers(self):
        assert has_explicit_reminder_intent("Напомни завтра купить масло")
        assert has_explicit_reminder_intent("Не забыть записаться к врачу")
        assert has_explicit_reminder_intent("Поставь напоминание на среду")

    def test_inferred_no_markers(self):
        assert not has_explicit_reminder_intent("Заказать фильтр до пятницы")
        assert not has_explicit_reminder_intent("Нужно купить масло")

    def test_vague_no_markers(self):
        assert not has_explicit_reminder_intent("Надо бы потом купить бананы")
        assert not has_explicit_reminder_intent("Когда-нибудь разобраться")


# ---------------------------------------------------------------------------
# Group 3: Extraction service (async, uses DB)
# ---------------------------------------------------------------------------

class TestReminderExtraction:

    @pytest.fixture
    def svc(self, db):
        return ReminderExtractionService(db)

    async def _create_note_with_task(self, db, content, task_desc, due_date=""):
        """Helper: create note + task, return (note_id, task_id)."""
        note_id = await db.save_note(content=content, source="telegram")
        task_id = await db.save_task(note_id, task_desc, due_date=due_date)
        return note_id, task_id

    @pytest.mark.asyncio
    async def test_explicit_creates_reminder(self, db, svc):
        """Note with 'напомни' → auto-creates reminder."""
        note_id, task_id = await self._create_note_with_task(
            db, "Напомни завтра купить масло", "Купить масло", due_date="завтра",
        )
        count = await svc.extract(note_id)
        assert count == 1

        reminders = await db.list_note_reminders()
        assert len(reminders) == 1
        assert reminders[0]["note_id"] == note_id
        assert reminders[0]["task_id"] == task_id
        assert reminders[0]["source"] == "explicit"
        assert reminders[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_inferred_no_auto_create(self, db, svc):
        """Note without explicit marker → no auto-created reminder."""
        note_id, _ = await self._create_note_with_task(
            db, "Заказать фильтр до пятницы", "Заказать фильтр", due_date="пятница",
        )
        count = await svc.extract(note_id)
        assert count == 0

        reminders = await db.list_note_reminders()
        assert len(reminders) == 0

    @pytest.mark.asyncio
    async def test_reprocess_no_duplicate(self, db, svc):
        """Processing same note twice → no duplicate reminders."""
        note_id, _ = await self._create_note_with_task(
            db, "Напомни купить молоко", "Купить молоко",
        )
        count1 = await svc.extract(note_id)
        count2 = await svc.extract(note_id)
        assert count1 == 1
        assert count2 == 0  # dedup

        reminders = await db.list_note_reminders()
        assert len(reminders) == 1

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

    @pytest.mark.asyncio
    async def test_create_for_inferred_skips_no_due(self, db, svc):
        """create_for_inferred skips tasks without due_date."""
        note_id, _ = await self._create_note_with_task(
            db, "Потом разберусь", "Разобраться",
        )
        count = await svc.create_for_inferred(note_id)
        assert count == 0


# ---------------------------------------------------------------------------
# Group 4: DB queries and actions
# ---------------------------------------------------------------------------

class TestReminderDBActions:

    @pytest.mark.asyncio
    async def test_get_due_finds_past(self, db):
        """Reminder with remind_at in the past → found by get_due_note_reminders."""
        note_id = await db.save_note(content="test", source="web")
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        await db.create_note_reminder(note_id, "Test reminder", past)

        due = await db.get_due_note_reminders()
        assert len(due) == 1
        assert due[0]["description"] == "Test reminder"

    @pytest.mark.asyncio
    async def test_get_due_ignores_future(self, db):
        """Reminder in the future → not returned."""
        note_id = await db.save_note(content="test", source="web")
        future = (datetime.now() + timedelta(days=1)).isoformat()
        await db.create_note_reminder(note_id, "Future reminder", future)

        due = await db.get_due_note_reminders()
        assert len(due) == 0

    @pytest.mark.asyncio
    async def test_complete_reminder(self, db):
        """Complete sets status=done and completed_at."""
        note_id = await db.save_note(content="test", source="web")
        rem_id = await db.create_note_reminder(note_id, "Test", datetime.now().isoformat())
        await db.complete_note_reminder(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        done = [r for r in reminders if r["id"] == rem_id]
        assert len(done) == 1
        assert done[0]["status"] == "done"
        assert done[0]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_cancel_reminder(self, db):
        """Cancel sets status=cancelled."""
        note_id = await db.save_note(content="test", source="web")
        rem_id = await db.create_note_reminder(note_id, "Test", datetime.now().isoformat())
        await db.cancel_note_reminder(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        cancelled = [r for r in reminders if r["id"] == rem_id]
        assert cancelled[0]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_snooze_shifts_time(self, db):
        """Snooze resets to pending and shifts remind_at."""
        note_id = await db.save_note(content="test", source="web")
        original = datetime.now().isoformat()
        rem_id = await db.create_note_reminder(note_id, "Test", original)
        await db.mark_note_reminder_sent(rem_id)
        await db.snooze_note_reminder(rem_id, hours=24)

        reminders = await db.list_note_reminders()
        snoozed = [r for r in reminders if r["id"] == rem_id]
        assert len(snoozed) == 1
        assert snoozed[0]["status"] == "pending"
        assert snoozed[0]["sent_at"] is None  # cleared
