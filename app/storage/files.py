"""File storage — save, organize, and deduplicate files on disk."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4


@dataclass
class FileRecord:
    """Result of saving a file to storage."""
    id: str
    original_name: str
    stored_path: Path
    sha256: str
    size_bytes: int
    mime_type: str
    category: str = "uncategorized"
    is_duplicate: bool = False
    created_at: datetime = field(default_factory=datetime.now)


class FileStorage:
    """Manages file storage with categorized directories and deduplication."""

    def __init__(self, base_path: str | Path, allowed_extensions: list[str] | None = None):
        self.base_path = Path(base_path).expanduser().resolve()
        self.allowed_extensions = set(allowed_extensions or [])
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _hash_file(self, path: Path) -> str:
        """Compute SHA-256 hash of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _hash_bytes(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _detect_mime(self, filename: str) -> str:
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"

    def _build_storage_path(
        self,
        category: str,
        original_name: str,
        naming_template: str | None = None,
        metadata: dict | None = None,
    ) -> Path:
        """Build target path: base/<category>/<YYYY-MM>/<filename>."""
        now = datetime.now()
        month_dir = self.base_path / category / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        # Always keep original filename
        name = Path(original_name).name
        target = month_dir / name
        # Avoid collisions
        stem_base = Path(name).stem
        ext = Path(name).suffix
        counter = 1
        while target.exists():
            target = month_dir / f"{stem_base}_{counter}{ext}"
            counter += 1

        return target

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
        """Save bytes to storage, checking for duplicates."""
        sha = self._hash_bytes(data)
        target = self._build_storage_path(category, original_name, naming_template, metadata)

        target.write_bytes(data)

        return FileRecord(
            id=uuid4().hex,
            original_name=original_name,
            stored_path=target,
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
        """Copy file from source path to storage."""
        original_name = original_name or source.name
        sha = self._hash_file(source)
        target = self._build_storage_path(category, original_name, naming_template, metadata)

        shutil.copy2(source, target)

        return FileRecord(
            id=uuid4().hex,
            original_name=original_name,
            stored_path=target,
            sha256=sha,
            size_bytes=target.stat().st_size,
            mime_type=self._detect_mime(original_name),
            category=category,
        )

    async def find_by_hash(self, sha256: str) -> list[Path]:
        """Find all files with matching hash (for dedup check)."""
        matches = []
        for f in self.base_path.rglob("*"):
            if f.is_file() and self._hash_file(f) == sha256:
                matches.append(f)
        return matches

    async def delete(self, path: Path) -> bool:
        """Delete a file from storage."""
        if path.exists() and path.is_relative_to(self.base_path):
            path.unlink()
            return True
        return False
