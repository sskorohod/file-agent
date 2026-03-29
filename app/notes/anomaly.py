"""Anomaly Detection — proactive alerts for unusual metric patterns."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    alert_type: str
    severity: str  # warning, critical
    message: str
    context: str = ""


class AnomalyDetector:
    """Detect anomalies in daily metrics and send Telegram alerts."""

    def __init__(self, db):
        self.db = db

    async def check_anomalies(self) -> list[Alert]:
        """Run all anomaly checks. Returns list of new alerts."""
        alerts: list[Alert] = []
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        alerts.extend(await self._check_mood_drop(today, yesterday))
        alerts.extend(await self._check_sleep_deficit(today))
        alerts.extend(await self._check_missed_food(today))
        alerts.extend(await self._check_weight_spike(today))

        # Dedup: filter out already-sent alerts
        new_alerts = []
        for a in alerts:
            if not await self._was_sent(a.alert_type, today):
                new_alerts.append(a)
                await self._record_alert(a.alert_type, today, a.message)

        return new_alerts

    async def _check_mood_drop(self, today: str, yesterday: str) -> list[Alert]:
        """Alert if mood dropped by 3+ points since yesterday."""
        today_mood = await self._get_metric_avg(today, "mood_score")
        yest_mood = await self._get_metric_avg(yesterday, "mood_score")
        if today_mood is not None and yest_mood is not None:
            delta = yest_mood - today_mood
            if delta >= 3:
                return [Alert(
                    alert_type="mood_drop",
                    severity="warning",
                    message=f"Настроение упало с {yest_mood:.0f} до {today_mood:.0f} (−{delta:.0f})",
                    context="Всё ли в порядке? Может, стоит сделать паузу.",
                )]
        return []

    async def _check_sleep_deficit(self, today: str) -> list[Alert]:
        """Alert if sleep < 6h for 2+ consecutive days."""
        count = 0
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            sleep = await self._get_metric_avg(date, "sleep_hours")
            if sleep is not None and sleep < 6:
                count += 1
            else:
                break

        if count >= 2:
            return [Alert(
                alert_type="sleep_deficit",
                severity="warning" if count == 2 else "critical",
                message=f"Недосып {count} дня подряд (менее 6ч)",
                context="Хронический недосып снижает когнитивные функции и иммунитет.",
            )]
        return []

    async def _check_missed_food(self, today: str) -> list[Alert]:
        """Alert if no food entries for 2+ consecutive days."""
        count = 0
        for i in range(3):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            entries = await self.db.get_food_entries_by_date(date)
            if not entries:
                count += 1
            else:
                break

        if count >= 2:
            return [Alert(
                alert_type="missed_food",
                severity="warning",
                message=f"Нет записей о еде уже {count} дня",
                context="Не забывай логировать приёмы пищи для отслеживания калорий.",
            )]
        return []

    async def _check_weight_spike(self, today: str) -> list[Alert]:
        """Alert if weight changed by >1.5kg in one day."""
        today_w = await self._get_metric_avg(today, "weight_kg")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        yest_w = await self._get_metric_avg(yesterday, "weight_kg")

        if today_w is not None and yest_w is not None:
            delta = abs(today_w - yest_w)
            if delta > 1.5:
                direction = "вырос" if today_w > yest_w else "снизился"
                return [Alert(
                    alert_type="weight_spike",
                    severity="warning",
                    message=f"Вес {direction} на {delta:.1f} кг за день ({yest_w:.1f} → {today_w:.1f})",
                    context="Резкие колебания могут быть связаны с водным балансом.",
                )]
        return []

    async def _get_metric_avg(self, date: str, key: str) -> float | None:
        """Get average metric value for a date."""
        cursor = await self.db.db.execute(
            "SELECT AVG(value_num) FROM note_facts WHERE key=? AND date=? AND value_num IS NOT NULL",
            (key, date),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    async def _was_sent(self, alert_type: str, date: str) -> bool:
        """Check if alert was already sent today."""
        cursor = await self.db.db.execute(
            "SELECT 1 FROM anomaly_alerts WHERE alert_type=? AND date=? LIMIT 1",
            (alert_type, date),
        )
        return bool(await cursor.fetchone())

    async def _record_alert(self, alert_type: str, date: str, message: str):
        """Record sent alert for dedup."""
        await self.db.db.execute(
            "INSERT INTO anomaly_alerts (alert_type, date, message) VALUES (?, ?, ?)",
            (alert_type, date, message),
        )
        await self.db.db.commit()
