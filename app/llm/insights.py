"""AI Insights — deep analysis of document categories with web research."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from app.llm.router import LLMRouter
from app.storage.db import Database

logger = logging.getLogger(__name__)

INSIGHT_SYSTEM = """Ты — персональный AI-аналитик и жизненный консультант. Твоя задача — проанализировать документы пользователя
и дать полезные, мотивирующие рекомендации по улучшению ситуации.

Категория: {category}

Документы пользователя:
{documents}

{web_context}

Ответь СТРОГО в формате JSON (без markdown fences):
{{
  "summary": "Обзор текущей ситуации по этой категории (3-5 предложений). Что есть, что в порядке, что требует внимания.",
  "key_issues": "Ключевые проблемы и дедлайны. Что просрочено, что скоро истекает, что требует действий. Конкретные даты и документы.",
  "recommendations": "Конкретные, мотивирующие рекомендации (5-8 пунктов). Каждый пункт начинай с эмодзи. Фокус на улучшении жизни, здоровья, финансов. Не просто 'продлите документ', а объясни ПОЧЕМУ это важно и КАК это улучшит жизнь. Добавь совет по образу жизни если категория health.",
  "search_queries": ["поисковый запрос 1 на английском для актуальной информации", "запрос 2"]
}}

Правила:
- Пиши на русском, дружелюбно и мотивирующе
- Давай КОНКРЕТНЫЕ рекомендации с датами и шагами
- Для health: анализируй тренды здоровья, давай советы по профилактике
- Для personal: следи за сроками документов, напоминай о продлении заранее
- Для business: анализируй финансы, лицензии, контракты
- Мотивируй: "Вы на правильном пути!", "Это поможет вам..."
- Если есть проблемы — не пугай, а предложи план решения
"""

DAILY_ADVICE_SYSTEM = """Ты — дружелюбный персональный помощник. На основе обзора документов пользователя
составь короткий мотивирующий совет дня.

Текущие обзоры по категориям:
{insights}

Напоминания на ближайшие дни:
{reminders}

Время суток: {time_of_day}

Правила:
- Максимум 3-4 предложения
- Начни с приветствия (утром: "Доброе утро! 🌅", вечером: "Добрый вечер! 🌙")
- Выбери ОДНУ самую актуальную тему
- Дай конкретный совет, который можно выполнить сегодня/завтра
- Будь тёплым и мотивирующим
- Если всё хорошо — похвали и предложи что-то для улучшения
- Пиши на русском
"""


class InsightsEngine:
    """Generate and cache AI insights for document categories."""

    def __init__(self, llm: LLMRouter, db: Database):
        self.llm = llm
        self.db = db

    async def refresh_category(self, category: str) -> dict:
        """Regenerate insight for a specific category."""
        # 1. Gather all documents in category
        files = await self.db.list_files(category=category, limit=100)
        if not files:
            return {}

        # Build document context
        doc_lines = []
        for f in files:
            meta = {}
            try:
                meta = json.loads(f.get("metadata_json", "{}") or "{}")
            except Exception:
                pass
            ef = meta.get("extracted_fields", {})
            doc_lines.append(
                f"- {f['original_name']}: {f.get('summary', 'нет описания')}\n"
                f"  Тип: {meta.get('document_type', '?')}, "
                f"  Дата: {f.get('created_at', '')[:10]}, "
                f"  Истекает: {ef.get('expiry_date', 'нет')}, "
                f"  Важность: {ef.get('priority', '?')}, "
                f"  Действия: {ef.get('action_required', 'нет')}"
            )
        documents_text = "\n".join(doc_lines)

        # 2. First LLM call — analysis + search queries
        prompt = INSIGHT_SYSTEM.format(
            category=category,
            documents=documents_text,
            web_context="",
        )

        try:
            response = await self.llm.extract(text="Проанализируй документы", system=prompt)
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(raw)
        except Exception as e:
            logger.error(f"Insight generation failed for {category}: {e}")
            return {}

        # 3. Web research (optional — if search queries provided)
        web_results = ""
        search_queries = result.get("search_queries", [])
        if search_queries:
            web_results = await self._web_research(search_queries[:3])

        # 4. If web results found — do a second pass with enriched context
        if web_results:
            prompt2 = INSIGHT_SYSTEM.format(
                category=category,
                documents=documents_text,
                web_context=f"\nАктуальная информация из интернета:\n{web_results}",
            )
            try:
                response2 = await self.llm.extract(text="Обнови анализ с учётом найденной информации", system=prompt2)
                raw2 = response2.text.strip()
                if raw2.startswith("```"):
                    raw2 = raw2.split("\n", 1)[1].rsplit("```", 1)[0]
                result = json.loads(raw2)
            except Exception:
                pass  # Keep first result

        # 5. Save to DB — ensure all values are strings
        def _to_str(val):
            if isinstance(val, list):
                return "\n".join(str(v) for v in val)
            return str(val) if val else ""

        await self.db.upsert_insight(
            category=category,
            summary_text=_to_str(result.get("summary", "")),
            recommendations=_to_str(result.get("recommendations", "")),
            key_issues=_to_str(result.get("key_issues", "")),
            web_research=_to_str(web_results),
            document_count=len(files),
        )

        logger.info(f"Insight refreshed for '{category}' ({len(files)} docs)")
        return result

    async def refresh_all(self) -> list[dict]:
        """Refresh insights for all categories."""
        stats = await self.db.get_stats()
        categories = list(stats.get("categories", {}).keys())
        results = []
        for cat in categories:
            r = await self.refresh_category(cat)
            if r:
                results.append({"category": cat, **r})
        return results

    async def generate_daily_advice(self, time_of_day: str = "morning") -> str:
        """Generate a motivational daily advice message."""
        insights = await self.db.get_all_insights()
        reminders = await self.db.list_reminders(include_sent=False)

        insights_text = ""
        for ins in insights:
            insights_text += f"\n[{ins['category']}]\n{ins['summary_text']}\n{ins['key_issues']}\n"

        reminders_text = ""
        for r in reminders[:5]:
            reminders_text += f"- {r.get('original_name', '?')}: {r.get('message', '')} (дата: {r.get('remind_at', '')[:10]})\n"

        prompt = DAILY_ADVICE_SYSTEM.format(
            insights=insights_text or "Пока нет данных",
            reminders=reminders_text or "Нет напоминаний",
            time_of_day="утро" if time_of_day == "morning" else "вечер",
        )

        try:
            response = await self.llm.extract(text="Составь совет дня", system=prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"Daily advice generation failed: {e}")
            return ""

    async def _web_research(self, queries: list[str]) -> str:
        """Search the web for relevant information."""
        results = []
        for query in queries:
            try:
                # Try Tavily search if available
                from tavily import TavilyClient
                import os
                api_key = os.environ.get("TAVILY_API_KEY", "")
                if api_key:
                    client = TavilyClient(api_key=api_key)
                    response = client.search(query, max_results=2)
                    for r in response.get("results", []):
                        results.append(f"[{r['title']}] {r['content'][:300]}")
                    continue
            except (ImportError, Exception):
                pass

            # Fallback: skip web search
            logger.debug(f"Web search skipped for: {query} (no Tavily API key)")

        return "\n\n".join(results) if results else ""
