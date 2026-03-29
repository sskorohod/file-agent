"""File storage dispatcher — routes to local disk, S3, or Google Drive backends."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.storage.backends.base import StorageBackend


@dataclass
class FileRecord:
    """Result of saving a file to storage."""
    id: str
    original_name: str
    stored_path: str  # URI: bare path (local), s3://..., gdrive://...
    sha256: str
    size_bytes: int
    mime_type: str
    category: str = "uncategorized"
    is_duplicate: bool = False
    created_at: datetime = field(default_factory=datetime.now)


class FileStorage:
    """Dispatcher: routes file operations to the active storage backend.

    Writes go to the active backend. Reads/deletes dispatch by URI prefix.
    Local backend is always available for backward compatibility.
    """

    def __init__(
        self,
        active_backend: str,
        backends: dict[str, StorageBackend],
        allowed_extensions: list[str] | None = None,
    ):
        self._active = active_backend
        self._backends = backends
        self.allowed_extensions = set(allowed_extensions or [])

    def _backend_for_uri(self, uri: str) -> StorageBackend:
        """Route to correct backend based on URI scheme."""
        if "://" in uri:
            scheme = uri.split("://", 1)[0]
        else:
            scheme = "local"  # Bare paths = legacy local files
        backend = self._backends.get(scheme)
        if not backend:
            raise ValueError(f"No backend for scheme '{scheme}' (URI: {uri[:50]})")
        return backend

    @property
    def _active_backend(self) -> StorageBackend:
        return self._backends[self._active]

    # Expose base_path for backward compat (orphan cleanup, path validation)
    @property
    def base_path(self) -> Path:
        local = self._backends.get("local")
        if local and hasattr(local, "base_path"):
            return local.base_path
        return Path(".")

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _detect_mime(filename: str) -> str:
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"

    def check_extension(self, filename: str) -> bool:
        """Check if file extension is allowed."""
        if not self.allowed_extensions:
            return True
        return Path(filename).suffix.lower() in self.allowed_extensions

    async def save_from_bytes(
        self,
        data: bytes,
        original_name: str,
        category: str = "uncategorized",
        naming_template: str | None = None,
        metadata: dict | None = None,
    ) -> FileRecord:
        """Save bytes via active backend. Returns FileRecord with URI."""
        sha = self._hash_bytes(data)
        uri = await self._active_backend.write(data, category, original_name)

        return FileRecord(
            id=uuid4().hex,
            original_name=original_name,
            stored_path=uri,
            sha256=sha,
            size_bytes=len(data),
            mime_type=self._detect_mime(original_name),
            category=category,
        )

    async def save_from_path(
        self,
        source: Path,
        original_name: str | None = None,
        category: str = "uncategorized",
        naming_template: str | None = None,
        metadata: dict | None = None,
    ) -> FileRecord:
        """Read file from local path, save via active backend."""
        original_name = original_name or source.name
        data = source.read_bytes()
        return await self.save_from_bytes(data, original_name, category)

    async def read_file(self, uri_or_path) -> bytes:
        """Read file from any backend, auto-detecting by URI scheme."""
        uri = str(uri_or_path)
        return await self._backend_for_uri(uri).read(uri)

    async def delete(self, uri_or_path) -> bool:
        """Delete file from the appropriate backend."""
        uri = str(uri_or_path)
        return await self._backend_for_uri(uri).delete(uri)

    async def exists(self, uri_or_path) -> bool:
        """Check if file exists on the appropriate backend."""
        uri = str(uri_or_path)
        return await self._backend_for_uri(uri).exists(uri)
