"""Evening Check-in — Telegram bot asks missing daily questions."""

from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Questions per category with templates
CHECKIN_QUESTIONS = {
    "food": {
        "question": "🍽 Что ты ел сегодня? Опиши кратко приёмы пищи.",
        "emoji": "🍽",
        "type": "text",
    },
    "personal": {
        "question": "💭 Как настроение? Оцени от 1 до 10.",
        "emoji": "💭",
        "type": "mood_scale",
    },
    "fitness": {
        "question": "💪 Вес сегодня? (в кг)",
        "emoji": "💪",
        "type": "text",
    },
    "sleep": {
        "question": "😴 Сколько часов спал прошлой ночью?",
        "emoji": "😴",
        "type": "text",
    },
    "gratitude": {
        "question": "🙏 Назови одну хорошую вещь, которая произошла сегодня.",
        "emoji": "🙏",
        "type": "text",
    },
    "business": {
        "question": "💼 Итоги дня по работе — что сделал, что запланировал?",
        "emoji": "💼",
        "type": "text",
    },
    "health": {
        "question": "🏥 Как самочувствие? Есть жалобы?",
        "emoji": "🏥",
        "type": "text",
    },
}


class EveningCheckin:
    """Manages evening check-in conversation via Telegram."""

    def __init__(self, db, note_agent=None, expected_categories: list[str] | None = None, capture=None):
        self.db = db
        self.note_agent = note_agent
        self._capture = capture
        self.expected_categories = expected_categories or ["food", "personal", "fitness"]

    async def get_missing_categories(self, date: str) -> list[str]:
        """Determine which expected categories have no notes today."""
        notes = await self.db.get_daily_notes(date)

        covered = set()
        for note in notes:
            cat = note.get("category", "")
            if cat:
                covered.add(cat)
            # Map subcategories to expected categories
            subcat = note.get("subcategory", "")
            if subcat == "sleep":
                covered.add("sleep")
            if subcat == "gratitude":
                covered.add("gratitude")

        missing = [c for c in self.expected_categories if c not in covered]
        return missing

    async def run_checkin(self, tg_app, chat_id: int):
        """Start evening check-in: send first missing question."""
        today = datetime.now().strftime("%Y-%m-%d")

        # Check if already completed today
        state = await self.db.get_checkin_state(today)
        if state and state.get("completed"):
            return

        missing = await self.get_missing_categories(today)
        if not missing:
            # All categories covered — send summary
            await self._send_daily_summary(tg_app, chat_id, today)
            await self.db.save_checkin_state(today, "[]", completed=1)
            return

        # Start check-in
        await self.db.save_checkin_state(today, json.dumps(missing), completed=0)

        greeting = "🌙 Добрый вечер! Давай подведём итоги дня.\n\n"
        await tg_app.bot.send_message(chat_id=chat_id, text=greeting)

        # Send first question
        await self._send_question(tg_app, chat_id, missing[0], today)

    async def _send_question(self, tg_app, chat_id: int, category: str, date: str):
        """Send a check-in question for a specific category."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        q = CHECKIN_QUESTIONS.get(category)
        if not q:
            return

        if q["type"] == "mood_scale":
            # Send mood scale with inline buttons
            buttons = []
            row = []
            for i in range(1, 11):
                row.append(InlineKeyboardButton(str(i), callback_data=f"ci:score:{i}:{category}"))
                if i % 5 == 0:
                    buttons.append(row)
                    row = []
            buttons.append([
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"ci:skip:{category}"),
            ])
            keyboard = InlineKeyboardMarkup(buttons)
            await tg_app.bot.send_message(
                chat_id=chat_id,
                text=q["question"],
                reply_markup=keyboard,
            )
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"ci:skip:{category}"),
                InlineKeyboardButton("✅ Завершить", callback_data="ci:done"),
            ]])
            await tg_app.bot.send_message(
                chat_id=chat_id,
                text=q["question"],
                reply_markup=keyboard,
            )

    async def handle_answer(self, text: str, chat_id: int, tg_app, category: str = ""):
        """Process a check-in answer: capture note and ask next question."""
        today = datetime.now().strftime("%Y-%m-%d")

        # Capture via service (async enrichment)
        if self._capture:
            note_id = await self._capture.capture(text, source="checkin")
        else:
            note_id = await self.db.save_note(content=text, source="checkin", title="")

        # Get remaining questions
        state = await self.db.get_checkin_state(today)
        if not state:
            return

        remaining = json.loads(state.get("questions_asked", "[]"))
        if category and category in remaining:
            remaining.remove(category)
            await self.db.save_checkin_state(today, json.dumps(remaining), completed=0)

        if remaining:
            # Ask next question
            await self._send_question(tg_app, chat_id, remaining[0], today)
        else:
            # All done
            await self.db.save_checkin_state(today, "[]", completed=1)
            await self._send_daily_summary(tg_app, chat_id, today)

    async def handle_skip(self, chat_id: int, tg_app, category: str):
        """Skip current question, move to next."""
        today = datetime.now().strftime("%Y-%m-%d")

        state = await self.db.get_checkin_state(today)
        if not state:
            return

        remaining = json.loads(state.get("questions_asked", "[]"))
        if category in remaining:
            remaining.remove(category)
            await self.db.save_checkin_state(today, json.dumps(remaining), completed=0)

        if remaining:
            await self._send_question(tg_app, chat_id, remaining[0], today)
        else:
            await self.db.save_checkin_state(today, "[]", completed=1)
            await self._send_daily_summary(tg_app, chat_id, today)

    async def handle_mood_score(self, score: int, chat_id: int, tg_app, category: str):
        """Handle mood score from inline keyboard."""
        text = f"Настроение сегодня: {score}/10"
        await self.handle_answer(text, chat_id, tg_app, category)

    async def handle_done(self, chat_id: int, tg_app):
        """End check-in early."""
        today = datetime.now().strftime("%Y-%m-%d")
        await self.db.save_checkin_state(today, "[]", completed=1)
        await self._send_daily_summary(tg_app, chat_id, today)

    async def _send_daily_summary(self, tg_app, chat_id: int, date: str):
        """Send end-of-day summary."""
        notes = await self.db.get_daily_notes(date)
        metrics = await self.db.get_daily_facts(date) if hasattr(self.db, 'get_daily_facts') else await self.db.get_daily_metrics(date)

        parts = [f"📊 Итоги дня ({date}):", ""]

        # Notes count by category
        by_cat: dict[str, int] = {}
        for n in notes:
            cat = n.get("category", "other") or "other"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        if by_cat:
            parts.append(f"📝 Заметок: {len(notes)}")
            for cat, cnt in sorted(by_cat.items()):
                parts.append(f"  • {cat}: {cnt}")

        # Metrics
        if "calories" in metrics:
            parts.append(f"🍽 Калории: ~{int(metrics['calories']['total'])} kcal")
        if "mood_score" in metrics:
            parts.append(f"💭 Настроение: {metrics['mood_score']['avg']:.1f}/10")
        if "weight_kg" in metrics:
            parts.append(f"⚖️ Вес: {metrics['weight_kg']['avg']:.1f} кг")
        if "sleep_hours" in metrics:
            parts.append(f"😴 Сон: {metrics['sleep_hours']['total']:.1f}ч")

        if not notes:
            parts.append("Заметок сегодня не было.")

        parts.append("\nСпокойной ночи! 🌙")

        await tg_app.bot.send_message(chat_id=chat_id, text="\n".join(parts))
