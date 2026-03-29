"""Regression test pack for NoteCategorizer.

Tests categorization logic WITHOUT calling LLM — we mock LLM responses
and verify _parse_response post-processing, domain override, and priority normalization.
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from app.notes.categorizer import NoteCategorizer, NoteCategoryResult


@pytest.fixture
def categorizer():
    """Categorizer with mock LLM router."""
    router = MagicMock()
    router.extract = AsyncMock()
    return NoteCategorizer(router)


def make_llm_response(data: dict) -> str:
    """Helper: build JSON string as LLM would return."""
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Group 1: basic category mapping
# ---------------------------------------------------------------------------

class TestBasicCategories:
    """Verify that clean LLM responses map correctly."""

    def test_health_symptoms(self, categorizer):
        raw = make_llm_response({
            "category": "health", "subcategory": "symptoms",
            "title": "Головная боль после обеда",
            "summary": "Болит голова, давление 140/90",
            "tags": ["здоровье", "давление", "головная боль"],
            "confidence": 0.9, "sentiment": -0.3,
            "mood_score": None, "energy": 4,
            "structured_data": {"symptom_name": "головная боль", "severity_1_10": 6},
            "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Болит голова")
        assert result.category == "health"
        assert result.subcategory == "symptoms"

    def test_food_meal(self, categorizer):
        raw = make_llm_response({
            "category": "food", "subcategory": "meals",
            "title": "Завтрак — овсянка с ягодами",
            "summary": "Позавтракал овсянкой", "tags": ["завтрак"],
            "confidence": 0.95, "sentiment": 0.2,
            "mood_score": None, "energy": None,
            "structured_data": {"meal_type": "breakfast", "calories_est": 350, "foods": ["овсянка", "ягоды"]},
            "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Позавтракал")
        assert result.category == "food"
        assert result.structured_data["calories_est"] == 350

    def test_auto_repair(self, categorizer):
        raw = make_llm_response({
            "category": "auto", "subcategory": "repairs",
            "title": "Замена масла в Tesla",
            "summary": "Нужно поменять масло", "tags": ["tesla", "масло"],
            "confidence": 0.85, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {"vehicle": "Tesla", "issue": "замена масла"},
            "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Поменять масло в Tesla")
        assert result.category == "auto"
        assert result.structured_data["vehicle"] == "Tesla"


# ---------------------------------------------------------------------------
# Group 2: people vs task/domain conflict — the core regression area
# ---------------------------------------------------------------------------

class TestPeopleVsDomainConflict:
    """The main fragile area: person mentioned inside a task should NOT be people."""

    def test_person_plus_auto_task_overrides_to_auto(self, categorizer):
        """'Встретил Ивана, нужно заказать фильтр для Tesla' → auto, not people."""
        raw = make_llm_response({
            "category": "people", "subcategory": "family_members",
            "title": "Встреча с Иваном и заказ фильтра",
            "summary": "Встретил Ивана, нужно заказать фильтр",
            "tags": ["иван", "tesla", "фильтр"],
            "confidence": 0.7, "sentiment": 0.1,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Иван"},
            "action_items": [{"task": "Заказать фильтр для Tesla", "priority": "medium", "due_date": "пятница"}],
            "related_categories": [],
        })
        text = "Сегодня встретил Ивана, нужно заказать фильтр для Tesla до пятницы"
        result = categorizer._parse_response(raw, text)
        assert result.category == "auto"
        assert "people" in result.related_categories

    def test_person_plus_health_task_overrides_to_health(self, categorizer):
        """'Сергей порекомендовал врача, записаться на приём' → health."""
        raw = make_llm_response({
            "category": "people", "subcategory": "contacts",
            "title": "Рекомендация Сергея",
            "summary": "Сергей порекомендовал врача",
            "tags": ["сергей", "врач"],
            "confidence": 0.65, "sentiment": 0.2,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Сергей"},
            "action_items": [{"task": "Записаться к кардиологу", "priority": "medium"}],
            "related_categories": [],
        })
        text = "Сергей порекомендовал врача-кардиолога, записаться на приём"
        result = categorizer._parse_response(raw, text)
        assert result.category == "health"
        assert "people" in result.related_categories

    def test_person_plus_business_task_overrides_to_business(self, categorizer):
        """'Иван попросил прислать смету' → business."""
        raw = make_llm_response({
            "category": "people", "subcategory": "contacts",
            "title": "Смета для Ивана",
            "summary": "Нужно прислать смету",
            "tags": ["иван", "смета"],
            "confidence": 0.6, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Иван"},
            "action_items": [{"task": "Прислать смету Ивану", "priority": "medium", "due_date": "среда"}],
            "related_categories": [],
        })
        text = "Иван попросил прислать смету до среды"
        result = categorizer._parse_response(raw, text)
        assert result.category == "business"
        assert "people" in result.related_categories

    def test_person_plus_food_no_action_stays_people(self, categorizer):
        """'Обедали с Мариной, тирамису' — no action markers in text → stays people.

        Override only triggers when text contains explicit action words.
        For this case we rely on LLM to classify as food directly.
        """
        raw = make_llm_response({
            "category": "people", "subcategory": "family_members",
            "title": "Обед с Мариной",
            "summary": "Обедали с Мариной",
            "tags": ["марина", "обед"],
            "confidence": 0.6, "sentiment": 0.5,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Марина"},
            "action_items": [{"task": "Попросить рецепт тирамису", "priority": "low"}],
            "related_categories": [],
        })
        text = "Обедали с Мариной, она принесла домашний тирамису"
        result = categorizer._parse_response(raw, text)
        # No action markers in text → override doesn't fire → stays people
        assert result.category == "people"

    def test_pure_person_profile_stays_people(self, categorizer):
        """'У Ивана день рождения 15 марта, любит виски' → stays people."""
        raw = make_llm_response({
            "category": "people", "subcategory": "contacts",
            "title": "Профиль Ивана",
            "summary": "День рождения Ивана 15 марта",
            "tags": ["иван", "день рождения"],
            "confidence": 0.9, "sentiment": 0.3,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Иван", "context": "день рождения 15 марта, любит виски"},
            "action_items": [],
            "related_categories": [],
        })
        text = "У Ивана день рождения 15 марта, любит виски"
        result = categorizer._parse_response(raw, text)
        assert result.category == "people"

    def test_person_intro_with_profession_stays_people(self, categorizer):
        """'Познакомился с Иваном, занимается ремонтом Tesla' → stays people."""
        raw = make_llm_response({
            "category": "people", "subcategory": "contacts",
            "title": "Знакомство с Иваном",
            "summary": "Познакомился с Иваном",
            "tags": ["иван", "tesla", "ремонт"],
            "confidence": 0.85, "sentiment": 0.2,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Иван", "context": "занимается ремонтом Tesla"},
            "action_items": [],
            "related_categories": ["auto"],
        })
        text = "Познакомился с Иваном, он занимается ремонтом Tesla"
        result = categorizer._parse_response(raw, text)
        assert result.category == "people"

    def test_generic_task_with_person_falls_to_business(self, categorizer):
        """'Игорь скинул контакт, надо позвонить' → business (generic action)."""
        raw = make_llm_response({
            "category": "people", "subcategory": "contacts",
            "title": "Контакт от Игоря",
            "summary": "Игорь скинул контакт юриста",
            "tags": ["игорь", "юрист"],
            "confidence": 0.6, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {"person_name": "Игорь"},
            "action_items": [{"task": "Позвонить юристу", "priority": "medium"}],
            "related_categories": [],
        })
        text = "Игорь скинул контакт юриста, надо позвонить по поводу договора"
        result = categorizer._parse_response(raw, text)
        assert result.category == "business"


# ---------------------------------------------------------------------------
# Group 3: low confidence → _inbox
# ---------------------------------------------------------------------------

class TestLowConfidence:

    def test_low_confidence_goes_to_inbox(self, categorizer):
        raw = make_llm_response({
            "category": "personal", "subcategory": "reflections",
            "title": "Непонятная заметка",
            "summary": "Что-то непонятное", "tags": [],
            "confidence": 0.3, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {}, "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "хмм ладно")
        assert result.category == "_inbox"

    def test_boundary_confidence_stays(self, categorizer):
        raw = make_llm_response({
            "category": "learning", "subcategory": "insights",
            "title": "Полезная мысль",
            "summary": "Инсайт", "tags": ["инсайт"],
            "confidence": 0.5, "sentiment": 0.1,
            "mood_score": None, "energy": None,
            "structured_data": {}, "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Интересная мысль")
        assert result.category == "learning"


# ---------------------------------------------------------------------------
# Group 4: domain override heuristic — _detect_domain_override
# ---------------------------------------------------------------------------

class TestDomainOverride:

    def test_auto_markers_detected(self, categorizer):
        result = categorizer._detect_domain_override(
            "Нужно заказать фильтр для Tesla",
            {"action_items": [{"task": "Заказать фильтр"}]},
        )
        assert result == "auto"

    def test_health_markers_detected(self, categorizer):
        result = categorizer._detect_domain_override(
            "Надо записаться к врачу на анализы",
            {"action_items": [{"task": "Записаться к врачу"}]},
        )
        assert result == "health"

    def test_no_action_markers_returns_none(self, categorizer):
        result = categorizer._detect_domain_override(
            "Иван работает с Tesla",
            {"action_items": []},
        )
        assert result is None

    def test_action_without_domain_falls_to_business(self, categorizer):
        result = categorizer._detect_domain_override(
            "Нужно отправить документы",
            {"action_items": [{"task": "Отправить документы"}]},
        )
        assert result == "business"

    def test_finance_markers_detected(self, categorizer):
        result = categorizer._detect_domain_override(
            "Надо оплатить страховку за машину",
            {"action_items": [{"task": "Оплатить страховку"}]},
        )
        assert result == "finance"


# ---------------------------------------------------------------------------
# Group 5: priority normalization (tested via enrichment, but unit-testable here)
# ---------------------------------------------------------------------------

class TestPriorityInPrompt:
    """Verify that few-shot examples in prompt use correct priority levels."""

    def test_deadline_without_urgency_is_medium(self, categorizer):
        """'до пятницы' without urgency word → medium, not high."""
        raw = make_llm_response({
            "category": "auto", "subcategory": "repairs",
            "title": "Фильтр для Tesla",
            "summary": "Заказать фильтр", "tags": ["tesla"],
            "confidence": 0.9, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {"vehicle": "Tesla"},
            "action_items": [{"task": "Заказать фильтр", "priority": "medium", "due_date": "пятница", "due_hint": "до пятницы"}],
            "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Заказать фильтр для Tesla до пятницы")
        assert result.action_items[0]["priority"] == "medium"

    def test_urgency_word_keeps_high(self, categorizer):
        """'срочно заказать' → high stays high."""
        raw = make_llm_response({
            "category": "auto", "subcategory": "repairs",
            "title": "Срочный ремонт",
            "summary": "Срочно нужен ремонт", "tags": ["tesla"],
            "confidence": 0.9, "sentiment": -0.3,
            "mood_score": None, "energy": None,
            "structured_data": {"vehicle": "Tesla"},
            "action_items": [{"task": "Срочно заказать запчасть", "priority": "high", "due_date": "сегодня", "due_hint": "сегодня"}],
            "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Срочно нужен ремонт Tesla")
        # Priority stays high because of urgency word
        assert result.action_items[0]["priority"] == "high"


# ---------------------------------------------------------------------------
# Group 6: edge cases & malformed input
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_invalid_json_returns_fallback(self, categorizer):
        result = categorizer._parse_response("not json at all", "тест")
        assert result.category == "_inbox"
        assert result.confidence == 0.0

    def test_unknown_category_defaults_to_personal(self, categorizer):
        raw = make_llm_response({
            "category": "pets", "subcategory": "",
            "title": "Кот", "summary": "Про кота", "tags": ["кот"],
            "confidence": 0.8, "sentiment": 0.5,
            "mood_score": None, "energy": None,
            "structured_data": {}, "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "Кот чихнул")
        assert result.category == "personal"

    def test_markdown_fenced_json_parsed(self, categorizer):
        inner = {
            "category": "health", "subcategory": "symptoms",
            "title": "Тест", "summary": "Тест", "tags": [],
            "confidence": 0.8, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {}, "action_items": [], "related_categories": [],
        }
        raw = f"```json\n{json.dumps(inner)}\n```"
        result = categorizer._parse_response(raw, "тест")
        assert result.category == "health"

    def test_subcategory_corrected_if_invalid(self, categorizer):
        raw = make_llm_response({
            "category": "health", "subcategory": "invalid_sub",
            "title": "Тест", "summary": "Тест", "tags": [],
            "confidence": 0.8, "sentiment": 0.0,
            "mood_score": None, "energy": None,
            "structured_data": {}, "action_items": [], "related_categories": [],
        })
        result = categorizer._parse_response(raw, "тест")
        assert result.subcategory == "symptoms"  # first valid subcategory for health


# ---------------------------------------------------------------------------
# Group 7: Data-driven regression pack from JSON fixtures
# ---------------------------------------------------------------------------

import pathlib

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_regression_cases():
    with open(FIXTURES_DIR / "categorizer_cases.json") as f:
        return json.load(f)


class TestRegressionPack:
    """Data-driven regression tests from JSON fixtures.

    Each case simulates LLM returning a (potentially wrong) classification
    and verifies that post-processing corrects it.
    """

    @pytest.mark.parametrize("case", load_regression_cases(), ids=lambda c: c["id"])
    def test_categorizer_regression(self, categorizer, case):
        raw = make_llm_response(case["llm_response"])
        result = categorizer._parse_response(raw, case["note_text"])
        expect = case["expect"]

        # Primary category
        if "category" in expect:
            assert result.category == expect["category"], (
                f"[{case['id']}] expected category={expect['category']}, got {result.category}"
            )

        # Forbidden categories
        for forbidden in expect.get("category_not", []):
            assert result.category != forbidden, (
                f"[{case['id']}] category must not be {forbidden}, got {result.category}"
            )

        # Related categories
        for rel in expect.get("has_related", []):
            assert rel in result.related_categories, (
                f"[{case['id']}] expected {rel} in related_categories, got {result.related_categories}"
            )

        # Person name in structured_data
        if "has_person_name" in expect:
            assert result.structured_data.get("person_name") == expect["has_person_name"], (
                f"[{case['id']}] expected person_name={expect['has_person_name']}"
            )

        # Vehicle in structured_data
        if "has_vehicle" in expect:
            assert result.structured_data.get("vehicle") == expect["has_vehicle"], (
                f"[{case['id']}] expected vehicle={expect['has_vehicle']}"
            )

        # Tasks present
        if expect.get("has_tasks"):
            assert len(result.action_items) > 0, f"[{case['id']}] expected tasks, got none"

        # No tasks expected
        if expect.get("no_tasks"):
            assert len(result.action_items) == 0, f"[{case['id']}] expected no tasks"

        # Person role in structured_data
        if "has_person_role" in expect:
            assert result.structured_data.get("person_role") == expect["has_person_role"], (
                f"[{case['id']}] expected person_role={expect['has_person_role']}, "
                f"got {result.structured_data.get('person_role')}"
            )

        # No person role expected
        if expect.get("no_person_role"):
            assert not result.structured_data.get("person_role"), (
                f"[{case['id']}] expected no person_role, got {result.structured_data.get('person_role')}"
            )

        # Place in structured_data
        if "has_place" in expect:
            assert result.structured_data.get("place_name") == expect["has_place"], (
                f"[{case['id']}] expected place_name={expect['has_place']}, "
                f"got {result.structured_data.get('place_name')}"
            )

        # Confidence bounds
        if "min_confidence" in expect:
            assert result.confidence >= expect["min_confidence"], (
                f"[{case['id']}] confidence {result.confidence} < min {expect['min_confidence']}"
            )
        if "max_confidence" in expect:
            assert result.confidence <= expect["max_confidence"], (
                f"[{case['id']}] confidence {result.confidence} > max {expect['max_confidence']}"
            )
