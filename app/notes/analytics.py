"""Notes Analytics Engine — trends, correlations, and insights via note_facts."""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class NoteAnalytics:
    """Query note_facts for trends, correlations, and daily summaries."""

    def __init__(self, db, llm_router=None):
        self.db = db
        self.llm = llm_router

    async def get_calorie_trend(self, days: int = 30) -> list[dict]:
        return await self._get_trend("calories", days)

    async def get_mood_trend(self, days: int = 30) -> list[dict]:
        return await self._get_trend("mood_score", days)

    async def get_weight_trend(self, days: int = 90) -> list[dict]:
        return await self._get_trend("weight_kg", days)

    async def get_sleep_trend(self, days: int = 30) -> list[dict]:
        return await self._get_trend("sleep_hours", days)

    async def _get_trend(self, key: str, days: int) -> list[dict]:
        """Try note_facts first, fall back to note_data for compat."""
        result = await self.db.get_facts_trend(key, days)
        if result:
            return result
        # Fallback to legacy note_data
        try:
            return await self.db.get_metric_trend(key, days)
        except Exception:
            return []

    async def get_category_distribution(self, days: int = 30) -> dict:
        return await self.db.get_category_distribution(days)

    async def get_daily_summary_data(self, date: str) -> dict:
        """Get full summary data for a specific day."""
        notes = await self.db.get_daily_notes(date)
        # Try note_facts first, fall back to note_data
        metrics = await self.db.get_daily_facts(date)
        if not metrics:
            try:
                metrics = await self.db.get_daily_metrics(date)
            except Exception:
                metrics = {}

        by_cat: dict[str, list[dict]] = {}
        for n in notes:
            cat = n.get("category", "other") or "other"
            by_cat.setdefault(cat, []).append(n)

        return {
            "date": date,
            "notes_count": len(notes),
            "notes": notes,
            "notes_by_category": by_cat,
            "metrics": metrics,
        }

    async def detect_correlations(
        self, metric_a: str, metric_b: str, days: int = 60,
    ) -> dict:
        """Detect Pearson correlation between two daily metrics."""
        trend_a = await self._get_trend(metric_a, days)
        trend_b = await self._get_trend(metric_b, days)

        map_a = {r["date"]: r["avg"] for r in trend_a}
        map_b = {r["date"]: r["avg"] for r in trend_b}

        common_dates = sorted(set(map_a.keys()) & set(map_b.keys()))
        if len(common_dates) < 5:
            return {
                "metric_a": metric_a, "metric_b": metric_b,
                "correlation": None, "data_points": len(common_dates),
                "interpretation": "Недостаточно данных для анализа корреляции",
            }

        values_a = [map_a[d] for d in common_dates]
        values_b = [map_b[d] for d in common_dates]
        corr = self._pearson(values_a, values_b)

        return {
            "metric_a": metric_a, "metric_b": metric_b,
            "correlation": round(corr, 3) if corr is not None else None,
            "data_points": len(common_dates),
            "interpretation": self._interpret_correlation(corr, metric_a, metric_b),
        }

    async def get_all_correlations(self, days: int = 60) -> list[dict]:
        from app.config import get_settings
        metrics = get_settings().notes.correlation_metrics
        results = []
        for i, m_a in enumerate(metrics):
            for m_b in metrics[i + 1:]:
                corr = await self.detect_correlations(m_a, m_b, days)
                if corr.get("correlation") is not None:
                    results.append(corr)
        results.sort(key=lambda r: abs(r.get("correlation", 0) or 0), reverse=True)
        return results

    @staticmethod
    def _pearson(x: list[float], y: list[float]) -> float | None:
        n = len(x)
        if n < 2:
            return None
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
        std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
        if std_x == 0 or std_y == 0:
            return None
        return cov / (std_x * std_y)

    @staticmethod
    def _interpret_correlation(corr: float | None, metric_a: str, metric_b: str) -> str:
        if corr is None:
            return "Не удалось вычислить корреляцию"
        names = {
            "mood_score": "настроение", "calories": "калории",
            "sleep_hours": "сон", "weight_kg": "вес",
            "exercise_min": "тренировки", "water_ml": "вода",
            "steps": "шаги", "energy": "энергия",
        }
        a, b = names.get(metric_a, metric_a), names.get(metric_b, metric_b)
        abs_corr = abs(corr)
        if abs_corr < 0.2:
            return f"Между {a} и {b} нет значимой связи"
        elif abs_corr < 0.5:
            return f"Слабая {'прямая' if corr > 0 else 'обратная'} связь между {a} и {b}"
        elif abs_corr < 0.7:
            return f"Когда {a} выше, {b} {'растёт' if corr > 0 else 'снижается'}"
        else:
            return f"{a.capitalize()} и {b} {'сильно связаны' if corr > 0 else 'обратно связаны'} (r={corr:.2f})"
