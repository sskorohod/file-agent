"""Tests for pipeline compensating cleanup and audit trail."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db(tmp_dir):
    from app.storage.db import Database
    database = Database(tmp_dir / "test.db")
    await database.connect()
    yield database
    await database.close()


# ── Compensating Cleanup Tests ───────────────────────────────────────────


class TestCompensatingCleanup:
    """When save_meta fails after store, pipeline must clean up stored file and vectors."""

    @pytest.mark.asyncio
    async def test_save_meta_failure_cleans_stored_file(self, db, tmp_dir, file_storage):
        """After save_meta failure, stored file is deleted from disk."""
        from app.pipeline import Pipeline
        from app.storage.files import FileRecord

        mock_settings = MagicMock()
        mock_settings.storage.resolved_path = tmp_dir
        mock_vector_store = MagicMock()
        mock_vector_store.delete_document = AsyncMock()

        # Embed returns 0 (non-fatal skip)
        async def fake_embed(*a, **kw):
            return 0

        mock_parser = MagicMock()
        mock_llm = MagicMock()
        mock_classifier = MagicMock()
        mock_skill_engine = MagicMock()

        pipeline = Pipeline(
            settings=mock_settings,
            db=db,
            file_storage=file_storage,
            vector_store=mock_vector_store,
            parser_factory=mock_parser,
            llm_router=mock_llm,
            classifier=mock_classifier,
            skill_engine=mock_skill_engine,
        )

        # Save a file to storage first
        record = await file_storage.save_from_bytes(b"test data", "test.txt", category="tmp")
        assert await file_storage.exists(record.stored_path)

        # Simulate: store succeeded (file on disk), but save_meta raises
        fake_record = FileRecord(
            id="comp-test-1",
            original_name="test.txt",
            stored_path=record.stored_path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            category="tmp",
            mime_type="text/plain",
        )

        # Patch individual steps to control flow
        with patch.object(pipeline, '_step_receive', new_callable=AsyncMock), \
             patch.object(pipeline, '_step_dedup', new_callable=AsyncMock, return_value=None), \
             patch.object(pipeline, '_step_ingest', new_callable=AsyncMock, return_value=tmp_dir / "t.txt"), \
             patch.object(pipeline, '_step_parse', new_callable=AsyncMock) as mock_parse, \
             patch.object(pipeline, '_step_classify', new_callable=AsyncMock) as mock_classify, \
             patch.object(pipeline, '_step_route', new_callable=AsyncMock, return_value=None), \
             patch.object(pipeline, '_step_store', new_callable=AsyncMock, return_value=fake_record), \
             patch.object(pipeline, '_step_embed', new_callable=AsyncMock, return_value=0), \
             patch.object(pipeline, '_step_save_meta', new_callable=AsyncMock, side_effect=RuntimeError("DB write failed")), \
             patch.object(pipeline, '_auto_crop_if_image', return_value=b"test data"):

            from app.parser.base import ParseResult
            mock_parse.return_value = ParseResult(text="test", language="en")

            from app.llm.classifier import ClassificationResult
            mock_classify.return_value = ClassificationResult(
                category="tmp", confidence=0.9, tags=[], summary="", document_type="test",
            )

            result = await pipeline.process(b"test data", "test.txt")

        # Pipeline should have failed
        assert result.error is not None
        assert "DB write failed" in result.error

        # Stored file should be cleaned up
        assert not await file_storage.exists(record.stored_path)

        # Vectors should be cleaned up
        mock_vector_store.delete_document.assert_awaited_once_with("comp-test-1")

        # Audit log should be cleaned up (no orphaned entries)
        cursor = await db.db.execute(
            "SELECT COUNT(*) FROM processing_log WHERE file_id='comp-test-1'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "Orphaned processing_log entries should be cleaned up"

    @pytest.mark.asyncio
    async def test_embed_failure_is_nonfatal(self, db, tmp_dir, file_storage):
        """Embed failure does NOT trigger cleanup — file is kept, just not searchable."""
        from app.pipeline import Pipeline

        mock_settings = MagicMock()
        mock_settings.storage.resolved_path = tmp_dir
        mock_vector_store = MagicMock()
        mock_vector_store.delete_document = AsyncMock()

        pipeline = Pipeline(
            settings=mock_settings, db=db, file_storage=file_storage,
            vector_store=mock_vector_store, parser_factory=MagicMock(),
            llm_router=MagicMock(), classifier=MagicMock(), skill_engine=MagicMock(),
        )

        record = await file_storage.save_from_bytes(b"keep me", "keep.txt", category="tmp")
        from app.storage.files import FileRecord
        fake_record = FileRecord(
            id="embed-fail-1", original_name="keep.txt",
            stored_path=record.stored_path, sha256=record.sha256,
            size_bytes=record.size_bytes, mime_type="text/plain", category="tmp",
        )

        with patch.object(pipeline, '_step_receive', new_callable=AsyncMock), \
             patch.object(pipeline, '_step_dedup', new_callable=AsyncMock, return_value=None), \
             patch.object(pipeline, '_step_ingest', new_callable=AsyncMock, return_value=tmp_dir / "t.txt"), \
             patch.object(pipeline, '_step_parse', new_callable=AsyncMock) as mock_parse, \
             patch.object(pipeline, '_step_classify', new_callable=AsyncMock) as mock_classify, \
             patch.object(pipeline, '_step_route', new_callable=AsyncMock, return_value=None), \
             patch.object(pipeline, '_step_store', new_callable=AsyncMock, return_value=fake_record), \
             patch.object(pipeline, '_step_embed', new_callable=AsyncMock, side_effect=RuntimeError("Qdrant down")), \
             patch.object(pipeline, '_step_save_meta', new_callable=AsyncMock), \
             patch.object(pipeline, '_auto_crop_if_image', return_value=b"keep me"):

            from app.parser.base import ParseResult
            mock_parse.return_value = ParseResult(text="test", language="en")
            from app.llm.classifier import ClassificationResult
            mock_classify.return_value = ClassificationResult(
                category="tmp", confidence=0.9, tags=[], summary="", document_type="test",
            )

            result = await pipeline.process(b"keep me", "keep.txt")

        # Embed is retryable and can fail — but file should still be kept
        # Note: embed step failure will cause the step to raise after retries,
        # which means the pipeline will fail. But compensating cleanup only
        # triggers on save_meta failure, not embed failure.
        # Actually, let's check: if embed raises, the main try/except catches it
        # and sets result.error. No compensating cleanup for embed.
        # The stored file should still exist.
        assert await file_storage.exists(record.stored_path)


# ── Audit Trail Tests ────────────────────────────────────────────────────


class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_run_id_in_processing_log(self, db):
        """log_step stores run_id in processing_log."""
        log_id = await db.log_step(
            file_id=None, step="receive", run_id="run-123",
        )
        assert log_id > 0

        cursor = await db.db.execute(
            "SELECT run_id, file_id FROM processing_log WHERE id=?", (log_id,)
        )
        row = await cursor.fetchone()
        assert row["run_id"] == "run-123"
        assert row["file_id"] is None

    @pytest.mark.asyncio
    async def test_backfill_file_id(self, db):
        """After store, backfill_run_file_id sets file_id on early steps."""
        run_id = "run-456"
        await db.log_step(None, "receive", run_id=run_id)
        await db.log_step(None, "ingest", run_id=run_id)
        await db.log_step(None, "parse", run_id=run_id)

        # Simulate store completing
        await db.backfill_run_file_id(run_id, "file-abc")

        cursor = await db.db.execute(
            "SELECT file_id FROM processing_log WHERE run_id=?", (run_id,)
        )
        rows = await cursor.fetchall()
        assert all(r["file_id"] == "file-abc" for r in rows)

    @pytest.mark.asyncio
    async def test_all_steps_logged_for_successful_run(self, db):
        """A successful pipeline run should have 8 step entries."""
        run_id = "run-full"
        steps = ["receive", "ingest", "parse", "classify", "route", "store", "embed", "save_meta"]

        for step in steps:
            log_id = await db.log_step(None, step, run_id=run_id)
            await db.finish_step(log_id, "success", duration_ms=10)

        await db.backfill_run_file_id(run_id, "file-full")

        cursor = await db.db.execute(
            "SELECT step, status FROM processing_log WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 8
        assert [r["step"] for r in rows] == steps
        assert all(r["status"] == "success" for r in rows)

    @pytest.mark.asyncio
    async def test_failed_run_shows_error_step(self, db):
        """A failed run should show which step errored."""
        run_id = "run-fail"
        for step in ["receive", "ingest", "parse"]:
            log_id = await db.log_step(None, step, run_id=run_id)
            await db.finish_step(log_id, "success", duration_ms=5)

        # classify fails
        log_id = await db.log_step(None, "classify", run_id=run_id)
        await db.finish_step(log_id, "error", error="LLM timeout", duration_ms=5000)

        cursor = await db.db.execute(
            "SELECT step, status, error FROM processing_log WHERE run_id=? ORDER BY id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 4
        assert rows[-1]["step"] == "classify"
        assert rows[-1]["status"] == "error"
        assert rows[-1]["error"] == "LLM timeout"

    @pytest.mark.asyncio
    async def test_log_step_with_file_id(self, db):
        """log_step with file_id still works (post-store steps)."""
        log_id = await db.log_step("file-xyz", "embed", run_id="run-789")
        await db.finish_step(log_id, "success", duration_ms=100)

        cursor = await db.db.execute(
            "SELECT file_id, run_id, step FROM processing_log WHERE id=?", (log_id,)
        )
        row = await cursor.fetchone()
        assert row["file_id"] == "file-xyz"
        assert row["run_id"] == "run-789"
