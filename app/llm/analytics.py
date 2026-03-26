"""LLM Analytics — multi-document analysis with structured extraction and charting."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.llm.router import LLMRouter
from app.storage.db import Database
from app.storage.vectors import VectorStore

logger = logging.getLogger(__name__)

# ── Trigger detection ──────────────────────────────────────────────────

ANALYTICS_TRIGGERS = [
    "проанализируй", "покажи в динамике", "динамика", "сравни",
    "график", "тренд", "изменения за", "анализ за период",
    "статистика по", "analyze", "trend", "compare", "chart",
    "покажи график", "как менялся", "как менялась", "как менялось",
    "в динамике", "аудит", "сводка по",
]


def is_analytics_query(text: str) -> bool:
    """Check if user query is an analytics request (not regular search)."""
    lower = text.lower()
    return any(trigger in lower for trigger in ANALYTICS_TRIGGERS)


# ── Data structures ────────────────────────────────────────────────────

@dataclass
class DataPoint:
    date: str  # ISO YYYY-MM-DD
    metric: str
    value: float
    unit: str
    reference_min: float | None = None
    reference_max: float | None = None
    file_id: str = ""
    source_filename: str = ""


@dataclass
class AnalyticsResult:
    text_summary: str
    data_points: list[DataPoint] = field(default_factory=list)
    chart_png: bytes | None = None
    file_ids: dict[str, str] = field(default_factory=dict)  # file_id → filename


# ── Prompts ────────────────────────────────────────────────────────────

SCOPE_SYSTEM = """You determine the scope of a data analysis request.

Given a user query, extract:
- category: the document category to search (health, business, personal, receipts, or null if unclear)
- metrics: list of specific metrics/values to track (e.g. ["hemoglobin", "WBC", "RBC"]). Empty list if all metrics requested.
- time_range_days: how far back to look (365 for "за год", 180 for "полгода", 90 for "3 months", 0 for all time)
- fts_query: a short search query for full-text search to narrow down documents (e.g. "анализ крови" or "invoice")

Return ONLY valid JSON, no other text.
Example: {"category": "health", "metrics": ["hemoglobin", "WBC"], "time_range_days": 365, "fts_query": "анализ крови"}"""

EXTRACT_SYSTEM = """You extract structured quantitative data from document text.

Extract ALL quantitative measurements as a JSON array.
Each measurement:
- "date": measurement date (ISO YYYY-MM-DD). If only month/year given, use 1st of month.
- "metric": standardized metric name (e.g. "Hemoglobin" not "Hgb", "Гемоглобин" not "HGB")
- "value": numeric value (float)
- "unit": unit of measurement (e.g. "g/dL", "mmol/L", "₽", "$")
- "reference_min": lower bound of normal range (null if unknown)
- "reference_max": upper bound of normal range (null if unknown)

If the document has no quantitative data, return [].
Return ONLY a valid JSON array, no other text."""

SUMMARY_SYSTEM = """You are a data analyst summarizing trends from structured data.

Rules:
- Describe key trends: increasing, decreasing, stable, out of range
- Highlight any values outside reference ranges
- Note the time period covered
- Be specific with numbers and dates
- Respond in the same language as the question
- Keep it concise: 3-5 sentences"""

# ── Concurrency limit for LLM extraction calls ────────────────────────
_EXTRACTION_SEMAPHORE = asyncio.Semaphore(5)


class LLMAnalytics:
    """Multi-document analytics with LLM extraction and chart generation."""

    def __init__(self, vector_store: VectorStore, llm: LLMRouter, db: Database):
        self.vector_store = vector_store
        self.llm = llm
        self.db = db

    async def analyze(self, query: str) -> AnalyticsResult:
        """Full analytics pipeline: scope → retrieve → extract → chart → summary."""

        # Step 1: Determine scope
        scope = await self._determine_scope(query)
        logger.info(f"Analytics scope: {scope}")

        # Step 2: Retrieve matching documents
        docs = await self._retrieve_documents(scope)
        if not docs:
            return AnalyticsResult(
                text_summary="Не найдено документов для анализа по вашему запросу."
            )

        logger.info(f"Analytics: found {len(docs)} documents to analyze")

        # Step 3: Extract structured data from each document
        all_points = await self._extract_data_points(docs, scope.get("metrics", []))

        if not all_points:
            # No quantitative data found — return text-only summary
            file_ids = {d["id"]: d["original_name"] for d in docs[:10]}
            return AnalyticsResult(
                text_summary=(
                    f"Найдено {len(docs)} документов, но числовых данных для анализа "
                    f"не обнаружено. Попробуйте уточнить запрос."
                ),
                file_ids=file_ids,
            )

        # Step 4: Generate chart
        chart_png = self._generate_chart(all_points, query)

        # Step 5: Generate text summary
        summary = await self._generate_summary(all_points, query)

        file_ids = {dp.file_id: dp.source_filename for dp in all_points if dp.file_id}

        return AnalyticsResult(
            text_summary=summary,
            data_points=all_points,
            chart_png=chart_png,
            file_ids=file_ids,
        )

    # ── Step 1: Scope ──────────────────────────────────────────────────

    async def _determine_scope(self, query: str) -> dict:
        """Use LLM to parse query into structured scope."""
        try:
            response = await self.llm.complete(
                role="classification",  # Haiku — fast and cheap
                messages=[{"role": "user", "content": query}],
                system=SCOPE_SYSTEM,
                max_tokens=256,
                temperature=0.0,
            )
            return json.loads(response.text)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Scope extraction failed: {e}, using defaults")
            return {"category": None, "metrics": [], "time_range_days": 0, "fts_query": ""}

    # ── Step 2: Retrieve ───────────────────────────────────────────────

    async def _retrieve_documents(self, scope: dict) -> list[dict]:
        """Fetch all matching documents from DB."""
        category = scope.get("category")
        fts_query = scope.get("fts_query", "")
        time_range_days = scope.get("time_range_days", 0)

        docs = []

        # Primary: category-based retrieval
        if category:
            docs = await self.db.list_files(category=category, limit=200)

        # Fallback/supplement: FTS search
        if fts_query and len(docs) < 5:
            fts_docs = await self.db.search_files(fts_query, limit=50)
            existing_ids = {d["id"] for d in docs}
            for d in fts_docs:
                if d["id"] not in existing_ids:
                    docs.append(d)

        # Filter by time range
        if time_range_days > 0:
            cutoff = datetime.now() - timedelta(days=time_range_days)
            filtered = []
            for d in docs:
                try:
                    created = datetime.fromisoformat(d["created_at"])
                    if created >= cutoff:
                        filtered.append(d)
                except (ValueError, KeyError):
                    filtered.append(d)  # keep if date unparseable
            docs = filtered

        return docs

    # ── Step 3: Extract ────────────────────────────────────────────────

    async def _extract_data_points(
        self, docs: list[dict], metrics: list[str]
    ) -> list[DataPoint]:
        """Extract structured data from each document via LLM."""
        tasks = [
            self._extract_from_one(doc, metrics)
            for doc in docs
            if doc.get("extracted_text")
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_points = []
        for r in results:
            if isinstance(r, list):
                all_points.extend(r)
            elif isinstance(r, Exception):
                logger.debug(f"Extraction error: {r}")

        # Sort by date (filter out points without valid dates)
        all_points = [p for p in all_points if p.date]
        all_points.sort(key=lambda p: p.date)
        return all_points

    async def _extract_from_one(
        self, doc: dict, metrics: list[str]
    ) -> list[DataPoint]:
        """Extract data points from a single document."""
        async with _EXTRACTION_SEMAPHORE:
            text = doc.get("extracted_text", "")[:8000]
            file_id = doc.get("id", "")
            filename = doc.get("original_name", "unknown")

            # Check if we have cached extracted_fields in metadata
            try:
                meta = json.loads(doc.get("metadata_json", "{}") or "{}")
                cached_fields = meta.get("extracted_fields")
                if cached_fields:
                    # Try to use cached data if it has numeric values
                    # (future optimization — for now always re-extract)
                    pass
            except (json.JSONDecodeError, TypeError):
                pass

            try:
                response = await self.llm.complete(
                    role="analysis",
                    messages=[{"role": "user", "content": text}],
                    system=EXTRACT_SYSTEM,
                    temperature=0.0,
                )

                raw = response.text.strip()
                # Handle markdown code blocks
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()

                items = json.loads(raw)
                if not isinstance(items, list):
                    return []

                points = []
                for item in items:
                    try:
                        metric_name = item.get("metric", "")
                        # Filter by requested metrics if specified
                        if metrics:
                            if not any(
                                m.lower() in metric_name.lower()
                                for m in metrics
                            ):
                                continue

                        points.append(DataPoint(
                            date=item["date"],
                            metric=metric_name,
                            value=float(item["value"]),
                            unit=item.get("unit", ""),
                            reference_min=float(item["reference_min"]) if item.get("reference_min") is not None else None,
                            reference_max=float(item["reference_max"]) if item.get("reference_max") is not None else None,
                            file_id=file_id,
                            source_filename=filename,
                        ))
                    except (KeyError, ValueError, TypeError):
                        continue

                return points

            except Exception as e:
                logger.debug(f"Extraction failed for {filename}: {e}")
                return []

    # ── Step 4: Chart ──────────────────────────────────────────────────

    def _generate_chart(self, data_points: list[DataPoint], query: str) -> bytes | None:
        """Generate a chart from data points."""
        from app.llm.chart import generate_time_series_chart

        chart_data = [
            {
                "date": dp.date,
                "metric": dp.metric,
                "value": dp.value,
                "unit": dp.unit,
                "reference_min": dp.reference_min,
                "reference_max": dp.reference_max,
            }
            for dp in data_points
        ]

        # Build a short title from the query
        title = query[:60] + ("..." if len(query) > 60 else "")

        return generate_time_series_chart(chart_data, title=title)

    # ── Step 5: Summary ────────────────────────────────────────────────

    async def _generate_summary(self, data_points: list[DataPoint], query: str) -> str:
        """Generate a narrative summary of the data trends."""
        # Build structured data text for LLM
        lines = []
        for dp in data_points:
            ref = ""
            if dp.reference_min is not None and dp.reference_max is not None:
                ref = f" (норма: {dp.reference_min}-{dp.reference_max})"
            elif dp.reference_min is not None:
                ref = f" (мин: {dp.reference_min})"
            elif dp.reference_max is not None:
                ref = f" (макс: {dp.reference_max})"
            lines.append(f"{dp.date} | {dp.metric}: {dp.value} {dp.unit}{ref} [{dp.source_filename}]")

        data_text = "\n".join(lines)

        user_msg = f"Запрос: {query}\n\nДанные:\n{data_text}"

        try:
            response = await self.llm.complete(
                role="analysis",
                messages=[{"role": "user", "content": user_msg}],
                system=SUMMARY_SYSTEM,
                max_tokens=1024,
            )
            return response.text
        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            return f"Извлечено {len(data_points)} значений из {len(set(dp.file_id for dp in data_points))} документов."
