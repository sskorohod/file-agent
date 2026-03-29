"""Tests for FileLifecycleService — delete, reclassify, cache invalidation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.services.lifecycle import FileLifecycleService


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_dir):
    from app.storage.db import Database
    database = Database(tmp_dir / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def db_encrypted(tmp_dir):
    """DB initialized with safe FTS (encrypted mode, 3-column FTS)."""
    from app.storage.db import Database
    database = Database(tmp_dir / "enc.db", encryption_key=b"test-key-32-bytes-long-padding!!")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def mock_vector_store():
    vs = MagicMock()
    vs.delete_document = AsyncMock(return_value=0)
    vs.update_document_metadata = AsyncMock()
    return vs


@pytest.fixture
def mock_llm_search():
    s = MagicMock()
    s.invalidate_cache = AsyncMock()
    return s


@pytest.fixture
def mock_classifier():
    from app.llm.classifier import ClassificationResult
    c = MagicMock()
    c.classify = AsyncMock(return_value=ClassificationResult(
        category="health",
        confidence=0.95,
        tags=["medical", "lab"],
        summary="Результаты анализов крови",
        document_type="lab_result",
        model_used="test-model",
    ))
    return c


async def _insert_test_file(db, file_id="test-file-1", category="uncategorized"):
    """Helper to insert a file for testing."""
    await db.insert_file(
        id=file_id,
        original_name="report.pdf",
        stored_path="/tmp/fake/report.pdf",
        sha256="abc123",
        size_bytes=1024,
        mime_type="application/pdf",
        category=category,
        tags=["test"],
        summary="Test file",
        extracted_text="Blood test results: hemoglobin 14.2",
        metadata={"document_type": "unknown"},
    )


# ── Delete Tests ─────────────────────────────────────────────────────────


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_file(self, db, file_storage, mock_vector_store, mock_llm_search):
        """Delete cascades through vectors, storage, DB, and cache."""
        record = await file_storage.save_from_bytes(b"data", "test.txt", category="tmp")
        await db.insert_file(
            id="f1", original_name="test.txt", stored_path=record.stored_path,
            sha256=record.sha256, size_bytes=record.size_bytes, category="tmp",
        )

        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search)
        result = await svc.delete("f1")

        assert result is True
        # DB record gone
        assert await db.get_file("f1") is None
        # Vectors deleted
        mock_vector_store.delete_document.assert_awaited_once_with("f1")
        # File removed from disk
        assert not await file_storage.exists(record.stored_path)
        # Cache invalidated
        mock_llm_search.invalidate_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, db, file_storage, mock_vector_store, mock_llm_search):
        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search)
        result = await svc.delete("no-such-id")
        assert result is False
        mock_vector_store.delete_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_with_normal_fts(self, db, mock_vector_store, mock_llm_search, file_storage):
        """Delete works with full 5-column FTS schema."""
        await _insert_test_file(db, "fts-test")
        # Verify FTS finds the file
        results = await db.search_files("blood test")
        assert any(r["id"] == "fts-test" for r in results)

        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search)
        await svc.delete("fts-test")

        # File gone from DB and FTS
        assert await db.get_file("fts-test") is None
        results = await db.search_files("blood test")
        assert not any(r["id"] == "fts-test" for r in results)

    @pytest.mark.asyncio
    async def test_delete_with_safe_fts(self, db_encrypted, mock_vector_store, mock_llm_search, file_storage):
        """Delete works with safe 3-column FTS schema (encrypted mode)."""
        await db_encrypted.insert_file(
            id="enc-test", original_name="report.pdf", stored_path="/tmp/fake.pdf",
            sha256="abc", size_bytes=100, category="health",
            tags=["test"], summary="Summary", extracted_text="Blood results",
        )
        # Verify file exists
        f = await db_encrypted.get_file("enc-test")
        assert f is not None

        svc = FileLifecycleService(db_encrypted, file_storage, mock_vector_store, mock_llm_search)
        # This must NOT crash with "no such column: summary"
        await svc.delete("enc-test")

        assert await db_encrypted.get_file("enc-test") is None


# ── Reclassify Tests ─────────────────────────────────────────────────────


class TestReclassify:
    @pytest.mark.asyncio
    async def test_reclassify_updates_all_layers(
        self, db, file_storage, mock_vector_store, mock_llm_search, mock_classifier
    ):
        """Reclassify updates DB, Qdrant payload, and invalidates cache."""
        await _insert_test_file(db, "reclass-1", category="uncategorized")

        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search, mock_classifier)
        result = await svc.reclassify("reclass-1")

        assert result is not None
        assert result["file_id"] == "reclass-1"
        assert result["category"] == "health"
        assert result["document_type"] == "lab_result"
        assert "medical" in result["tags"]

        # DB updated
        file = await db.get_file("reclass-1")
        assert file["category"] == "health"

        # metadata_json updated with document_type
        meta = json.loads(file["metadata_json"]) if isinstance(file["metadata_json"], str) else file["metadata_json"]
        assert meta["document_type"] == "lab_result"

        # Qdrant payload updated
        mock_vector_store.update_document_metadata.assert_awaited_once()
        call_args = mock_vector_store.update_document_metadata.call_args
        assert call_args[0][0] == "reclass-1"
        assert call_args[0][1]["category"] == "health"

        # Cache invalidated
        mock_llm_search.invalidate_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reclassify_result_schema(
        self, db, file_storage, mock_vector_store, mock_llm_search, mock_classifier
    ):
        """Reclassify returns normalised dict with all required fields."""
        await _insert_test_file(db, "schema-test")

        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search, mock_classifier)
        result = await svc.reclassify("schema-test")

        required_keys = {"file_id", "category", "tags", "summary", "document_type"}
        assert required_keys == set(result.keys())

    @pytest.mark.asyncio
    async def test_reclassify_nonexistent(
        self, db, file_storage, mock_vector_store, mock_llm_search, mock_classifier
    ):
        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search, mock_classifier)
        result = await svc.reclassify("no-such-id")
        assert result is None
        mock_classifier.classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reclassify_without_classifier(self, db, file_storage, mock_vector_store, mock_llm_search):
        await _insert_test_file(db, "no-clf")
        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search, classifier=None)
        result = await svc.reclassify("no-clf")
        assert result is None


# ── Cache Invalidation Tests ─────────────────────────────────────────────


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_cache_cleared_after_delete(self, db, file_storage, mock_vector_store, mock_llm_search):
        await _insert_test_file(db, "cache-del")
        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search)
        await svc.delete("cache-del")
        mock_llm_search.invalidate_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_cleared_after_reclassify(
        self, db, file_storage, mock_vector_store, mock_llm_search, mock_classifier
    ):
        await _insert_test_file(db, "cache-rcl")
        svc = FileLifecycleService(db, file_storage, mock_vector_store, mock_llm_search, mock_classifier)
        await svc.reclassify("cache-rcl")
        mock_llm_search.invalidate_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_crash_without_llm_search(self, db, file_storage, mock_vector_store):
        """Service works even if llm_search is None."""
        await _insert_test_file(db, "no-search")
        svc = FileLifecycleService(db, file_storage, mock_vector_store, llm_search=None)
        result = await svc.delete("no-search")
        assert result is True
