"""Enrichment Service — AI classification, fact/entity/task extraction."""

from __future__ import annotations

import asyncio
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

            # 6.5 Food analytics extraction (for food/finance notes)
            if result.category in ("food", "finance"):
                try:
                    from app.notes.food import FoodAnalyticsService
                    food_svc = FoodAnalyticsService(self.db, self.categorizer.llm)
                    await food_svc.extract(note_id)
                except Exception as e:
                    logger.warning(f"Food analytics failed for note #{note_id}: {e}")

            # 6.6 Reminder extraction (for notes with tasks)
            if result.action_items:
                try:
                    from app.notes.reminders import ReminderExtractionService
                    rem_svc = ReminderExtractionService(self.db)
                    await rem_svc.extract(note_id)
                except Exception as e:
                    logger.warning(f"Reminder extraction failed for note #{note_id}: {e}")

            # 7. Update bridge fields (skip if user manually edited metadata)
            note_fresh = await self.db.get_note(note_id)
            if not (note_fresh and note_fresh.get("metadata_manual") in (1, "1")):
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

        except asyncio.CancelledError:
            # Recover to a terminal or retryable state on reload/shutdown.
            try:
                note_fresh = await self.db.get_note(note_id)
                if note_fresh and note_fresh.get("current_enrichment_id"):
                    await self.db.set_note_status(note_id, "enriched")
                else:
                    await self.db.set_note_status(note_id, "captured")
            except Exception:
                pass
            raise
        except Exception as e:
            logger.error(f"Enrichment failed for note #{note_id}: {e}", exc_info=True)
            await self.db.set_note_status(note_id, "failed")
            return False

    async def _save_entities(self, note_id: int, result: NoteCategoryResult):
        """Extract and save entities from structured data."""
        sd = result.structured_data

        # Person entities — with optional role
        person_role = sd.get("person_role", "")
        if not isinstance(person_role, str):
            person_role = ""
        for field in ("person_name", "patient_name"):
            name = sd.get(field)
            if name and isinstance(name, str):
                await self.db.save_entity(
                    note_id, "person", name,
                    normalized_value=name.lower().strip(),
                    role=person_role,
                )

        # Vehicle/product asset (not company — Tesla in auto context is a car, not a corp)
        vehicle = sd.get("vehicle")
        if vehicle and isinstance(vehicle, str):
            await self.db.save_entity(note_id, "vehicle", vehicle, normalized_value=vehicle.lower())

        # Place entities
        place = sd.get("place_name")
        if place and isinstance(place, str):
            await self.db.save_entity(note_id, "place", place, normalized_value=place.lower().strip())

        # Project context
        project = sd.get("project_name")
        if project and isinstance(project, str):
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

    # Explicit urgency markers that justify high priority
    _URGENCY_WORDS = {"срочно", "немедленно", "критично", "asap", "urgent", "сегодня", "завтра"}

    async def _save_tasks(self, note_id: int, result: NoteCategoryResult):
        """Extract and save tasks from action_items with priority normalization."""
        # Check original note text for urgency words once
        note = await self.db.get_note(note_id)
        note_lower = (note.get("raw_content") or note.get("content", "")).lower() if note else ""
        has_urgency = any(w in note_lower for w in self._URGENCY_WORDS)

        for item in result.action_items:
            if isinstance(item, dict):
                desc = item.get("task", "")
                priority = item.get("priority", "medium")
                due = item.get("due_date", "")
                due_hint = item.get("due_hint", "")
            elif isinstance(item, str):
                desc = item
                priority = "medium"
                due = ""
                due_hint = ""
            else:
                continue

            # Normalize priority: high only with explicit urgency or very close deadline
            if priority == "high" and not has_urgency:
                # Check due_hint for same-day / next-day signals
                hint_lower = (due_hint or "").lower()
                close_deadline = any(w in hint_lower for w in ("сегодня", "завтра", "через час", "немедленно"))
                if not close_deadline:
                    priority = "medium"

            if desc:
                await self.db.save_task(note_id, desc, priority=priority, due_date=due or "")
