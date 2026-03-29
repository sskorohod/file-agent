"""Tests for analytics hybrid retrieval, document_date, and cached fields."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.llm.analytics import LLMAnalytics, DataPoint
from app.storage.vectors import SearchResult


@pytest_asyncio.fixture
async def db(tmp_dir):
    from app.storage.db import Database
    database = Database(tmp_dir / "test.db")
    await database.connect()
    yield database
    await database.close()


def _make_vector_store(results=None):
    vs = MagicMock()
    vs.search = AsyncMock(return_value=results or [])
    return vs


def _make_llm():
    llm = MagicMock()
    llm.complete = AsyncMock()
    return llm


async def _insert_file(db, file_id, category="health", extracted_text="Blood test",
                        metadata=None, document_date=None):
    await db.insert_file(
        id=file_id, original_name=f"{file_id}.pdf", stored_path=f"/tmp/{file_id}.pdf",
        sha256=f"hash-{file_id}", size_bytes=100, category=category,
        extracted_text=extracted_text, metadata=metadata or {},
        document_date=document_date,
    )


# ── Hybrid Retrieval Tests ───────────────────────────────────────────────


class TestAnalyticsRetrieval:
    @pytest.mark.asyncio
    async def test_vector_candidates_included(self, db):
        """Vector search adds candidates not found by category/FTS."""
        await _insert_file(db, "cat-1", category="health")
        await _insert_file(db, "vec-1", category="personal",
                           extracted_text="Lab results from hospital")

        vs = _make_vector_store([
            SearchResult(file_id="vec-1", chunk_index=0, text="lab", score=0.7, metadata={}),
        ])
        analytics = LLMAnalytics(vs, _make_llm(), db)

        scope = {"category": "health", "fts_query": "", "metrics": [], "time_range_days": 0}
        docs = await analytics._retrieve_documents(scope)

        file_ids = {d["id"] for d in docs}
        assert "cat-1" in file_ids  # from category
        assert "vec-1" in file_ids  # from vector search

    @pytest.mark.asyncio
    async def test_deduplication(self, db):
        """Same file found by multiple channels appears only once."""
        await _insert_file(db, "dup-1", category="health",
                           extracted_text="Blood test hemoglobin")

        vs = _make_vector_store([
            SearchResult(file_id="dup-1", chunk_index=0, text="blood", score=0.8, metadata={}),
        ])
        analytics = LLMAnalytics(vs, _make_llm(), db)

        scope = {"category": "health", "fts_query": "blood test", "metrics": [], "time_range_days": 0}
        docs = await analytics._retrieve_documents(scope)

        ids = [d["id"] for d in docs]
        assert ids.count("dup-1") == 1

    @pytest.mark.asyncio
    async def test_low_score_vector_excluded(self, db):
        """Vector results below threshold are excluded."""
        await _insert_file(db, "low-1", category="personal")

        vs = _make_vector_store([
            SearchResult(file_id="low-1", chunk_index=0, text="weak", score=0.3, metadata={}),
        ])
        analytics = LLMAnalytics(vs, _make_llm(), db)

        scope = {"category": None, "fts_query": "something", "metrics": [], "time_range_days": 0}
        docs = await analytics._retrieve_documents(scope)
        file_ids = {d["id"] for d in docs}
        assert "low-1" not in file_ids


# ── Document Date Tests ──────────────────────────────────────────────────


class TestDocumentDate:
    @pytest.mark.asyncio
    async def test_document_date_stored(self, db):
        """document_date is saved and retrievable."""
        await _insert_file(db, "dated-1", document_date="2025-06-15")
        f = await db.get_file("dated-1")
        assert f["document_date"] == "2025-06-15"

    @pytest.mark.asyncio
    async def test_document_date_used_for_filtering(self, db):
        """Time filter uses document_date over created_at."""
        # Recent upload but old document
        await _insert_file(db, "old-doc", document_date="2020-01-01")
        # Recent document
        await _insert_file(db, "new-doc", document_date="2026-03-01")

        vs = _make_vector_store([])
        analytics = LLMAnalytics(vs, _make_llm(), db)

        scope = {"category": None, "fts_query": "blood test",
                 "metrics": [], "time_range_days": 365}
        docs = await analytics._retrieve_documents(scope)
        file_ids = {d["id"] for d in docs}

        assert "new-doc" in file_ids
        assert "old-doc" not in file_ids  # old document_date filtered out

    def test_extract_document_date_priority(self):
        """Pipeline extracts date by priority: date > issue_date > date_of_service."""
        from app.pipeline import Pipeline

        # "date" wins
        assert Pipeline._extract_document_date({"date": "2025-06-15", "issue_date": "2025-01-01"}) == "2025-06-15"
        # Fallback to issue_date
        assert Pipeline._extract_document_date({"issue_date": "15.03.2025"}) == "2025-03-15"
        # Fallback to date_of_service
        assert Pipeline._extract_document_date({"date_of_service": "2024-12-01"}) == "2024-12-01"
        # None if no date fields
        assert Pipeline._extract_document_date({"amount": "500"}) is None
        assert Pipeline._extract_document_date(None) is None


# ── Cached Extracted Fields Tests ────────────────────────────────────────


class TestCachedFields:
    def test_valid_cached_fields_used(self):
        """Structured numeric fields with date → DataPoints returned."""
        doc = {
            "id": "cf-1",
            "original_name": "lab.pdf",
            "metadata_json": json.dumps({
                "extracted_fields": {
                    "date": "2025-06-15",
                    "hemoglobin": "14.2 g/dL",
                    "WBC": "5.8 10^9/L",
                    "doctor": "Dr. Smith",  # should be skipped
                }
            }),
        }
        points = LLMAnalytics._try_cached_fields(doc, metrics=[])
        assert points is not None
        assert len(points) == 2
        assert all(p.date == "2025-06-15" for p in points)
        metrics_found = {p.metric for p in points}
        assert "Hemoglobin" in metrics_found
        assert "Wbc" in metrics_found

    def test_no_date_returns_none(self):
        """Without a valid date, cached fields are not used."""
        doc = {
            "id": "cf-2",
            "metadata_json": json.dumps({
                "extracted_fields": {"amount": "500 USD"}
            }),
        }
        assert LLMAnalytics._try_cached_fields(doc, metrics=[]) is None

    def test_no_numeric_returns_none(self):
        """Fields with date but no numeric values → fallback."""
        doc = {
            "id": "cf-3",
            "metadata_json": json.dumps({
                "extracted_fields": {
                    "date": "2025-01-01",
                    "doctor": "Dr. Jones",
                    "clinic": "City Hospital",
                }
            }),
        }
        assert LLMAnalytics._try_cached_fields(doc, metrics=[]) is None

    def test_metrics_filter_applied(self):
        """Only matching metrics are returned when filter is set."""
        doc = {
            "id": "cf-4",
            "metadata_json": json.dumps({
                "extracted_fields": {
                    "date": "2025-06-15",
                    "hemoglobin": "14.2 g/dL",
                    "WBC": "5.8 10^9/L",
                }
            }),
        }
        points = LLMAnalytics._try_cached_fields(doc, metrics=["hemoglobin"])
        assert points is not None
        assert len(points) == 1
        assert points[0].metric == "Hemoglobin"

    def test_invalid_metadata_returns_none(self):
        """Corrupted metadata_json → fallback gracefully."""
        doc = {"id": "cf-5", "metadata_json": "not json{{{"}
        assert LLMAnalytics._try_cached_fields(doc, metrics=[]) is None

    def test_numeric_int_value(self):
        """Integer values in extracted_fields are handled."""
        doc = {
            "id": "cf-6",
            "metadata_json": json.dumps({
                "extracted_fields": {
                    "date": "2025-01-15",
                    "total_amount": 1500,
                }
            }),
        }
        points = LLMAnalytics._try_cached_fields(doc, metrics=[])
        assert points is not None
        assert len(points) == 1
        assert points[0].value == 1500.0
