"""Tests for search cache key and hybrid retrieval."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.llm.search import LLMSearch
from app.storage.vectors import SearchResult


@pytest_asyncio.fixture
async def db(tmp_dir):
    from app.storage.db import Database
    database = Database(tmp_dir / "test.db")
    await database.connect()
    yield database
    await database.close()


def _make_vector_store(results: list[SearchResult] | None = None):
    vs = MagicMock()
    vs.search = AsyncMock(return_value=results or [])
    return vs


def _make_llm():
    from app.llm.router import LLMResponse
    llm = MagicMock()
    llm.search_answer = AsyncMock(return_value=LLMResponse(
        text="Test answer", model="test", role="search",
    ))
    return llm


# ── Cache Key Tests ──────────────────────────────────────────────────────


class TestCacheKey:
    def test_different_compact_different_key(self):
        k1 = LLMSearch._cache_key("test query", compact=False)
        k2 = LLMSearch._cache_key("test query", compact=True)
        assert k1 != k2

    def test_different_top_k_different_key(self):
        k1 = LLMSearch._cache_key("test query", top_k=5)
        k2 = LLMSearch._cache_key("test query", top_k=10)
        assert k1 != k2

    def test_different_history_different_key(self):
        k1 = LLMSearch._cache_key("test query", history=None)
        k2 = LLMSearch._cache_key("test query", history=[{"q": "prev", "a": "ans"}])
        assert k1 != k2

    def test_same_params_same_key(self):
        h = [{"q": "prev", "a": "ans"}]
        k1 = LLMSearch._cache_key("test", compact=True, top_k=3, history=h)
        k2 = LLMSearch._cache_key("test", compact=True, top_k=3, history=h)
        assert k1 == k2

    def test_case_insensitive(self):
        k1 = LLMSearch._cache_key("Test Query")
        k2 = LLMSearch._cache_key("test query")
        assert k1 == k2


# ── Hybrid Retrieval Tests ───────────────────────────────────────────────


class TestHybridRetrieval:
    @pytest.mark.asyncio
    async def test_vector_only(self):
        """When FTS returns nothing, vector results are used."""
        vs = _make_vector_store([
            SearchResult(file_id="f1", chunk_index=0, text="chunk", score=0.8, metadata={"filename": "a.pdf"}),
        ])
        search = LLMSearch(vs, _make_llm(), db=None)
        scores = await search._hybrid_retrieve("test", top_k=5)

        assert "f1" in scores
        assert scores["f1"]["vector_score"] == 0.8
        assert scores["f1"]["fts_score"] == 0.0

    @pytest.mark.asyncio
    async def test_fts_only(self, db):
        """When vector search returns nothing, FTS results are used."""
        # Insert a file so FTS can find it
        await db.insert_file(
            id="fts-only-1", original_name="blood_test.pdf", stored_path="/tmp/f.pdf",
            sha256="abc", size_bytes=100, category="health",
            extracted_text="Blood test hemoglobin results",
        )
        vs = _make_vector_store([])  # empty vector results
        search = LLMSearch(vs, _make_llm(), db=db)
        scores = await search._hybrid_retrieve("blood test", top_k=5)

        assert "fts-only-1" in scores
        assert scores["fts-only-1"]["fts_score"] > 0
        assert scores["fts-only-1"]["vector_score"] == 0.0

    @pytest.mark.asyncio
    async def test_fusion_boosts_intersection(self, db):
        """Files found by both channels get a higher score than either alone."""
        await db.insert_file(
            id="both-1", original_name="report.pdf", stored_path="/tmp/r.pdf",
            sha256="abc", size_bytes=100, category="health",
            extracted_text="Medical report with diagnosis",
        )
        vs = _make_vector_store([
            SearchResult(file_id="both-1", chunk_index=0, text="chunk", score=0.75, metadata={"filename": "report.pdf"}),
            SearchResult(file_id="vec-only", chunk_index=0, text="other", score=0.75, metadata={"filename": "other.pdf"}),
        ])
        search = LLMSearch(vs, _make_llm(), db=db)
        scores = await search._hybrid_retrieve("medical report", top_k=5)

        # "both-1" should have a higher merged score due to FTS boost
        assert "both-1" in scores
        assert scores["both-1"]["fts_score"] > 0
        assert scores["both-1"]["vector_score"] > 0
        # The combined score should be higher than vector-only score
        if "vec-only" in scores:
            assert scores["both-1"]["score"] > scores["vec-only"]["score"]

    @pytest.mark.asyncio
    async def test_empty_channels(self):
        """When both channels are empty, returns empty dict."""
        vs = _make_vector_store([])
        search = LLMSearch(vs, _make_llm(), db=None)
        scores = await search._hybrid_retrieve("nothing", top_k=5)
        assert scores == {}

    @pytest.mark.asyncio
    async def test_low_score_filtered(self):
        """Vector results below MIN_SCORE are excluded."""
        vs = _make_vector_store([
            SearchResult(file_id="low", chunk_index=0, text="weak", score=0.3, metadata={}),
        ])
        search = LLMSearch(vs, _make_llm(), db=None)
        scores = await search._hybrid_retrieve("test", top_k=5)
        assert "low" not in scores


# ── Integration: Cache + Hybrid ──────────────────────────────────────────


class TestSearchIntegration:
    @pytest.mark.asyncio
    async def test_compact_cached_separately(self, db):
        """compact=True and compact=False produce different cache entries."""
        vs = _make_vector_store([
            SearchResult(file_id="f1", chunk_index=0, text="data", score=0.8, metadata={"filename": "a.pdf"}),
        ])
        await db.insert_file(
            id="f1", original_name="a.pdf", stored_path="/tmp/a.pdf",
            sha256="abc", size_bytes=100, extracted_text="Some data",
        )
        search = LLMSearch(vs, _make_llm(), db=db)

        # First call: compact=False
        r1 = await search.answer("test query", compact=False)
        assert r1["cached"] is False

        # Second call: compact=True — should NOT hit cache
        r2 = await search.answer("test query", compact=True)
        assert r2["cached"] is False

        # Third call: compact=False again — should hit cache
        r3 = await search.answer("test query", compact=False)
        assert r3["cached"] is True

    @pytest.mark.asyncio
    async def test_different_history_not_cached(self, db):
        """Different conversation history produces cache miss."""
        vs = _make_vector_store([
            SearchResult(file_id="f1", chunk_index=0, text="data", score=0.8, metadata={"filename": "a.pdf"}),
        ])
        await db.insert_file(
            id="f1", original_name="a.pdf", stored_path="/tmp/a.pdf",
            sha256="abc", size_bytes=100, extracted_text="Some data",
        )
        search = LLMSearch(vs, _make_llm(), db=db)

        h1 = [{"q": "previous question", "a": "previous answer"}]
        h2 = [{"q": "different question", "a": "different answer"}]

        await search.answer("test", history=h1)
        r2 = await search.answer("test", history=h2)
        assert r2["cached"] is False
