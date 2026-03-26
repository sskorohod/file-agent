"""Tests for SQLite database."""

import pytest


class TestDatabase:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, db):
        await db.insert_file(
            id="test-1", original_name="doc.pdf", stored_path="/tmp/doc.pdf",
            sha256="abc123", size_bytes=1024, mime_type="application/pdf",
            category="health", tags=["medical"], summary="A test doc",
        )
        f = await db.get_file("test-1")
        assert f is not None
        assert f["original_name"] == "doc.pdf"
        assert f["category"] == "health"

    @pytest.mark.asyncio
    async def test_count(self, db):
        for i in range(3):
            await db.insert_file(
                id=f"f-{i}", original_name=f"doc{i}.pdf", stored_path=f"/tmp/doc{i}.pdf",
                sha256=f"hash{i}", size_bytes=100, category="business",
            )
        assert await db.count_files() == 3
        assert await db.count_files(category="business") == 3
        assert await db.count_files(category="health") == 0

    @pytest.mark.asyncio
    async def test_list_files(self, db):
        for i in range(5):
            await db.insert_file(
                id=f"l-{i}", original_name=f"file{i}.txt", stored_path=f"/tmp/f{i}",
                sha256=f"h{i}", size_bytes=50,
            )
        files = await db.list_files(limit=3)
        assert len(files) == 3

    @pytest.mark.asyncio
    async def test_update_file(self, db):
        await db.insert_file(
            id="u-1", original_name="test.pdf", stored_path="/tmp/test.pdf",
            sha256="xyz", size_bytes=200, category="personal",
        )
        await db.update_file("u-1", category="business", summary="Updated")
        f = await db.get_file("u-1")
        assert f["category"] == "business"
        assert f["summary"] == "Updated"

    @pytest.mark.asyncio
    async def test_stats(self, db):
        await db.insert_file(id="s-1", original_name="a.pdf", stored_path="/a", sha256="a", size_bytes=100, category="health")
        await db.insert_file(id="s-2", original_name="b.pdf", stored_path="/b", sha256="b", size_bytes=200, category="business")
        stats = await db.get_stats()
        assert stats["total_files"] == 2
        assert stats["total_size_bytes"] == 300
        assert "health" in stats["categories"]

    @pytest.mark.asyncio
    async def test_processing_log(self, db):
        await db.insert_file(id="log-1", original_name="x.pdf", stored_path="/x", sha256="x", size_bytes=10)
        log_id = await db.log_step("log-1", "parse", "started")
        assert log_id > 0
        await db.finish_step(log_id, "success", duration_ms=150)
        logs = await db.get_file_log("log-1")
        assert len(logs) == 1
        assert logs[0]["status"] == "success"
        assert logs[0]["duration_ms"] == 150

    @pytest.mark.asyncio
    async def test_get_by_hash(self, db):
        await db.insert_file(id="h-1", original_name="dup.pdf", stored_path="/dup", sha256="samehash", size_bytes=50)
        f = await db.get_file_by_hash("samehash")
        assert f is not None
        assert f["id"] == "h-1"
        assert await db.get_file_by_hash("nothash") is None
