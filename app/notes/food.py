"""Food Analytics Service — extract food entries and grocery expenses from notes."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

FOOD_EXTRACTION_PROMPT = """\
Ты анализируешь заметку о еде/питании. Извлеки каждый продукт/блюдо ОТДЕЛЬНО.

Правила:
- Каждый продукт или блюдо — отдельный item
- Калории оценивай по стандартным порциям (если не указано явно)
- Белки, жиры, углеводы — оценка в граммах
- Если калории указаны пользователем явно (число + kcal/ккал) → estimated=false
- Если калории оценены тобой → estimated=true
- meal_type: breakfast, lunch, dinner, snack, unknown

Если в заметке есть расходы на еду (купил, потратил, заказал, доставка):
- Извлеки их в expenses

Ответь ТОЛЬКО JSON (без ```):
{
  "items": [
    {"food_name": "паста", "calories": 400, "protein": 12, "fat": 8, "carbs": 65, "quantity": "порция", "estimated": true}
  ],
  "meal_type": "lunch",
  "expenses": [
    {"merchant": "Перекрёсток", "amount": 3500, "currency": "RUB", "category": "groceries"}
  ]
}

Категории расходов: groceries, restaurant, delivery, supplements, coffee
"""

# Expense markers for rule-based detection
_EXPENSE_MARKERS = [
    "купил", "потратил", "заказал", "доставк", "чек", "оплатил",
    "₽", "руб", "$", "€", "usd", "eur",
]
_EXPENSE_PATTERN = re.compile(
    r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб|р\b|\$|€|usd|eur)",
    re.IGNORECASE,
)
_CURRENCY_MAP = {
    "₽": "RUB", "руб": "RUB", "р": "RUB",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
}


class FoodAnalyticsService:
    """Extract food entries and grocery expenses from food-related notes."""

    def __init__(self, db, llm_router=None):
        self.db = db
        self.llm = llm_router

    async def extract(self, note_id: int) -> bool:
        """Extract food entries and/or grocery expenses from a note.

        Returns True if any entries were created.
        """
        note = await self.db.get_note(note_id)
        if not note:
            return False

        enrichment = await self.db.get_latest_enrichment(note_id)
        if not enrichment:
            return False

        category = enrichment.get("category", "")
        if category not in ("food", "finance"):
            return False

        content = note.get("raw_content") or note.get("content", "")
        date_str = note.get("created_at", "")[:10] or datetime.now().strftime("%Y-%m-%d")

        # Clear old entries (idempotent reprocess)
        await self.db.clear_food_entries(note_id)
        await self.db.clear_grocery_expenses(note_id)

        created = False

        # Parse enrichment structured_data
        raw_json = enrichment.get("raw_llm_json", "{}")
        if isinstance(raw_json, str):
            try:
                parsed = json.loads(raw_json)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
        else:
            parsed = raw_json
        sd = parsed.get("structured_data", {})

        # Fast path: use existing enrichment data for food entries
        if category == "food" and sd.get("foods"):
            created = await self._extract_from_structured_data(note_id, sd, date_str)

        # If no food entries from structured data but category is food, try LLM
        if not created and category == "food" and self.llm and content.strip():
            created = await self._extract_with_llm(note_id, content, date_str)

        # Grocery expenses: check for expense markers
        expense_created = await self._extract_grocery(note_id, content, sd, date_str)
        created = created or expense_created

        if created:
            logger.info(f"Food analytics extracted for note #{note_id}")
        return created

    async def _extract_from_structured_data(
        self, note_id: int, sd: dict, date: str,
    ) -> bool:
        """Fast path: create food_entries from existing enrichment structured_data."""
        foods = sd.get("foods", [])
        if not foods or not isinstance(foods, list):
            return False

        meal_type = sd.get("meal_type", "unknown")
        total_cal = sd.get("calories_est")

        # Split calories evenly if we have total but no per-item
        per_item_cal = None
        if total_cal and len(foods) > 0:
            per_item_cal = total_cal / len(foods)

        for food in foods:
            if not food or not isinstance(food, str):
                continue
            await self.db.save_food_entry(
                note_id=note_id,
                food_name=food,
                consumed_at=date,
                entry_type="meal",
                meal_type=meal_type,
                calories_kcal=per_item_cal,
                estimated=1,
                confidence=0.5,
                source_text=food,
            )
        return bool(foods)

    async def _extract_with_llm(
        self, note_id: int, content: str, date: str,
    ) -> bool:
        """Deep extraction: ask LLM for per-item breakdown."""
        try:
            response = await self.llm.extract(
                text=content[:2000],
                system=FOOD_EXTRACTION_PROMPT,
            )
            data = self._parse_json(response.text)
            if not data:
                return False

            meal_type = data.get("meal_type", "unknown")
            items = data.get("items", [])
            created = False

            for item in items:
                if not isinstance(item, dict):
                    continue
                food_name = item.get("food_name", "")
                if not food_name:
                    continue

                await self.db.save_food_entry(
                    note_id=note_id,
                    food_name=food_name,
                    consumed_at=date,
                    entry_type="meal",
                    meal_type=meal_type,
                    calories_kcal=item.get("calories"),
                    protein_g=item.get("protein"),
                    fat_g=item.get("fat"),
                    carbs_g=item.get("carbs"),
                    quantity_value=None,
                    quantity_unit=item.get("quantity", ""),
                    estimated=1 if item.get("estimated", True) else 0,
                    confidence=0.7,
                    source_text=food_name,
                )
                created = True

            # Also extract expenses from LLM response
            for exp in data.get("expenses", []):
                if isinstance(exp, dict) and exp.get("amount"):
                    await self.db.save_grocery_expense(
                        note_id=note_id,
                        amount=float(exp["amount"]),
                        date=date,
                        merchant=exp.get("merchant", ""),
                        currency=exp.get("currency", "USD"),
                        expense_category=exp.get("category", "groceries"),
                        estimated=0,
                        confidence=0.7,
                    )
                    created = True

            return created
        except Exception as e:
            logger.warning(f"Food LLM extraction failed for note #{note_id}: {e}")
            return False

    async def _extract_grocery(
        self, note_id: int, content: str, sd: dict, date: str,
    ) -> bool:
        """Extract grocery/food expenses from text using rule-based parsing."""
        lower = content.lower()

        # Check for expense markers
        has_expense = any(m in lower for m in _EXPENSE_MARKERS)
        if not has_expense:
            return False

        # Try to extract amount from structured_data first
        amount = sd.get("amount")
        currency = sd.get("currency", "USD")
        if amount:
            await self.db.save_grocery_expense(
                note_id=note_id,
                amount=float(amount),
                date=date,
                currency=currency,
                expense_category=self._detect_expense_category(lower),
                estimated=0,
                confidence=0.8,
                source_text=content[:200],
            )
            return True

        # Try regex extraction
        match = _EXPENSE_PATTERN.search(content)
        if match:
            amount_str = match.group(1).replace(" ", "").replace(",", ".")
            try:
                amount_val = float(amount_str)
            except ValueError:
                return False

            # Detect currency from matched text
            after_match = content[match.end():match.end() + 5].lower().strip()
            full_match = content[match.start():match.end()].lower()
            detected_currency = "USD"
            for marker, cur in _CURRENCY_MAP.items():
                if marker in full_match:
                    detected_currency = cur
                    break

            await self.db.save_grocery_expense(
                note_id=note_id,
                amount=amount_val,
                date=date,
                currency=detected_currency,
                expense_category=self._detect_expense_category(lower),
                estimated=1,
                confidence=0.5,
                source_text=content[:200],
            )
            return True

        return False

    def _detect_expense_category(self, text_lower: str) -> str:
        """Detect expense category from note text."""
        if any(w in text_lower for w in ("доставк", "delivery", "яндекс еда", "самокат")):
            return "delivery"
        if any(w in text_lower for w in ("ресторан", "кафе", "бар", "restaurant")):
            return "restaurant"
        if any(w in text_lower for w in ("кофе", "кофейн", "coffee", "старбакс")):
            return "coffee"
        if any(w in text_lower for w in ("витамин", "бад", "supplement", "протеин")):
            return "supplements"
        return "groceries"

    def _parse_json(self, text: str) -> dict | None:
        """Parse JSON from LLM response."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass
        return None
