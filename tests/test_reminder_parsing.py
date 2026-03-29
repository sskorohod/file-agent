"""Tests for reminder parsing and policy layer."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.notes.reminders import parse_due_datetime, has_explicit_reminder_intent


# ---------------------------------------------------------------------------
# Due-date parsing
# ---------------------------------------------------------------------------

class TestParseDueDatetime:
    """Natural language due-date parser tests."""

    # Reference: Wednesday 2026-03-25 at 14:00
    REF = datetime(2026, 3, 25, 14, 0, 0)

    def test_empty_returns_none(self):
        assert parse_due_datetime("", self.REF) is None
        assert parse_due_datetime(None, self.REF) is None

    def test_today(self):
        result = parse_due_datetime("сегодня", self.REF)
        assert result == self.REF.replace(hour=18, minute=0, second=0, microsecond=0)

    def test_tomorrow(self):
        result = parse_due_datetime("завтра", self.REF)
        expected = (self.REF + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_day_after_tomorrow(self):
        result = parse_due_datetime("послезавтра", self.REF)
        expected = (self.REF + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_in_n_days(self):
        result = parse_due_datetime("через 2 дня", self.REF)
        expected = (self.REF + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_in_n_hours(self):
        result = parse_due_datetime("через 2 часа", self.REF)
        expected = self.REF + timedelta(hours=2)
        assert result == expected

    def test_in_week(self):
        result = parse_due_datetime("через неделю", self.REF)
        expected = (self.REF + timedelta(weeks=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_in_month(self):
        result = parse_due_datetime("через месяц", self.REF)
        expected = (self.REF + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)
        assert result == expected

    def test_weekday_forward(self):
        # REF is Wednesday (2). "в пятницу" -> Friday = weekday 4 -> +2 days
        result = parse_due_datetime("в пятницу", self.REF)
        assert result is not None
        assert result.weekday() == 4
        assert result.hour == 9

    def test_weekday_before(self):
        # "до пятницы" -> Thursday (day before Friday) at 18:00
        result = parse_due_datetime("до пятницы", self.REF)
        assert result is not None
        assert result.weekday() == 3
        assert result.hour == 18

    def test_weekday_same_day_wraps(self):
        # REF is Wednesday. "в среду" -> next Wednesday
        result = parse_due_datetime("в среду", self.REF)
        assert result is not None
        assert result.weekday() == 2
        assert result.date() == (self.REF + timedelta(days=7)).date()

    def test_iso_date(self):
        result = parse_due_datetime("2026-04-05", self.REF)
        assert result == datetime(2026, 4, 5, 9, 0, 0)

    def test_unknown_returns_none(self):
        assert parse_due_datetime("когда-нибудь", self.REF) is None
        assert parse_due_datetime("потом", self.REF) is None


# ---------------------------------------------------------------------------
# Explicit reminder intent detection
# ---------------------------------------------------------------------------

class TestExplicitIntent:
    """Policy: only explicit markers trigger auto-creation."""

    # --- Explicit cases ---
    def test_napomni(self):
        assert has_explicit_reminder_intent("Напомни завтра купить масло")

    def test_ne_zabyt(self):
        assert has_explicit_reminder_intent("Не забыть в среду записаться к врачу")

    def test_postav_napominanie(self):
        assert has_explicit_reminder_intent("Поставь напоминание через 2 дня оплатить счёт")

    def test_case_insensitive(self):
        assert has_explicit_reminder_intent("НАПОМНИ купить хлеб")

    def test_remind_english(self):
        assert has_explicit_reminder_intent("Remind me to call John")

    # --- Inferred cases (no auto-create) ---
    def test_inferred_deadline(self):
        assert not has_explicit_reminder_intent("Заказать фильтр до пятницы")

    def test_inferred_call(self):
        assert not has_explicit_reminder_intent("Позвонить Сергею в понедельник")

    def test_inferred_due(self):
        assert not has_explicit_reminder_intent("Сдать анализы через месяц")

    # --- No reminder intent at all ---
    def test_vague_potom(self):
        assert not has_explicit_reminder_intent("Надо бы потом купить бананы")

    def test_vague_kogda_nibud(self):
        assert not has_explicit_reminder_intent("Когда-нибудь разобраться с этим")

    def test_vague_think(self):
        assert not has_explicit_reminder_intent("Странный день, надо подумать")
