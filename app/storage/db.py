"""SQLite database — async storage for file metadata and processing logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 1

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
    document_date TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(created_at);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256);
-- NOTE: idx_files_docdate is created in post-migration block (after ALTER TABLE adds the column)

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

CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, original_name, category, tags, summary, extracted_text)
    VALUES ('delete', old.rowid, old.original_name, old.category, old.tags, old.summary, old.extracted_text);
END;

CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT,
    run_id TEXT,
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
-- NOTE: idx_log_run is created in post-migration block (after ALTER TABLE adds the column)

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
    category TEXT DEFAULT '',
    subcategory TEXT DEFAULT '',
    structured_json TEXT DEFAULT '{}',
    mood_score INTEGER,
    processed_at TEXT,
    vault_path TEXT DEFAULT '',
    embedded INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS note_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT DEFAULT '',
    date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_note_data_metric_date ON note_data(metric, date);
CREATE INDEX IF NOT EXISTS idx_note_data_note ON note_data(note_id);

CREATE TABLE IF NOT EXISTS note_links (
    source_note_id INTEGER NOT NULL,
    target_note_id INTEGER NOT NULL,
    link_type TEXT DEFAULT 'related',
    strength REAL DEFAULT 0.5,
    PRIMARY KEY (source_note_id, target_note_id)
);

CREATE TABLE IF NOT EXISTS checkin_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    questions_asked TEXT DEFAULT '[]',
    completed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_checkin_date ON checkin_state(date);

CREATE TABLE IF NOT EXISTS note_enrichments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    suggested_title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    category TEXT DEFAULT '',
    subcategory TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0,
    sentiment REAL,
    energy INTEGER,
    mood_score INTEGER,
    raw_llm_json TEXT DEFAULT '{}',
    model TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_enrichments_note ON note_enrichments(note_id);

CREATE TABLE IF NOT EXISTS note_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_value TEXT NOT NULL,
    normalized_value TEXT DEFAULT '',
    role TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entities_note ON note_entities(note_id);
CREATE INDEX IF NOT EXISTS idx_entities_type ON note_entities(entity_type, normalized_value);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    alias TEXT NOT NULL,
    canonical_value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_type, alias)
);

CREATE TABLE IF NOT EXISTS checkin_signal_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    date TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_checkin_stats ON checkin_signal_stats(signal_id, date);

CREATE TABLE IF NOT EXISTS anomaly_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    date TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_anomaly_date ON anomaly_alerts(alert_type, date);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    frequency TEXT NOT NULL DEFAULT 'daily',
    target_value REAL DEFAULT 1,
    metric_key TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS habit_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    completed INTEGER DEFAULT 0,
    value REAL DEFAULT 0,
    auto_detected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(habit_id, date)
);

CREATE INDEX IF NOT EXISTS idx_habit_entries ON habit_entries(habit_id, date);

CREATE TABLE IF NOT EXISTS note_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    fact_type TEXT NOT NULL,
    key TEXT NOT NULL,
    value_num REAL,
    value_text TEXT DEFAULT '',
    unit TEXT DEFAULT '',
    date TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_facts_note ON note_facts(note_id);
CREATE INDEX IF NOT EXISTS idx_facts_type_key ON note_facts(fact_type, key, date);

CREATE TABLE IF NOT EXISTS note_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'open',
    due_date TEXT DEFAULT '',
    done_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_note ON note_tasks(note_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON note_tasks(status);

CREATE TABLE IF NOT EXISTS note_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    relation_type TEXT DEFAULT 'related',
    reason TEXT DEFAULT '',
    score REAL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_note_id, target_note_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON note_relations(source_note_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON note_relations(target_note_id);

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

-- Food Analytics v1
CREATE TABLE IF NOT EXISTS food_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    entry_type TEXT DEFAULT 'meal',
    meal_type TEXT DEFAULT 'unknown',
    food_name TEXT NOT NULL,
    quantity_value REAL,
    quantity_unit TEXT DEFAULT '',
    calories_kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    estimated INTEGER DEFAULT 1,
    confidence REAL DEFAULT 0,
    consumed_at TEXT NOT NULL,
    source_text TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_food_entries_note ON food_entries(note_id);
CREATE INDEX IF NOT EXISTS idx_food_entries_date ON food_entries(consumed_at);

CREATE TABLE IF NOT EXISTS grocery_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    merchant TEXT DEFAULT '',
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    expense_category TEXT DEFAULT 'groceries',
    date TEXT NOT NULL,
    estimated INTEGER DEFAULT 1,
    confidence REAL DEFAULT 0,
    source_text TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_grocery_note ON grocery_expenses(note_id);
CREATE INDEX IF NOT EXISTS idx_grocery_date ON grocery_expenses(date);

-- Reminder System v1
CREATE TABLE IF NOT EXISTS note_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    task_id INTEGER REFERENCES note_tasks(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    source TEXT DEFAULT 'explicit',
    confidence REAL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_note_rem_status ON note_reminders(status, remind_at);
CREATE INDEX IF NOT EXISTS idx_note_rem_note ON note_reminders(note_id);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str | Path, encryption_key: bytes | None = None):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._enc_key = encryption_key

    def _encrypt(self, value: str) -> str:
        """Encrypt a string if encryption is enabled."""
        if not self._enc_key or not value:
            return value
        from app.utils.crypto import encrypt_text
        return encrypt_text(value, self._enc_key)

    def _decrypt(self, value: str) -> str:
        """Decrypt a string if encryption is enabled."""
        if not self._enc_key or not value:
            return value
        from app.utils.crypto import decrypt_text
        return decrypt_text(value, self._enc_key)

    async def connect(self):
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,)
        )
        # Migrate: add new columns to notes if missing
        for col, default in [
            ("category", "''"), ("subcategory", "''"), ("structured_json", "'{}'"),
            ("mood_score", "NULL"), ("processed_at", "NULL"),
            ("vault_path", "''"), ("embedded", "0"),
            ("sentiment", "NULL"), ("energy", "NULL"), ("confidence", "0"),
            # v1.5: Inbox-First refactor
            ("raw_content", "''"), ("status", "'enriched'"),
            ("content_type", "'text'"), ("user_title", "''"),
            ("current_enrichment_id", "NULL"), ("archived_at", "NULL"),
            # v1.6: Manual metadata edit
            ("metadata_manual", "0"),
            # v1.7: Note pinning
            ("is_pinned", "0"),
        ]:
            try:
                await self._db.execute(f"ALTER TABLE notes ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass  # column already exists
        # Create indexes on new columns (after migration)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category)",
            "CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_notes_processed ON notes(processed_at)",
        ]:
            try:
                await self._db.execute(idx_sql)
            except Exception:
                pass
        # v1.5 backfill: copy content → raw_content if empty
        try:
            await self._db.execute(
                "UPDATE notes SET raw_content = content WHERE raw_content = '' AND content != ''"
            )
        except Exception:
            pass
        # v1.5 backfill: legacy status repair for pre-v1.5 rows only.
        # Source of truth for modern notes is status + current_enrichment_id;
        # processed_at is compatibility-only and must not downgrade runtime state.
        try:
            await self._db.execute(
                "UPDATE notes SET status = 'enriched' "
                "WHERE current_enrichment_id IS NOT NULL AND status = 'captured'"
            )
            await self._db.execute(
                "UPDATE notes SET status = 'enriched' "
                "WHERE current_enrichment_id IS NOT NULL AND status = 'processing'"
            )
            await self._db.execute(
                "UPDATE notes SET status = 'captured' "
                "WHERE current_enrichment_id IS NULL AND status = 'processing'"
            )
            await self._db.execute(
                "UPDATE notes SET status = 'enriched' "
                "WHERE current_enrichment_id IS NULL "
                "AND processed_at IS NOT NULL "
                "AND status IS NOT NULL "
                "AND status NOT IN ('processing', 'enriched', 'needs_review', 'failed', 'archived')"
            )
            await self._db.execute(
                "UPDATE notes SET status = 'captured' "
                "WHERE current_enrichment_id IS NULL "
                "AND processed_at IS NULL "
                "AND (status IS NULL OR status = '' OR status = 'enriched')"
            )
        except Exception:
            pass
        # v1.5: add status index
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status)")
        except Exception:
            pass
        # Migrate: add run_id column to processing_log if missing
        for col, default in [("run_id", "NULL")]:
            try:
                await self._db.execute(f"ALTER TABLE processing_log ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_log_run ON processing_log(run_id)")
        except Exception:
            pass
        # Migrate: add document_date column to files if missing
        try:
            await self._db.execute("ALTER TABLE files ADD COLUMN document_date TEXT")
            import logging as _log
            _log.getLogger(__name__).info("Migrated: added document_date column to files")
        except Exception:
            pass  # column already exists
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_files_docdate ON files(document_date)")
        except Exception:
            pass

        # Migrate: add recurrence columns to note_reminders
        for col, default in [
            ("recurrence_rule", "''"),
            ("recurrence_end", "''"),
        ]:
            try:
                await self._db.execute(
                    f"ALTER TABLE note_reminders ADD COLUMN {col} TEXT DEFAULT {default}"
                )
            except Exception:
                pass

        # Create notes FTS5 index (after migration to ensure columns exist)
        await self._setup_notes_fts()
        # If encryption enabled, rebuild FTS5 without sensitive columns
        if self._enc_key:
            await self._rebuild_fts_safe()
        await self._db.commit()

    async def _setup_notes_fts(self):
        """Create notes FTS5 index and triggers if not present."""
        try:
            await self._db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    title, content, tags,
                    content=notes,
                    content_rowid=id
                );

                CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
                    VALUES ('delete', old.id, old.title, old.content, old.tags);
                    INSERT INTO notes_fts(rowid, title, content, tags)
                    VALUES (new.id, new.title, new.content, new.tags);
                END;
            """)
        except Exception:
            pass  # FTS setup is non-fatal

    async def _rebuild_fts_safe(self):
        """Rebuild FTS5 index without sensitive columns (summary, extracted_text)."""
        try:
            await self._db.executescript("""
                DROP TRIGGER IF EXISTS files_ai;
                DROP TRIGGER IF EXISTS files_au;
                DROP TRIGGER IF EXISTS files_ad;
                DROP TABLE IF EXISTS files_fts;

                CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                    original_name, category, tags,
                    content=files, content_rowid=rowid
                );

                CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                    INSERT INTO files_fts(rowid, original_name, category, tags)
                    VALUES (new.rowid, new.original_name, new.category, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                    INSERT INTO files_fts(files_fts, rowid, original_name, category, tags)
                    VALUES ('delete', old.rowid, old.original_name, old.category, old.tags);
                    INSERT INTO files_fts(rowid, original_name, category, tags)
                    VALUES (new.rowid, new.original_name, new.category, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                    INSERT INTO files_fts(files_fts, rowid, original_name, category, tags)
                    VALUES ('delete', old.rowid, old.original_name, old.category, old.tags);
                END;

                INSERT INTO files_fts(files_fts) VALUES('rebuild');
            """)
        except Exception:
            pass  # FTS rebuild is non-fatal

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
        document_date: str | None = None,
    ) -> str:
        await self.db.execute(
            """INSERT INTO files
               (id, original_name, stored_path, sha256, size_bytes, mime_type,
                category, tags, summary, source, extracted_text, metadata_json, priority,
                document_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id, original_name, str(stored_path), sha256, size_bytes, mime_type,
                category, json.dumps(tags or []), self._encrypt(summary),
                source, self._encrypt(extracted_text),
                self._encrypt(json.dumps(metadata or {})), priority,
                document_date,
            ),
        )
        await self.db.commit()
        return id

    _ALLOWED_UPDATE_COLUMNS = frozenset({
        "original_name", "category", "tags", "summary", "extracted_text",
        "metadata_json", "priority", "source", "document_date", "updated_at",
    })

    async def update_file(self, id: str, **fields) -> bool:
        if not fields:
            return False
        # Validate column names against allowlist
        for k in fields:
            if k not in self._ALLOWED_UPDATE_COLUMNS and k != "updated_at":
                raise ValueError(f"Invalid column name: {k}")
        # Serialize lists/dicts
        _sensitive = {"extracted_text", "summary", "metadata_json"}
        for k, v in fields.items():
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v)
        # Encrypt sensitive fields on update
        for k in _sensitive:
            if k in fields and fields[k]:
                fields[k] = self._encrypt(fields[k])
        fields["updated_at"] = datetime.now(tz=None).isoformat()
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [id]
        await self.db.execute(f"UPDATE files SET {set_clause} WHERE id=?", values)
        await self.db.commit()
        return True

    def _decrypt_file_row(self, row: dict) -> dict:
        """Decrypt sensitive columns in a file row."""
        if self._enc_key:
            for col in ("extracted_text", "summary", "metadata_json"):
                if row.get(col):
                    row[col] = self._decrypt(row[col])
        return row

    async def get_file(self, id: str) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM files WHERE id=?", (id,))
        row = await cursor.fetchone()
        return self._decrypt_file_row(dict(row)) if row else None

    async def get_file_by_hash(self, sha256: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM files WHERE sha256=? ORDER BY created_at DESC LIMIT 1", (sha256,)
        )
        row = await cursor.fetchone()
        return self._decrypt_file_row(dict(row)) if row else None

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
        return [self._decrypt_file_row(dict(r)) for r in await cursor.fetchall()]

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
        return [self._decrypt_file_row(dict(r)) for r in await cursor.fetchall()]

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
        file_id: str | None,
        step: str,
        status: str = "started",
        error: str | None = None,
        details: dict | None = None,
        run_id: str | None = None,
    ) -> int:
        """Log a pipeline step.

        Pipeline contract: exactly 8 steps per successful run
        (receive, ingest, parse, classify, route, store, embed, save_meta).
        Completion is indicated by save_meta finishing with status=success.
        A ninth pseudo-step must NOT be added.

        Args:
            file_id: File ID (None for pre-store steps).
            step: Step name.
            run_id: Pipeline run UUID that links all steps of a single run.
        """
        cursor = await self.db.execute(
            """INSERT INTO processing_log (file_id, run_id, step, status, error, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (file_id, run_id, step, status, error, json.dumps(details or {})),
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
        """Mark a pipeline step as finished.

        For the final step (save_meta), status=success indicates pipeline completion.
        """
        await self.db.execute(
            """UPDATE processing_log
               SET status=?, finished_at=datetime('now'), duration_ms=?, error=?
               WHERE id=?""",
            (status, duration_ms, error, log_id),
        )
        await self.db.commit()

    async def backfill_run_file_id(self, run_id: str, file_id: str):
        """After store step, backfill file_id on all earlier steps of this run."""
        await self.db.execute(
            "UPDATE processing_log SET file_id=? WHERE run_id=? AND file_id IS NULL",
            (file_id, run_id),
        )
        await self.db.commit()

    async def delete_run_logs(self, run_id: str):
        """Delete all processing_log entries for a pipeline run.

        Used during compensating cleanup when save_meta fails — prevents
        orphaned audit entries pointing to a file_id that no longer exists.
        """
        await self.db.execute(
            "DELETE FROM processing_log WHERE run_id=?", (run_id,),
        )
        await self.db.commit()

    async def delete_file(self, file_id: str) -> bool:
        """Delete file record and all associated processing logs.

        FTS cleanup is handled automatically by the files_ad AFTER DELETE trigger.
        """
        await self.db.execute("DELETE FROM processing_log WHERE file_id=?", (file_id,))
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

    def _decrypt_note_row(self, row: dict) -> dict:
        """Decrypt sensitive columns in a note row and parse tags."""
        if row.get("content"):
            row["content"] = self._decrypt(row["content"])
        if row.get("raw_content"):
            row["raw_content"] = self._decrypt(row["raw_content"])
        # Parse tags from JSON string to list
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            try:
                row["tags"] = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                row["tags"] = []
        return row

    async def save_note(
        self, content: str, file_id: str = "", source: str = "voice",
        title: str = "", md_path: str = "", tags: str = "[]",
        content_type: str = "text",
    ) -> int:
        encrypted = self._encrypt(content)
        cursor = await self.db.execute(
            """INSERT INTO notes
               (content, raw_content, title, user_title, file_id, md_path, source, tags,
                status, content_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'captured', ?)""",
            (encrypted, encrypted, title, title, file_id, md_path, source, tags, content_type),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_note(self, note_id: int) -> dict | None:
        cursor = await self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,))
        row = await cursor.fetchone()
        return self._decrypt_note_row(dict(row)) if row else None

    async def update_note_processed(
        self, note_id: int, category: str, subcategory: str,
        mood_score: int | None, vault_path: str, structured_json: str,
        title: str = "", tags: str = "[]",
        sentiment: float | None = None, energy: int | None = None,
        confidence: float = 0.0,
    ):
        await self.db.execute(
            """UPDATE notes SET category=?, subcategory=?, mood_score=?,
               vault_path=?, structured_json=?, processed_at=datetime('now'),
               title=?, tags=?, sentiment=?, energy=?, confidence=?
               WHERE id=?""",
            (category, subcategory, mood_score, vault_path, structured_json,
             title, tags, sentiment, energy, confidence, note_id),
        )
        await self.db.commit()

    async def mark_note_embedded(self, note_id: int):
        await self.db.execute("UPDATE notes SET embedded=1 WHERE id=?", (note_id,))
        await self.db.commit()

    async def get_unprocessed_notes(self, limit: int = 50) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM notes WHERE processed_at IS NULL ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def list_notes(self, limit: int = 50, file_id: str = "",
                         category: str = "") -> list[dict]:
        query = "SELECT * FROM notes"
        params: list[Any] = []
        conditions = []
        if file_id:
            conditions.append("file_id=?")
            params.append(file_id)
        if category:
            conditions.append("category=?")
            params.append(category)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY COALESCE(is_pinned, 0) DESC, created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, params)
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def get_daily_notes(self, date: str) -> list[dict]:
        """Get all notes for a specific date (YYYY-MM-DD)."""
        cursor = await self.db.execute(
            "SELECT * FROM notes WHERE created_at LIKE ? ORDER BY created_at ASC",
            (f"{date}%",),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def search_notes(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across notes (FTS5 — works on unencrypted fields only)."""
        cursor = await self.db.execute(
            """SELECT n.* FROM notes n
               JOIN notes_fts fts ON n.id = fts.rowid
               WHERE notes_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def search_notes_keyword(
        self, query: str, category: str = "", status: str = "",
        source: str = "", limit: int = 30,
    ) -> list[dict]:
        """Keyword search across unencrypted note fields (title, tags, category)."""
        q = f"%{query}%"
        sql = """SELECT * FROM notes WHERE (
            user_title LIKE ? OR title LIKE ? OR tags LIKE ?
            OR category LIKE ? OR subcategory LIKE ?
        )"""
        params: list[Any] = [q, q, q, q, q]

        if category:
            sql += " AND category=?"
            params.append(category)
        if status:
            sql += " AND status=?"
            params.append(status)
        else:
            sql += " AND status != 'archived'"
        if source:
            sql += " AND source=?"
            params.append(source)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.db.execute(sql, params)
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def delete_note(self, note_id: int) -> bool:
        """Cascading delete: v1.5 derived + legacy + note."""
        for t in ("note_enrichments", "note_entities", "note_facts", "note_tasks",
                   "food_entries", "grocery_expenses", "note_reminders"):
            await self.db.execute(f"DELETE FROM {t} WHERE note_id=?", (note_id,))
        for t in ("note_relations", "note_links"):
            await self.db.execute(
                f"DELETE FROM {t} WHERE source_note_id=? OR target_note_id=?",
                (note_id, note_id),
            )
        await self.db.execute("DELETE FROM note_data WHERE note_id=?", (note_id,))
        await self.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        await self.db.commit()
        return True

    # ── Note Data (metrics) ──────────────────────────────────────────

    async def save_note_data(
        self, note_id: int, metric: str, value: float,
        unit: str = "", date: str = "",
    ):
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        await self.db.execute(
            "INSERT INTO note_data (note_id, metric, value, unit, date) VALUES (?, ?, ?, ?, ?)",
            (note_id, metric, value, unit, date),
        )
        await self.db.commit()

    async def get_metric_trend(self, metric: str, days: int = 30) -> list[dict]:
        """Get daily aggregated metric values for trend charts."""
        cursor = await self.db.execute(
            """SELECT date, SUM(value) as total, AVG(value) as avg, COUNT(*) as count
               FROM note_data
               WHERE metric=? AND date >= date('now', ?)
               GROUP BY date ORDER BY date""",
            (metric, f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_daily_metrics(self, date: str) -> dict:
        """Get all metrics for a specific date."""
        cursor = await self.db.execute(
            "SELECT metric, SUM(value) as total, AVG(value) as avg "
            "FROM note_data WHERE date=? GROUP BY metric",
            (date,),
        )
        return {r["metric"]: {"total": r["total"], "avg": r["avg"]} for r in await cursor.fetchall()}

    async def search_notes_by_metric(
        self, metric: str, operator: str, value: float, limit: int = 50,
    ) -> list[dict]:
        """Search notes by metric value (e.g. calories > 2000)."""
        if operator not in ("<", ">", "<=", ">=", "="):
            raise ValueError(f"Invalid operator: {operator}")
        cursor = await self.db.execute(
            f"""SELECT n.*, nd.value as metric_value, nd.date as metric_date
                FROM notes n
                JOIN note_data nd ON n.id = nd.note_id
                WHERE nd.metric=? AND nd.value {operator} ?
                ORDER BY nd.date DESC LIMIT ?""",
            (metric, value, limit),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    # ── Note Links ───────────────────────────────────────────────────

    async def save_note_link(
        self, source_id: int, target_id: int,
        link_type: str = "related", strength: float = 0.5,
    ):
        await self.db.execute(
            "INSERT OR REPLACE INTO note_links (source_note_id, target_note_id, link_type, strength) "
            "VALUES (?, ?, ?, ?)",
            (source_id, target_id, link_type, strength),
        )
        await self.db.commit()

    async def get_note_links(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT nl.*, n.title, n.category, n.subcategory
               FROM note_links nl
               JOIN notes n ON (nl.target_note_id = n.id OR nl.source_note_id = n.id)
               WHERE (nl.source_note_id=? OR nl.target_note_id=?) AND n.id != ?
               ORDER BY nl.strength DESC""",
            (note_id, note_id, note_id),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Check-in State ───────────────────────────────────────────────

    async def get_checkin_state(self, date: str) -> dict | None:
        cursor = await self.db.execute(
            "SELECT * FROM checkin_state WHERE date=? ORDER BY id DESC LIMIT 1", (date,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_checkin_state(self, date: str, questions_asked: str = "[]", completed: int = 0):
        existing = await self.get_checkin_state(date)
        if existing:
            await self.db.execute(
                "UPDATE checkin_state SET questions_asked=?, completed=? WHERE id=?",
                (questions_asked, completed, existing["id"]),
            )
        else:
            await self.db.execute(
                "INSERT INTO checkin_state (date, questions_asked, completed) VALUES (?, ?, ?)",
                (date, questions_asked, completed),
            )
        await self.db.commit()

    async def record_checkin_signal(self, signal_id: str, date: str, action: str):
        """Record a checkin signal answer or skip."""
        await self.db.execute(
            "INSERT INTO checkin_signal_stats (signal_id, date, action) VALUES (?, ?, ?)",
            (signal_id, date, action),
        )
        await self.db.commit()

    async def get_signal_skip_rate(self, signal_id: str, lookback_days: int = 30) -> float:
        """Get skip rate for a signal over the last N days. Returns 0.0-1.0."""
        cursor = await self.db.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN action='skipped' THEN 1 ELSE 0 END) as skipped
               FROM checkin_signal_stats
               WHERE signal_id=? AND date >= date('now', ?)""",
            (signal_id, f"-{lookback_days} days"),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return 0.0
        return row[1] / row[0]

    async def get_all_signal_skip_rates(self, lookback_days: int = 30) -> dict[str, float]:
        """Get skip rates for all signals."""
        cursor = await self.db.execute(
            """SELECT signal_id,
                      COUNT(*) as total,
                      SUM(CASE WHEN action='skipped' THEN 1 ELSE 0 END) as skipped
               FROM checkin_signal_stats
               WHERE date >= date('now', ?)
               GROUP BY signal_id""",
            (f"-{lookback_days} days",),
        )
        result = {}
        for row in await cursor.fetchall():
            if row[1]:
                result[row[0]] = row[2] / row[1]
        return result

    async def get_category_distribution(self, days: int = 30) -> dict:
        """Note count by category for the last N days."""
        cursor = await self.db.execute(
            """SELECT category, COUNT(*) as cnt FROM notes
               WHERE category != '' AND created_at >= datetime('now', ?)
               GROUP BY category ORDER BY cnt DESC""",
            (f"-{days} days",),
        )
        return {r["category"]: r["cnt"] for r in await cursor.fetchall()}

    async def get_streak(self, days: int = 60) -> int:
        """Count consecutive days with at least one note, ending today."""
        cursor = await self.db.execute(
            """SELECT DISTINCT date(created_at) as d FROM notes
               WHERE created_at >= datetime('now', ?)
               ORDER BY d DESC""",
            (f"-{days} days",),
        )
        dates = [r[0] for r in await cursor.fetchall()]
        if not dates:
            return 0
        from datetime import datetime, timedelta
        streak = 0
        today = datetime.now().date()
        for i in range(len(dates)):
            expected = (today - timedelta(days=i)).isoformat()
            if dates[i] == expected:
                streak += 1
            else:
                break
        return streak

    async def get_weekly_metrics(self, week_start: str) -> dict:
        """Aggregate metrics for a 7-day period starting from week_start."""
        cursor = await self.db.execute(
            """SELECT date, metric, SUM(value) as total, AVG(value) as avg, COUNT(*) as cnt
               FROM note_data
               WHERE date >= ? AND date < date(?, '+7 days')
               GROUP BY date, metric
               ORDER BY date, metric""",
            (week_start, week_start),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        # Group by date
        by_date: dict[str, dict] = {}
        for r in rows:
            d = r["date"]
            by_date.setdefault(d, {})[r["metric"]] = {"total": r["total"], "avg": r["avg"], "count": r["cnt"]}
        return by_date

    async def get_inbox_notes(self, limit: int = 50) -> list[dict]:
        """Get notes classified as _inbox (low confidence)."""
        cursor = await self.db.execute(
            "SELECT * FROM notes WHERE category='_inbox' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def get_notes_count_by_date(self, days: int = 7) -> dict:
        """Count notes per date for the last N days."""
        cursor = await self.db.execute(
            """SELECT date(created_at) as d, COUNT(*) as cnt FROM notes
               WHERE created_at >= datetime('now', ?)
               GROUP BY d ORDER BY d""",
            (f"-{days} days",),
        )
        return {r[0]: r[1] for r in await cursor.fetchall()}

    # ── Notes v1.5: Status, Enrichments, Entities, Facts, Tasks, Relations ──

    async def set_note_status(self, note_id: int, status: str):
        await self.db.execute("UPDATE notes SET status=? WHERE id=?", (status, note_id))
        await self.db.commit()

    async def bulk_archive_notes(self, note_ids: list[int]) -> int:
        """Archive multiple notes. Returns count affected."""
        if not note_ids:
            return 0
        placeholders = ",".join("?" * len(note_ids))
        cursor = await self.db.execute(
            f"UPDATE notes SET status='archived', archived_at=datetime('now') WHERE id IN ({placeholders})",
            note_ids,
        )
        await self.db.commit()
        return cursor.rowcount

    async def bulk_delete_notes(self, note_ids: list[int]) -> int:
        """Delete multiple notes. Returns count affected."""
        if not note_ids:
            return 0
        placeholders = ",".join("?" * len(note_ids))
        # Cascade deletes handle enrichments, entities, facts, tasks, relations
        cursor = await self.db.execute(
            f"DELETE FROM notes WHERE id IN ({placeholders})", note_ids,
        )
        await self.db.commit()
        return cursor.rowcount

    async def bulk_set_category(self, note_ids: list[int], category: str) -> int:
        """Set category for multiple notes."""
        if not note_ids:
            return 0
        placeholders = ",".join("?" * len(note_ids))
        cursor = await self.db.execute(
            f"UPDATE notes SET category=?, metadata_manual=1 WHERE id IN ({placeholders})",
            [category] + note_ids,
        )
        await self.db.commit()
        return cursor.rowcount

    async def toggle_note_pin(self, note_id: int) -> bool:
        """Toggle pin status. Returns new is_pinned state."""
        cursor = await self.db.execute("SELECT is_pinned FROM notes WHERE id=?", (note_id,))
        row = await cursor.fetchone()
        if not row:
            return False
        new_val = 0 if row[0] else 1
        await self.db.execute("UPDATE notes SET is_pinned=? WHERE id=?", (new_val, note_id))
        await self.db.commit()
        return bool(new_val)

    async def get_notes_by_status(self, status: str, limit: int = 50) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM notes WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        return [self._decrypt_note_row(dict(r)) for r in await cursor.fetchall()]

    async def save_enrichment(
        self, note_id: int, suggested_title: str = "", summary: str = "",
        category: str = "", subcategory: str = "", tags: str = "[]",
        confidence: float = 0, sentiment: float | None = None,
        energy: int | None = None, mood_score: int | None = None,
        raw_llm_json: str = "{}", model: str = "", prompt_version: str = "",
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO note_enrichments
               (note_id, suggested_title, summary, category, subcategory, tags,
                confidence, sentiment, energy, mood_score, raw_llm_json, model, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, suggested_title, summary, category, subcategory, tags,
             confidence, sentiment, energy, mood_score, raw_llm_json, model, prompt_version),
        )
        enrichment_id = cursor.lastrowid
        # Set as current enrichment
        await self.db.execute(
            "UPDATE notes SET current_enrichment_id=? WHERE id=?",
            (enrichment_id, note_id),
        )
        await self.db.commit()
        return enrichment_id

    async def get_latest_enrichment(self, note_id: int) -> dict | None:
        """Get current enrichment for a note (via current_enrichment_id)."""
        cursor = await self.db.execute(
            """SELECT e.* FROM note_enrichments e
               JOIN notes n ON n.current_enrichment_id = e.id
               WHERE n.id=?""",
            (note_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_enrichment_history(self, note_id: int) -> list[dict]:
        """Get all enrichments for a note, newest first."""
        cursor = await self.db.execute(
            "SELECT * FROM note_enrichments WHERE note_id=? ORDER BY processed_at DESC",
            (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def update_note_bridge_fields(
        self, note_id: int, category: str = "", subcategory: str = "",
        tags: str = "[]", vault_path: str = "",
    ):
        """Update compatibility bridge fields on notes from enrichment."""
        await self.db.execute(
            "UPDATE notes SET category=?, subcategory=?, tags=?, vault_path=? WHERE id=?",
            (category, subcategory, tags, vault_path, note_id),
        )
        await self.db.commit()

    async def update_note_metadata(
        self, note_id: int, user_title: str = "",
        category: str = "", subcategory: str = "",
        tags: str = "[]",
    ):
        """Save user-edited metadata and set metadata_manual flag."""
        await self.db.execute(
            "UPDATE notes SET user_title=?, category=?, subcategory=?, tags=?, metadata_manual=1 WHERE id=?",
            (user_title, category, subcategory, tags, note_id),
        )
        await self.db.commit()

    # ── Entities ──

    async def save_entity(
        self, note_id: int, entity_type: str, entity_value: str,
        normalized_value: str = "", role: str = "",
    ):
        # Check alias table for canonical normalization
        if normalized_value:
            cursor = await self.db.execute(
                "SELECT canonical_value FROM entity_aliases WHERE entity_type=? AND alias=?",
                (entity_type, normalized_value),
            )
            alias_row = await cursor.fetchone()
            if alias_row:
                normalized_value = alias_row[0]

        await self.db.execute(
            "INSERT INTO note_entities (note_id, entity_type, entity_value, normalized_value, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, entity_type, entity_value, normalized_value, role),
        )
        await self.db.commit()

    async def get_entities_by_note(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM note_entities WHERE note_id=? ORDER BY entity_type",
            (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def find_duplicate_entities(self) -> list[dict]:
        """Find entity clusters with multiple distinct surface forms."""
        cursor = await self.db.execute(
            """SELECT entity_type, normalized_value,
                      GROUP_CONCAT(DISTINCT entity_value) as variants,
                      COUNT(DISTINCT entity_value) as variant_count,
                      COUNT(*) as total_count
               FROM note_entities
               WHERE normalized_value != ''
               GROUP BY entity_type, normalized_value
               HAVING COUNT(DISTINCT entity_value) > 1
               ORDER BY total_count DESC""",
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_all_entity_clusters(self) -> list[dict]:
        """Get all unique entities grouped by type and normalized value."""
        cursor = await self.db.execute(
            """SELECT entity_type, normalized_value,
                      GROUP_CONCAT(DISTINCT entity_value) as variants,
                      COUNT(*) as count
               FROM note_entities
               WHERE normalized_value != ''
               GROUP BY entity_type, normalized_value
               ORDER BY entity_type, count DESC""",
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def merge_entities(
        self, entity_type: str, canonical: str, aliases: list[str],
    ) -> int:
        """Merge entity aliases into canonical form. Updates entities + saves aliases."""
        count = 0
        for alias in aliases:
            if alias == canonical:
                continue
            # Update existing entities
            cursor = await self.db.execute(
                "UPDATE note_entities SET normalized_value=? WHERE entity_type=? AND normalized_value=?",
                (canonical, entity_type, alias),
            )
            count += cursor.rowcount
            # Save alias for future auto-normalization
            await self.db.execute(
                "INSERT OR REPLACE INTO entity_aliases (entity_type, alias, canonical_value) VALUES (?, ?, ?)",
                (entity_type, alias, canonical),
            )
        await self.db.commit()
        return count

    async def clear_note_derived(self, note_id: int):
        """Delete all derived data for a note (entities, facts, tasks) before re-enrichment."""
        await self.db.execute("DELETE FROM note_entities WHERE note_id=?", (note_id,))
        await self.db.execute("DELETE FROM note_facts WHERE note_id=?", (note_id,))
        await self.db.execute("DELETE FROM note_tasks WHERE note_id=?", (note_id,))
        await self.db.commit()

    # ── Facts ──

    async def save_fact(
        self, note_id: int, fact_type: str, key: str,
        value_num: float | None = None, value_text: str = "",
        unit: str = "", date: str = "", metadata_json: str = "{}",
    ):
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        await self.db.execute(
            """INSERT INTO note_facts
               (note_id, fact_type, key, value_num, value_text, unit, date, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, fact_type, key, value_num, value_text, unit, date, metadata_json),
        )
        await self.db.commit()

    async def get_facts_by_note(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM note_facts WHERE note_id=? ORDER BY fact_type, key",
            (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_facts_trend(self, key: str, days: int = 30) -> list[dict]:
        """Get daily aggregated fact values (replaces get_metric_trend)."""
        cursor = await self.db.execute(
            """SELECT date, SUM(value_num) as total, AVG(value_num) as avg, COUNT(*) as count
               FROM note_facts
               WHERE key=? AND date >= date('now', ?)
               GROUP BY date ORDER BY date""",
            (key, f"-{days} days"),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_daily_facts(self, date: str) -> dict:
        """Get all facts for a specific date (replaces get_daily_metrics)."""
        cursor = await self.db.execute(
            "SELECT key, SUM(value_num) as total, AVG(value_num) as avg "
            "FROM note_facts WHERE date=? AND value_num IS NOT NULL GROUP BY key",
            (date,),
        )
        return {r["key"]: {"total": r["total"], "avg": r["avg"]} for r in await cursor.fetchall()}

    # ── Tasks ──

    async def save_task(
        self, note_id: int, description: str,
        priority: str = "medium", due_date: str = "",
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO note_tasks (note_id, description, priority, due_date) VALUES (?, ?, ?, ?)",
            (note_id, description, priority, due_date),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_tasks_by_note(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM note_tasks WHERE note_id=? ORDER BY priority, created_at",
            (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def update_task_status(self, task_id: int, status: str):
        done_at = datetime.now().isoformat() if status == "done" else None
        await self.db.execute(
            "UPDATE note_tasks SET status=?, done_at=? WHERE id=?",
            (status, done_at, task_id),
        )
        await self.db.commit()

    async def get_open_tasks(self, limit: int = 50) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT t.*, n.user_title, n.category FROM note_tasks t
               JOIN notes n ON t.note_id = n.id
               WHERE t.status='open' ORDER BY
               CASE t.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
               t.created_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Relations v1.5 ──

    async def save_relation(
        self, source_id: int, target_id: int,
        relation_type: str = "related", reason: str = "", score: float = 0.5,
    ):
        await self.db.execute(
            """INSERT OR REPLACE INTO note_relations
               (source_note_id, target_note_id, relation_type, reason, score)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, target_id, relation_type, reason, score),
        )
        await self.db.commit()

    async def get_note_relations_v2(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT nr.*, n.user_title, n.category, n.status, n.vault_path
               FROM note_relations nr
               JOIN notes n ON (nr.target_note_id = n.id OR nr.source_note_id = n.id)
               WHERE (nr.source_note_id=? OR nr.target_note_id=?) AND n.id != ?
               ORDER BY nr.score DESC""",
            (note_id, note_id, note_id),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def clear_note_relations(self, note_id: int):
        """Delete outgoing relations for a note (before re-linking)."""
        await self.db.execute(
            "DELETE FROM note_relations WHERE source_note_id=?", (note_id,)
        )
        await self.db.commit()

    async def get_graph_data(self, center_id: int = 0, limit: int = 100) -> dict:
        """Build graph data: nodes (notes + entities) and edges (relations + entity links).
        Returns {nodes: [...], edges: [...]}."""
        nodes = {}
        edges = []

        # Get recent notes as nodes
        if center_id:
            # Start from center note + its relations
            cursor = await self.db.execute(
                """SELECT DISTINCT n.id, n.user_title, n.category, n.created_at
                   FROM notes n
                   WHERE n.id = ?
                   UNION
                   SELECT DISTINCT n.id, n.user_title, n.category, n.created_at
                   FROM note_relations nr
                   JOIN notes n ON (n.id = nr.target_note_id OR n.id = nr.source_note_id)
                   WHERE nr.source_note_id = ? OR nr.target_note_id = ?
                   LIMIT ?""",
                (center_id, center_id, center_id, limit),
            )
        else:
            cursor = await self.db.execute(
                """SELECT id, user_title, category, created_at
                   FROM notes WHERE status != 'archived'
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )

        note_ids = []
        for r in await cursor.fetchall():
            r = dict(r)
            nid = f"n:{r['id']}"
            nodes[nid] = {
                "id": nid, "label": (r["user_title"] or f"#{r['id']}")[:30],
                "group": r["category"] or "other",
                "type": "note", "note_id": r["id"],
            }
            note_ids.append(r["id"])

        if not note_ids:
            return {"nodes": [], "edges": []}

        # Get relations between these notes
        ph = ",".join("?" * len(note_ids))
        cursor = await self.db.execute(
            f"""SELECT source_note_id, target_note_id, relation_type, score
                FROM note_relations
                WHERE source_note_id IN ({ph}) AND target_note_id IN ({ph})""",
            note_ids + note_ids,
        )
        for r in await cursor.fetchall():
            r = dict(r)
            edges.append({
                "from": f"n:{r['source_note_id']}", "to": f"n:{r['target_note_id']}",
                "label": r["relation_type"], "value": r["score"],
            })

        # Get entities shared across these notes
        cursor = await self.db.execute(
            f"""SELECT entity_type, normalized_value,
                       GROUP_CONCAT(DISTINCT note_id) as note_ids,
                       COUNT(DISTINCT note_id) as note_count
                FROM note_entities
                WHERE note_id IN ({ph}) AND normalized_value != ''
                GROUP BY entity_type, normalized_value
                HAVING COUNT(DISTINCT note_id) >= 2
                LIMIT 50""",
            note_ids,
        )
        for r in await cursor.fetchall():
            r = dict(r)
            eid = f"e:{r['entity_type']}:{r['normalized_value']}"
            type_shapes = {"person_name": "person", "place": "place", "vehicle": "vehicle", "project": "project"}
            nodes[eid] = {
                "id": eid, "label": r["normalized_value"][:20],
                "group": r["entity_type"], "type": "entity",
                "shape": type_shapes.get(r["entity_type"], "dot"),
            }
            for nid_str in r["note_ids"].split(","):
                edges.append({
                    "from": f"n:{nid_str}", "to": eid,
                    "label": r["entity_type"], "dashes": True,
                })

        return {"nodes": list(nodes.values()), "edges": edges}

    # ── Chat History (persistent dialog memory) ───────────────────────

    async def save_chat_message(self, chat_id: int, role: str, content: str, file_id: str = ""):
        await self.db.execute(
            "INSERT INTO chat_history (chat_id, role, content, file_id) VALUES (?, ?, ?, ?)",
            (chat_id, role, self._encrypt(content[:500]), file_id),
        )
        await self.db.commit()

    async def get_chat_history(self, chat_id: int, limit: int = 10) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT role, content, file_id FROM chat_history "
            "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = []
        for r in await cursor.fetchall():
            d = dict(r)
            if d.get("content"):
                d["content"] = self._decrypt(d["content"])
            rows.append(d)
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

    # ── Food Analytics ────────────────────────────────────────────────

    async def save_food_entry(
        self, note_id: int, food_name: str, consumed_at: str,
        entry_type: str = "meal", meal_type: str = "unknown",
        quantity_value: float | None = None, quantity_unit: str = "",
        calories_kcal: float | None = None,
        protein_g: float | None = None, fat_g: float | None = None,
        carbs_g: float | None = None,
        estimated: int = 1, confidence: float = 0, source_text: str = "",
    ):
        await self.db.execute(
            """INSERT INTO food_entries
               (note_id, entry_type, meal_type, food_name, quantity_value, quantity_unit,
                calories_kcal, protein_g, fat_g, carbs_g, estimated, confidence,
                consumed_at, source_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, entry_type, meal_type, food_name, quantity_value, quantity_unit,
             calories_kcal, protein_g, fat_g, carbs_g, estimated, confidence,
             consumed_at, source_text),
        )
        await self.db.commit()

    async def save_grocery_expense(
        self, note_id: int, amount: float, date: str,
        merchant: str = "", currency: str = "USD",
        expense_category: str = "groceries",
        estimated: int = 1, confidence: float = 0, source_text: str = "",
    ):
        await self.db.execute(
            """INSERT INTO grocery_expenses
               (note_id, merchant, amount, currency, expense_category, date,
                estimated, confidence, source_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, merchant, amount, currency, expense_category, date,
             estimated, confidence, source_text),
        )
        await self.db.commit()

    async def clear_food_entries(self, note_id: int):
        await self.db.execute("DELETE FROM food_entries WHERE note_id=?", (note_id,))
        await self.db.commit()

    async def clear_grocery_expenses(self, note_id: int):
        await self.db.execute("DELETE FROM grocery_expenses WHERE note_id=?", (note_id,))
        await self.db.commit()

    async def get_food_entries_by_date(self, date: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM food_entries WHERE consumed_at=? ORDER BY meal_type, id",
            (date,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_food_entries_by_note(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM food_entries WHERE note_id=? ORDER BY id", (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_daily_nutrition(self, date: str) -> dict:
        """Daily nutrition totals from food_entries."""
        cursor = await self.db.execute(
            """SELECT SUM(calories_kcal) as calories, SUM(protein_g) as protein,
                      SUM(fat_g) as fat, SUM(carbs_g) as carbs, COUNT(*) as items
               FROM food_entries WHERE consumed_at=?""",
            (date,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"calories": 0, "protein": 0, "fat": 0, "carbs": 0, "items": 0}
        return {
            "calories": row[0] or 0, "protein": row[1] or 0,
            "fat": row[2] or 0, "carbs": row[3] or 0, "items": row[4] or 0,
        }

    async def get_nutrition_trend(self, days: int = 7) -> list[dict]:
        """Per-day nutrition totals for last N days."""
        cursor = await self.db.execute(
            """SELECT consumed_at as date,
                      SUM(calories_kcal) as calories, SUM(protein_g) as protein,
                      SUM(fat_g) as fat, SUM(carbs_g) as carbs, COUNT(*) as items
               FROM food_entries WHERE consumed_at >= date('now', ?)
               GROUP BY consumed_at ORDER BY consumed_at""",
            (f"-{days} days",),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_monthly_food_spend(self, year_month: str) -> list[dict]:
        """Monthly food spending by category."""
        cursor = await self.db.execute(
            """SELECT expense_category, SUM(amount) as total, currency
               FROM grocery_expenses WHERE date LIKE ?
               GROUP BY expense_category, currency""",
            (f"{year_month}%",),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_grocery_expenses_by_note(self, note_id: int) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM grocery_expenses WHERE note_id=? ORDER BY id", (note_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ── Note Reminders ────────────────────────────────────────────────

    async def create_note_reminder(
        self, note_id: int, description: str, remind_at: str,
        task_id: int | None = None, source: str = "explicit",
        confidence: float = 0, recurrence_rule: str = "",
        recurrence_end: str = "",
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO note_reminders
               (note_id, task_id, description, remind_at, source, confidence,
                recurrence_rule, recurrence_end)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, task_id, description, remind_at, source, confidence,
             recurrence_rule, recurrence_end),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_due_note_reminders(self) -> list[dict]:
        """Get pending reminders whose remind_at <= now."""
        cursor = await self.db.execute(
            """SELECT r.*, n.user_title, n.category
               FROM note_reminders r
               JOIN notes n ON r.note_id = n.id
               WHERE r.status = 'pending' AND datetime(r.remind_at) <= datetime('now')
               ORDER BY r.remind_at""",
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_note_reminder_sent(self, reminder_id: int):
        await self.db.execute(
            "UPDATE note_reminders SET status='sent', sent_at=datetime('now') WHERE id=?",
            (reminder_id,),
        )
        await self.db.commit()

    async def complete_note_reminder(self, reminder_id: int):
        await self.db.execute(
            "UPDATE note_reminders SET status='done', completed_at=datetime('now') WHERE id=?",
            (reminder_id,),
        )
        await self.db.commit()

    async def snooze_note_reminder(self, reminder_id: int, hours: int = 24):
        await self.db.execute(
            f"UPDATE note_reminders SET status='pending', sent_at=NULL, "
            f"remind_at=datetime(remind_at, '+{hours} hours') WHERE id=?",
            (reminder_id,),
        )
        await self.db.commit()

    async def cancel_note_reminder(self, reminder_id: int):
        await self.db.execute(
            "UPDATE note_reminders SET status='cancelled' WHERE id=?",
            (reminder_id,),
        )
        await self.db.commit()

    async def list_note_reminders(self, include_done: bool = False) -> list[dict]:
        where = "WHERE r.status IN ('pending', 'sent')" if not include_done else ""
        cursor = await self.db.execute(
            f"""SELECT r.*, n.user_title, n.category
                FROM note_reminders r
                JOIN notes n ON r.note_id = n.id
                {where} ORDER BY r.remind_at""",
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_note_reminder_by_task(
        self, note_id: int, task_id: int,
    ) -> dict | None:
        """Check if reminder already exists for this note+task (dedup)."""
        cursor = await self.db.execute(
            """SELECT * FROM note_reminders
               WHERE note_id=? AND task_id=? AND status IN ('pending', 'sent')""",
            (note_id, task_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
