"""MCP Server — expose Smart Storage as tools + resources for AI agents."""

from __future__ import annotations

import base64
import json
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 50_000


def _get(key: str):
    from app.main import get_state
    return get_state(key)


def _validate_id(value: str) -> str:
    """Validate that ID looks like a hex UUID (no path traversal)."""
    clean = value.strip().replace("-", "")
    if not clean or not all(c in "0123456789abcdef" for c in clean):
        raise ValueError(f"Invalid ID format: {value}")
    return value.strip()


# ── Server ──────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Smart Storage",
    instructions=(
        "Personal document archive + smart notes + life tracking.\n"
        "Use find_and_get(query) to search and get full document content.\n"
        "Use get_archive_overview() first to understand what's available.\n"
        "Use create_smart_note() to capture notes with auto-enrichment.\n"
        "Use get_today_summary() for daily health/productivity overview.\n"
        "Interpret all content yourself — be specific about dates, names, amounts."
    ),
)

# ── File Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def get_archive_overview() -> str:
    """Get full overview: categories, file counts, notes count, recent files.
    Call this FIRST to understand what's in the archive."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})

    stats = await db.get_stats()
    recent = await db.list_files(limit=10)
    notes_count = len(await db.list_notes(limit=9999))

    return json.dumps({
        "total_files": stats.get("total_files", 0),
        "total_notes": notes_count,
        "categories": stats.get("categories", {}),
        "total_size_mb": round(stats.get("total_size_bytes", 0) / 1048576, 2),
        "recent_files": [
            {"id": f["id"], "name": f["original_name"], "category": f["category"],
             "summary": (f.get("summary", "") or "")[:100]}
            for f in recent
        ],
    })


@mcp.tool()
async def find_and_get(query: str) -> str:
    """Search archive and return BEST matching document with full text.
    Most efficient tool — combines search + text in 1 call."""
    vs = _get("vector_store")
    db = _get("db")
    if not vs or not db:
        return json.dumps({"error": "Not available"})

    results = await vs.search(query, top_k=5)
    if not results:
        return json.dumps({"found": False, "message": "Nothing found"})

    best = results[0]
    file = await db.get_file(best.file_id)
    if not file:
        return json.dumps({"found": False, "message": "File record not found"})

    text = (file.get("extracted_text", "") or "")[:_MAX_TEXT_CHARS]
    meta = {}
    try:
        meta = json.loads(file.get("metadata_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    return json.dumps({
        "found": True, "score": round(best.score, 3),
        "file_id": file["id"], "name": file["original_name"],
        "category": file["category"], "summary": file.get("summary", ""),
        "document_type": meta.get("document_type", ""),
        "tags": file.get("tags", "[]"), "created_at": file["created_at"],
        "text": text, "text_truncated": len(file.get("extracted_text", "") or "") > _MAX_TEXT_CHARS,
    })


@mcp.tool()
async def search_documents(query: str, top_k: int = 5) -> str:
    """Lightweight semantic search — returns matching chunks with scores.
    Use find_and_get() if you need full document text."""
    vs = _get("vector_store")
    if not vs:
        return json.dumps({"error": "Vector store not available"})
    results = await vs.search(query, top_k=min(top_k, 10))
    return json.dumps([
        {"file_id": r.file_id, "category": r.metadata.get("category", ""),
         "score": round(r.score, 3), "text_preview": r.text[:200]}
        for r in results
    ] if results else [])


@mcp.tool()
async def get_file_text(file_id: str, max_chars: int = 5000) -> str:
    """Get extracted text of a file by ID. Max 50000 chars."""
    try:
        file_id = _validate_id(file_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    max_chars = min(max(max_chars, 100), _MAX_TEXT_CHARS)
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    file = await db.get_file(file_id)
    if not file:
        return json.dumps({"error": "File not found"})
    text = file.get("extracted_text", "") or ""
    return json.dumps({"text": text[:max_chars], "truncated": len(text) > max_chars})


@mcp.tool()
async def get_file_metadata(file_id: str) -> str:
    """Get metadata for a file (no text)."""
    try:
        file_id = _validate_id(file_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    file = await db.get_file(file_id)
    if not file:
        return json.dumps({"error": "File not found"})
    return json.dumps({
        "id": file["id"], "name": file["original_name"],
        "category": file["category"], "summary": file.get("summary", ""),
        "tags": file.get("tags", "[]"), "created_at": file["created_at"],
    })


@mcp.tool()
async def list_files(category: str = "", limit: int = 20) -> str:
    """List files, optionally by category. Max 50."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    files = await db.list_files(category=category or None, limit=min(limit, 50))
    return json.dumps([
        {"id": f["id"], "name": f["original_name"], "category": f["category"],
         "summary": (f.get("summary", "") or "")[:80], "created_at": f["created_at"]}
        for f in files
    ])


@mcp.tool()
async def upload_file(filename: str, data_base64: str) -> str:
    """Upload and process a file. Data must be base64-encoded. Max 50MB."""
    pipeline = _get("pipeline")
    if not pipeline:
        return json.dumps({"error": "Pipeline not available"})
    try:
        file_data = base64.b64decode(data_base64)
    except Exception:
        return json.dumps({"error": "Invalid base64"})

    if len(file_data) > 50 * 1024 * 1024:
        return json.dumps({"error": "File too large (max 50MB)"})

    result = await pipeline.process(file_data, filename, source="mcp")
    if result.is_duplicate:
        return json.dumps({"status": "duplicate", "existing_id": result.duplicate_of.get("id", "")})
    return json.dumps({
        "status": "ok", "file_id": result.file_id,
        "category": result.classification.category if result.classification else "",
        "summary": result.classification.summary if result.classification else "",
    })


@mcp.tool()
async def delete_file(file_id: str) -> str:
    """Delete a file (removes file, vectors, metadata)."""
    try:
        file_id = _validate_id(file_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    lifecycle = _get("lifecycle")
    if not lifecycle:
        return json.dumps({"error": "Not available"})
    deleted = await lifecycle.delete(file_id)
    return json.dumps({"deleted": bool(deleted)})


@mcp.tool()
async def reclassify_file(file_id: str) -> str:
    """Re-classify a file with LLM and generate new summary."""
    try:
        file_id = _validate_id(file_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    lifecycle = _get("lifecycle")
    if not lifecycle:
        return json.dumps({"error": "Not available"})
    result = await lifecycle.reclassify(file_id)
    if not result:
        return json.dumps({"error": "File not found"})
    return json.dumps(result)


# ── Smart Notes Tools ───────────────────────────────────────────────────────


@mcp.tool()
async def create_smart_note(content: str, title: str = "") -> str:
    """Create a smart note with auto-enrichment (categorization, entity extraction,
    mood/sentiment detection, task extraction). Returns note ID."""
    capture = _get("note_capture")
    db = _get("db")
    if capture:
        note_id = await capture.capture(content, source="mcp", title=title)
    elif db:
        note_id = await db.save_note(content=content, source="mcp", title=title)
    else:
        return json.dumps({"error": "Not available"})
    return json.dumps({"created": True, "note_id": note_id})


@mcp.tool()
async def get_notes(category: str = "", limit: int = 20) -> str:
    """Get recent notes, optionally filtered by category."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    notes = await db.list_notes(limit=min(limit, 100), category=category)
    return json.dumps([
        {"id": n["id"], "title": n.get("user_title") or n.get("title", ""),
         "content": (n.get("raw_content") or n.get("content", ""))[:300],
         "category": n.get("category", ""), "mood_score": n.get("mood_score"),
         "source": n.get("source", ""), "status": n.get("status", ""),
         "created_at": n["created_at"]}
        for n in notes
    ])


@mcp.tool()
async def get_note_detail(note_id: int) -> str:
    """Get full note with enrichment, entities, facts, and tasks."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    note = await db.get_note(note_id)
    if not note:
        return json.dumps({"error": "Note not found"})
    enrichment = await db.get_latest_enrichment(note_id)
    entities = await db.get_entities_by_note(note_id)
    facts = await db.get_facts_by_note(note_id)
    tasks = await db.get_tasks_by_note(note_id)

    return json.dumps({
        "id": note["id"],
        "content": note.get("raw_content") or note.get("content", ""),
        "title": note.get("user_title") or (enrichment or {}).get("suggested_title", ""),
        "category": note.get("category", ""),
        "mood_score": note.get("mood_score"),
        "sentiment": note.get("sentiment"),
        "status": note.get("status", ""),
        "enrichment": {
            "summary": (enrichment or {}).get("summary", ""),
            "confidence": (enrichment or {}).get("confidence", 0),
        } if enrichment else None,
        "entities": [{"type": e["entity_type"], "value": e["entity_value"],
                      "role": e.get("role", "")} for e in entities],
        "facts": [{"key": f["key"], "value": f.get("value_num") or f.get("value_text", ""),
                   "unit": f.get("unit", "")} for f in facts],
        "tasks": [{"id": t["id"], "description": t["description"],
                   "priority": t.get("priority", ""), "status": t.get("status", ""),
                   "due_date": t.get("due_date", "")} for t in tasks],
        "created_at": note["created_at"],
    })


@mcp.tool()
async def get_note_tasks(status: str = "open", limit: int = 20) -> str:
    """List tasks extracted from notes. Status: open, done, all."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    where = ""
    params = []
    if status and status != "all":
        where = "WHERE t.status = ?"
        params.append(status)
    cursor = await db.db.execute(
        f"""SELECT t.*, n.user_title, n.category FROM note_tasks t
            JOIN notes n ON t.note_id = n.id {where}
            ORDER BY t.due_date ASC NULLS LAST, t.priority DESC LIMIT ?""",
        params + [min(limit, 50)],
    )
    tasks = [dict(r) for r in await cursor.fetchall()]
    return json.dumps([
        {"id": t["id"], "note_id": t["note_id"], "description": t["description"],
         "priority": t.get("priority", ""), "status": t["status"],
         "due_date": t.get("due_date", ""), "note_title": t.get("user_title", ""),
         "category": t.get("category", "")}
        for t in tasks
    ])


# ── Habits & Reminders Tools ───────────────────────────────────────────────


@mcp.tool()
async def get_habits() -> str:
    """Get all habits with today's status and streaks."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    from app.notes.habits import HabitTracker
    from datetime import datetime
    tracker = HabitTracker(db)
    today = datetime.now().strftime("%Y-%m-%d")
    statuses = await tracker.check_habits_for_date(today)
    return json.dumps([
        {"id": h["id"], "name": h["name"], "frequency": h["frequency"],
         "completed": h["completed"], "streak": h["streak"],
         "value": h.get("value", 0), "metric_key": h.get("metric_key", "")}
        for h in statuses
    ])


@mcp.tool()
async def create_habit(name: str, metric_key: str = "", target_value: float = 1,
                       frequency: str = "daily") -> str:
    """Create a habit to track. metric_key: food_log, calories, sleep_hours,
    exercise_min, weight_kg, mood_score, note_any, category:food, etc."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    from app.notes.habits import HabitTracker
    tracker = HabitTracker(db)
    habit_id = await tracker.create_habit(name, frequency, target_value, metric_key)
    return json.dumps({"created": True, "habit_id": habit_id})


@mcp.tool()
async def get_reminders(include_done: bool = False) -> str:
    """Get note reminders. By default shows only pending/sent."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    reminders = await db.list_note_reminders(include_done=include_done)
    return json.dumps([
        {"id": r["id"], "description": r["description"], "remind_at": r["remind_at"],
         "status": r["status"], "note_id": r["note_id"],
         "recurrence": r.get("recurrence_rule", "")}
        for r in reminders
    ])


# ── Analytics Tools ─────────────────────────────────────────────────────────


@mcp.tool()
async def get_today_summary() -> str:
    """Get today's health/productivity summary: notes, mood, sleep, calories,
    weight, tasks due, habits, anomalies."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    from datetime import datetime
    from app.notes.analytics import NoteAnalytics
    today = datetime.now().strftime("%Y-%m-%d")
    analytics = NoteAnalytics(db)
    data = await analytics.get_daily_summary_data(today)

    # Tasks due
    due_tasks = 0
    try:
        cursor = await db.db.execute(
            "SELECT COUNT(*) FROM note_tasks WHERE status='open' AND due_date <= ?", (today,))
        due_tasks = (await cursor.fetchone())[0] or 0
    except Exception:
        pass

    # Habits
    habits_done = 0
    habits_total = 0
    try:
        from app.notes.habits import HabitTracker
        statuses = await HabitTracker(db).check_habits_for_date(today)
        habits_total = len(statuses)
        habits_done = sum(1 for h in statuses if h.get("completed"))
    except Exception:
        pass

    metrics = data.get("metrics", {})
    return json.dumps({
        "date": today,
        "notes_count": data.get("notes_count", 0),
        "mood": metrics.get("mood_score", {}).get("avg"),
        "sleep_hours": metrics.get("sleep_hours", {}).get("total"),
        "calories": metrics.get("calories", {}).get("total"),
        "weight_kg": metrics.get("weight_kg", {}).get("avg"),
        "due_tasks": due_tasks,
        "habits": f"{habits_done}/{habits_total}",
        "categories": {k: len(v) for k, v in data.get("notes_by_category", {}).items()},
    })


@mcp.tool()
async def get_correlations() -> str:
    """Get metric correlations (e.g. sleep↔mood, calories↔weight)."""
    db = _get("db")
    if not db:
        return json.dumps({"error": "DB not available"})
    from app.notes.analytics import NoteAnalytics
    analytics = NoteAnalytics(db)
    correlations = await analytics.get_all_correlations(days=60)
    return json.dumps([
        {"metric_a": c.get("metric_a", ""), "metric_b": c.get("metric_b", ""),
         "correlation": round(c["correlation"], 3) if c.get("correlation") else None,
         "interpretation": c.get("interpretation", ""),
         "data_points": c.get("data_points", 0)}
        for c in correlations[:5]
        if c.get("correlation") is not None and abs(c["correlation"]) >= 0.2
    ])


# ── Prompts ─────────────────────────────────────────────────────────────────


@mcp.prompt()
async def search_and_analyze(query: str) -> str:
    """Search the archive and analyze the found document."""
    return (
        f"Search the document archive for: {query}\n\n"
        "1. Use find_and_get() to find the best matching document\n"
        "2. Read the full text carefully\n"
        "3. Provide a detailed analysis: key facts, dates, amounts, action items\n"
        "4. Answer in the same language as the query"
    )


@mcp.prompt()
async def daily_brief() -> str:
    """Generate a personal daily briefing."""
    return (
        "Generate a daily briefing for the user:\n\n"
        "1. Call get_today_summary() for metrics\n"
        "2. Call get_note_tasks(status='open') for pending tasks\n"
        "3. Call get_reminders() for due reminders\n"
        "4. Call get_habits() for habit status\n\n"
        "Format as a concise briefing in Russian:\n"
        "- Overall day state\n"
        "- Key metrics (mood, sleep, calories)\n"
        "- Priority tasks\n"
        "- Habit streaks\n"
        "- One recommendation"
    )


@mcp.prompt()
async def capture_and_track(content: str) -> str:
    """Capture a note and set up tracking."""
    return (
        f"The user wants to capture this information:\n{content}\n\n"
        "1. Use create_smart_note() to save it\n"
        "2. If it contains a task with a deadline, mention that auto-extraction will handle it\n"
        "3. If it's about food/health/mood, mention that metrics will be auto-extracted\n"
        "4. Confirm what was captured"
    )


# ── Resources ───────────────────────────────────────────────────────────────


@mcp.resource("documents://overview")
async def resource_overview() -> str:
    """Archive overview."""
    return await get_archive_overview()


@mcp.resource("documents://recent")
async def resource_recent() -> str:
    """Last 10 files."""
    db = _get("db")
    if not db:
        return json.dumps([])
    files = await db.list_files(limit=10)
    return json.dumps([
        {"id": f["id"], "name": f["original_name"], "category": f["category"],
         "summary": (f.get("summary", "") or "")[:100], "created_at": f["created_at"]}
        for f in files
    ])


@mcp.resource("notes://recent")
async def resource_notes() -> str:
    """Recent notes with categories."""
    db = _get("db")
    if not db:
        return json.dumps([])
    notes = await db.list_notes(limit=20)
    return json.dumps([
        {"id": n["id"], "title": n.get("user_title", ""),
         "category": n.get("category", ""), "content": (n.get("content", ""))[:200],
         "created_at": n["created_at"]}
        for n in notes
    ])
