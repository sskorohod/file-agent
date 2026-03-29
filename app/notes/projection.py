"""Projection Service — build vault .md, Qdrant embeddings, MOCs."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class NoteProjectionService:
    """Build secondary outputs from enriched notes. Failure never invalidates the note."""

    def __init__(self, db, vector_store, vault):
        self.db = db
        self.vector_store = vector_store
        self.vault = vault

    async def project(self, note_id: int):
        """Build all projections for a note. Per-target success."""
        note = await self.db.get_note(note_id)
        if not note:
            return

        enrichment = await self.db.get_latest_enrichment(note_id)
        if not enrichment:
            return

        content = note.get("raw_content") or note.get("content", "")
        relations = await self.db.get_note_relations_v2(note_id)
        entities = await self.db.get_entities_by_note(note_id)
        facts = await self.db.get_facts_by_note(note_id)
        tasks = await self.db.get_tasks_by_note(note_id)

        # 1. Vault .md export
        vault_path = ""
        try:
            vault_path = self._write_vault(note, enrichment, content, relations, facts, tasks)
            await self.db.update_note_bridge_fields(
                note_id, vault_path=str(vault_path),
                category=enrichment.get("category", ""),
                subcategory=enrichment.get("subcategory", ""),
                tags=enrichment.get("tags", "[]"),
            )
            logger.debug(f"Note #{note_id} vault exported: {vault_path}")
        except Exception as e:
            logger.warning(f"Vault projection failed for note #{note_id}: {e}")

        # 2. Qdrant embedding
        try:
            await self._embed(note_id, content, enrichment)
            await self.db.db.execute("UPDATE notes SET embedded=1 WHERE id=?", (note_id,))
            await self.db.db.commit()
            logger.debug(f"Note #{note_id} embedded in Qdrant")
        except Exception as e:
            logger.warning(f"Embedding failed for note #{note_id}: {e}")

        # 3. Daily MOC
        try:
            date_str = note.get("created_at", "")[:10]
            if date_str:
                day_notes = await self.db.get_daily_notes(date_str)
                daily_facts = await self.db.get_daily_facts(date_str)
                moc_metrics = {}
                if "calories" in daily_facts:
                    moc_metrics["calories_total"] = daily_facts["calories"]["total"]
                if "mood_score" in daily_facts:
                    moc_metrics["mood_avg"] = round(daily_facts["mood_score"]["avg"], 1)
                if "weight_kg" in daily_facts:
                    moc_metrics["weight"] = daily_facts["weight_kg"]["avg"]
                self.vault.update_daily_moc(date_str, day_notes, moc_metrics)
        except Exception as e:
            logger.debug(f"Daily MOC update failed: {e}")

        # 4. Category MOC
        try:
            category = enrichment.get("category", "")
            if category and category != "_inbox":
                cat_notes = await self.db.list_notes(limit=100, category=category)
                self.vault.update_category_moc(category, cat_notes)
        except Exception as e:
            logger.debug(f"Category MOC update failed: {e}")

        # 5. Backlinks to related notes
        if vault_path:
            for rn in relations[:5]:
                rn_vault = rn.get("vault_path", "")
                if rn_vault and Path(rn_vault).exists():
                    try:
                        self.vault.add_backlink(
                            Path(rn_vault),
                            str(Path(vault_path).relative_to(self.vault.base_path).with_suffix("")),
                        )
                    except Exception:
                        pass

    def _write_vault(self, note, enrichment, content, relations, facts, tasks) -> str:
        """Write Obsidian .md file from DB state."""
        # Build structured_data dict from facts for vault display
        structured_data = {}
        for f in facts:
            if f.get("value_num") is not None:
                structured_data[f["key"]] = f["value_num"]
            elif f.get("value_text"):
                structured_data[f["key"]] = f["value_text"]

        # Build related_notes list for vault
        related_notes = []
        for r in relations:
            related_notes.append({
                "id": r.get("target_note_id") or r.get("source_note_id"),
                "title": r.get("user_title", ""),
                "category": r.get("category", ""),
                "vault_path": r.get("vault_path", ""),
                "score": r.get("score", 0.5),
            })

        tags_raw = enrichment.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except Exception:
                tags = []
        else:
            tags = tags_raw

        path = self.vault.write_note(
            note_id=note["id"],
            category=enrichment.get("category", ""),
            subcategory=enrichment.get("subcategory", ""),
            title=enrichment.get("suggested_title") or note.get("user_title", ""),
            content=content,
            tags=tags,
            summary=enrichment.get("summary", ""),
            related_notes=related_notes,
            structured_data=structured_data,
            mood_score=enrichment.get("mood_score"),
            source=note.get("source", "voice"),
            sentiment=enrichment.get("sentiment"),
            energy=enrichment.get("energy"),
            confidence=enrichment.get("confidence", 0),
        )
        return str(path)

    async def _embed(self, note_id: int, content: str, enrichment: dict):
        """Embed note text into Qdrant."""
        from qdrant_client.models import PointStruct

        chunks = self.vector_store.chunk_text(content) if content.strip() else [content]
        if not chunks:
            return

        tags_raw = enrichment.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except Exception:
                tags = []
        else:
            tags = tags_raw

        vectors = self.vector_store.embed(chunks)
        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"note:{note_id}:{i}"))
            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "type": "note",
                    "note_id": note_id,
                    "chunk_index": i,
                    "text": chunk,
                    "category": enrichment.get("category", ""),
                    "subcategory": enrichment.get("subcategory", ""),
                    "tags": tags,
                },
            ))

        if points:
            self.vector_store.client.upsert(
                collection_name=self.vector_store.qdrant_config.collection_name,
                points=points,
            )
