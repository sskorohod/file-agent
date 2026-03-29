"""Smart Notes v1.5 — Inbox-first knowledge capture with async enrichment."""

from app.notes.capture import NoteCaptureService
from app.notes.enrichment import NoteEnrichmentService
from app.notes.relations import NoteRelationService
from app.notes.projection import NoteProjectionService
from app.notes.processor import NoteProcessor
from app.notes.categorizer import NoteCategorizer, NoteCategoryResult

__all__ = [
    "NoteCaptureService", "NoteEnrichmentService",
    "NoteRelationService", "NoteProjectionService",
    "NoteProcessor", "NoteCategorizer", "NoteCategoryResult",
]
