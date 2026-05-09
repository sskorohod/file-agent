"""Encryption utilities.

Two unrelated layers in this module:

* ``Fernet`` (PBKDF2 over a user-provided secret key) — used to encrypt
  small string secrets in the ``secrets`` table (API keys, OAuth tokens).
  Owned by the dashboard Settings UI.

* ``AES-256-GCM with magic header FAGB`` — used to encrypt the bytes of
  *sensitive* documents at rest on disk. The system key (32 random bytes
  in ``data/.system_key``) is loaded once at startup and held in memory.
  Decryption to actually open a file is gated by a separate PIN check
  (``app/web/routes.py``); the system key alone never leaves the
  process. See ``docs/architecture-audit-2026-05-08.md`` for the
  threat-model trade-off behind "soft" selective encryption.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Fernet (legacy, for secrets table) ──────────────────────────────────────

_SALT = b"fileagent-secrets-v1"
_ITERATIONS = 600_000


def get_fernet(secret_key: str) -> Fernet:
    """Create Fernet instance using PBKDF2 key derivation."""
    dk = hashlib.pbkdf2_hmac("sha256", secret_key.encode(), _SALT, _ITERATIONS)
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


# ── Sensitive-file encryption (AES-256-GCM, magic FAGB) ─────────────────────

# Format on disk:
#   header(5) "FAGB\x01"
#   nonce(12)  random
#   ciphertext + GCM tag(16)
#
# This is intentionally distinguishable from the legacy "FAGE" format that
# came from feature/encryption-at-rest — selective encryption uses a
# different (per-process) system key, so the formats must not be confused.
_MAGIC = b"FAGB\x01"
_MAGIC_LEN = len(_MAGIC)
_NONCE_LEN = 12

# Default location for the system key — gitignored.
DEFAULT_SYSTEM_KEY_PATH = Path("data/.system_key")


def is_encrypted_blob(data: bytes) -> bool:
    """True if the bytes start with the FAGB magic header."""
    return len(data) >= _MAGIC_LEN and data[:_MAGIC_LEN] == _MAGIC


def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM encrypt with the FAGB magic header.

    ``key`` must be exactly 32 bytes.
    """
    if len(key) != 32:
        raise ValueError(f"system key must be 32 bytes, got {len(key)}")
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, data, None)
    return _MAGIC + nonce + ciphertext


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt. Strips the FAGB header if present.

    Raises if the blob is not a FAGB-encrypted payload or if the key
    does not match (cryptography raises ``InvalidTag``).
    """
    if len(key) != 32:
        raise ValueError(f"system key must be 32 bytes, got {len(key)}")
    if blob[:_MAGIC_LEN] == _MAGIC:
        blob = blob[_MAGIC_LEN:]
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


def load_or_create_system_key(path: str | Path = DEFAULT_SYSTEM_KEY_PATH) -> bytes:
    """Read the system key from disk, generating it on first start.

    Returns 32 random bytes. The file is created with mode 0o600.
    Idempotent — subsequent calls return the same bytes.
    """
    p = Path(path)
    if p.exists():
        data = p.read_bytes()
        if len(data) != 32:
            raise ValueError(
                f"{p} exists but is {len(data)} bytes (expected 32). "
                "Refusing to overwrite — investigate before deleting."
            )
        return data

    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    p.write_bytes(key)
    p.chmod(0o600)
    return key


# ── PIN hashing (Argon2id) ──────────────────────────────────────────────────

# PIN is *authorization*, not key material — it gates the use of the
# in-memory system key when opening a sensitive document. We hash with
# Argon2id (default profile) and store the encoded string as a secret.
# Numeric PINs (4–6 digits) are weak; the hash slows brute-force only
# on this single device. The threat model is "casual access while the
# laptop is unlocked," not "full disk seizure" — see audit doc.
_PIN_HASHER = PasswordHasher()


def hash_pin(pin: str) -> str:
    """Argon2id-hash a PIN and return the encoded hash string."""
    return _PIN_HASHER.hash(pin)


def verify_pin(pin: str, encoded_hash: str) -> bool:
    """Constant-time PIN check. Returns False on any mismatch/format error."""
    if not encoded_hash:
        return False
    try:
        return _PIN_HASHER.verify(encoded_hash, pin)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False
