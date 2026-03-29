"""Tests for reminder scheduler delivery loop and status transitions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


class TestReminderScheduler:
    """Test _note_reminder_loop: sends due, skips future, handles errors."""

    async def _insert_reminder(self, db, description, remind_at, status="pending"):
        """Helper: create note + reminder, return reminder_id."""
        note_id = await db.save_note(content="test note", source="web")
        rem_id = await db.create_note_reminder(
            note_id, description, remind_at.isoformat(),
        )
        return rem_id, note_id

    def _make_tg_app(self):
        """Create a mock Telegram Application with bot.send_message."""
        tg_app = MagicMock()
        tg_app.bot = MagicMock()
        tg_app.bot.send_message = AsyncMock()
        return tg_app

    @pytest.mark.asyncio
    async def test_sends_due_pending_reminders(self, db):
        """Reminder with remind_at <= now -> found by get_due, mark_sent transitions state."""
        past = datetime.now() - timedelta(hours=1)
        rem_id, _ = await self._insert_reminder(db, "Buy milk", past)

        tg_app = self._make_tg_app()

        # Simulate one iteration of _note_reminder_loop
        due = await db.get_due_note_reminders()
        assert len(due) == 1
        assert due[0]["description"] == "Buy milk"

        for r in due:
            # Simulate Telegram send
            await tg_app.bot.send_message(
                chat_id=12345,
                text=f"Reminder: {r['description']}",
            )
            await db.mark_note_reminder_sent(r["id"])

        # Verify send was called
        tg_app.bot.send_message.assert_called_once()

        # Verify status transition
        reminders = await db.list_note_reminders(include_done=True)
        sent = [r for r in reminders if r["id"] == rem_id]
        assert sent[0]["status"] == "sent"
        assert sent[0]["sent_at"] is not None

    @pytest.mark.asyncio
    async def test_does_not_send_future_reminders(self, db):
        """Reminder with remind_at > now -> not returned by get_due, no send."""
        future = datetime.now() + timedelta(days=1)
        await self._insert_reminder(db, "Future task", future)

        due = await db.get_due_note_reminders()
        assert len(due) == 0

    @pytest.mark.asyncio
    async def test_send_failure_does_not_corrupt_state(self, db):
        """If Telegram send raises, reminder stays 'pending'."""
        past = datetime.now() - timedelta(hours=1)
        rem_id, _ = await self._insert_reminder(db, "Failing reminder", past)

        tg_app = self._make_tg_app()
        tg_app.bot.send_message.side_effect = Exception("Network error")

        due = await db.get_due_note_reminders()
        for r in due:
            try:
                await tg_app.bot.send_message(chat_id=12345, text="test")
                await db.mark_note_reminder_sent(r["id"])
            except Exception:
                pass  # Same as _note_reminder_loop: logs warning, does not mark sent

        # Reminder should still be pending (mark_sent was not called)
        reminders = await db.list_note_reminders()
        pending = [r for r in reminders if r["id"] == rem_id]
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_no_duplicate_send(self, db):
        """Already 'sent' reminder -> not returned by get_due."""
        past = datetime.now() - timedelta(hours=1)
        rem_id, _ = await self._insert_reminder(db, "Already sent", past)
        await db.mark_note_reminder_sent(rem_id)

        due = await db.get_due_note_reminders()
        assert len(due) == 0

    @pytest.mark.asyncio
    async def test_mark_sent_populates_sent_at(self, db):
        """mark_note_reminder_sent sets status='sent' and sent_at."""
        past = datetime.now() - timedelta(minutes=30)
        rem_id, _ = await self._insert_reminder(db, "Check sent_at", past)
        await db.mark_note_reminder_sent(rem_id)

        reminders = await db.list_note_reminders(include_done=True)
        r = [x for x in reminders if x["id"] == rem_id][0]
        assert r["status"] == "sent"
        assert r["sent_at"] is not None

    @pytest.mark.asyncio
    async def test_only_pending_are_due(self, db):
        """get_due only returns pending, not done/cancelled."""
        past = datetime.now() - timedelta(hours=1)

        rem1, _ = await self._insert_reminder(db, "Done one", past)
        await db.complete_note_reminder(rem1)

        rem2, _ = await self._insert_reminder(db, "Cancelled one", past)
        await db.cancel_note_reminder(rem2)

        rem3, _ = await self._insert_reminder(db, "Pending one", past)

        due = await db.get_due_note_reminders()
        assert len(due) == 1
        assert due[0]["id"] == rem3
