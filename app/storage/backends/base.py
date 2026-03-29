"""Abstract storage backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract file storage backend."""

    scheme: str  # URI scheme: "local", "s3", "gdrive"

    @abstractmethod
    async def write(
        self, data: bytes, category: str, original_name: str,
    ) -> str:
        """Write file bytes. Returns URI string (e.g. 's3://bucket/key')."""

    @abstractmethod
    async def read(self, uri: str) -> bytes:
        """Read file bytes by URI. Handles decryption if encrypted."""

    @abstractmethod
    async def delete(self, uri: str) -> bool:
        """Delete file by URI. Returns True if deleted."""

    @abstractmethod
    async def exists(self, uri: str) -> bool:
        """Check if file exists at URI."""
