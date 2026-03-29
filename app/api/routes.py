"""REST API v1 — authenticated endpoints for external integrations."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse

from app.web.limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def _get(key: str):
    from app.main import get_state
    return get_state(key)


async def verify_api_key(authorization: str = Header(None)) -> dict:
    """Dependency: validate Bearer token, return {key, mode}."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    key = authorization[7:]
    db = _get("db")
    if not db:
        raise HTTPException(status_code=503, detail="DB not available")
    mode = await db.validate_api_key(key)
    if mode is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"key": key, "mode": mode}


@router.get("/stats")
async def api_stats(auth: dict = Depends(verify_api_key)):
    db = _get("db")
    stats = await db.get_stats()
    return {
        "total_files": stats.get("total_files", 0),
        "categories": stats.get("categories", {}),
        "total_size_mb": round(stats.get("total_size_bytes", 0) / 1048576, 2),
    }


@router.get("/files")
async def api_list_files(
    category: str | None = None, limit: int = 20, offset: int = 0,
    auth: dict = Depends(verify_api_key),
):
    db = _get("db")
    files = await db.list_files(category=category, limit=min(limit, 100), offset=offset)
    return [
        {
            "id": f["id"], "name": f["original_name"], "category": f["category"],
            "size_bytes": f["size_bytes"], "mime_type": f.get("mime_type", ""),
            "summary": f.get("summary", ""), "created_at": f["created_at"],
        }
        for f in files
    ]


@router.get("/files/{file_id}")
async def api_get_file(file_id: str, auth: dict = Depends(verify_api_key)):
    db = _get("db")
    file = await db.get_file(file_id)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "id": file["id"],
        "name": file["original_name"],
        "category": file["category"],
        "mime_type": file.get("mime_type", ""),
        "size_bytes": file["size_bytes"],
        "tags": file.get("tags", "[]"),
        "summary": file.get("summary", ""),
        "created_at": file["created_at"],
    }


@router.get("/files/{file_id}/text")
async def api_get_file_text(
    file_id: str, max_chars: int = 5000,
    auth: dict = Depends(verify_api_key),
):
    db = _get("db")
    file = await db.get_file(file_id)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    text = file.get("extracted_text", "") or ""
    return {
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "total_chars": len(text),
    }


@router.get("/files/{file_id}/download")
async def api_download_file(file_id: str, auth: dict = Depends(verify_api_key)):
    db = _get("db")
    file = await db.get_file(file_id)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(file["stored_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path, filename=file["original_name"], media_type=file.get("mime_type", ""))


@router.get("/search")
@limiter.limit("30/minute")
async def api_search(
    request: Request,
    q: str, top_k: int = 5,
    auth: dict = Depends(verify_api_key),
):
    """Semantic search. Lite: raw chunks. Full: LLM-synthesized answer."""
    mode = auth["mode"]

    if mode == "full":
        # Full mode: LLM generates answer from search results
        search = _get("llm_search")
        if search:
            result = await search.answer(q, top_k=min(top_k, 10))
            return {
                "mode": "full",
                "answer": result.get("text", ""),
                "file_ids": result.get("file_ids", {}),
            }

    # Lite mode (default): raw vector search, no LLM
    vs = _get("vector_store")
    if not vs:
        raise HTTPException(status_code=503, detail="Vector store not available")
    results = await vs.search(q, top_k=min(top_k, 20))
    return {
        "mode": "lite",
        "results": [
            {
                "file_id": r.file_id,
                "filename": r.metadata.get("filename", ""),
                "category": r.metadata.get("category", ""),
                "score": round(r.score, 4),
                "text": r.text[:500],
            }
            for r in results
        ],
    }


@router.post("/files/upload")
@limiter.limit("10/minute")
async def api_upload_file(
    request: Request,
    body: dict,
    auth: dict = Depends(verify_api_key),
):
    """Upload a file for processing. Body: {filename, data (base64), comment?}"""
    filename = body.get("filename")
    data_b64 = body.get("data")
    if not filename or not data_b64:
        raise HTTPException(status_code=400, detail="filename and data (base64) required")

    try:
        file_data = base64.b64decode(data_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 data")

    pipeline = _get("pipeline")
    if not pipeline:
        raise HTTPException(status_code=503, detail="Pipeline not available")

    result = await pipeline.process(file_data, filename, source="api")

    if result.is_duplicate:
        return {"status": "duplicate", "existing_file_id": result.duplicate_of.get("id", "")}

    return {
        "status": "ok",
        "file_id": result.file_id,
        "category": result.classification.category if result.classification else "",
        "summary": result.classification.summary if result.classification else "",
        "chunks_embedded": result.chunks_embedded,
    }


@router.delete("/files/{file_id}")
@limiter.limit("20/minute")
async def api_delete_file(request: Request, file_id: str, auth: dict = Depends(verify_api_key)):
    """Cascading delete via lifecycle service."""
    lifecycle = _get("lifecycle")
    if not lifecycle:
        raise HTTPException(status_code=500, detail="Lifecycle service not available")
    deleted = await lifecycle.delete(file_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    return {"deleted": True, "file_id": file_id}


@router.post("/files/{file_id}/reclassify")
@limiter.limit("10/minute")
async def api_reclassify_file(request: Request, file_id: str, auth: dict = Depends(verify_api_key)):
    """Reclassify a file via lifecycle service."""
    lifecycle = _get("lifecycle")
    if not lifecycle:
        raise HTTPException(status_code=500, detail="Lifecycle service not available")
    result = await lifecycle.reclassify(file_id)
    if not result:
        raise HTTPException(status_code=404, detail="File not found or classifier unavailable")
    return result
