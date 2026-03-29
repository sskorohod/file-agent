"""Tests for encryption utilities — AES-256-GCM, magic bytes, recovery keys."""

import pytest

from app.utils.crypto import (
    decrypt_bytes,
    decrypt_text,
    encrypt_bytes,
    encrypt_text,
    generate_encryption_key,
    generate_recovery_key,
    is_encrypted,
    is_encryption_configured,
    parse_encryption_key,
    recover_key_from_recovery,
    setup_master_password,
    unlock_with_password,
)


@pytest.fixture
def key():
    hex_key = generate_encryption_key()
    return parse_encryption_key(hex_key)


class TestAES256GCM:
    def test_roundtrip(self, key):
        data = b"Hello, World! Some sensitive document content."
        encrypted = encrypt_bytes(data, key)
        decrypted = decrypt_bytes(encrypted, key)
        assert decrypted == data

    def test_magic_header(self, key):
        data = b"test data"
        encrypted = encrypt_bytes(data, key)
        assert encrypted[:4] == b"FAGE"
        assert encrypted[4] == 1  # version

    def test_is_encrypted(self, key):
        data = b"plaintext"
        encrypted = encrypt_bytes(data, key)
        assert is_encrypted(encrypted) is True
        assert is_encrypted(data) is False

    def test_wrong_key(self, key):
        data = b"secret"
        encrypted = encrypt_bytes(data, key)
        wrong_key = bytes(32)  # all zeros
        with pytest.raises(Exception):
            decrypt_bytes(encrypted, wrong_key)

    def test_empty_data(self, key):
        encrypted = encrypt_bytes(b"", key)
        assert decrypt_bytes(encrypted, key) == b""

    def test_large_data(self, key):
        data = b"x" * (10 * 1024 * 1024)  # 10MB
        encrypted = encrypt_bytes(data, key)
        assert decrypt_bytes(encrypted, key) == data

    def test_overhead(self, key):
        data = b"test"
        encrypted = encrypt_bytes(data, key)
        # Magic(5) + nonce(12) + data(4) + GCM tag(16) = 37
        assert len(encrypted) == len(data) + 33


class TestTextEncryption:
    def test_roundtrip(self, key):
        text = "Паспорт серия 1234 номер 567890"
        encrypted = encrypt_text(text, key)
        assert encrypted != text
        decrypted = decrypt_text(encrypted, key)
        assert decrypted == text

    def test_empty_passthrough(self, key):
        assert encrypt_text("", key) == ""
        assert decrypt_text("", key) == ""

    def test_backward_compat(self, key):
        """decrypt_text on plaintext returns as-is (for unencrypted DB rows)."""
        plain = "not encrypted"
        assert decrypt_text(plain, key) == plain


class TestKeyManagement:
    def test_generate_key_length(self):
        hex_key = generate_encryption_key()
        assert len(hex_key) == 64  # 32 bytes in hex

    def test_parse_key(self):
        hex_key = generate_encryption_key()
        key = parse_encryption_key(hex_key)
        assert len(key) == 32

    def test_parse_invalid_key(self):
        with pytest.raises(ValueError):
            parse_encryption_key("abcd")  # too short

    def test_recovery_roundtrip(self, key):
        recovery = generate_recovery_key(key)
        recovered = recover_key_from_recovery(recovery)
        assert recovered == key

    def test_recovery_tampered(self, key):
        recovery = generate_recovery_key(key)
        # Tamper with recovery key
        tampered = recovery[:-1] + ("A" if recovery[-1] != "A" else "B")
        with pytest.raises(Exception):
            recover_key_from_recovery(tampered)


class TestMasterPassword:
    def test_setup_and_unlock(self, tmp_path):
        keyfile = str(tmp_path / "test.key")
        password = "my-strong-password-123"

        key1 = setup_master_password(password, keyfile)
        assert len(key1) == 32
        assert is_encryption_configured(keyfile)

        key2 = unlock_with_password(password, keyfile)
        assert key1 == key2  # Same password → same key

    def test_wrong_password(self, tmp_path):
        keyfile = str(tmp_path / "test.key")
        setup_master_password("correct-password", keyfile)

        with pytest.raises(ValueError, match="Неверный пароль"):
            unlock_with_password("wrong-password", keyfile)

    def test_missing_keyfile(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            unlock_with_password("any", str(tmp_path / "nope.key"))

    def test_key_never_on_disk(self, tmp_path):
        keyfile = str(tmp_path / "test.key")
        password = "test-password-456"
        key = setup_master_password(password, keyfile)

        # The keyfile must NOT contain the raw key
        data = open(keyfile, "rb").read()
        assert key not in data
        assert key.hex().encode() not in data

    def test_encrypted_data_works(self, tmp_path):
        """Full roundtrip: password → key → encrypt → new session → password → decrypt."""
        keyfile = str(tmp_path / "test.key")
        password = "roundtrip-test"

        key1 = setup_master_password(password, keyfile)
        ciphertext = encrypt_bytes(b"secret document", key1)

        # Simulate restart — key gone from memory
        del key1

        key2 = unlock_with_password(password, keyfile)
        plaintext = decrypt_bytes(ciphertext, key2)
        assert plaintext == b"secret document"

    def test_2fa_with_key_file(self, tmp_path):
        """Password + key file = different key than password alone."""
        keyfile = str(tmp_path / "test.key")
        password = "same-password"
        key_file_data = b"secret-usb-key-content"

        key_2fa = setup_master_password(password, keyfile, key_file_data=key_file_data)

        # Unlock requires both password AND key file
        key_unlocked = unlock_with_password(password, keyfile, key_file_data=key_file_data)
        assert key_2fa == key_unlocked

        # Password alone fails when 2FA is configured
        with pytest.raises(ValueError, match="двухфакторно|файл-ключ"):
            unlock_with_password(password, keyfile)

    def test_brute_force_lockout(self, tmp_path):
        from app.utils.crypto import _save_lockout
        keyfile = str(tmp_path / "bf.key")
        setup_master_password("correct-pass", keyfile)

        # Clear any previous lockout state
        _save_lockout({"count": 0, "last": 0})

        # 5 wrong attempts
        for _ in range(5):
            with pytest.raises(ValueError, match="Неверный пароль"):
                unlock_with_password("wrong", keyfile)

        # 6th attempt should be rate-limited
        with pytest.raises(ValueError, match="Слишком много попыток"):
            unlock_with_password("wrong", keyfile)

        # Clean up
        _save_lockout({"count": 0, "last": 0})
