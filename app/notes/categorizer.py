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
- **people**: clients, contacts, family_members — заметки ТОЛЬКО о самом человеке (профиль, факты, контакт)
- **ideas**: (нет подкатегорий) — бизнес-идеи, случайные мысли, изобретения
- **family**: events, milestones — семья, личная жизнь, события
- **auto**: tesla, tools, repairs — машины, оборудование, техника

## ПРАВИЛО ГЛАВНОГО НАМЕРЕНИЯ (CRITICAL):

Определи ГЛАВНОЕ ДЕЙСТВИЕ в заметке. Категория = домен действия, а НЕ упомянутого человека.

- Если в заметке есть конкретное действие, задача, дедлайн или обязательство → категория определяется по ДОМЕНУ действия (auto, business, health и т.д.), а НЕ по упомянутому человеку
- **people** ТОЛЬКО если заметка ЦЕЛИКОМ о человеке: знакомство, профиль, факты о нём, контактные данные, день рождения
- Упоминание человека внутри задачи НЕ делает заметку категорией people — человек идёт в structured_data.person_name и entities
- Если человек упомянут в контексте задачи/встречи/проекта → поставь related_categories: ["people"]

## Примеры правильной категоризации:

Заметка: "Сегодня встретил Ивана, нужно заказать фильтр для Tesla до пятницы"
→ category: "auto", subcategory: "repairs"
→ structured_data: {"vehicle": "Tesla", "issue": "заказать фильтр", "person_name": "Иван"}
→ action_items: [{"task": "Заказать фильтр для Tesla", "priority": "medium", "due_date": "пятница", "due_hint": "до пятницы"}]
→ related_categories: ["people"]

Заметка: "Познакомился с Иваном, он занимается ремонтом Tesla"
→ category: "people", subcategory: "contacts"
→ structured_data: {"person_name": "Иван", "context": "занимается ремонтом Tesla"}

Заметка: "Иван попросил прислать смету до среды"
→ category: "business", subcategory: "tasks"
→ action_items: [{"task": "Прислать смету Ивану", "priority": "medium", "due_date": "среда", "due_hint": "до среды"}]
→ related_categories: ["people"]

Заметка: "У Ивана день рождения 15 марта, любит виски"
→ category: "people", subcategory: "contacts"
→ structured_data: {"person_name": "Иван", "context": "день рождения 15 марта, любит виски"}

Заметка: "Созвон с Иваном по ремонту Tesla — нужно менять тормозные колодки"
→ category: "auto", subcategory: "repairs"
→ structured_data: {"vehicle": "Tesla", "issue": "замена тормозных колодок", "person_name": "Иван"}
→ related_categories: ["people"]

Заметка: "Сергей порекомендовал врача-кардиолога, записаться на приём"
→ category: "health", subcategory: "doctor_visits"
→ action_items: [{"task": "Записаться к кардиологу", "priority": "medium", "due_date": null}]
→ structured_data: {"person_name": "Сергей"}
→ related_categories: ["people"]

Заметка: "Обедали с Мариной, она принесла домашний тирамису — невероятно вкусно"
→ category: "food", subcategory: "meals"
→ structured_data: {"meal_type": "lunch", "foods": ["тирамису"], "person_name": "Марина"}
→ related_categories: ["people"]

Заметка: "Марина — диетолог, специализируется на спортивном питании"
→ category: "people", subcategory: "contacts"
→ structured_data: {"person_name": "Марина", "context": "диетолог, спортивное питание"}

Заметка: "Игорь скинул контакт юриста, надо позвонить по поводу договора аренды"
→ category: "business", subcategory: "tasks"
→ action_items: [{"task": "Позвонить юристу по договору аренды", "priority": "medium", "due_date": null}]
→ structured_data: {"person_name": "Игорь"}
→ related_categories: ["people"]

Заметка: "Тренировка с Алексом — жим лёжа 80кг, присед 100кг"
→ category: "fitness", subcategory: "exercise"
→ structured_data: {"exercise_type": "силовая", "person_name": "Алекс", "person_role": "тренер"}
→ related_categories: ["people"]

Заметка: "Алекс — мой тренер, работает в FitLife на Ленина"
→ category: "people", subcategory: "contacts"
→ structured_data: {"person_name": "Алекс", "person_role": "тренер", "place_name": "FitLife", "context": "тренер, FitLife на Ленина"}

## Правила извлечения entities (structured_data.person_name, vehicle и т.д.):
- Извлекай ТОЛЬКО полезные для поиска и связей сущности:
  - **person_name**: имена людей (Иван, Марина, доктор Петров) — ДА
  - **person_role**: роль/профессия человека если упомянута (врач, тренер, юрист, клиент, сосед) — ДА
  - **vehicle**: марка/модель транспорта (Tesla, Model 3, BMW X5) — ДА
  - **place_name**: конкретные места и организации (FitLife, клиника на Ленина, Перекрёсток) — ДА
  - **project_name**: названия проектов, компаний в бизнес-контексте — ДА
- НЕ извлекай как entity:
  - еду (йогурт, паста, кофе) → это food fact/tag
  - бытовые предметы (фильтр, лампочка) → это часть task description
  - абстрактные понятия (здоровье, спорт) → это category/tag
- Правило: entity = то, что имеет смысл искать повторно и связывать между заметками

## Правила извлечения structured_data:

Для **food/meals**: {"meal_type": "breakfast|lunch|dinner|snack", "calories_est": число, "foods": ["еда1", "еда2"], "water_ml": число}
Для **fitness/weight**: {"weight_kg": число}
Для **fitness/exercise**: {"exercise_type": "тип", "duration_min": число}
Для **fitness/sleep**: {"sleep_hours": число, "sleep_quality": число 1-10}
Для **health/symptoms**: {"symptom_name": "название", "severity_1_10": число, "pain_location": "где болит"}
Для **health/medications**: {"medication_name": "название", "dosage": "доза"}
Для **personal/mood**: {"mood_score_1_10": число, "emotion": "эмоция"}
Для **business/tasks** или **ideas**: {"action_items": [{"task": "описание", "priority": "high|medium|low", "due_date": "дата или null", "due_hint": "оригинальная фраза дедлайна из текста, например 'до пятницы', 'к среде', 'завтра'"}]}

## Правила priority для action_items:
- **high**: ТОЛЬКО если есть явное слово «срочно», «немедленно», «критично», «ASAP», или дедлайн ≤ 1 день
- **medium**: есть конкретный дедлайн (до пятницы, к среде, через неделю) — но нет слов-усилителей
- **low**: нет ни дедлайна, ни слов-усилителей, просто «было бы неплохо», «когда-нибудь»
- Не ставь high только из-за наличия дедлайна — «до пятницы» это medium, не high
Для **finance/expenses**: {"amount": число, "currency": "USD|RUB|EUR", "description": "описание"}
Для **learning**: {"key_insight": "главный вывод"}
Для **goals**: {"goal_description": "описание", "deadline": "дата если есть"}
Для **people**: {"person_name": "имя", "context": "контекст знакомства/встречи"}
Для **auto**: {"vehicle": "марка/модель", "issue": "описание проблемы", "cost": число}
ВАЖНО: Если в заметке упомянут человек, ВСЕГДА добавляй "person_name" в structured_data, независимо от категории.
ВАЖНО: Если упомянута роль/профессия человека, добавь "person_role" (врач, тренер, юрист, клиент, сосед и т.д.)
ВАЖНО: Если упомянуто конкретное место/организация, добавь "place_name" (название клиники, спортзала, магазина, ресторана)

## Правила:
- Оцени калории по описанию еды максимально точно (каждый продукт отдельно)
- mood_score_1_10: если явно не указано, определи по тону заметки (null если невозможно)
- Если заметка затрагивает несколько категорий — выбери основную по ДОМЕНУ ДЕЙСТВИЯ, остальные укажи в related_categories
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

        # Post-processing: override weak "people" when dominant intent is action/domain
        action_items = data.get("action_items", [])
        if category == "people" and action_items:
            override = self._detect_domain_override(original, data)
            if override:
                logger.info(
                    f"Category override: people → {override} "
                    f"(action_items present, domain markers detected)"
                )
                category = override
                # Preserve person in related_categories
                related = data.get("related_categories", [])
                if "people" not in related:
                    related.append("people")
                data["related_categories"] = related

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

    # Domain markers for post-processing override of "people" category
    _DOMAIN_MARKERS: dict[str, list[str]] = {
        "auto": [
            "tesla", "машина", "авто", "фильтр", "масло", "ремонт", "сервис",
            "колодки", "шины", "тормоз", "двигатель", "то ", "техосмотр",
        ],
        "health": [
            "врач", "доктор", "анализ", "рецепт", "таблетк", "лекарств",
            "больниц", "клиник", "симптом", "давлени", "прием",
        ],
        "business": [
            "смета", "счёт", "счет", "контракт", "договор", "проект",
            "клиент", "оплат", "бюджет", "презентац", "отчёт", "отчет",
        ],
        "finance": [
            "оплатить", "перевести", "долг", "кредит", "ипотек",
            "страховк", "налог",
        ],
        "food": [
            "продукты", "магазин", "рецепт", "приготов", "ужин", "обед",
            "завтрак", "закупк",
        ],
        "fitness": [
            "тренировк", "зал ", "спортзал", "пробежк", "бассейн",
        ],
    }
    # Action markers that signal a task/intent (not just a mention)
    _ACTION_MARKERS: list[str] = [
        "нужно", "надо", "заказать", "купить", "позвонить", "записаться",
        "отправить", "прислать", "сделать", "оплатить", "забрать",
        "починить", "поменять", "проверить", "до ", "к ",
    ]

    def _detect_domain_override(self, text: str, data: dict) -> str | None:
        """Detect if a 'people'-classified note should be overridden by domain.

        Returns target category or None if people is correct.
        """
        lower = text.lower()

        # Must have action intent
        has_action = any(m in lower for m in self._ACTION_MARKERS)
        if not has_action:
            return None

        # Find strongest domain match
        best_cat = None
        best_score = 0
        for cat, markers in self._DOMAIN_MARKERS.items():
            score = sum(1 for m in markers if m in lower)
            if score > best_score:
                best_score = score
                best_cat = cat

        if best_score >= 1:
            return best_cat

        # If action_items present but no domain match, default to business/tasks
        if data.get("action_items"):
            return "business"

        return None

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
