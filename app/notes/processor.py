"""Note Processor — coordinates enrichment, relations, and projection pipeline."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class NoteProcessor:
    """Async processor: drains queue, falls back to DB scan, processes one at a time."""

    def __init__(self, db, enrichment, relations, projection, tg_app=None):
        self.db = db
        self.enrichment = enrichment
        self.relations = relations
        self.projection = projection
        self.tg_app = tg_app
        # Legacy compat: code that accesses note_agent.vault
        self.vault = projection.vault if projection else None
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._in_progress: set[int] = set()

    async def enqueue(self, note_id: int):
        """Add note to processing queue. Ignores duplicates."""
        if note_id not in self._in_progress:
            await self._queue.put(note_id)

    async def run(self):
        """Main loop: drain queue → fallback DB scan → process → sleep."""
        logger.info("NoteProcessor started")
        await asyncio.sleep(5)  # initial delay

        while True:
            try:
                batch = self._drain_queue()
                if not batch:
                    batch = await self._scan_db()

                if batch:
                    for note_id in batch:
                        await self._process_one(note_id)
                else:
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"NoteProcessor error: {e}")
                await asyncio.sleep(10)

    def _drain_queue(self) -> list[int]:
        """Get all items from queue without blocking."""
        items = []
        seen = set()
        while not self._queue.empty():
            try:
                note_id = self._queue.get_nowait()
                if note_id not in seen and note_id not in self._in_progress:
                    items.append(note_id)
                    seen.add(note_id)
            except asyncio.QueueEmpty:
                break
        return items

    async def _scan_db(self) -> list[int]:
        """Fallback: find notes with status='captured' in DB."""
        notes = await self.db.get_notes_by_status("captured", limit=10)
        return [n["id"] for n in notes]

    async def _process_one(self, note_id: int):
        """Run full pipeline: enrich → link → project → notify."""
        if note_id in self._in_progress:
            return

        self._in_progress.add(note_id)
        try:
            # EnrichmentService atomically handles status transitions
            ok = await self.enrichment.process(note_id)

            if ok:
                # Relations (failure = skip)
                try:
                    await self.relations.link(note_id)
                except Exception as e:
                    logger.warning(f"Relations failed for note #{note_id}: {e}")

                # Projection (failure = skip)
                try:
                    await self.projection.project(note_id)
                except Exception as e:
                    logger.warning(f"Projection failed for note #{note_id}: {e}")

                # Telegram notification
                await self._notify_telegram(note_id)
        finally:
            self._in_progress.discard(note_id)

    async def _notify_telegram(self, note_id: int):
        """Send second message with enrichment result to Telegram."""
        if not self.tg_app:
            return

        try:
            from app.bot.handlers import get_owner_chat_id_async
            chat_id = await get_owner_chat_id_async(self.db)
            if not chat_id:
                return

            note = await self.db.get_note(note_id)
            enrichment = await self.db.get_latest_enrichment(note_id)
            if not enrichment:
                return

            # Only notify for recently captured notes (not reprocessed old ones)
            from datetime import datetime
            created = note.get("created_at", "")
            if created:
                try:
                    age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
                    if age > 300:  # older than 5 minutes — skip notification
                        return
                except Exception:
                    pass

            # Build message
            import json
            cat_emoji = {
                "food": "🍽", "health": "🏥", "fitness": "💪", "business": "💼",
                "personal": "💭", "finance": "💰", "learning": "📚", "goals": "🎯",
                "people": "👥", "ideas": "💡", "family": "👨‍👩‍👧‍👦", "auto": "🚗",
            }
            category = enrichment.get("category", "")
            emoji = cat_emoji.get(category, "📝")

            parts = [f"📝 #{note_id} обработано:"]
            title = enrichment.get("suggested_title", "")
            if title:
                parts.append(f"**{title}**")
            parts.append(f"{emoji} {category}/{enrichment.get('subcategory', '')}")

            tags = enrichment.get("tags", "[]")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            if tags:
                parts.append(f"🏷 {', '.join(tags[:5])}")

            # Entities
            entities = await self.db.get_entities_by_note(note_id)
            for ent in entities[:3]:
                etype = {"person": "👤", "company": "🏢", "place": "📍", "project": "📁"}.get(ent["entity_type"], "")
                parts.append(f"{etype} {ent['entity_value']} ({ent.get('role', ent['entity_type'])})")

            # Tasks
            tasks = await self.db.get_tasks_by_note(note_id)
            for task in tasks[:3]:
                pri = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(task.get("priority", ""), "")
                parts.append(f"✅ {pri} {task['description']}")

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("OK", callback_data=f"note:ok:{note_id}"),
                InlineKeyboardButton("Edit", callback_data=f"note:edit:{note_id}"),
                InlineKeyboardButton("Archive", callback_data=f"note:archive:{note_id}"),
            ]])

            await self.tg_app.bot.send_message(
                chat_id=chat_id,
                text="\n".join(parts),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.debug(f"Telegram notify failed for note #{note_id}: {e}")
