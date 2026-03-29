"""Relation Service — find and create links between related notes."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class NoteRelationService:
    """Discover and persist note-to-note relations."""

    def __init__(self, db, vector_store):
        self.db = db
        self.vector_store = vector_store

    async def link(self, note_id: int):
        """Find related notes and create links. Only runs after enrichment."""
        note = await self.db.get_note(note_id)
        if not note:
            return

        content = note.get("raw_content") or note.get("content", "")
        if not content:
            return

        # Clear old outgoing relations
        await self.db.clear_note_relations(note_id)

        # 1. Semantic search via Qdrant
        try:
            results = await self.vector_store.search(query=content[:500], top_k=5)
            for r in results:
                if r.metadata.get("type") == "note":
                    target_id = r.metadata.get("note_id")
                    if target_id and target_id != note_id:
                        await self.db.save_relation(
                            note_id, target_id, "related",
                            reason=f"semantic: {r.score:.2f}",
                            score=r.score,
                        )
        except Exception as e:
            logger.debug(f"Semantic search for relations failed: {e}")

        # 2. Tag overlap from recent notes
        enrichment = await self.db.get_latest_enrichment(note_id)
        if enrichment:
            tags = enrichment.get("tags", "[]")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []

            if tags:
                recent = await self.db.list_notes(limit=50)
                existing_targets = set()
                for rn in recent:
                    rn_id = rn.get("id")
                    if rn_id == note_id or rn_id in existing_targets:
                        continue
                    rn_tags = rn.get("tags", [])
                    if isinstance(rn_tags, str):
                        try:
                            rn_tags = json.loads(rn_tags)
                        except Exception:
                            rn_tags = []
                    overlap = set(tags) & set(rn_tags)
                    if overlap:
                        await self.db.save_relation(
                            note_id, rn_id, "same_topic",
                            reason=f"tag overlap: {', '.join(overlap)}",
                            score=len(overlap) / max(len(tags), 1),
                        )
                        existing_targets.add(rn_id)
                        if len(existing_targets) >= 10:
                            break

        # 3. Same entity links
        entities = await self.db.get_entities_by_note(note_id)
        for entity in entities:
            if entity.get("normalized_value"):
                # Find other notes with same entity
                try:
                    cursor = await self.db.db.execute(
                        """SELECT DISTINCT note_id FROM note_entities
                           WHERE entity_type=? AND normalized_value=? AND note_id != ?
                           LIMIT 5""",
                        (entity["entity_type"], entity["normalized_value"], note_id),
                    )
                    for row in await cursor.fetchall():
                        await self.db.save_relation(
                            note_id, row[0], "related",
                            reason=f"same {entity['entity_type']}: {entity['entity_value']}",
                            score=0.7,
                        )
                except Exception:
                    pass

        logger.debug(f"Note #{note_id} relations linked")
