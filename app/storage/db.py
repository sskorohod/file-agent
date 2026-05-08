"""SQLite database — async storage for file metadata and processing logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mime_type TEXT,
    category TEXT DEFAULT 'uncategorized',
    tags TEXT DEFAULT '[]',
    summary TEXT,
    source TEXT DEFAULT 'telegram',
    extracted_text TEXT,
    metadata_json TEXT DEFAULT '{}',
    priority TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    original_name, category, tags, summary, extracted_text,
    content=files,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, original_name, category, tags, summary, extracted_text)
    VALUES (new.rowid, new.original_name, new.category, new.tags, new.summary, new.extracted_text);
END;

CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, original_name, category, tags, summary, extracted_text)
    VALUES ('delete', old.rowid, old.original_name, old.category, old.tags, old.summary, old.extracted_text);
    INSERT INTO files_fts(rowid, original_name, category, tags, summary, extracted_text)
    VALUES (new.rowid, new.original_name, new.category, new.tags, new.summary, new.extracted_text);
END;

CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_ms INTEGER,
    error TEXT,
    details_json TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_log_file ON processing_log(file_id);
CREATE INDEX IF NOT EXISTS idx_log_status ON processing_log(status);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_role ON llm_usage(role);

CREATE TABLE IF NOT EXISTS search_cache (
    query_hash TEXT PRIMARY KEY,
    query TEXT,
    response TEXT,
    file_ids TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    hits INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    message TEXT,
    sent INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_sent ON reminders(sent);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_folders (
    file_id TEXT NOT NULL,
    folder_id INTEGER NOT NULL,
    PRIMARY KEY (file_id, folder_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    title TEXT DEFAULT '',
    file_id TEXT,
    md_path TEXT DEFAULT '',
    source TEXT DEFAULT 'voice',
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    file_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_history_chat ON chat_history(chat_id, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'lite',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS insights (
    category TEXT PRIMARY KEY,
    summary_text TEXT NOT NULL DEFAULT '',
    recommendations TEXT NOT NULL DEFAULT '',
    key_issues TEXT NOT NULL DEFAULT '',
    web_research TEXT NOT NULL DEFAULT '',
    document_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dev_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,)
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # ── Files CRUD ──────────────────────────────────────────────────────

    async def insert_file(
        self,
        id: str,
        original_name: str,
        stored_path: str,
        sha256: str,
        size_bytes: int,
        mime_type: str = "",
        category: str = "uncategorized",
        tags: list[str] | None = None,
        summary: str = "",
        source: str = "telegram",
        extracted_text: str = "",
        metadata: dict | None = None,
        priority: str = "",
    ) -> str:
        await self.db.execute(
            """INSERT INTO files
               (id, original_name, stored_path, sha256, size_bytes, mime_type,
                category, tags, summary, source, extracted_text, metadata_json, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id, original_name, str(stored_path), sha256, size_bytes, mime_type,
                category, json.dumps(tags or []), summary, source, extracted_text,
                json.dumps(metadata or {}), priority,
            ),
        )
        await self.db.commit()
        return id

    _ALLOWED_UPDATE_COLUMNS = frozenset({
        "original_name", "category", "tags", "summary", "extracted_text",
        "metadata_json", "priority", "source", "updated_at",
    })

    async def update_file(self, id: str, **fields) -> bool:
        if not fields:
            return False
        # Validate column names against allowlist
        for k in fields:
            if k not in self._ALLOWED_UPDATE_COLUMNS and k != "updated_at":
                raise ValueError(f"Invalid column name: {k}")
        # Serialize lists/dicts
        for k, v in fields.items():
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v)
        fields["updated_at"] = datetime.now(tz=None).isoformat()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [id]
        await self.db.execute(f"UPDATE files SET {set_clause} WHERE id=?", values)
        await self.db.commit()
        return True

    async def get_file(self, id: str) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM files WHERE id=?", (id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_file_by_hash(self, sha256: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE sha256=? ORDER BY created_at DESC LIMIT 1", (sha256,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_files(
        self,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        query = "SELECT * FROM files"
        params: list[Any] = []
        if category:
            query += " WHERE category=?"
            params.append(category)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = await self.db.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def count_files(self, category: str | None = None) -> int:
        if category:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM files WHERE category=?", (category,)
            )
        else:
            cursor = await self.db.execute("SELECT COUNT(*) FROM files")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def search_files(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across file metadata."""
        cursor = await self.db.execute(
            """SELECT f.* FROM files f
               JOIN files_fts fts ON f.rowid = fts.rowid
               WHERE files_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_stats(self) -> dict:
        """Get aggregate stats."""
        total = await self.count_files()
        cursor = await self.db.execute(
            "SELECT category, COUNT(*) as cnt FROM files GROUP BY category ORDER BY cnt DESC"
        )
        categories = {r["category"]: r["cnt"] for r in await cursor.fetchall()}
        cursor = await self.db.execute("SELECT SUM(size_bytes) FROM files")
        row = await cursor.fetchone()
        total_size = row[0] or 0
        return {"total_files": total, "categories": categories, "total_size_bytes": total_size}

    # ── Processing Log ──────────────────────────────────────────────────

    async def log_step(
        self,
        file_id: str,
        step: str,
        status: str = "started",
        error: str | None = None,
        details: dict | None = None,
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO processing_log (file_id, step, status, error, details_json)
               VALUES (?, ?, ?, ?, ?)""",
            (file_id, step, status, error, json.dumps(details or {})),
        )
        await self.db.commit()
        return cursor.lastrowid or 0

    async def finish_step(
        self,
        log_id: int,
        status: str = "success",
        error: str | None = None,
        duration_ms: int | None = None,
    ):
        await self.db.execute(
            """UPDATE processing_log
               SET status=?, finished_at=datetime('now'), duration_ms=?, error=?
               WHERE id=?""",
            (status, duration_ms, error, log_id),
        )
        await self.db.commit()

    async def delete_file(self, file_id: str) -> bool:
        """Delete file record and all associated processing logs."""
        await self.db.execute("DELETE FROM processing_log WHERE file_id=?", (file_id,))
        # Delete from FTS
        cursor = await self.db.execute("SELECT rowid FROM files WHERE id=?", (file_id,))
        row = await cursor.fetchone()
        if row:
            await self.db.execute(
                "INSERT INTO files_fts(files_fts, rowid, original_name, category, tags, summary, extracted_text) "
                "SELECT 'delete', rowid, original_name, category, tags, summary, extracted_text FROM files WHERE id=?",
                (file_id,),
            )
        await self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
        await self.db.commit()
        return True

    async def list_file_paths(self) -> list[dict]:
        """Return lightweight list of all files with id and stored_path."""
        cursor = await self.db.execute("SELECT id, stored_path FROM files")
        return [dict(r) for r in await cursor.fetchall()]

    async def get_file_log(self, file_id: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM processing_log WHERE file_id=? ORDER BY id", (file_id,)
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_recent_logs(self, limit: int = 50, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM processing_log"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]

    # ── LLM Usage Tracking ────────────────────────────────────────────

    async def log_llm_usage(
        self, role: str, model: str, input_tokens: int = 0,
        output_tokens: int = 0, cost_usd: float = 0, latency_ms: int = 0,
    ):
        await self.db.execute(
            "INSERT INTO llm_usage (role, model, input_tokens, output_tokens, cost_usd, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (role, model, input_tokens, output_tokens, cost_usd, latency_ms),
        )
        await self.db.commit()

    async def get_llm_stats(self, since: str | None = None) -> dict:
        """Get aggregated LLM usage stats. Optionally filter by date."""
        where = "WHERE created_at >= ?" if since else ""
        params = [since] if since else []
        cursor = await self.db.execute(f"""
            SELECT
                COUNT(*) as total_calls,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd
            FROM llm_usage {where}
        """, params)
        row = await cursor.fetchone()
        result = dict(row) if row else {}

        # Per-role breakdown (grouped by role, show most-used model)
        cursor2 = await self.db.execute(f"""
            SELECT role,
                (SELECT model FROM llm_usage u2 WHERE u2.role = llm_usage.role
                 GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1) as model,
                COUNT(*) as calls,
                COALESCE(SUM(input_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(cost_usd), 0) as cost_usd
            FROM llm_usage {where}
            GROUP BY role ORDER BY calls DESC
        """, params)
        result["by_role"] = [dict(r) for r in await cursor2.fetchall()]
        return result

    # ── API Keys ──────────────────────────────────────────────────────

    async def create_api_key(self, name: str, mode: str = "lite") -> str:
        import secrets
        key = f"fag_{secrets.token_urlsafe(32)}"
        await self.db.execute(
            "INSERT INTO api_keys (key, name, mode) VALUES (?, ?, ?)", (key, name, mode),
        )
        await self.db.commit()
        return key

    async def validate_api_key(self, key: str) -> str | None:
        """Validate key and return its mode ('lite' or 'full'), or None if invalid."""
        cursor = await self.db.execute("SELECT key, mode FROM api_keys WHERE key=?", (key,))
        row = await cursor.fetchone()
        if row:
            await self.db.execute(
                "UPDATE api_keys SET last_used_at=datetime('now') WHERE key=?", (key,),
            )
            await self.db.commit()
            return row["mode"] or "lite"
        return None

    async def list_api_keys(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT key, name, created_at, last_used_at FROM api_keys ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_api_key(self, key: str) -> bool:
        await self.db.execute("DELETE FROM api_keys WHERE key=?", (key,))
        await self.db.commit()
        return True

    # ── Notes ─────────────────────────────────────────────────────────

    async def save_note(
        self, content: str, file_id: str = "", source: str = "voice",
        title: str = "", md_path: str = "", tags: str = "[]",
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO notes (content, title, file_id, md_path, source, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (content, title, file_id, md_path, source, tags),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def list_notes(self, limit: int = 50, file_id: str = "") -> list[dict]:
        if file_id:
            cursor = await self.db.execute(
                "SELECT * FROM notes WHERE file_id=? ORDER BY created_at DESC LIMIT ?",
                (file_id, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Chat History (persistent dialog memory) ───────────────────────

    async def save_chat_message(self, chat_id: int, role: str, content: str, file_id: str = ""):
        await self.db.execute(
            "INSERT INTO chat_history (chat_id, role, content, file_id) VALUES (?, ?, ?, ?)",
            (chat_id, role, content[:500], file_id),
        )
        await self.db.commit()

    async def get_chat_history(self, chat_id: int, limit: int = 10) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT role, content, file_id FROM chat_history "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        rows.reverse()  # oldest first
        return rows

    # ── Dashboard Analytics ──────────────────────────────────────────

    async def get_total_queries(self, since: str | None = None) -> int:
        """Count total search queries (search_cache + chat_history user messages)."""
        if since:
            cursor = await self.db.execute(
                "SELECT (SELECT COUNT(*) FROM search_cache WHERE created_at >= ?) + "
                "(SELECT COUNT(*) FROM chat_history WHERE role='user' AND created_at >= ?) as total",
                (since, since),
            )
        else:
            cursor = await self.db.execute(
                "SELECT (SELECT COUNT(*) FROM search_cache) + "
                "(SELECT COUNT(*) FROM chat_history WHERE role='user') as total"
            )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_query_history(self, limit: int = 15) -> list[dict]:
        """Recent search queries from cache and chat history."""
        cursor = await self.db.execute(
            """SELECT query as text, 'web' as source, created_at, hits as cache_hits
               FROM search_cache
               UNION ALL
               SELECT content as text, 'telegram' as source, created_at, 0 as cache_hits
               FROM chat_history WHERE role='user'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_pipeline_health(self, limit: int = 10) -> list[dict]:
        """Recent pipeline runs with status and timing."""
        cursor = await self.db.execute(
            """SELECT pl.file_id, f.original_name, pl.step, pl.status,
                      pl.duration_ms, pl.error, pl.started_at
               FROM processing_log pl
               LEFT JOIN files f ON pl.file_id = f.id
               WHERE pl.step = 'save_meta' OR pl.status = 'error'
               ORDER BY pl.id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_source_distribution(self) -> dict:
        """File count by source (telegram, web, api, mcp)."""
        cursor = await self.db.execute(
            "SELECT source, COUNT(*) as cnt FROM files GROUP BY source ORDER BY cnt DESC"
        )
        return {r["source"]: r["cnt"] for r in await cursor.fetchall()}

    async def get_error_count(self, since: str | None = None) -> int:
        """Count pipeline errors."""
        where = "WHERE status='error'"
        params = ()
        if since:
            where += " AND started_at >= ?"
            params = (since,)
        cursor = await self.db.execute(
            f"SELECT COUNT(*) FROM processing_log {where}", params
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_processed_count(self) -> int:
        """Count successfully processed files."""
        cursor = await self.db.execute(
            "SELECT COUNT(DISTINCT file_id) FROM processing_log WHERE step='save_meta' AND status='completed'"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Reminders ─────────────────────────────────────────────────────

    async def create_reminder(self, file_id: str, remind_at: str, message: str = ""):
        await self.db.execute(
            "INSERT INTO reminders (file_id, remind_at, message) VALUES (?, ?, ?)",
            (file_id, remind_at, message),
        )
        await self.db.commit()

    async def get_due_reminders(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT r.*, f.original_name, f.category FROM reminders r "
            "LEFT JOIN files f ON r.file_id = f.id "
            "WHERE r.sent = 0 AND r.remind_at <= datetime('now') ORDER BY r.remind_at"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_reminder_sent(self, reminder_id: int):
        await self.db.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))
        await self.db.commit()

    async def list_reminders(self, include_sent: bool = False) -> list[dict]:
        where = "" if include_sent else "WHERE r.sent = 0"
        cursor = await self.db.execute(
            f"SELECT r.*, f.original_name, f.category, f.summary, f.metadata_json FROM reminders r "
            f"LEFT JOIN files f ON r.file_id = f.id {where} ORDER BY r.remind_at"
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Secrets (encrypted) ─────────────────────────────────────────

    async def set_secret(self, name: str, encrypted_value: str):
        await self.db.execute(
            "INSERT INTO secrets (name, encrypted_value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET encrypted_value=?, updated_at=datetime('now')",
            (name, encrypted_value, encrypted_value),
        )
        await self.db.commit()

    async def get_secret(self, name: str) -> str | None:
        cursor = await self.db.execute("SELECT encrypted_value FROM secrets WHERE name=?", (name,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def list_secret_names(self) -> list[str]:
        cursor = await self.db.execute("SELECT name FROM secrets ORDER BY name")
        return [r[0] for r in await cursor.fetchall()]

    async def delete_secret(self, name: str):
        await self.db.execute("DELETE FROM secrets WHERE name=?", (name,))
        await self.db.commit()

    # ── Insights ─────────────────────────────────────────────────────

    async def upsert_insight(self, category: str, summary_text: str, recommendations: str,
                              key_issues: str, web_research: str, document_count: int):
        await self.db.execute(
            "INSERT INTO insights (category, summary_text, recommendations, key_issues, web_research, document_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(category) DO UPDATE SET summary_text=?, recommendations=?, key_issues=?, web_research=?, document_count=?, updated_at=datetime('now')",
            (category, summary_text, recommendations, key_issues, web_research, document_count,
             summary_text, recommendations, key_issues, web_research, document_count),
        )
        await self.db.commit()

    async def get_insight(self, category: str) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM insights WHERE category=?", (category,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_all_insights(self) -> list[dict]:
        cursor = await self.db.execute("SELECT * FROM insights ORDER BY category")
        return [dict(r) for r in await cursor.fetchall()]

    # ── Folders ───────────────────────────────────────────────────────

    async def create_folder(self, name: str, description: str = "") -> int:
        cursor = await self.db.execute(
            "INSERT INTO folders (name, description) VALUES (?, ?)", (name, description),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def list_folders(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT f.*, COUNT(ff.file_id) as file_count "
            "FROM folders f LEFT JOIN file_folders ff ON f.id = ff.folder_id "
            "GROUP BY f.id ORDER BY f.name"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def add_file_to_folder(self, file_id: str, folder_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO file_folders (file_id, folder_id) VALUES (?, ?)",
            (file_id, folder_id),
        )
        await self.db.commit()

    async def remove_file_from_folder(self, file_id: str, folder_id: int):
        await self.db.execute(
            "DELETE FROM file_folders WHERE file_id=? AND folder_id=?",
            (file_id, folder_id),
        )
        await self.db.commit()

    async def get_file_folders(self, file_id: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT f.* FROM folders f JOIN file_folders ff ON f.id = ff.folder_id "
            "WHERE ff.file_id=?", (file_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_files_in_folder(self, folder_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT fi.* FROM files fi JOIN file_folders ff ON fi.id = ff.file_id "
            "WHERE ff.folder_id=? ORDER BY fi.created_at DESC", (folder_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_folder(self, folder_id: int):
        await self.db.execute("DELETE FROM file_folders WHERE folder_id=?", (folder_id,))
        await self.db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        await self.db.commit()

    # ── Dev Projects (Phase 5: dev memory dataset-per-project) ────────

    async def create_dev_project(
        self, name: str, repo_path: str = "", description: str = "",
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO dev_projects (name, repo_path, description) VALUES (?, ?, ?)",
            (name, repo_path, description),
        )
        await self.db.commit()
        return cursor.lastrowid or 0

    async def get_dev_project(self, project_id: int) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM dev_projects WHERE id=?", (project_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_dev_project_by_name(self, name: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM dev_projects WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_dev_projects(self) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM dev_projects ORDER BY id"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_dev_project(self, project_id: int) -> bool:
        await self.db.execute("DELETE FROM dev_projects WHERE id=?", (project_id,))
        await self.db.commit()
        return True
