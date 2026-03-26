"""Tests for file storage."""

import pytest
from pathlib import Path


class TestFileStorage:
    @pytest.mark.asyncio
    async def test_save_from_bytes(self, file_storage):
        record = await file_storage.save_from_bytes(
            data=b"hello world",
            original_name="test.txt",
            category="personal",
        )
        assert record.stored_path.exists()
        assert record.size_bytes == 11
        assert record.sha256
        assert record.category == "personal"
        assert record.stored_path.read_bytes() == b"hello world"

    @pytest.mark.asyncio
    async def test_save_from_path(self, file_storage, sample_text_file):
        record = await file_storage.save_from_path(
            source=sample_text_file,
            category="health",
        )
        assert record.stored_path.exists()
        assert record.original_name == "test.txt"

    @pytest.mark.asyncio
    async def test_category_dir_structure(self, file_storage):
        record = await file_storage.save_from_bytes(
            data=b"data", original_name="doc.pdf", category="business",
        )
        # Should be in base/business/YYYY-MM/
        parts = record.stored_path.parts
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
        assert record.stored_path.exists()
        result = await file_storage.delete(record.stored_path)
        assert result is True
        assert not record.stored_path.exists()

    @pytest.mark.asyncio
    async def test_hash_consistency(self, file_storage):
        data = b"same content"
        r1 = await file_storage.save_from_bytes(data=data, original_name="a.txt", category="x")
        r2 = await file_storage.save_from_bytes(data=data, original_name="b.txt", category="x")
        assert r1.sha256 == r2.sha256
