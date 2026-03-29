"""Enrichment Service — AI classification, fact/entity/task extraction."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from app.notes.categorizer import NoteCategorizer, NoteCategoryResult

logger = logging.getLogger(__name__)


class NoteEnrichmentService:
    """Classify notes, extract structured data, assign status."""

    def __init__(self, db, llm_router):
        self.db = db
        self.categorizer = NoteCategorizer(llm_router)

    async def process(self, note_id: int) -> bool:
        """Enrich a single note. Atomically: → processing → final status.

        Returns True if enrichment succeeded, False otherwise.
        """
        note = await self.db.get_note(note_id)
        if not note:
            logger.warning(f"Note #{note_id} not found")
            return False

        content = note.get("raw_content") or note.get("content", "")
        if not content:
            await self.db.set_note_status(note_id, "failed")
            return False

        # Atomically set processing
        await self.db.set_note_status(note_id, "processing")

        try:
            # 1. LLM categorization
            result = await self.categorizer.categorize(content)
            date_str = note.get("created_at", "")[:10] or datetime.now().strftime("%Y-%m-%d")

            # 2. Clear old derived data (idempotent reprocess)
            await self.db.clear_note_derived(note_id)

            # 3. Save enrichment record
            enrichment_id = await self.db.save_enrichment(
                note_id=note_id,
                suggested_title=result.title,
                summary=result.summary,
                category=result.category,
                subcategory=result.subcategory,
                tags=json.dumps(result.tags, ensure_ascii=False),
                confidence=result.confidence,
                sentiment=result.sentiment,
                energy=result.energy,
                mood_score=result.mood_score,
                raw_llm_json=json.dumps({
                    "structured_data": result.structured_data,
                    "related_categories": result.related_categories,
                    "action_items": result.action_items,
                }, ensure_ascii=False),
            )

            # 4. Extract entities
            await self._save_entities(note_id, result)

            # 5. Extract facts/metrics
            await self._save_facts(note_id, result, date_str)

            # 6. Extract tasks
            await self._save_tasks(note_id, result)

            # 7. Update bridge fields for compatibility
            await self.db.update_note_bridge_fields(
                note_id=note_id,
                category=result.category,
                subcategory=result.subcategory,
                tags=json.dumps(result.tags, ensure_ascii=False),
            )

            # 8. Set final status
            if result.confidence < 0.5:
                await self.db.set_note_status(note_id, "needs_review")
            else:
                await self.db.set_note_status(note_id, "enriched")

            logger.info(
                f"Note #{note_id} enriched: {result.category}/{result.subcategory} "
                f"(confidence={result.confidence:.2f})"
            )
            return True

        except Exception as e:
            logger.error(f"Enrichment failed for note #{note_id}: {e}", exc_info=True)
            await self.db.set_note_status(note_id, "failed")
            return False

    async def _save_entities(self, note_id: int, result: NoteCategoryResult):
        """Extract and save entities from structured data."""
        sd = result.structured_data

        # Person entities
        for field in ("person_name", "patient_name"):
            name = sd.get(field)
            if name and isinstance(name, str):
                await self.db.save_entity(
                    note_id, "person", name,
                    normalized_value=name.lower().strip(),
                )

        # Company/vehicle from auto category
        vehicle = sd.get("vehicle")
        if vehicle:
            await self.db.save_entity(note_id, "company", vehicle, normalized_value=vehicle.lower())

        # Project context
        project = sd.get("project_name")
        if project:
            await self.db.save_entity(note_id, "project", project, normalized_value=project.lower())

    async def _save_facts(self, note_id: int, result: NoteCategoryResult, date: str):
        """Extract typed facts from categorization result."""
        sd = result.structured_data

        # Metric facts
        metric_fields = {
            "calories_est": ("calories", "kcal"),
            "weight_kg": ("weight_kg", "kg"),
            "sleep_hours": ("sleep_hours", "h"),
            "sleep_quality": ("sleep_quality", ""),
            "water_ml": ("water_ml", "ml"),
            "duration_min": ("exercise_min", "min"),
            "steps": ("steps", ""),
            "amount": ("expense", ""),
            "cost": ("auto_cost", ""),
            "severity_1_10": ("symptom_severity", ""),
        }

        for field, (key, unit) in metric_fields.items():
            value = sd.get(field)
            if value is not None:
                try:
                    await self.db.save_fact(
                        note_id, "metric", key,
                        value_num=float(value), unit=unit, date=date,
                    )
                except (ValueError, TypeError):
                    pass

        # Journal signals (mood, energy, sentiment from top-level)
        if result.mood_score is not None:
            await self.db.save_fact(
                note_id, "journal_signal", "mood_score",
                value_num=float(result.mood_score), date=date,
            )
        if result.energy is not None:
            await self.db.save_fact(
                note_id, "journal_signal", "energy",
                value_num=float(result.energy), date=date,
            )
        if result.sentiment is not None:
            await self.db.save_fact(
                note_id, "journal_signal", "sentiment",
                value_num=float(result.sentiment), date=date,
            )

        # Idea facts
        idea = sd.get("idea_summary")
        if idea:
            await self.db.save_fact(
                note_id, "idea", "idea",
                value_text=idea, date=date,
            )

        # Event facts
        emotion = sd.get("emotion")
        if emotion:
            await self.db.save_fact(
                note_id, "journal_signal", "emotion",
                value_text=emotion, date=date,
            )

        # Food items as text facts
        foods = sd.get("foods")
        if foods and isinstance(foods, list):
            await self.db.save_fact(
                note_id, "event", "food_items",
                value_text=", ".join(str(f) for f in foods), date=date,
            )

    async def _save_tasks(self, note_id: int, result: NoteCategoryResult):
        """Extract and save tasks from action_items."""
        for item in result.action_items:
            if isinstance(item, dict):
                desc = item.get("task", "")
                priority = item.get("priority", "medium")
                due = item.get("due_date", "")
            elif isinstance(item, str):
                desc = item
                priority = "medium"
                due = ""
            else:
                continue

            if desc:
                await self.db.save_task(note_id, desc, priority=priority, due_date=due or "")
