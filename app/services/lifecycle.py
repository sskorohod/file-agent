"""File lifecycle service — centralised delete, reclassify, and cache invalidation."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class FileLifecycleService:
    """Single entry-point for file mutations that must stay consistent
    across disk, SQLite, Qdrant, and search cache."""

    def __init__(
        self,
        db: Any,
        file_storage: Any,
        vector_store: Any | None = None,
        llm_search: Any | None = None,
        classifier: Any | None = None,
    ):
        self.db = db
        self.file_storage = file_storage
        self.vector_store = vector_store
        self.llm_search = llm_search
        self.classifier = classifier

    # ── Delete ───────────────────────────────────────────────────────────

    async def delete(self, file_id: str) -> bool:
        """Cascade delete: Qdrant vectors → file storage → DB → cache.

        Returns True if the file existed and was deleted.
        """
        file = await self.db.get_file(file_id)
        if not file:
            return False

        # 1. Delete vectors from Qdrant
        if self.vector_store:
            try:
                await self.vector_store.delete_document(file_id)
            except Exception as e:
                logger.warning(f"Failed to delete vectors for {file_id}: {e}")

        # 2. Delete file from storage backend
        if file.get("stored_path"):
            try:
                await self.file_storage.delete(file["stored_path"])
            except Exception as e:
                logger.warning(f"Failed to delete stored file {file_id}: {e}")

        # 3. Delete from DB (files + processing_log + FTS via trigger)
        await self.db.delete_file(file_id)

        # 4. Invalidate search cache
        await self._invalidate_cache()

        logger.info(f"Cascade deleted file {file_id}")
        return True

    # ── Reclassify ───────────────────────────────────────────────────────

    async def reclassify(self, file_id: str) -> dict | None:
        """Re-classify a file and propagate changes to DB, Qdrant, and cache.

        Returns normalised result dict or None if file not found / no classifier.
        """
        if not self.classifier:
            logger.warning("Reclassify called but no classifier configured")
            return None

        file = await self.db.get_file(file_id)
        if not file:
            return None

        text = file.get("extracted_text") or ""
        filename = file.get("original_name", "")
        mime_type = file.get("mime_type", "")

        # 1. Classify
        result = await self.classifier.classify(
            text=text, filename=filename, mime_type=mime_type,
        )

        # 2. Update DB columns
        await self.db.update_file(
            file_id,
            category=result.category,
            tags=result.tags,
            summary=result.summary,
        )

        # 3. Update document_type inside metadata_json
        meta_raw = file.get("metadata_json") or "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta["document_type"] = result.document_type
        await self.db.update_file(file_id, metadata_json=meta)

        # 4. Update Qdrant payload (no re-embedding needed)
        if self.vector_store:
            try:
                await self.vector_store.update_document_metadata(
                    file_id,
                    {
                        "category": result.category,
                        "document_type": result.document_type,
                        "filename": filename,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to update Qdrant payload for {file_id}: {e}")

        # 5. Invalidate search cache
        await self._invalidate_cache()

        logger.info(f"Reclassified {file_id} → {result.category}")
        return {
            "file_id": file_id,
            "category": result.category,
            "tags": result.tags,
            "summary": result.summary,
            "document_type": result.document_type,
        }

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _invalidate_cache(self):
        """Invalidate search cache (fire-and-forget)."""
        if self.llm_search:
            try:
                await self.llm_search.invalidate_cache()
            except Exception:
                pass
