"""Decrypt legacy FAGE-encrypted notes via the cached MDK in data/.session_key.

Mirrors `decrypt_legacy_files.py` (which handled `files.summary` /
`files.extracted_text` / `files.metadata_json`) but applies to:

- `notes.content`           (the transcript itself)
- `notes.title`
- `notes.raw_content`
- `notes.tags`              (sometimes JSON-string ciphertext)
- `notes.structured_json`   (LLM-extracted fields)

Fields are independently base64(FAGE+...) encrypted; we walk every row,
detect FAGE prefix, decrypt, write back. Idempotent — rows where every
relevant field is already plaintext are skipped.

Usage:
    .venv/bin/python scripts/decrypt_legacy_notes.py [--db data/agent.db] \\
        [--via-session-key] [--session-secret <override>]

If --via-session-key is omitted, the script falls through to
master-password unlock from a v2 keyfile (legacy path; rarely needed
anymore since FAG already cached MDK in data/.session_key).
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

# Re-use crypto helpers from the files-recovery script.
sys.path.insert(0, str(Path(__file__).parent))
from decrypt_legacy_files import (  # noqa: E402
    _MAGIC,
    decrypt_text,
    unwrap_session_key,
)


def _looks_encrypted(s: str) -> bool:
    if not s:
        return False
    try:
        return base64.b64decode(s[:8])[:5] == _MAGIC
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/agent.db")
    ap.add_argument(
        "--via-session-key",
        action="store_true",
        help="unwrap MDK from data/.session_key (default if no keyfile path provided)",
    )
    ap.add_argument(
        "--session-key",
        default="data/.session_key",
        help="path to the cached session key file",
    )
    ap.add_argument(
        "--session-secret",
        default=None,
        help="override WEB__SESSION_SECRET (else read from .env)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # --- get MDK ---
    secret = args.session_secret
    if not secret:
        try:
            import dotenv
            secret = dotenv.dotenv_values(".env").get("WEB__SESSION_SECRET", "")
        except Exception:
            secret = ""
    if not secret:
        print("✗ WEB__SESSION_SECRET not found", file=sys.stderr)
        return 2

    try:
        mdk = unwrap_session_key(Path(args.session_key), secret)
    except Exception as exc:
        print(f"✗ unwrap_session_key failed: {exc}", file=sys.stderr)
        return 3
    print(f"✓ unwrapped MDK ({len(mdk)} bytes)")

    # --- decrypt notes ---
    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, title, content, raw_content, tags, structured_json "
        "FROM notes ORDER BY id"
    ).fetchall()
    print(f"scanning {len(rows)} notes")

    fixed = 0
    skipped = 0
    failed = 0
    for r in rows:
        nid = r["id"]
        updates: dict[str, str] = {}
        any_enc = False
        for col in ("title", "content", "raw_content", "tags", "structured_json"):
            val = r[col] or ""
            if not _looks_encrypted(val):
                continue
            any_enc = True
            try:
                plain = decrypt_text(val, mdk)
            except Exception as exc:
                print(f"  ✗ note {nid}.{col}: {exc}")
                failed += 1
                plain = ""  # leave column empty rather than ciphertext mush
            updates[col] = plain
        if not any_enc:
            skipped += 1
            continue
        if args.dry_run:
            content_preview = (updates.get("content") or "")[:60]
            print(f"  [DRY] note {nid}: {content_preview!r}")
            fixed += 1
            continue

        cols_sql = ", ".join(f"{c}=?" for c in updates)
        con.execute(
            f"UPDATE notes SET {cols_sql} WHERE id=?",
            (*updates.values(), nid),
        )
        fixed += 1
        if fixed % 20 == 0:
            con.commit()
            print(f"  ... {fixed} processed")

    if not args.dry_run:
        # Rebuild notes_fts so the new plaintext is searchable.
        try:
            con.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        except Exception as exc:
            print(f"⚠ notes_fts rebuild: {exc}")
        con.commit()
    con.close()

    print(f"\n=== SUMMARY ===\n  fixed: {fixed}\n  skipped (already plain): {skipped}\n  failed: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
