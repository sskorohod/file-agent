"""Tests for Telegram/web reminder actions: create, done, snooze."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
import pytest_asyncio

from app.notes.reminders import ReminderExtractionService


# ---------------------------------------------------------------------------
# Telegram callback action tests
# ---------------------------------------------------------------------------

class TestTelegramReminderActions:
    """Test handle_note_reminder_action callback handler logic."""

    async def _create_note_with_task(self, db, content, task_desc, due_date=""):
        note_id = await db.save_note(content=content, source="telegram")
        task_id = await db.save_task(note_id, task_desc, due_date=due_date)
        return note_id, task_id

    # --- "Remind" button creates reminder ---

    @pytest.mark.asyncio
    async def test_remind_button_creates_reminder(self, db):
        """User clicks 'Remind' on inferred task -> reminder row created."""
        note_id, task_id = await self._create_note_with_task(
            db, "Заказать фильтр до пятницы", "Заказать фильтр", due_date="пятница",
        )

        # Simulate what handle_note_reminder_action does for "create"
        svc = ReminderExtractionService(db)
        count = await svc.create_for_inferred(note_id)
        assert count == 1

        reminders = await db.list_note_reminders()
        assert len(reminders) == 1
        assert reminders[0]["note_id"] == note_id
        assert reminders[0]["source"] == "inferred"
        assert reminders[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_remind_button_no_tasks_with_due(self, db):
        """Remind on note without due-date tasks -> 0 created."""
        note_id = await db.save_note(content="Просто заметка", source="telegram")

        svc = ReminderExtractionService(db)
        count = await svc.create_for_inferred(note_id)
        assert count == 0

    # --- "Done" action ---

    @pytest.mark.asyncio
    async def test_done_action(self, db):
        """User clicks 'Done' -> status='done', completed_at populated."""
        note_id = await db.save_note(content="test", source="web")
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        rem_id = await db.create_note_reminder(note_id, "Buy groceries", past)
        await db.mark_note_reminder_sent(rem_id)

        # Simulate "done" action from handle_note_reminder_action
        await db.complete_note_reminder(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        done = [r for r in reminders if r["id"] == rem_id]
        assert len(done) == 1
        assert done[0]["status"] == "done"
        assert done[0]["completed_at"] is not None

    # --- "Snooze" action ---

    @pytest.mark.asyncio
    async def test_snooze_action(self, db):
        """User clicks 'Snooze 1d' -> remind_at shifted, status back to 'pending'."""
        note_id = await db.save_note(content="test", source="web")
        original_time = datetime.now() - timedelta(hours=1)
        rem_id = await db.create_note_reminder(
            note_id, "Call dentist", original_time.isoformat(),
        )
        await db.mark_note_reminder_sent(rem_id)

        # Simulate "snooze" action (default 24h)
        await db.snooze_note_reminder(rem_id, hours=24)

        reminders = await db.list_note_reminders()
        snoozed = [r for r in reminders if r["id"] == rem_id]
        assert len(snoozed) == 1
        assert snoozed[0]["status"] == "pending"
        assert snoozed[0]["sent_at"] is None  # cleared

        # remind_at should have moved forward
        new_time = datetime.fromisoformat(snoozed[0]["remind_at"])
        assert new_time > original_time

    @pytest.mark.asyncio
    async def test_snooze_makes_reminder_due_again_later(self, db):
        """After snooze, reminder is not immediately due."""
        note_id = await db.save_note(content="test", source="web")
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        rem_id = await db.create_note_reminder(note_id, "Follow up", past)
        await db.mark_note_reminder_sent(rem_id)
        await db.snooze_note_reminder(rem_id, hours=24)

        # Should not appear in due list right away
        due = await db.get_due_note_reminders()
        assert all(r["id"] != rem_id for r in due)


# ---------------------------------------------------------------------------
# Web action tests (route-level logic)
# ---------------------------------------------------------------------------

class TestWebReminderActions:
    """Test DB operations matching web route handlers."""

    @pytest.mark.asyncio
    async def test_web_done_action(self, db):
        """POST /notes/reminders/{id}/done -> complete_note_reminder."""
        note_id = await db.save_note(content="test", source="web")
        rem_id = await db.create_note_reminder(
            note_id, "Pay bill", datetime.now().isoformat(),
        )
        await db.complete_note_reminder(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        r = [x for x in reminders if x["id"] == rem_id][0]
        assert r["status"] == "done"

    @pytest.mark.asyncio
    async def test_web_cancel_action(self, db):
        """POST /notes/reminders/{id}/cancel -> cancel_note_reminder."""
        note_id = await db.save_note(content="test", source="web")
        rem_id = await db.create_note_reminder(
            note_id, "Optional task", datetime.now().isoformat(),
        )
        await db.cancel_note_reminder(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        r = [x for x in reminders if x["id"] == rem_id][0]
        assert r["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_list_excludes_done_by_default(self, db):
        """list_note_reminders(include_done=False) hides done/cancelled."""
        note_id = await db.save_note(content="test", source="web")
        rem1 = await db.create_note_reminder(note_id, "Pending", datetime.now().isoformat())
        rem2 = await db.create_note_reminder(note_id, "Done one", datetime.now().isoformat())
        await db.complete_note_reminder(rem2)

        visible = await db.list_note_reminders(include_done=False)
        ids = [r["id"] for r in visible]
        assert rem1 in ids
        assert rem2 not in ids
