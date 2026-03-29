"""Note Agent — background processor for smart note categorization and linking."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from app.notes.categorizer import NoteCategorizer, NoteCategoryResult

logger = logging.getLogger(__name__)

# Metrics that map to note_data table
NUMERIC_METRICS = {
    "calories_est": ("calories", "kcal"),
    "weight_kg": ("weight_kg", "kg"),
    "mood_score_1_10": ("mood_score", ""),
    "sleep_hours": ("sleep_hours", "h"),
    "sleep_quality": ("sleep_quality", ""),
    "water_ml": ("water_ml", "ml"),
    "duration_min": ("exercise_min", "min"),
    "steps": ("steps", ""),
    "amount": ("expense", ""),
    "severity_1_10": ("symptom_severity", ""),
    "cost": ("auto_cost", ""),
}


class NoteAgent:
    """Background agent that categorizes, links, and embeds notes."""

    def __init__(self, db, llm_router, vector_store, vault_path: str | Path = ""):
        self.db = db
        self.llm = llm_router
        self.vector_store = vector_store
        self.categorizer = NoteCategorizer(llm_router)

        from app.config import get_settings
        settings = get_settings()
        vp = vault_path or settings.notes.vault_path
        if not vp:
            vp = settings.storage.resolved_path / "notes"

        from app.notes.vault import ObsidianVault
        # Pass file encryption key if available
        from app.main import _state
        enc_key = _state.get("_encryption_key") if settings.encryption.files else None
        self.vault = ObsidianVault(vp, encryption_key=enc_key)

    async def process_single(self, note_id: int) -> NoteCategoryResult | None:
        """Process a single note: categorize, extract data, link, embed, write vault."""
        note = await self.db.get_note(note_id)
        if not note:
            logger.warning(f"Note {note_id} not found")
            return None

        content = note.get("content", "")
        if not content:
            return None

        # 1. Categorize via LLM
        result = await self.categorizer.categorize(content)
        date_str = note.get("created_at", "")[:10] or datetime.now().strftime("%Y-%m-%d")

        # 2. Save numeric metrics to note_data
        await self._save_metrics(note_id, result.structured_data, date_str)

        # Also save top-level metrics if present
        if result.mood_score is not None:
            await self.db.save_note_data(
                note_id, "mood_score", float(result.mood_score), "", date_str,
            )
        if result.energy is not None:
            await self.db.save_note_data(
                note_id, "energy", float(result.energy), "", date_str,
            )
        if result.sentiment is not None:
            await self.db.save_note_data(
                note_id, "sentiment", float(result.sentiment), "", date_str,
            )

        # 3. Find related notes via semantic search
        related_notes = await self._find_related(content, note_id)

        # 4. Save links
        for rn in related_notes:
            rn_id = rn.get("id")
            if rn_id and rn_id != note_id:
                await self.db.save_note_link(note_id, rn_id, "related", rn.get("score", 0.5))

        # 5. Write Obsidian .md
        vault_path = self.vault.write_note(
            note_id=note_id,
            category=result.category,
            subcategory=result.subcategory,
            title=result.title,
            content=content,
            tags=result.tags,
            summary=result.summary,
            related_notes=related_notes,
            structured_data=result.structured_data,
            mood_score=result.mood_score,
            source=note.get("source", "voice"),
            sentiment=result.sentiment,
            energy=result.energy,
            confidence=result.confidence,
        )

        # 6. Update note in DB
        await self.db.update_note_processed(
            note_id=note_id,
            category=result.category,
            subcategory=result.subcategory,
            mood_score=result.mood_score,
            vault_path=str(vault_path),
            structured_json=json.dumps(result.structured_data, ensure_ascii=False),
            title=result.title,
            tags=json.dumps(result.tags, ensure_ascii=False),
            sentiment=result.sentiment,
            energy=result.energy,
            confidence=result.confidence,
        )

        # 7. Embed in Qdrant
        try:
            await self._embed_note(note_id, content, result)
            await self.db.mark_note_embedded(note_id)
        except Exception as e:
            logger.warning(f"Failed to embed note {note_id}: {e}")

        # 8. Add backlinks to related notes
        for rn in related_notes:
            rn_vault = rn.get("vault_path", "")
            if rn_vault:
                try:
                    self.vault.add_backlink(
                        Path(rn_vault),
                        str(vault_path.relative_to(self.vault.base_path).with_suffix("")),
                    )
                except Exception:
                    pass

        # 9. Update category MOC
        if result.category and result.category != "_inbox":
            try:
                cat_notes = await self.db.list_notes(limit=100, category=result.category)
                self.vault.update_category_moc(result.category, cat_notes)
            except Exception:
                pass

        logger.info(
            f"Note {note_id} processed: {result.category}/{result.subcategory} "
            f"(confidence={result.confidence:.2f}, {len(related_notes)} links)"
        )
        return result

    async def process_unprocessed(self) -> int:
        """Process all unprocessed notes. Returns count of processed notes."""
        notes = await self.db.get_unprocessed_notes(limit=20)
        if not notes:
            return 0

        count = 0
        for note in notes:
            try:
                result = await self.process_single(note["id"])
                if result:
                    count += 1
            except Exception as e:
                logger.error(f"Failed to process note {note['id']}: {e}")

        # Update daily summary
        if count > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            await self.generate_daily_summary(today)

        return count

    async def generate_daily_summary(self, date: str):
        """Generate/update daily MOC for the given date."""
        notes = await self.db.get_daily_notes(date)
        if not notes:
            return

        metrics = await self.db.get_daily_facts(date) if hasattr(self.db, 'get_daily_facts') else await self.db.get_daily_metrics(date)

        # Build metrics dict for MOC
        moc_metrics = {}
        if "calories" in metrics:
            moc_metrics["calories_total"] = metrics["calories"]["total"]
        if "mood_score" in metrics:
            moc_metrics["mood_avg"] = round(metrics["mood_score"]["avg"], 1)
        if "weight_kg" in metrics:
            moc_metrics["weight"] = metrics["weight_kg"]["avg"]

        self.vault.update_daily_moc(date, notes, moc_metrics)

    async def reprocess_all(self) -> int:
        """Reprocess all notes (admin trigger)."""
        cursor = await self.db.db.execute("SELECT id FROM notes ORDER BY created_at")
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            try:
                # Reset processed state
                await self.db.db.execute(
                    "UPDATE notes SET processed_at=NULL WHERE id=?", (row[0],)
                )
                await self.db.db.commit()
                result = await self.process_single(row[0])
                if result:
                    count += 1
            except Exception as e:
                logger.error(f"Reprocess failed for note {row[0]}: {e}")
        return count

    async def _save_metrics(self, note_id: int, structured_data: dict, date: str):
        """Extract and save numeric metrics from structured data."""
        for field_name, (metric_name, unit) in NUMERIC_METRICS.items():
            value = structured_data.get(field_name)
            if value is not None:
                try:
                    await self.db.save_note_data(
                        note_id, metric_name, float(value), unit, date,
                    )
                except (ValueError, TypeError):
                    pass

    async def _find_related(self, content: str, note_id: int) -> list[dict]:
        """Find related notes via semantic search."""
        related = []
        try:
            results = await self.vector_store.search(
                query=content[:500],
                top_k=5,
            )
            for r in results:
                # Check if this is a note point
                if r.metadata.get("type") == "note":
                    rn_id = r.metadata.get("note_id")
                    if rn_id and rn_id != note_id:
                        note = await self.db.get_note(rn_id)
                        if note:
                            related.append({
                                "id": rn_id,
                                "title": note.get("title", ""),
                                "category": note.get("category", ""),
                                "subcategory": note.get("subcategory", ""),
                                "vault_path": note.get("vault_path", ""),
                                "score": r.score,
                            })
        except Exception as e:
            logger.debug(f"Semantic search for related notes failed: {e}")

        # Also check tag overlap from recent notes
        try:
            recent = await self.db.list_notes(limit=50)
            for rn in recent:
                rn_id = rn.get("id")
                if rn_id == note_id or any(r["id"] == rn_id for r in related):
                    continue
                rn_tags = json.loads(rn.get("tags", "[]")) if isinstance(rn.get("tags"), str) else rn.get("tags", [])
                # Simple overlap check
                if rn_tags and any(tag.lower() in content.lower() for tag in rn_tags[:5]):
                    related.append({
                        "id": rn_id,
                        "title": rn.get("title", ""),
                        "category": rn.get("category", ""),
                        "subcategory": rn.get("subcategory", ""),
                        "vault_path": rn.get("vault_path", ""),
                        "score": 0.5,
                    })
                    if len(related) >= 10:
                        break
        except Exception:
            pass

        return related[:10]

    async def _embed_note(self, note_id: int, content: str, result: NoteCategoryResult):
        """Embed note text into Qdrant with note-specific metadata."""
        from qdrant_client.models import PointStruct

        chunks = self.vector_store.chunk_text(content) if content.strip() else [content]
        if not chunks:
            return

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
                    "category": result.category,
                    "subcategory": result.subcategory,
                    "tags": result.tags,
                },
            ))

        if points:
            self.vector_store.client.upsert(
                collection_name=self.vector_store.qdrant_config.collection_name,
                points=points,
            )
            logger.info(f"Embedded note {note_id}: {len(points)} points")
