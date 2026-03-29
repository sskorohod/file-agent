"""Reminder Extraction Service — detect reminder intent and create scheduled reminders."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Explicit reminder intent markers
REMINDER_MARKERS = [
    "напомни", "не забыть", "напомнить", "поставь напоминание",
    "обязательно напомни", "remind",
]

# Weekday name → Python weekday (Monday=0)
_WEEKDAY_MAP = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среда": 2, "среду": 2, "среды": 2,
    "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4,
    "суббота": 5, "субботу": 5, "субботы": 5,
    "воскресенье": 6, "воскресенью": 6, "воскресенья": 6,
}


def parse_due_datetime(text: str, reference: datetime) -> datetime | None:
    """Parse natural language due date relative to reference datetime.

    Supports: сегодня, завтра, послезавтра, через N дней,
    weekday names (в понедельник, до пятницы), ISO dates (2026-04-05).
    """
    if not text:
        return None

    lower = text.lower().strip()

    # Exact ISO date: 2026-04-05
    iso_match = re.match(r"(\d{4}-\d{2}-\d{2})", lower)
    if iso_match:
        try:
            dt = datetime.fromisoformat(iso_match.group(1))
            return dt.replace(hour=9, minute=0, second=0, microsecond=0)
        except ValueError:
            pass

    # Relative: сегодня, завтра, послезавтра
    if "сегодня" in lower:
        return reference.replace(hour=18, minute=0, second=0, microsecond=0)
    if "послезавтра" in lower:
        return (reference + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "завтра" in lower:
        return (reference + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    # через N дней/дня
    m = re.search(r"через\s+(\d+)\s+дн", lower)
    if m:
        days = int(m.group(1))
        return (reference + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)

    # через N часов/часа
    m = re.search(r"через\s+(\d+)\s+час", lower)
    if m:
        hours = int(m.group(1))
        return reference + timedelta(hours=hours)

    # через неделю
    if "через неделю" in lower:
        return (reference + timedelta(weeks=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    # через месяц
    if "через месяц" in lower:
        return (reference + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)

    # Weekday: в понедельник, до пятницы, к среде
    is_before = "до " in lower or "к " in lower
    for name, wd in _WEEKDAY_MAP.items():
        if name in lower:
            days_ahead = (wd - reference.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = reference + timedelta(days=days_ahead)
            if is_before:
                # "до пятницы" → day before at 18:00
                target -= timedelta(days=1)
                return target.replace(hour=18, minute=0, second=0, microsecond=0)
            return target.replace(hour=9, minute=0, second=0, microsecond=0)

    return None


def has_explicit_reminder_intent(text: str) -> bool:
    """Check if text contains explicit reminder request."""
    lower = text.lower()
    return any(m in lower for m in REMINDER_MARKERS)


# Recurrence rules: daily, weekly, weekdays, monthly, weekly:MON, weekly:MON,WED,FRI
RECURRENCE_MARKERS = {
    "каждый день": "daily",
    "ежедневно": "daily",
    "каждую неделю": "weekly",
    "еженедельно": "weekly",
    "каждый месяц": "monthly",
    "ежемесячно": "monthly",
    "по будням": "weekdays",
    "по рабочим дням": "weekdays",
    "по понедельникам": "weekly:0",
    "по вторникам": "weekly:1",
    "по средам": "weekly:2",
    "по четвергам": "weekly:3",
    "по пятницам": "weekly:4",
    "по субботам": "weekly:5",
    "по воскресеньям": "weekly:6",
}


def parse_recurrence(text: str) -> str:
    """Detect recurrence pattern in text. Returns rule string or ''."""
    lower = text.lower()
    for marker, rule in RECURRENCE_MARKERS.items():
        if marker in lower:
            return rule
    return ""


def compute_next_occurrence(remind_at: str, rule: str, recurrence_end: str = "") -> str | None:
    """Compute next reminder time based on recurrence rule.

    Returns ISO datetime string or None if recurrence has ended.
    """
    try:
        current = datetime.fromisoformat(remind_at[:19])
    except (ValueError, TypeError):
        return None

    if recurrence_end:
        try:
            end = datetime.fromisoformat(recurrence_end[:19])
            if current >= end:
                return None
        except (ValueError, TypeError):
            pass

    if rule == "daily":
        next_dt = current + timedelta(days=1)
    elif rule == "weekly":
        next_dt = current + timedelta(weeks=1)
    elif rule == "monthly":
        next_dt = current + timedelta(days=30)
    elif rule == "weekdays":
        next_dt = current + timedelta(days=1)
        while next_dt.weekday() >= 5:  # Skip Sat/Sun
            next_dt += timedelta(days=1)
    elif rule.startswith("weekly:"):
        # weekly:0 = every Monday, weekly:0,2,4 = Mon/Wed/Fri
        day_nums = [int(d) for d in rule.split(":")[1].split(",")]
        next_dt = current + timedelta(days=1)
        for _ in range(8):
            if next_dt.weekday() in day_nums:
                break
            next_dt += timedelta(days=1)
        else:
            return None
    else:
        return None

    if recurrence_end:
        try:
            end = datetime.fromisoformat(recurrence_end[:19])
            if next_dt > end:
                return None
        except (ValueError, TypeError):
            pass

    return next_dt.isoformat()


class ReminderExtractionService:
    """Extract reminders from note tasks based on explicit intent or due dates."""

    def __init__(self, db):
        self.db = db

    async def extract(self, note_id: int) -> int:
        """Extract reminders from note tasks with explicit reminder intent.

        Returns count of created reminders.
        """
        note = await self.db.get_note(note_id)
        if not note:
            return 0

        tasks = await self.db.get_tasks_by_note(note_id)
        if not tasks:
            return 0

        content = (note.get("raw_content") or note.get("content", "")).lower()
        try:
            created_at = datetime.fromisoformat(note["created_at"][:19])
        except (ValueError, TypeError):
            created_at = datetime.now()

        is_explicit = has_explicit_reminder_intent(content)
        if not is_explicit:
            return 0  # Only auto-create for explicit intent

        # Detect recurrence pattern in original text
        recurrence_rule = parse_recurrence(content)

        count = 0
        for task in tasks:
            due_text = task.get("due_date", "")

            # Parse due date for reminder time
            remind_at = parse_due_datetime(due_text, created_at) if due_text else None
            if not remind_at:
                # Default: tomorrow 09:00
                remind_at = (created_at + timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0,
                )

            # Dedup: don't create if same note+task already has pending/sent reminder
            existing = await self.db.get_note_reminder_by_task(note_id, task["id"])
            if existing:
                continue

            await self.db.create_note_reminder(
                note_id=note_id,
                description=task["description"],
                remind_at=remind_at.isoformat(),
                task_id=task["id"],
                source="explicit",
                confidence=0.9,
                recurrence_rule=recurrence_rule,
            )
            count += 1
            logger.info(
                f"Reminder created for note #{note_id}: "
                f"'{task['description'][:50]}' at {remind_at.isoformat()}"
            )

        return count

    async def create_for_inferred(self, note_id: int) -> int:
        """Create reminders for inferred tasks (user clicked Remind button).

        Unlike extract(), this creates reminders regardless of explicit intent.
        """
        note = await self.db.get_note(note_id)
        if not note:
            return 0

        tasks = await self.db.get_tasks_by_note(note_id)
        if not tasks:
            return 0

        try:
            created_at = datetime.fromisoformat(note["created_at"][:19])
        except (ValueError, TypeError):
            created_at = datetime.now()

        count = 0
        for task in tasks:
            due_text = task.get("due_date", "")
            if not due_text:
                continue  # Only tasks with due dates

            remind_at = parse_due_datetime(due_text, created_at)
            if not remind_at:
                # Fallback: tomorrow 09:00
                remind_at = (created_at + timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0,
                )

            # Dedup
            existing = await self.db.get_note_reminder_by_task(note_id, task["id"])
            if existing:
                continue

            await self.db.create_note_reminder(
                note_id=note_id,
                description=task["description"],
                remind_at=remind_at.isoformat(),
                task_id=task["id"],
                source="inferred",
                confidence=0.6,
            )
            count += 1

        return count
