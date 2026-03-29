"""Morning Briefing Engine — personalized daily brief from notes, facts, tasks, reminders."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

MORNING_BRIEF_SYSTEM = """Ты — персональный утренний помощник. Составь короткий, конкретный, мотивирующий brief на основе данных пользователя.

Контекст пользователя:

{context}

Правила:
- Начни с "Доброе утро! 🌅"
- Выбери ОДНУ главную тему дня (наиболее срочное/важное)
- Дай 2-3 конкретных действия (не общие советы, а привязанные к данным)
- Если есть предупреждение (плохой сон, пропуски, просрочки) — одно предупреждение максимум
- Заверши одним предложением-мотиватором
- Не повторяй завершённые задачи
- Не выдумывай задач или данных, которых нет в контексте
- Если данных мало — скажи только то, что точно известно
- Пиши на русском, кратко
- Максимум 8-10 строк

Формат ответа — обычный текст для Telegram, не JSON.
"""


# ---------------------------------------------------------------------------
# Deterministic priority scoring
# ---------------------------------------------------------------------------

def _score_signals(context: dict) -> list[tuple[str, float, str]]:
    """Score signals for priority ranking. Returns [(type, score, description), ...]."""
    scored: list[tuple[str, float, str]] = []

    # Overdue tasks — highest priority
    for t in context.get("overdue_tasks", []):
        scored.append(("today_focus", 100.0, f"Просрочено: {t['description'][:80]}"))

    # Due-today tasks
    for t in context.get("due_today_tasks", []):
        scored.append(("today_focus", 80.0, f"Сегодня: {t['description'][:80]}"))

    # High-priority open tasks
    for t in context.get("high_priority_tasks", []):
        scored.append(("today_focus", 60.0, f"Важно: {t['description'][:80]}"))

    # Due-today/tomorrow reminders
    for r in context.get("due_reminders", []):
        scored.append(("reminder_followup", 70.0, f"Напоминание: {r['description'][:80]}"))

    # Health warnings
    sleep = context.get("yesterday_metrics", {}).get("sleep_hours")
    if sleep is not None and sleep < 6:
        scored.append(("health", 50.0, f"Сон вчера: {sleep:.1f}ч — ниже нормы"))

    mood = context.get("yesterday_metrics", {}).get("mood_score")
    if mood is not None and mood < 5:
        scored.append(("health", 45.0, f"Настроение вчера: {mood:.0f}/10 — низкое"))

    # Sleep trend — 2+ consecutive low days
    sleep_trend = context.get("sleep_trend_3d", [])
    low_sleep_days = sum(1 for d in sleep_trend if d.get("avg", 8) < 6)
    if low_sleep_days >= 2:
        scored.append(("health", 55.0, f"Сон ниже 6ч уже {low_sleep_days} дня подряд"))

    # Missing food data yesterday
    if not context.get("yesterday_had_food"):
        scored.append(("food", 30.0, "Вчера не зафиксирована еда"))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

async def _build_context(db, date: str) -> dict:
    """Assemble structured context from DB for morning briefing."""
    yesterday = (datetime.fromisoformat(date) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Yesterday's summary
    yesterday_facts = await db.get_daily_facts(yesterday)
    yesterday_notes = await db.get_daily_notes(yesterday)

    yesterday_metrics = {}
    for key in ("sleep_hours", "mood_score", "calories", "weight_kg", "energy"):
        if key in yesterday_facts:
            yesterday_metrics[key] = yesterday_facts[key]["avg"] if key != "calories" else yesterday_facts[key]["total"]

    # Yesterday category counts
    by_cat: dict[str, int] = {}
    for n in yesterday_notes:
        cat = n.get("category", "other") or "other"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    # Food check — food entries or calories fact
    yesterday_food = await db.get_food_entries_by_date(yesterday)
    yesterday_had_food = bool(yesterday_food) or "calories" in yesterday_facts

    # Open tasks
    open_tasks = await db.get_open_tasks(limit=20)

    today_dt = datetime.fromisoformat(date)
    today_str_lower = date

    overdue_tasks = []
    due_today_tasks = []
    high_priority_tasks = []

    for t in open_tasks:
        due = t.get("due_date", "")
        priority = t.get("priority", "medium")

        if due and due < today_str_lower:
            overdue_tasks.append(t)
        elif due and due.startswith(today_str_lower):
            due_today_tasks.append(t)
        elif priority == "high":
            high_priority_tasks.append(t)

    # Reminders due today/tomorrow
    tomorrow = (today_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    all_reminders = await db.list_note_reminders(include_done=False)
    due_reminders = [
        r for r in all_reminders
        if r.get("remind_at", "")[:10] in (today_str_lower, tomorrow)
    ]

    # Recent trends (3 days)
    sleep_trend = await db.get_facts_trend("sleep_hours", 3)
    mood_trend = await db.get_facts_trend("mood_score", 3)

    # Recent enriched notes (last 3 days, top 5)
    recent_notes = []
    for day_offset in range(3):
        d = (today_dt - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        notes = await db.get_daily_notes(d)
        for n in notes:
            if n.get("category") and n.get("user_title"):
                recent_notes.append({
                    "title": n["user_title"][:60],
                    "category": n["category"],
                    "date": d,
                })
        if len(recent_notes) >= 5:
            break
    recent_notes = recent_notes[:5]

    return {
        "date": date,
        "yesterday": yesterday,
        "yesterday_metrics": yesterday_metrics,
        "yesterday_notes_count": len(yesterday_notes),
        "yesterday_categories": by_cat,
        "yesterday_had_food": yesterday_had_food,
        "overdue_tasks": overdue_tasks,
        "due_today_tasks": due_today_tasks,
        "high_priority_tasks": high_priority_tasks[:3],
        "due_reminders": due_reminders[:5],
        "sleep_trend_3d": sleep_trend,
        "mood_trend_3d": mood_trend,
        "recent_notes": recent_notes,
    }


def _format_context_for_prompt(context: dict, scored: list[tuple[str, float, str]]) -> str:
    """Format context + scored signals into text for LLM prompt."""
    parts: list[str] = []

    # Date
    parts.append(f"Дата: {context['date']}")

    # Yesterday summary
    ym = context.get("yesterday_metrics", {})
    if ym:
        lines = []
        if "sleep_hours" in ym:
            lines.append(f"Сон: {ym['sleep_hours']:.1f}ч")
        if "mood_score" in ym:
            lines.append(f"Настроение: {ym['mood_score']:.0f}/10")
        if "calories" in ym:
            lines.append(f"Калории: ~{int(ym['calories'])} kcal")
        if "weight_kg" in ym:
            lines.append(f"Вес: {ym['weight_kg']:.1f} кг")
        if lines:
            parts.append(f"Вчера: {', '.join(lines)}")
    elif context.get("yesterday_notes_count", 0) > 0:
        parts.append(f"Вчера: {context['yesterday_notes_count']} заметок")
    else:
        parts.append("Вчера: данных нет")

    # Priority signals (from deterministic scoring)
    if scored:
        top = scored[:5]
        parts.append("\nПриоритеты:")
        for typ, score, desc in top:
            parts.append(f"  [{typ}] {desc}")

    # Open tasks summary
    overdue = context.get("overdue_tasks", [])
    due_today = context.get("due_today_tasks", [])
    high = context.get("high_priority_tasks", [])
    if overdue or due_today or high:
        parts.append(f"\nЗадачи: {len(overdue)} просрочено, {len(due_today)} на сегодня, {len(high)} высокий приоритет")

    # Reminders
    due_rem = context.get("due_reminders", [])
    if due_rem:
        parts.append(f"Напоминания на сегодня/завтра: {len(due_rem)}")
        for r in due_rem[:3]:
            parts.append(f"  - {r['description'][:60]} ({r.get('remind_at', '')[:10]})")

    # Recent notes (compressed)
    recent = context.get("recent_notes", [])
    if recent:
        titles = [f"{n['title']} ({n['category']})" for n in recent[:3]]
        parts.append(f"\nПоследние заметки: {'; '.join(titles)}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class MorningBriefingEngine:
    """Generate personalized morning briefing from notes/facts/tasks/reminders."""

    def __init__(self, db, llm_router):
        self.db = db
        self.llm = llm_router

    async def generate_morning_brief(self, date: str | None = None) -> dict:
        """Build context, score priorities, generate LLM brief.

        Returns dict with keys: date, headline, text, priorities, warnings, metrics.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        context = await _build_context(self.db, date)
        scored = _score_signals(context)

        # Check if enough data to generate meaningful brief
        has_data = (
            context.get("yesterday_notes_count", 0) > 0
            or context.get("overdue_tasks")
            or context.get("due_today_tasks")
            or context.get("due_reminders")
        )

        if not has_data:
            return {
                "date": date,
                "text": "Доброе утро! 🌅\n\nВчера данных не было. Начни день с одной простой заметки — это запустит систему рекомендаций.",
                "priorities": [],
                "warnings": [],
                "metrics": context.get("yesterday_metrics", {}),
            }

        # Format context for LLM
        context_text = _format_context_for_prompt(context, scored)
        prompt = MORNING_BRIEF_SYSTEM.format(context=context_text)

        try:
            response = await self.llm.extract(text="Составь утренний brief", system=prompt)
            text = response.text.strip()
        except Exception as e:
            logger.error(f"Morning brief generation failed: {e}")
            text = self._fallback_brief(context, scored)

        # Extract structured data for optional UI
        priorities = [desc for typ, _, desc in scored[:3] if typ == "today_focus"]
        warnings = [desc for typ, _, desc in scored if typ == "health"]

        return {
            "date": date,
            "text": text,
            "priorities": priorities,
            "warnings": warnings[:1],
            "metrics": context.get("yesterday_metrics", {}),
        }

    def _fallback_brief(self, context: dict, scored: list[tuple[str, float, str]]) -> str:
        """Generate brief without LLM when it fails."""
        parts = ["Доброе утро! 🌅\n"]

        if scored:
            top = scored[0]
            parts.append(f"Главное на сегодня: {top[2]}")

        ym = context.get("yesterday_metrics", {})
        if "sleep_hours" in ym:
            parts.append(f"Сон вчера: {ym['sleep_hours']:.1f}ч")

        overdue = context.get("overdue_tasks", [])
        if overdue:
            parts.append(f"Просроченных задач: {len(overdue)}")

        parts.append("\nУдачного дня!")
        return "\n".join(parts)

    def format_telegram_brief(self, brief: dict) -> str:
        """Format brief dict for Telegram. Returns the text field."""
        return brief.get("text", "")
