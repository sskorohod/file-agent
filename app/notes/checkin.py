"""Evening Check-in — Telegram bot asks missing daily questions.

v1: category-based (CHECKIN_QUESTIONS, get_missing_categories)
v2: signal-based  (CHECKIN_PROMPTS, get_missing_signals, build_question_plan)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy v1: questions per category
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# v2: signal-based prompts with tiers
# ---------------------------------------------------------------------------

CHECKIN_PROMPTS: dict[str, dict] = {
    # Tier 1 — core daily signals, ask first
    "food_intake": {
        "question": "🍽 Что было на завтрак, обед, ужин и перекусы сегодня?",
        "emoji": "🍽",
        "type": "text",
        "tier": 1,
    },
    "mood_score": {
        "question": "💭 Как настроение сегодня по шкале от 1 до 10?",
        "emoji": "💭",
        "type": "mood_scale",
        "tier": 1,
    },
    "sleep_hours": {
        "question": "😴 Сколько часов спал прошлой ночью?",
        "emoji": "😴",
        "type": "text",
        "tier": 1,
    },
    # Tier 2 — ask if missing and budget allows
    "health_status": {
        "question": "🏥 Как самочувствие? Были боль, симптомы или жалобы?",
        "emoji": "🏥",
        "type": "text",
        "tier": 2,
    },
    "activity": {
        "question": "🏃 Была ли сегодня тренировка, прогулка или заметная активность?",
        "emoji": "🏃",
        "type": "text",
        "tier": 2,
    },
    "weight_kg": {
        "question": "⚖️ Вес сегодня? (в кг)",
        "emoji": "⚖️",
        "type": "text",
        "tier": 2,
    },
    # Tier 3 — closing / reflective, one optional
    "daily_reflection": {
        "question": "📝 Что было самым важным за сегодня?",
        "emoji": "📝",
        "type": "text",
        "tier": 3,
    },
    "tomorrow_reminder": {
        "question": "⏰ Что важно не забыть завтра?",
        "emoji": "⏰",
        "type": "text",
        "tier": 3,
    },
}


# ---------------------------------------------------------------------------
# Signal context — cached DB queries for one check-in run
# ---------------------------------------------------------------------------

@dataclass
class _SignalContext:
    facts: dict = field(default_factory=dict)       # {key: {total, avg}}
    food: list = field(default_factory=list)         # food_entries rows
    notes: list = field(default_factory=list)        # daily notes


# ---------------------------------------------------------------------------
# State helpers — v2 stores dict, legacy stores list
# ---------------------------------------------------------------------------

def _parse_state(raw: str) -> tuple[list[str], list[str]]:
    """Parse checkin_state.questions_asked into (remaining, skipped)."""
    data = json.loads(raw)
    if isinstance(data, list):
        return data, []  # legacy format
    return data.get("remaining", []), data.get("skipped", [])


def _serialize_state(remaining: list[str], skipped: list[str]) -> str:
    """Serialize v2 state."""
    return json.dumps({"remaining": remaining, "skipped": skipped})


class EveningCheckin:
    """Manages evening check-in conversation via Telegram."""

    def __init__(
        self,
        db,
        note_agent=None,
        expected_categories: list[str] | None = None,
        expected_signals: list[str] | None = None,
        capture=None,
        max_questions: int = 5,
        include_closing: bool = True,
        weight_frequency_days: int = 7,
    ):
        self.db = db
        self.note_agent = note_agent
        self._capture = capture

        # v2 mode activates when expected_signals is explicitly provided
        self._v2_mode = expected_signals is not None
        self.expected_signals = expected_signals or []
        self.max_questions = max_questions
        self.include_closing = include_closing
        self.weight_frequency_days = weight_frequency_days

        # Legacy
        self.expected_categories = expected_categories or ["food", "personal", "fitness"]

    # ------------------------------------------------------------------
    # v1 legacy: category-based coverage
    # ------------------------------------------------------------------

    async def get_missing_categories(self, date: str) -> list[str]:
        """Determine which expected categories have no notes today."""
        notes = await self.db.get_daily_notes(date)

        covered = set()
        for note in notes:
            cat = note.get("category", "")
            if cat:
                covered.add(cat)
            subcat = note.get("subcategory", "")
            if subcat == "sleep":
                covered.add("sleep")
            if subcat == "gratitude":
                covered.add("gratitude")

        return [c for c in self.expected_categories if c not in covered]

    # ------------------------------------------------------------------
    # v2: signal-based coverage
    # ------------------------------------------------------------------

    async def _is_signal_covered(self, signal_id: str, date: str, ctx: _SignalContext) -> bool:
        """Check if a signal has already been captured today."""
        if signal_id == "food_intake":
            if ctx.food:
                return True
            return "calories" in ctx.facts

        if signal_id == "mood_score":
            return "mood_score" in ctx.facts

        if signal_id == "sleep_hours":
            return "sleep_hours" in ctx.facts

        if signal_id == "health_status":
            return any(
                n.get("category") == "health" or n.get("subcategory") == "health"
                for n in ctx.notes
            )

        if signal_id == "activity":
            return "exercise_min" in ctx.facts or "steps" in ctx.facts

        if signal_id == "weight_kg":
            if "weight_kg" in ctx.facts:
                return True
            # Frequency gate: skip if recorded within last N days
            trend = await self.db.get_facts_trend("weight_kg", self.weight_frequency_days)
            return len(trend) > 0

        # Tier 3 reflective — always considered missing
        if signal_id in ("daily_reflection", "tomorrow_reminder"):
            return False

        return False

    async def get_missing_signals(self, date: str) -> list[str]:
        """Return signal IDs not yet covered today."""
        ctx = _SignalContext(
            facts=await self.db.get_daily_facts(date),
            food=await self.db.get_food_entries_by_date(date),
            notes=await self.db.get_daily_notes(date),
        )
        return [
            s for s in self.expected_signals
            if s in CHECKIN_PROMPTS and not await self._is_signal_covered(s, date, ctx)
        ]

    async def build_question_plan(self, date: str) -> list[str]:
        """Ordered signal list within budget: tier 1 first, then 2, then tier 3.
        Tier 2+ signals with >70% skip rate are auto-excluded."""
        missing = await self.get_missing_signals(date)
        by_tier = sorted(missing, key=lambda s: CHECKIN_PROMPTS[s]["tier"])

        # Load skip rates for adaptive filtering
        skip_rates = await self.db.get_all_signal_skip_rates(30)

        plan: list[str] = []
        for sig in by_tier:
            if len(plan) >= self.max_questions:
                break
            tier = CHECKIN_PROMPTS[sig]["tier"]
            # Tier 2+ signals: skip if user always skips them (>70%)
            if tier >= 2 and skip_rates.get(sig, 0) > 0.7:
                continue
            if tier <= 2:
                plan.append(sig)
            elif tier == 3 and self.include_closing:
                plan.append(sig)
        return plan

    # ------------------------------------------------------------------
    # Check-in flow
    # ------------------------------------------------------------------

    async def run_checkin(self, tg_app, chat_id: int):
        """Start evening check-in: send first missing question."""
        today = datetime.now().strftime("%Y-%m-%d")

        state = await self.db.get_checkin_state(today)
        if state and state.get("completed"):
            return

        if self._v2_mode:
            plan = await self.build_question_plan(today)
        else:
            plan = await self.get_missing_categories(today)

        if not plan:
            await self._send_daily_summary(tg_app, chat_id, today)
            await self.db.save_checkin_state(today, "[]", completed=1)
            return

        # Save initial state
        if self._v2_mode:
            state_json = _serialize_state(plan, [])
        else:
            state_json = json.dumps(plan)
        await self.db.save_checkin_state(today, state_json, completed=0)

        greeting = "🌙 Добрый вечер! Давай подведём итоги дня.\n\n"
        await tg_app.bot.send_message(chat_id=chat_id, text=greeting)
        await self._send_question(tg_app, chat_id, plan[0], today)

    async def _send_question(self, tg_app, chat_id: int, signal_or_cat: str, date: str):
        """Send a check-in question with inline buttons."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._v2_mode:
            q = CHECKIN_PROMPTS.get(signal_or_cat)
        else:
            q = CHECKIN_QUESTIONS.get(signal_or_cat)
        if not q:
            return

        if q["type"] == "mood_scale":
            buttons = []
            row = []
            for i in range(1, 11):
                row.append(InlineKeyboardButton(str(i), callback_data=f"ci:score:{i}:{signal_or_cat}"))
                if i % 5 == 0:
                    buttons.append(row)
                    row = []
            buttons.append([
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"ci:skip:{signal_or_cat}"),
            ])
            keyboard = InlineKeyboardMarkup(buttons)
            await tg_app.bot.send_message(chat_id=chat_id, text=q["question"], reply_markup=keyboard)
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"ci:skip:{signal_or_cat}"),
                InlineKeyboardButton("✅ Завершить", callback_data="ci:done"),
            ]])
            await tg_app.bot.send_message(chat_id=chat_id, text=q["question"], reply_markup=keyboard)

    async def handle_answer(self, text: str, chat_id: int, tg_app, category: str = ""):
        """Process a check-in answer: capture note and ask next question."""
        today = datetime.now().strftime("%Y-%m-%d")

        # Record answer for adaptive checkin
        if self._v2_mode and category:
            try:
                await self.db.record_checkin_signal(category, today, "answered")
            except Exception:
                pass

        # In v2, prefix with signal context for structured capture
        if self._v2_mode and category:
            prompt = CHECKIN_PROMPTS.get(category, {})
            emoji = prompt.get("emoji", "")
            capture_text = f"{emoji} Check-in / {category}: {text}"
        else:
            capture_text = text

        if self._capture:
            await self._capture.capture(capture_text, source="checkin")
        else:
            await self.db.save_note(content=capture_text, source="checkin", title="")

        # Advance state
        state = await self.db.get_checkin_state(today)
        if not state:
            return

        raw = state.get("questions_asked", "[]")
        remaining, skipped = _parse_state(raw)
        if category and category in remaining:
            remaining.remove(category)
            if self._v2_mode:
                new_state = _serialize_state(remaining, skipped)
            else:
                new_state = json.dumps(remaining)
            await self.db.save_checkin_state(today, new_state, completed=0)

        if remaining:
            await self._send_question(tg_app, chat_id, remaining[0], today)
        else:
            await self.db.save_checkin_state(today, "[]", completed=1)
            await self._send_daily_summary(tg_app, chat_id, today)

    async def handle_skip(self, chat_id: int, tg_app, category: str):
        """Skip current question, move to next."""
        today = datetime.now().strftime("%Y-%m-%d")

        # Record skip for adaptive checkin
        if self._v2_mode and category:
            try:
                await self.db.record_checkin_signal(category, today, "skipped")
            except Exception:
                pass

        state = await self.db.get_checkin_state(today)
        if not state:
            return

        raw = state.get("questions_asked", "[]")
        remaining, skipped = _parse_state(raw)
        if category in remaining:
            remaining.remove(category)
            skipped.append(category)
            if self._v2_mode:
                new_state = _serialize_state(remaining, skipped)
            else:
                new_state = json.dumps(remaining)
            await self.db.save_checkin_state(today, new_state, completed=0)

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
