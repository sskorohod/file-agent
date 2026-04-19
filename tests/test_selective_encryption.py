"""Tests for selective encryption by skill."""

from pathlib import Path

import pytest

from app.skills.engine import SkillDefinition


class TestSkillEncryptField:
    def test_encrypt_default_is_false(self):
        skill = SkillDefinition(name="demo", category="demo")
        assert skill.encrypt is False

    def test_encrypt_true_parses(self):
        skill = SkillDefinition(name="demo", category="demo", encrypt=True)
        assert skill.encrypt is True

    def test_encrypt_false_parses(self):
        skill = SkillDefinition(name="demo", category="demo", encrypt=False)
        assert skill.encrypt is False


class TestSkillYamlEncryptFlag:
    @pytest.mark.asyncio
    async def test_personal_skill_has_encrypt_true(self):
        from app.skills.engine import SkillEngine
        engine = SkillEngine(Path(__file__).parent.parent / "skills")
        await engine.load_all()
        personal = engine.get_skill("personal")
        assert personal is not None, "personal skill should load from skills/"
        assert personal.encrypt is True

    @pytest.mark.asyncio
    async def test_business_skill_encrypt_default_false(self):
        from app.skills.engine import SkillEngine
        engine = SkillEngine(Path(__file__).parent.parent / "skills")
        await engine.load_all()
        business = engine.get_skill("business")
        assert business is not None
        assert business.encrypt is False


class TestLocalBackendSelectiveEncryption:
    @pytest.mark.asyncio
    async def test_write_plaintext_when_encrypt_false(self, tmp_dir):
        from app.storage.backends.local import LocalBackend
        key = b"\x01" * 32
        backend = LocalBackend(base_path=tmp_dir / "files", encryption_key=key)
        uri = await backend.write(b"plain payload", "test", "a.txt", encrypt=False)
        raw = Path(uri).read_bytes()
        assert not raw.startswith(b"FAGE\x01")
        assert raw == b"plain payload"

    @pytest.mark.asyncio
    async def test_write_encrypted_when_encrypt_true(self, tmp_dir):
        from app.storage.backends.local import LocalBackend
        from app.utils.crypto import decrypt_bytes
        key = b"\x02" * 32
        backend = LocalBackend(base_path=tmp_dir / "files", encryption_key=key)
        uri = await backend.write(b"secret payload", "test", "s.txt", encrypt=True)
        raw = Path(uri).read_bytes()
        assert raw.startswith(b"FAGE\x01")
        assert decrypt_bytes(raw, key) == b"secret payload"

    @pytest.mark.asyncio
    async def test_encrypt_true_without_key_writes_plaintext(self, tmp_dir, caplog):
        import logging
        from app.storage.backends.local import LocalBackend
        backend = LocalBackend(base_path=tmp_dir / "files", encryption_key=None)
        with caplog.at_level(logging.WARNING):
            uri = await backend.write(b"hello", "test", "h.txt", encrypt=True)
        raw = Path(uri).read_bytes()
        assert raw == b"hello"
        assert any("no encryption key" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_roundtrip_mixed(self, tmp_dir):
        from app.storage.backends.local import LocalBackend
        key = b"\x03" * 32
        backend = LocalBackend(base_path=tmp_dir / "files", encryption_key=key)
        enc_uri = await backend.write(b"enc", "x", "e.txt", encrypt=True)
        plain_uri = await backend.write(b"plain", "x", "p.txt", encrypt=False)
        assert await backend.read(enc_uri) == b"enc"
        assert await backend.read(plain_uri) == b"plain"


class TestFileStorageSelectiveEncryption:
    @pytest.mark.asyncio
    async def test_save_from_bytes_encrypt_false_default(self, tmp_dir):
        from app.storage.backends.local import LocalBackend
        from app.storage.files import FileStorage
        key = b"\x04" * 32
        local = LocalBackend(tmp_dir / "f", encryption_key=key)
        fs = FileStorage("local", {"local": local})
        rec = await fs.save_from_bytes(b"plain", "a.txt", category="x")
        assert rec.encrypted is False
        assert not Path(rec.stored_path).read_bytes().startswith(b"FAGE\x01")

    @pytest.mark.asyncio
    async def test_save_from_bytes_encrypt_true(self, tmp_dir):
        from app.storage.backends.local import LocalBackend
        from app.storage.files import FileStorage
        key = b"\x05" * 32
        local = LocalBackend(tmp_dir / "f", encryption_key=key)
        fs = FileStorage("local", {"local": local})
        rec = await fs.save_from_bytes(b"secret", "s.txt", category="x", encrypt=True)
        assert rec.encrypted is True
        assert Path(rec.stored_path).read_bytes().startswith(b"FAGE\x01")


class TestDbEncryptedColumn:
    @pytest.mark.asyncio
    async def test_encrypted_column_exists(self, db):
        cursor = await db._db.execute("PRAGMA table_info(files)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "encrypted" in cols

    @pytest.mark.asyncio
    async def test_encrypted_column_defaults_to_zero(self, db):
        await db._db.execute(
            """INSERT INTO files
               (id, original_name, stored_path, sha256, size_bytes, mime_type, category)
               VALUES ('legacy1', 'x.txt', '/tmp/x', 'abc', 1, 'text/plain', 'x')"""
        )
        await db._db.commit()
        cursor = await db._db.execute("SELECT encrypted FROM files WHERE id='legacy1'")
        row = await cursor.fetchone()
        assert row[0] == 0


class TestDbInsertFileEncrypted:
    @pytest.mark.asyncio
    async def test_insert_with_encrypted_true(self, db):
        await db.insert_file(
            id="f1", original_name="p.pdf", stored_path="/tmp/p.pdf",
            sha256="a" * 64, size_bytes=100, mime_type="application/pdf",
            category="personal", encrypted=True,
        )
        row = await db.get_file("f1")
        assert row is not None
        assert row["encrypted"] is True

    @pytest.mark.asyncio
    async def test_insert_with_encrypted_false_default(self, db):
        await db.insert_file(
            id="f2", original_name="r.jpg", stored_path="/tmp/r.jpg",
            sha256="b" * 64, size_bytes=50, mime_type="image/jpeg",
            category="receipts",
        )
        row = await db.get_file("f2")
        assert row is not None
        assert row["encrypted"] is False
