"""Local disk storage backend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.storage.backends.base import StorageBackend


class LocalBackend(StorageBackend):
    """Store files on local disk with optional AES-256-GCM encryption."""

    scheme = "local"

    def __init__(self, base_path: str | Path, encryption_key: bytes | None = None):
        self.base_path = Path(base_path).expanduser().resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._encryption_key = encryption_key

    def _resolve_path(self, uri: str) -> Path:
        """Convert URI or bare path to local Path. Validates path is within base_path."""
        path_str = uri.removeprefix("local://") if uri.startswith("local://") else uri
        resolved = Path(path_str).resolve()
        if not resolved.is_relative_to(self.base_path):
            raise ValueError(f"Path traversal blocked: {uri}")
        return resolved

    def _build_path(self, category: str, original_name: str) -> Path:
        """Build target: base/<category>/<YYYY-MM>/<filename> with collision avoidance."""
        now = datetime.now()
        month_dir = self.base_path / category / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        name = Path(original_name).name
        target = month_dir / name
        stem = Path(name).stem
        ext = Path(name).suffix
        counter = 1
        while target.exists():
            target = month_dir / f"{stem}_{counter}{ext}"
            counter += 1
        return target

    async def write(
        self, data: bytes, category: str, original_name: str,
        encrypt: bool = False,
    ) -> str:
        target = self._build_path(category, original_name)

        # Check available disk space (require 10% buffer)
        import shutil
        free = shutil.disk_usage(target.parent).free
        if len(data) > free * 0.9:
            raise IOError(
                f"Insufficient disk space: need {len(data)} bytes, "
                f"only {free} available ({target.parent})"
            )

        write_data = data
        if encrypt:
            if self._encryption_key:
                from app.utils.crypto import encrypt_bytes
                write_data = encrypt_bytes(data, self._encryption_key)
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "encrypt=True requested but no encryption key configured — "
                    "writing plaintext (%s)", original_name,
                )
        target.write_bytes(write_data)
        return str(target)  # Bare path for backward compat

    async def read(self, uri: str) -> bytes:
        path = self._resolve_path(uri)
        data = path.read_bytes()
        if self._encryption_key:
            from app.utils.crypto import decrypt_bytes, is_encrypted
            if is_encrypted(data):
                return decrypt_bytes(data, self._encryption_key)
        return data

    async def delete(self, uri: str) -> bool:
        path = self._resolve_path(uri)
        if path.exists() and path.is_relative_to(self.base_path):
            path.unlink()
            return True
        return False

    async def exists(self, uri: str) -> bool:
        return self._resolve_path(uri).exists()
