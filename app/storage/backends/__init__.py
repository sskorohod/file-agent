"""Storage backends — pluggable file storage (local, S3, Google Drive)."""

from app.storage.backends.base import StorageBackend
from app.storage.backends.local import LocalBackend

__all__ = ["StorageBackend", "LocalBackend"]
