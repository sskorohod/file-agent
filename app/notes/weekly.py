"""Weekly Analyzer — 7-day pattern detection, insights, and reports."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

WEEKLY_ANALYSIS_PROMPT = """\
Ты — персональный аналитик здоровья и продуктивности. Проанализируй данные пользователя за неделю.

Данные по дням (JSON):
{data}

Общая статистика:
- Всего заметок: {total_notes}
- Стрик: {streak} дней подряд
- Категории: {categories}

## Задание:
1. Найди **паттерны** — что повторяется, что меняется по дням недели
2. Найди **корреляции** — связь между сном и настроением, едой и энергией и т.д.
3. Определи **тренды** — что растёт, что падает по сравнению с предыдущей неделей
4. Выяви **алерты** — пропущенные обеды, мало сна, низкое настроение
5. Отметь **достижения** — стрики, улучшения, хорошие показатели
6. Дай **3-5 конкретных рекомендаций** — основанных на данных, не generic

Ответь на русском. Формат:

## Паттерны
- ...

## Тренды
- ...

## Алерты
- ...

## Достижения
- ...

## Рекомендации
1. ...
2. ...
3. ..."""


class WeeklyAnalyzer:
    """Analyze 7 days of note data for patterns, trends, and recommendations."""

    def __init__(self, db, llm_router, vault=None):
        self.db = db
        self.llm = llm_router
        self.vault = vault

    async def generate_weekly_report(self, week_end_date: str = "") -> dict:
        """Analyze the last 7 days and generate a report.

        Returns dict with: text, metrics_by_day, insights, vault_path
        """
        if not week_end_date:
            week_end_date = datetime.now().strftime("%Y-%m-%d")

        end = datetime.strptime(week_end_date, "%Y-%m-%d")
        start = end - timedelta(days=6)
        start_str = start.strftime("%Y-%m-%d")

        # Collect data
        weekly_metrics = await self.db.get_weekly_metrics(start_str)
        notes_by_date = await self.db.get_notes_count_by_date(days=7)
        streak = await self.db.get_streak(days=60)
        cat_dist = await self.db.get_category_distribution(days=7)

        total_notes = sum(notes_by_date.values())

        # Build day-by-day summary for LLM
        day_summaries = {}
        for i in range(7):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            day_data = weekly_metrics.get(d, {})
            day_summaries[d] = {
                "notes": notes_by_date.get(d, 0),
                "calories": day_data.get("calories", {}).get("total", 0),
                "mood": day_data.get("mood_score", {}).get("avg", 0),
                "energy": day_data.get("energy", {}).get("avg", 0),
                "sleep": day_data.get("sleep_hours", {}).get("total", 0),
                "weight": day_data.get("weight_kg", {}).get("avg", 0),
            }

        # LLM analysis
        analysis_text = ""
        try:
            prompt = WEEKLY_ANALYSIS_PROMPT.format(
                data=json.dumps(day_summaries, indent=2, ensure_ascii=False),
                total_notes=total_notes,
                streak=streak,
                categories=json.dumps(cat_dist, ensure_ascii=False),
            )
            response = await self.llm.extract(text=prompt, system="")
            analysis_text = response.text.strip()
        except Exception as e:
            logger.error(f"Weekly analysis LLM failed: {e}")
            analysis_text = "Не удалось сгенерировать анализ."

        # Write Obsidian note
        vault_path = ""
        if self.vault:
            vault_path = self._write_weekly_note(
                start_str, week_end_date, day_summaries,
                analysis_text, total_notes, streak, cat_dist,
            )

        return {
            "text": analysis_text,
            "metrics_by_day": day_summaries,
            "total_notes": total_notes,
            "streak": streak,
            "categories": cat_dist,
            "vault_path": str(vault_path),
            "period": f"{start_str} — {week_end_date}",
        }

    def _write_weekly_note(
        self, start: str, end: str, day_summaries: dict,
        analysis: str, total_notes: int, streak: int, categories: dict,
    ) -> str:
        """Write weekly report as Obsidian note."""
        from pathlib import Path

        # ISO week number
        dt = datetime.strptime(end, "%Y-%m-%d")
        week_num = dt.isocalendar()[1]
        year = dt.isocalendar()[0]
        filename = f"week-{year}-W{week_num:02d}.md"

        daily_dir = self.vault._ensure_dir("daily")
        path = daily_dir / filename

        lines = [
            "---",
            f"type: weekly_report",
            f"period: {start} — {end}",
            f"week: {year}-W{week_num:02d}",
            f"total_notes: {total_notes}",
            f"streak: {streak}",
            "---",
            "",
            f"# Неделя {week_num} ({start} — {end})",
            "",
        ]

        # Day-by-day table
        lines.append("## Данные по дням")
        lines.append("")
        lines.append("| Дата | Заметки | Калории | Настроение | Энергия | Сон | Вес |")
        lines.append("|------|---------|---------|------------|---------|-----|-----|")
        for d, data in sorted(day_summaries.items()):
            cal = f"{int(data['calories'])}" if data["calories"] else "—"
            mood = f"{data['mood']:.1f}" if data["mood"] else "—"
            energy = f"{data['energy']:.0f}" if data["energy"] else "—"
            sleep = f"{data['sleep']:.1f}" if data["sleep"] else "—"
            weight = f"{data['weight']:.1f}" if data["weight"] else "—"
            lines.append(f"| {d} | {data['notes']} | {cal} | {mood} | {energy} | {sleep} | {weight} |")
        lines.append("")

        # Categories
        if categories:
            lines.append("## Категории")
            for cat, cnt in sorted(categories.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"- **{cat}**: {cnt}")
            lines.append("")

        # AI analysis
        lines.extend(["## AI-анализ", "", analysis, ""])

        # Daily links
        lines.append("## Ежедневные обзоры")
        for d in sorted(day_summaries.keys()):
            lines.append(f"- [[daily/{d}]]")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Weekly report written: {path}")
        return str(path)

    async def get_weekly_telegram_summary(self, week_end_date: str = "") -> str:
        """Generate a short Telegram-friendly weekly summary."""
        report = await self.generate_weekly_report(week_end_date)

        parts = [f"📊 Недельный отчёт ({report['period']})", ""]

        # Key metrics
        metrics = report["metrics_by_day"]
        total_cal = sum(d.get("calories", 0) for d in metrics.values())
        avg_mood = 0
        mood_days = [d["mood"] for d in metrics.values() if d.get("mood")]
        if mood_days:
            avg_mood = sum(mood_days) / len(mood_days)

        parts.append(f"📝 Заметок: {report['total_notes']}")
        if total_cal:
            parts.append(f"🍽 Калории (сумма): ~{int(total_cal)} kcal")
        if avg_mood:
            parts.append(f"💭 Настроение (avg): {avg_mood:.1f}/10")
        parts.append(f"🔥 Стрик: {report['streak']} дней")
        parts.append("")

        # Truncated AI analysis
        analysis = report.get("text", "")
        if analysis:
            # Take first 500 chars
            short = analysis[:500]
            if len(analysis) > 500:
                short += "..."
            parts.append(short)

        return "\n".join(parts)
