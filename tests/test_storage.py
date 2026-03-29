"""Tests for file storage (dispatcher + local backend)."""

from pathlib import Path

import pytest


class TestFileStorage:
    @pytest.mark.asyncio
    async def test_save_from_bytes(self, file_storage):
        record = await file_storage.save_from_bytes(
            data=b"hello world",
            original_name="test.txt",
            category="personal",
        )
        assert Path(record.stored_path).exists()
        assert record.size_bytes == 11
        assert record.sha256
        assert record.category == "personal"
        assert Path(record.stored_path).read_bytes() == b"hello world"

    @pytest.mark.asyncio
    async def test_save_from_path(self, file_storage, sample_text_file):
        record = await file_storage.save_from_path(
            source=sample_text_file,
            category="health",
        )
        assert Path(record.stored_path).exists()
        assert record.original_name == "test.txt"

    @pytest.mark.asyncio
    async def test_category_dir_structure(self, file_storage):
        record = await file_storage.save_from_bytes(
            data=b"data", original_name="doc.pdf", category="business",
        )
        parts = Path(record.stored_path).parts
        assert "business" in parts

    def test_check_extension(self, file_storage):
        assert file_storage.check_extension("test.pdf")
        assert file_storage.check_extension("test.txt")
        assert not file_storage.check_extension("test.exe")
        assert not file_storage.check_extension("test.zip")

    @pytest.mark.asyncio
    async def test_delete(self, file_storage):
        record = await file_storage.save_from_bytes(
            data=b"delete me", original_name="del.txt", category="tmp",
        )
        assert await file_storage.exists(record.stored_path)
        result = await file_storage.delete(record.stored_path)
        assert result is True
        assert not await file_storage.exists(record.stored_path)

    @pytest.mark.asyncio
    async def test_hash_consistency(self, file_storage):
        data = b"same content"
        r1 = await file_storage.save_from_bytes(data=data, original_name="a.txt", category="x")
        r2 = await file_storage.save_from_bytes(data=data, original_name="b.txt", category="x")
        assert r1.sha256 == r2.sha256

    @pytest.mark.asyncio
    async def test_read_file(self, file_storage):
        data = b"read me back"
        record = await file_storage.save_from_bytes(
            data=data, original_name="read.txt", category="test",
        )
        result = await file_storage.read_file(record.stored_path)
        assert result == data
