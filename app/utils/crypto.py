"""Encryption utilities — Fernet symmetric encryption for secrets."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

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
