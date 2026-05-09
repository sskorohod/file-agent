"""Lightweight ingestion helpers shared across handlers.

Files have a heavy pipeline of their own (`app/pipeline.py`); short text
streams (voice notes, chat user messages) only need a thin shim that hands
the text to the cognee sidecar and never raises. This package keeps that
shim out of the bot handlers themselves.
"""

from app.ingestion.text_ingest import ingest_text_to_cognee

__all__ = ["ingest_text_to_cognee"]
