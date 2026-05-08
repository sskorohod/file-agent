"""Send free-form text (voice notes, chat user messages) to the cognee sidecar.

Mirrors the file-pipeline's ``_step_cognee_ingest`` for inputs that don't go
through the document pipeline.
"""

from __future__ import annotations

import logging

from app.memory import CogneeClient, CogneeError

logger = logging.getLogger(__name__)

# Same lower bound the file pipeline uses for "worth running cognify on".
_TEXT_MIN_CHARS = 40


async def ingest_text_to_cognee(
    cognee_client: CogneeClient | None,
    *,
    content: str,
    source_type: str,
    source_id: str,
    dataset: str | None = None,
    filename: str | None = None,
) -> bool:
    """Hand ``content`` to the cognee sidecar as a single ingest.

    Returns ``True`` only if both add and cognify came back clean. Any
    failure — sidecar down, network error, cognify pipeline error — is
    logged and swallowed. This function MUST NOT raise; callers run it on
    the user's hot path (Telegram handler, voice note save) and must keep
    working when memory is degraded.

    ``source_type`` and ``source_id`` are stored on the cognee data record
    so we can later trace a memory back to its origin (note row, chat
    message, etc.).
    """
    if cognee_client is None or not cognee_client.healthy:
        return False
    if not content or len(content) < _TEXT_MIN_CHARS:
        return False

    target_dataset = dataset or cognee_client.config.default_dataset
    upload_name = filename or f"{source_type}_{source_id}.txt"

    try:
        await cognee_client.add(
            content=content,
            dataset=target_dataset,
            filename=upload_name,
        )
        await cognee_client.cognify(dataset=target_dataset)
    except CogneeError as exc:
        logger.warning(
            "cognee ingest failed for %s/%s: %s",
            source_type, source_id, exc,
        )
        return False
    except Exception as exc:  # never let this bubble into the handler
        logger.warning(
            "cognee ingest unexpected error for %s/%s: %s",
            source_type, source_id, exc,
        )
        return False

    logger.debug("cognee ingest ok: source_type=%s source_id=%s chars=%d",
                 source_type, source_id, len(content))
    return True
