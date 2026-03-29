"""Tests for Smart Notes recovery and replay hardening."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.notes.categorizer import NoteCategoryResult
from app.notes.enrichment import NoteEnrichmentService
from app.notes.processor import NoteProcessor
from app.storage.db import Database


@pytest.mark.asyncio
async def test_startup_repair_restores_note_status_from_current_enrichment(tmp_dir):
    """Startup migration should repair stale statuses using current_enrichment_id."""
    db_path = tmp_dir / "notes.db"

    db = Database(db_path)
    await db.connect()

    note_captured = await db.save_note("captured-but-enriched")
    note_processing = await db.save_note("processing-but-enriched")
    note_processing_no_enrichment = await db.save_note("processing-no-enrichment")

    enr1 = await db.save_enrichment(note_captured, category="food", subcategory="meals")
    enr2 = await db.save_enrichment(note_processing, category="health", subcategory="symptoms")

    await db.db.execute("UPDATE notes SET status='captured', current_enrichment_id=? WHERE id=?", (enr1, note_captured))
    await db.db.execute("UPDATE notes SET status='processing', current_enrichment_id=? WHERE id=?", (enr2, note_processing))
    await db.db.execute("UPDATE notes SET status='processing', current_enrichment_id=NULL WHERE id=?", (note_processing_no_enrichment,))
    await db.db.commit()
    await db.close()

    repaired = Database(db_path)
    await repaired.connect()

    note1 = await repaired.get_note(note_captured)
    note2 = await repaired.get_note(note_processing)
    note3 = await repaired.get_note(note_processing_no_enrichment)

    assert note1["status"] == "enriched"
    assert note2["status"] == "enriched"
    assert note3["status"] == "captured"

    captured = await repaired.get_notes_by_status("captured", limit=20)
    assert [n["id"] for n in captured] == [note_processing_no_enrichment]

    await repaired.close()


@pytest.mark.asyncio
async def test_enrichment_cancel_before_save_returns_note_to_captured(db):
    """Cancelled enrichment before save_enrichment should leave note retryable."""
    note_id = await db.save_note("cancel before enrichment save")
    svc = NoteEnrichmentService(db, MagicMock())

    async def _cancel(_text: str):
        raise asyncio.CancelledError()

    svc.categorizer.categorize = _cancel

    with pytest.raises(asyncio.CancelledError):
        await svc.process(note_id)

    note = await db.get_note(note_id)
    assert note["status"] == "captured"
    assert note.get("current_enrichment_id") in (None, "", 0)


@pytest.mark.asyncio
async def test_enrichment_cancel_after_save_restores_note_to_enriched(db):
    """Cancelled enrichment after save_enrichment should recover to enriched."""
    note_id = await db.save_note("cancel after enrichment save")
    svc = NoteEnrichmentService(db, MagicMock())

    async def _result(_text: str):
        return NoteCategoryResult(
            category="food",
            subcategory="meals",
            title="Test note",
            summary="Test summary",
            tags=["food"],
            confidence=0.9,
            structured_data={},
            action_items=[],
        )

    async def _cancel_after_save(_note_id: int, _result: NoteCategoryResult):
        raise asyncio.CancelledError()

    svc.categorizer.categorize = _result
    svc._save_entities = _cancel_after_save

    with pytest.raises(asyncio.CancelledError):
        await svc.process(note_id)

    note = await db.get_note(note_id)
    enrichment = await db.get_latest_enrichment(note_id)

    assert enrichment is not None
    assert note["status"] == "enriched"
    assert note.get("current_enrichment_id") is not None


@pytest.mark.asyncio
async def test_notify_skips_notes_with_preexisting_enrichment(db):
    """Replay-protection should skip second Telegram message for old notes."""
    note_id = await db.save_note("already enriched note")
    await db.save_enrichment(note_id, category="food", subcategory="meals", suggested_title="Old note")
    await db.set_note_status(note_id, "enriched")

    tg_app = MagicMock()
    tg_app.bot.send_message = AsyncMock()

    processor = NoteProcessor(
        db,
        enrichment=MagicMock(),
        relations=MagicMock(),
        projection=MagicMock(vault=None),
        tg_app=tg_app,
    )

    with patch("app.bot.handlers.get_owner_chat_id_async", new=AsyncMock(return_value=12345)):
        await processor._notify_telegram(note_id, had_enrichment_before=True)

    tg_app.bot.send_message.assert_not_called()
