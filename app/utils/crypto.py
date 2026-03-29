"""Encryption utilities — Fernet for secrets, AES-256-GCM for files and DB columns."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SALT = b'fileagent-secrets-v1'
_ITERATIONS = 600_000


def get_fernet(secret_key: str) -> Fernet:
    """Create Fernet instance using PBKDF2 key derivation."""
    dk = hashlib.pbkdf2_hmac('sha256', secret_key.encode(), _SALT, _ITERATIONS)
    key = base64.urlsafe_b64encode(dk)
    return Fernet(key)


def encrypt(value: str, secret_key: str) -> str:
    """Encrypt a string value. Returns base64-encoded ciphertext."""
    f = get_fernet(secret_key)
    return f.encrypt(value.encode()).decode()


def decrypt(encrypted: str, secret_key: str) -> str:
    """Decrypt a base64-encoded ciphertext. Returns plaintext."""
    f = get_fernet(secret_key)
    try:
        return f.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        return ""


def mask_key(value: str) -> str:
    """Mask a secret key for display: show first 4 + last 4 chars."""
    if not value or len(value) < 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


# ── AES-256-GCM for files and DB columns ──────────────────────────────────

_MAGIC = b"FAGE\x01"  # File Agent Encrypted, version 1
_MAGIC_LEN = len(_MAGIC)
_NONCE_LEN = 12


def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM encrypt with magic header. Format: FAGE\\x01 + nonce(12) + ciphertext + tag(16)."""
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, data, None)
    return _MAGIC + nonce + ciphertext


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt. Auto-detects magic header."""
    if data[:_MAGIC_LEN] == _MAGIC:
        data = data[_MAGIC_LEN:]
    nonce, ciphertext = data[:_NONCE_LEN], data[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def is_encrypted(data: bytes) -> bool:
    """Check if data starts with the FAGE magic header."""
    return data[:_MAGIC_LEN] == _MAGIC


def encrypt_text(value: str, key: bytes) -> str:
    """Encrypt a string for DB storage. Returns base64-encoded ciphertext."""
    if not value:
        return value
    encrypted = encrypt_bytes(value.encode("utf-8"), key)
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_text(value: str, key: bytes) -> str:
    """Decrypt a base64-encoded string from DB. Returns plaintext."""
    if not value:
        return value
    try:
        encrypted = base64.b64decode(value)
        return decrypt_bytes(encrypted, key).decode("utf-8")
    except Exception:
        return value  # Return as-is if not encrypted (backward compat)


# ── Key derivation (Argon2id — GPU/ASIC resistant) ───────────────────────

_KDF_SALT_LEN = 32
_VERIFY_PLAINTEXT = b"FILEAGENT_KEY_VERIFY_v2"

# Argon2id params: 64MB memory, 3 iterations, 1 thread
# One attempt ≈ 0.5s on modern CPU, GPU parallelism useless due to memory
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_KB = 65536  # 64 MB
_ARGON2_PARALLELISM = 1

# Brute-force protection (persisted to disk)
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_FILE = "data/.lockout"


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    """Derive 256-bit key from password using Argon2id (memory-hard, GPU-resistant)."""
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_KB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=32,
        type=Type.ID,  # Argon2id — best hybrid resistance
    )


def _load_lockout() -> dict:
    """Load persistent lockout state from disk."""
    import json
    from pathlib import Path
    p = Path(_LOCKOUT_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {"count": 0, "last": 0}
    return {"count": 0, "last": 0}


def _save_lockout(state: dict):
    """Save lockout state to disk (survives restarts)."""
    import json
    from pathlib import Path
    p = Path(_LOCKOUT_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def _check_brute_force(keyfile_path: str):
    """Raise if too many failed attempts (exponential backoff, persisted)."""
    import time
    state = _load_lockout()
    now = time.time()

    # Reset if last attempt was > 1 hour ago
    if now - state.get("last", 0) > 3600:
        state = {"count": 0, "last": 0}
        _save_lockout(state)
        return

    if state["count"] >= _MAX_FAILED_ATTEMPTS:
        wait = min(2 ** (state["count"] - _MAX_FAILED_ATTEMPTS + 1), 300)
        remaining = wait - (now - state["last"])
        if remaining > 0:
            raise ValueError(
                f"Слишком много попыток. Подождите {int(remaining)} сек."
            )


def _record_failed_attempt(keyfile_path: str):
    import time
    state = _load_lockout()
    state["count"] = state.get("count", 0) + 1
    state["last"] = time.time()
    _save_lockout(state)


def setup_master_password(
    password: str,
    keyfile_path: str = "data/encryption.key",
    key_file_data: bytes | None = None,
) -> bytes:
    """First-time setup: derive key from password, save salt + verification token.

    The keyfile stores ONLY:
    - salt (32 bytes) — random, for key derivation
    - verify_token — encrypted known plaintext to verify password on next start

    If key_file_data is provided (from USB/external), it is mixed into
    the key derivation as a second factor.

    The actual encryption key is NEVER written to disk.
    Returns the derived 256-bit key (in memory only).
    """
    salt = os.urandom(_KDF_SALT_LEN)

    # Combine password + optional key file (2FA)
    combined = password
    if key_file_data:
        combined = password + hashlib.sha256(key_file_data).hexdigest()

    key = derive_key_from_password(combined, salt)
    verify_token = encrypt_bytes(_VERIFY_PLAINTEXT, key)

    from pathlib import Path
    path = Path(keyfile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Header byte: 0x01 = password only, 0x02 = password + key file
    header = b"\x02" if key_file_data else b"\x01"
    path.write_bytes(header + salt + verify_token)
    return key


def unlock_with_password(
    password: str,
    keyfile_path: str = "data/encryption.key",
    key_file_data: bytes | None = None,
) -> bytes:
    """Unlock: derive key from password (+ optional key file), verify.

    Returns the derived key if credentials are correct.
    Raises ValueError on wrong password, missing keyfile, or brute-force lockout.
    """
    from pathlib import Path
    _check_brute_force(keyfile_path)

    path = Path(keyfile_path)
    if not path.exists():
        raise ValueError("Keyfile not found — run setup first")

    data = path.read_bytes()
    min_len = 1 + _KDF_SALT_LEN + _MAGIC_LEN + _NONCE_LEN + 16
    if len(data) < min_len:
        raise ValueError("Keyfile is corrupt")

    header = data[0]
    salt = data[1:1 + _KDF_SALT_LEN]
    verify_token = data[1 + _KDF_SALT_LEN:]

    # Check if key file is required
    if header == 0x02 and not key_file_data:
        raise ValueError(
            "Этот сервер защищён двухфакторно: "
            "нужен пароль + файл-ключ"
        )

    combined = password
    if key_file_data:
        combined = password + hashlib.sha256(key_file_data).hexdigest()

    key = derive_key_from_password(combined, salt)

    try:
        plaintext = decrypt_bytes(verify_token, key)
        if not secrets.compare_digest(plaintext, _VERIFY_PLAINTEXT):
            raise ValueError()
    except Exception:
        _record_failed_attempt(keyfile_path)
        raise ValueError("Неверный пароль") from None

    # Clear lockout on success
    _save_lockout({"count": 0, "last": 0})
    return key


def is_encryption_configured(keyfile_path: str = "data/encryption.key") -> bool:
    """Check if master password has been set up."""
    from pathlib import Path
    return Path(keyfile_path).exists()


def generate_key_file(path: str = "data/keyfile.secret") -> bytes:
    """Generate a random 256-bit key file for 2FA. Store on USB drive."""
    data = os.urandom(32)
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return data


def generate_encryption_key() -> str:
    """Generate a 256-bit hex encryption key."""
    return secrets.token_hex(32)


def parse_encryption_key(hex_key: str) -> bytes:
    """Parse hex key string to bytes. Raises ValueError if invalid."""
    key = bytes.fromhex(hex_key)
    if len(key) != 32:
        raise ValueError(f"Encryption key must be 32 bytes, got {len(key)}")
    return key


def generate_recovery_key(key: bytes) -> str:
    """Generate a recovery key with checksum for safe storage."""
    checksum = hashlib.sha256(key).digest()[:4]
    return base64.urlsafe_b64encode(key + checksum).decode()


def recover_key_from_recovery(recovery: str) -> bytes:
    """Recover encryption key from recovery key string."""
    payload = base64.urlsafe_b64decode(recovery)
    key, checksum = payload[:-4], payload[-4:]
    if hashlib.sha256(key).digest()[:4] != checksum:
        raise ValueError("Invalid recovery key — checksum mismatch")
    return key
