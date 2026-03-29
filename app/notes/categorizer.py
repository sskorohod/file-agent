"""Note Categorizer — LLM-based categorization with structured data extraction."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CATEGORIES = {
    "health": ["symptoms", "medications", "doctor_visits", "mental_health"],
    "food": ["meals", "water", "supplements"],
    "fitness": ["weight", "exercise", "sleep", "tracker"],
    "business": ["ideas", "meetings", "calls", "tasks", "projects"],
    "personal": ["mood", "reflections", "gratitude", "relationships"],
    "finance": ["expenses", "income", "investments"],
    "learning": ["books", "courses", "insights"],
    "goals": ["short_term", "long_term"],
    "people": ["clients", "contacts", "family_members"],
    "ideas": [],
    "family": ["events", "milestones"],
    "auto": ["tesla", "tools", "repairs"],
}

CATEGORIZATION_PROMPT = """\
Ты — система категоризации личных заметок. Проанализируй заметку и извлеки структурированные данные.

## Категории и подкатегории:
- **health**: symptoms, medications, doctor_visits, mental_health
- **food**: meals, water, supplements
- **fitness**: weight, exercise, sleep, tracker
- **business**: meetings, calls, tasks, projects
- **personal**: mood, reflections, gratitude, relationships
- **finance**: expenses, income, investments
- **learning**: books, courses, insights
- **goals**: short_term, long_term
- **people**: clients, contacts, family_members — заметки о конкретных людях
- **ideas**: (нет подкатегорий) — бизнес-идеи, случайные мысли, изобретения
- **family**: events, milestones — семья, личная жизнь, события
- **auto**: tesla, tools, repairs — машины, оборудование, техника

## Правила извлечения structured_data:

Для **food/meals**: {"meal_type": "breakfast|lunch|dinner|snack", "calories_est": число, "foods": ["еда1", "еда2"], "water_ml": число}
Для **fitness/weight**: {"weight_kg": число}
Для **fitness/exercise**: {"exercise_type": "тип", "duration_min": число}
Для **fitness/sleep**: {"sleep_hours": число, "sleep_quality": число 1-10}
Для **health/symptoms**: {"symptom_name": "название", "severity_1_10": число, "pain_location": "где болит"}
Для **health/medications**: {"medication_name": "название", "dosage": "доза"}
Для **personal/mood**: {"mood_score_1_10": число, "emotion": "эмоция"}
Для **business/tasks** или **ideas**: {"action_items": [{"task": "описание", "priority": "high|medium|low", "due_date": "дата или null"}]}
Для **finance/expenses**: {"amount": число, "currency": "USD|RUB|EUR", "description": "описание"}
Для **learning**: {"key_insight": "главный вывод"}
Для **goals**: {"goal_description": "описание", "deadline": "дата если есть"}
Для **people**: {"person_name": "имя", "context": "контекст знакомства/встречи"}
Для **auto**: {"vehicle": "марка/модель", "issue": "описание проблемы", "cost": число}

## Правила:
- Оцени калории по описанию еды максимально точно (каждый продукт отдельно)
- mood_score_1_10: если явно не указано, определи по тону заметки (null если невозможно)
- Если заметка затрагивает несколько категорий — выбери основную, остальные укажи в related_categories
- tags: 2-5 тегов на русском, lowercase
- title: краткий заголовок на русском (3-7 слов)
- summary: суть в 1-2 предложениях на русском
- sentiment: оцени тональность заметки от -1.0 (негативная) до +1.0 (позитивная), 0 = нейтральная
- energy: если упоминается уровень энергии/усталости, оцени 1-10 (null если не упомянуто)
- confidence: насколько ты уверен в выбранной категории, от 0.0 до 1.0

Ответь ТОЛЬКО JSON (без markdown, без ```):
{
  "category": "food",
  "subcategory": "meals",
  "title": "Обед дома — паста с курицей",
  "summary": "Пообедал пастой с курицей и томатным соусом",
  "tags": ["обед", "паста", "курица", "дом"],
  "mood_score": null,
  "sentiment": 0.3,
  "energy": null,
  "confidence": 0.95,
  "structured_data": {"meal_type": "lunch", "calories_est": 650, "foods": ["паста", "курица", "томатный соус"]},
  "action_items": [],
  "related_categories": ["health"]
}"""


@dataclass
class NoteCategoryResult:
    """Result of note categorization."""
    category: str = ""
    subcategory: str = ""
    title: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    mood_score: int | None = None
    structured_data: dict = field(default_factory=dict)
    related_categories: list[str] = field(default_factory=list)
    sentiment: float | None = None        # -1.0 to +1.0
    energy: int | None = None             # 1-10
    confidence: float = 0.0               # 0.0 to 1.0
    action_items: list[dict] = field(default_factory=list)


class NoteCategorizer:
    """Categorize notes using LLM with structured data extraction."""

    def __init__(self, llm_router):
        self.llm = llm_router

    async def categorize(self, text: str) -> NoteCategoryResult:
        """Categorize note text and extract structured data."""
        try:
            response = await self.llm.extract(
                text=text,
                system=CATEGORIZATION_PROMPT,
            )
            return self._parse_response(response.text, text)
        except Exception as e:
            logger.error(f"Note categorization failed: {e}")
            return self._fallback(text)

    def _parse_response(self, response_text: str, original: str) -> NoteCategoryResult:
        """Parse LLM JSON response into NoteCategoryResult."""
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse categorization JSON: {text[:200]}")
            return self._fallback(original)

        # Parse confidence first — low confidence → _inbox
        confidence = 0.0
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        except (ValueError, TypeError):
            pass

        category = data.get("category", "personal")
        if category not in CATEGORIES and category != "_inbox":
            category = "personal"
        if confidence < 0.5:
            category = "_inbox"

        subcategory = data.get("subcategory", "")
        if subcategory and category in CATEGORIES:
            if subcategory not in CATEGORIES.get(category, []):
                subcategory = CATEGORIES[category][0] if CATEGORIES[category] else ""

        mood = data.get("mood_score")
        if mood is not None:
            try:
                mood = max(1, min(10, int(mood)))
            except (ValueError, TypeError):
                mood = None

        sentiment = data.get("sentiment")
        if sentiment is not None:
            try:
                sentiment = max(-1.0, min(1.0, float(sentiment)))
            except (ValueError, TypeError):
                sentiment = None

        energy = data.get("energy")
        if energy is not None:
            try:
                energy = max(1, min(10, int(energy)))
            except (ValueError, TypeError):
                energy = None

        action_items = data.get("action_items", [])
        if not isinstance(action_items, list):
            action_items = []

        return NoteCategoryResult(
            category=category,
            subcategory=subcategory,
            title=data.get("title", original[:50]),
            summary=data.get("summary", original),
            tags=data.get("tags", [])[:5],
            mood_score=mood,
            structured_data=data.get("structured_data", {}),
            related_categories=data.get("related_categories", []),
            sentiment=sentiment,
            energy=energy,
            confidence=confidence,
            action_items=action_items,
        )

    def _fallback(self, text: str) -> NoteCategoryResult:
        """Fallback categorization when LLM fails."""
        return NoteCategoryResult(
            category="_inbox",
            subcategory="",
            title=text[:50],
            summary=text[:200],
            tags=[],
            mood_score=None,
            structured_data={},
            related_categories=[],
            sentiment=None,
            energy=None,
            confidence=0.0,
            action_items=[],
        )
