"""Capture Service — instant note saving, never blocks on AI."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class NoteCaptureService:
    """Save notes instantly and enqueue for async enrichment."""

    def __init__(self, db, processor=None):
        self.db = db
        self._processor = processor  # set after processor is created

    def set_processor(self, processor):
        self._processor = processor

    async def capture(
        self, content: str, source: str = "voice",
        title: str = "", content_type: str = "text",
    ) -> int:
        """Save note immediately, enqueue enrichment. Never blocks on LLM."""
        note_id = await self.db.save_note(
            content=content,
            source=source,
            title=title,
            content_type=content_type,
        )
        logger.info(f"Note #{note_id} captured (source={source})")

        # Enqueue for async processing
        await self.enqueue_enrichment(note_id)
        return note_id

    async def enqueue_enrichment(self, note_id: int):
        """Add note_id to processing queue."""
        if self._processor:
            await self._processor.enqueue(note_id)
        else:
            logger.warning(f"No processor available, note #{note_id} will be picked up by DB scan")
