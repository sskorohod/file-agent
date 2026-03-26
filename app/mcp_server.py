"""MCP Server — expose document intelligence as tools + resources for AI agents."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _get(key: str):
    from app.main import get_state
    return get_state(key)


async def _build_instructions() -> str:
    """Dynamic instructions with real archive context."""
    db = _get("db")
    if not db:
        return "Document archive (initializing...)"
    stats = await db.get_stats()
    cats = stats.get("categories", {})
    cats_str = ", ".join(f"{k}({v})" for k, v in cats.items()) if cats else "empty"
    return (
        f"Personal document archive: {stats.get('total_files', 0)} files — {cats_str}.\n"
        f"Size: {stats.get('total_size_bytes', 0) / 1048576:.1f} MB.\n\n"
        "Tools:\n"
        "- find_and_get(query) — BEST TOOL: search + return full document text in 1 call\n"
        "- search_documents(query) — lightweight search, returns chunks only\n"
        "- list_files(category) — browse files by category\n"
        "- get_file_text(file_id) — read full text of a specific file\n"
        "- save_note / get_notes — persistent memory between sessions\n\n"
        "Resources: documents://overview, documents://recent, documents://notes\n\n"
        "Always interpret document content yourself. Be specific about dates, names, amounts."
    )


# Create MCP server with static instructions (dynamic context via get_archive_overview tool)
mcp = FastMCP(
    "AI File Intelligence Agent",
    instructions=(
        "Personal document archive with semantic search.\n"
        "Use find_and_get(query) to search and get full document content in 1 call.\n"
        "Use get_archive_overview() first to understand what's in the archive.\n"
        "Interpret all content yourself — do not ask for analysis."
    ),
    transport_security={
        "allowed_hosts": [
            "localhost:8000",
            "127.0.0.1:8000",
        ],
    },
)


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_archive_overview() -> str:
    """Get full overview of the archive: categories, file counts, recent files, document types.
    Call this FIRST to understand what documents are available."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})

    stats = await db.get_stats()
    recent = await db.list_files(limit=10)

    return json.dumps({
        "total_files": stats.get("total_files", 0),
        "categories": stats.get("categories", {}),
        "total_size_mb": round(stats.get("total_size_bytes", 0) / 1048576, 2),
        "recent_files": [
            {
                "id": f["id"],
                "name": f["original_name"],
                "category": f["category"],
                "priority": f.get("priority", ""),
                "summary": (f.get("summary", "") or "")[:100],
            }
            for f in recent
        ],
    })


@mcp.tool()
async def find_and_get(query: str) -> str:
    """Search the archive and return the BEST matching document with full text.
    This is the most efficient tool — combines search + text retrieval in 1 call.
    Returns: metadata, summary, priority, and full extracted text."""
    vs = _get("vector_store")
    db = _get("db")
    if not vs or not db:
        return json.dumps({"error": "Not available"})

    results = await vs.search(query, top_k=5)
    if not results:
        return json.dumps({"found": False, "message": "Nothing found for this query"})

    # Pick the best file
    best = results[0]
    file = await db.get_file(best.file_id)
    if not file:
        return json.dumps({"found": False, "message": "File record not found"})

    text = (file.get("extracted_text", "") or "")[:5000]
    meta = {}
    try:
        meta = json.loads(file.get("metadata_json", "{}"))
    except Exception:
        pass

    return json.dumps({
        "found": True,
        "score": round(best.score, 3),
        "file_id": file["id"],
        "name": file["original_name"],
        "category": file["category"],
        "priority": file.get("priority", ""),
        "summary": file.get("summary", ""),
        "document_type": meta.get("document_type", ""),
        "tags": file.get("tags", "[]"),
        "created_at": file["created_at"],
        "text": text,
        "text_truncated": len(file.get("extracted_text", "") or "") > 5000,
        "download_url": f"https://fag.n8nskorx.top/files/{file['id']}/download",
        "hint": "Use get_file_download(file_id) to get the actual file as base64",
    })


@mcp.tool()
async def search_documents(query: str, top_k: int = 5) -> str:
    """Lightweight semantic search — returns matching chunks with scores.
    Use find_and_get() instead if you need full document content."""
    vs = _get("vector_store")
    if not vs:
        return json.dumps({"error": "Vector store not available"})
    results = await vs.search(query, top_k=min(top_k, 10))
    if not results:
        return json.dumps({"results": [], "message": "Nothing found"})
    return json.dumps([
        {
            "file_id": r.file_id,
            "category": r.metadata.get("category", ""),
            "score": round(r.score, 3),
            "text_preview": r.text[:200],
        }
        for r in results
    ])


@mcp.tool()
async def get_file_text(file_id: str, max_chars: int = 5000) -> str:
    """Get extracted text content of a specific file by ID."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    file = await db.get_file(file_id)
    if not file:
        return json.dumps({"error": "File not found"})
    text = file.get("extracted_text", "") or ""
    return json.dumps({
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    })


@mcp.tool()
async def get_file_metadata(file_id: str) -> str:
    """Get metadata for a specific file (no text — use get_file_text or find_and_get)."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    file = await db.get_file(file_id)
    if not file:
        return json.dumps({"error": "File not found"})
    return json.dumps({
        "id": file["id"],
        "name": file["original_name"],
        "category": file["category"],
        "priority": file.get("priority", ""),
        "summary": file.get("summary", ""),
        "tags": file.get("tags", "[]"),
        "created_at": file["created_at"],
    })


@mcp.tool()
async def list_files(category: str = "", limit: int = 20) -> str:
    """List files in the archive, optionally filtered by category."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    files = await db.list_files(category=category or None, limit=min(limit, 50))
    return json.dumps([
        {
            "id": f["id"],
            "category": f["category"],
            "priority": f.get("priority", ""),
            "summary": (f.get("summary", "") or "")[:80],
            "created_at": f["created_at"],
        }
        for f in files
    ])


@mcp.tool()
async def save_note(content: str, file_id: str = "") -> str:
    """Save a note or context for future reference. Optionally link to a file_id."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    note_id = await db.save_note(content=content, file_id=file_id, source="mcp")
    return json.dumps({"saved": True, "note_id": note_id})


@mcp.tool()
async def get_notes(limit: int = 10) -> str:
    """Read saved notes and context from previous sessions."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    notes = await db.list_notes(limit=limit)
    return json.dumps([
        {"id": n["id"], "content": n["content"], "source": n.get("source", ""), "created_at": n["created_at"]}
        for n in notes
    ])


@mcp.tool()
async def get_file_download(file_id: str) -> str:
    """Get the actual file content as base64 for preview/download.
    Returns: filename, mime_type, size, and base64-encoded file data.
    Use this when you need to see or deliver the actual file, not just its text."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    file = await db.get_file(file_id)
    if not file:
        return json.dumps({"error": "File not found"})

    stored_path = file.get("stored_path", "")
    if not stored_path:
        return json.dumps({"error": "No stored path"})

    p = Path(stored_path)
    if not p.exists():
        return json.dumps({"error": "File not found on disk"})

    data = p.read_bytes()
    return json.dumps({
        "file_id": file["id"],
        "filename": file["original_name"],
        "mime_type": file.get("mime_type", "application/octet-stream"),
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode(),
    })


@mcp.tool()
async def upload_file(filename: str, data_base64: str) -> str:
    """Upload and process a file. Data must be base64-encoded."""
    pipeline = _get("pipeline")
    if not pipeline:
        return json.dumps({"error": "Pipeline not available"})
    try:
        file_data = base64.b64decode(data_base64)
    except Exception:
        return json.dumps({"error": "Invalid base64"})

    result = await pipeline.process(file_data, filename, source="mcp")

    if result.is_duplicate:
        return json.dumps({"status": "duplicate", "existing_id": result.duplicate_of.get("id", "")})

    return json.dumps({
        "status": "ok",
        "file_id": result.file_id,
        "category": result.classification.category if result.classification else "",
        "summary": result.classification.summary if result.classification else "",
    })


@mcp.tool()
async def delete_file(file_id: str) -> str:
    """Delete a file from the archive (removes file, vectors, and metadata)."""
    db = _get("db")
    file = await db.get_file(file_id) if db else None
    if not file:
        return json.dumps({"error": "File not found"})

    vs = _get("vector_store")
    if vs:
        try:
            await vs.delete_document(file_id)
        except Exception:
            pass

    if file.get("stored_path"):
        try:
            p = Path(file["stored_path"])
            if p.exists():
                p.unlink()
        except Exception:
            pass

    await db.delete_file(file_id)
    return json.dumps({"deleted": True})


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("documents://overview")
async def resource_overview() -> str:
    """Archive overview: categories, counts, recent files."""
    return await get_archive_overview()


@mcp.resource("documents://recent")
async def resource_recent() -> str:
    """Last 10 files added to the archive."""
    db = _get("db")
    if not db:
        return json.dumps([])
    files = await db.list_files(limit=10)
    return json.dumps([
        {
            "id": f["id"],
            "name": f["original_name"],
            "category": f["category"],
            "priority": f.get("priority", ""),
            "summary": (f.get("summary", "") or "")[:100],
            "created_at": f["created_at"],
        }
        for f in files
    ])


@mcp.resource("documents://notes")
async def resource_notes() -> str:
    """All saved notes."""
    db = _get("db")
    if not db:
        return json.dumps([])
    notes = await db.list_notes(limit=50)
    return json.dumps([
        {"id": n["id"], "content": n["content"], "created_at": n["created_at"]}
        for n in notes
    ])
