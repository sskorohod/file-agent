#!/usr/bin/env python3
"""Migrate existing data to encrypted format.

Usage:
    ENCRYPTION_KEY=<64-char-hex> python scripts/migrate_encryption.py [--files] [--database] [--qdrant]

Options:
    --files      Encrypt files on disk (AES-256-GCM with FAGE header)
    --database   Encrypt sensitive DB columns (extracted_text, summary, metadata_json, etc.)
    --qdrant     Remove text from Qdrant payload
    --verify     Verify encrypted files after migration (slower but safer)
    --all        Enable all migrations
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.crypto import (
    decrypt_bytes,
    encrypt_bytes,
    encrypt_text,
    is_encrypted,
    parse_encryption_key,
)


def migrate_files(db_path: str, key: bytes, verify: bool = False):
    """Encrypt all unencrypted files on disk."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, stored_path, sha256 FROM files").fetchall()
    total = len(rows)
    encrypted = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        path = Path(row["stored_path"])
        fid = row["id"][:12]

        if not path.exists():
            print(f"  [{i}/{total}] SKIP (missing): {fid}...")
            skipped += 1
            continue

        data = path.read_bytes()
        if is_encrypted(data):
            print(f"  [{i}/{total}] SKIP (already encrypted): {fid}...")
            skipped += 1
            continue

        try:
            # Encrypt with atomic rename
            enc_data = encrypt_bytes(data, key)
            tmp = path.with_suffix(path.suffix + ".enc")
            tmp.write_bytes(enc_data)
            tmp.rename(path)
            encrypted += 1

            # Verify if requested
            if verify:
                dec = decrypt_bytes(path.read_bytes(), key)
                actual_hash = hashlib.sha256(dec).hexdigest()
                expected = row["sha256"]
                if actual_hash != expected:
                    print(f"  [{i}/{total}] VERIFY FAILED: {fid} (hash mismatch)")
                    errors += 1
                    continue

            print(f"  [{i}/{total}] OK: {fid}... ({len(data)} → {len(enc_data)} bytes)")
        except Exception as e:
            print(f"  [{i}/{total}] ERROR: {fid}... — {e}")
            errors += 1

    conn.close()
    print(f"\nFiles: {encrypted} encrypted, {skipped} skipped, {errors} errors")
    return errors == 0


def migrate_database(db_path: str, key: bytes):
    """Encrypt sensitive columns in SQLite database."""
    import shutil

    # Backup first
    backup = db_path + ".plain.bak"
    if not Path(backup).exists():
        shutil.copy2(db_path, backup)
        print(f"  Backup created: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. files: extracted_text, summary, metadata_json
    rows = conn.execute("SELECT id, extracted_text, summary, metadata_json FROM files").fetchall()
    updated = 0
    for row in rows:
        fid = row["id"]
        updates = {}
        for col in ("extracted_text", "summary", "metadata_json"):
            val = row[col]
            if val and not val.startswith("RkFH"):  # base64 of "FAG" magic — already encrypted
                updates[col] = encrypt_text(val, key)
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE files SET {set_clause} WHERE id=?",
                (*updates.values(), fid),
            )
            updated += 1
    print(f"  files: {updated}/{len(rows)} rows encrypted")

    # 2. chat_history: content
    rows = conn.execute("SELECT id, content FROM chat_history").fetchall()
    updated = 0
    for row in rows:
        val = row["content"]
        if val and not val.startswith("RkFH"):
            enc = encrypt_text(val, key)
            conn.execute("UPDATE chat_history SET content=? WHERE id=?", (enc, row["id"]))
            updated += 1
    print(f"  chat_history: {updated}/{len(rows)} rows encrypted")

    # 3. notes: content
    rows = conn.execute("SELECT id, content FROM notes").fetchall()
    updated = 0
    for row in rows:
        val = row["content"]
        if val and not val.startswith("RkFH"):
            enc = encrypt_text(val, key)
            conn.execute("UPDATE notes SET content=? WHERE id=?", (enc, row["id"]))
            updated += 1
    print(f"  notes: {updated}/{len(rows)} rows encrypted")

    # 4. search_cache: response
    rows = conn.execute("SELECT query_hash, response FROM search_cache").fetchall()
    updated = 0
    for row in rows:
        val = row["response"]
        if val and not val.startswith("RkFH"):
            enc = encrypt_text(val, key)
            conn.execute(
                "UPDATE search_cache SET response=? WHERE query_hash=?",
                (enc, row["query_hash"]),
            )
            updated += 1
    print(f"  search_cache: {updated}/{len(rows)} rows encrypted")

    conn.commit()
    conn.close()
    print(f"\nDatabase migration complete. Backup at: {backup}")


def migrate_qdrant(qdrant_host: str, qdrant_port: int, collection: str, api_key: str = ""):
    """Remove text from Qdrant payload."""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, MatchValue, Filter

        client = QdrantClient(
            url=f"http://{qdrant_host}:{qdrant_port}",
            api_key=api_key or None,
            prefer_grpc=False,
        )

        # Get all point IDs
        offset = None
        total = 0
        while True:
            result = client.scroll(
                collection_name=collection,
                limit=100,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            points, offset = result
            if not points:
                break
            ids = [p.id for p in points]
            client.delete_payload(
                collection_name=collection,
                keys=["text"],
                points=ids,
            )
            total += len(ids)
            print(f"  Stripped text from {total} points...")
            if offset is None:
                break

        print(f"\nQdrant: removed text from {total} points in '{collection}'")
    except Exception as e:
        print(f"\nQdrant migration failed: {e}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate data to encrypted format")
    parser.add_argument("--files", action="store_true", help="Encrypt files on disk")
    parser.add_argument("--database", action="store_true", help="Encrypt DB columns")
    parser.add_argument("--qdrant", action="store_true", help="Strip text from Qdrant")
    parser.add_argument("--verify", action="store_true", help="Verify after file encryption")
    parser.add_argument("--all", action="store_true", help="Enable all migrations")
    parser.add_argument("--db-path", default="data/agent.db", help="SQLite database path")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--collection", default="file_agent_v2")
    args = parser.parse_args()

    key_hex = os.environ.get("ENCRYPTION_KEY", "")
    if not key_hex:
        print("ERROR: Set ENCRYPTION_KEY environment variable (64-char hex)")
        sys.exit(1)

    key = parse_encryption_key(key_hex)
    print(f"Encryption key loaded ({len(key)} bytes)")

    if args.all:
        args.files = args.database = args.qdrant = True

    if not (args.files or args.database or args.qdrant):
        print("Nothing to do. Use --files, --database, --qdrant, or --all")
        sys.exit(0)

    if args.files:
        print("\n=== Migrating files ===")
        migrate_files(args.db_path, key, verify=args.verify)

    if args.database:
        print("\n=== Migrating database ===")
        migrate_database(args.db_path, key)

    if args.qdrant:
        print("\n=== Migrating Qdrant ===")
        api_key = os.environ.get("QDRANT__API_KEY", "")
        migrate_qdrant(args.qdrant_host, args.qdrant_port, args.collection, api_key)

    print("\n✅ Migration complete!")


if __name__ == "__main__":
    main()
