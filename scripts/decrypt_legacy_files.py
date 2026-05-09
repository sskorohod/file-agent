"""One-shot recovery: decrypt files left over from feature/encryption-at-rest.

Some files in agent.db carry encrypted=1 with FAGE-magic ciphertext on disk
and base64(FAGE+...) in summary/extracted_text/metadata_json. The current
branch has no decrypt path. This script restores them to plain text using
the v2 keyfile + the user's primary key (and optional 2FA keyfile data).

After a successful run:
  - File on disk is plain (overwritten in place)
  - summary, extracted_text, metadata_json are plain JSON / utf-8
  - encrypted=0 in the row
  - Vector store entries are NOT touched (they were embedded from plain
    text at write time, so they are already searchable; only the
    user-facing summary was scrambled)

Run from the project root:

  python scripts/decrypt_legacy_files.py --db data/agent.db
  # password is read from stdin (echo off)

Idempotent: a second run finds nothing to do.
Dry run: add --dry-run to see what would be decrypted without writing.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

# ── crypto primitives lifted from feature/encryption-at-rest ────────────────

_MAGIC = b"FAGE\x01"
_MAGIC_LEN = len(_MAGIC)
_NONCE_LEN = 12
_KDF_SALT_LEN = 32
_VERIFY_PLAINTEXT = b"FILEAGENT_KEY_VERIFY_v2"
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_KB = 65536
_ARGON2_PARALLELISM = 1


def derive_key(password: str, salt: bytes) -> bytes:
    from argon2.low_level import Type, hash_secret_raw

    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_KB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=32,
        type=Type.ID,
    )


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob[:_MAGIC_LEN] == _MAGIC:
        blob = blob[_MAGIC_LEN:]
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


def decrypt_text(b64: str, key: bytes) -> str:
    """Base64 → AES-GCM → utf-8 string. Returns input as-is if not encrypted."""
    if not b64:
        return b64
    try:
        ct = base64.b64decode(b64)
    except Exception:
        return b64
    if ct[:_MAGIC_LEN] != _MAGIC:
        return b64
    return decrypt_bytes(ct, key).decode("utf-8")


def unwrap_session_key(session_key_path: Path, session_secret: str) -> bytes:
    """Mirror app/web/routes.py:_derive_session_fernet + load_session_key.

    The running FAG process saves the MDK into data/.session_key wrapped
    with a Fernet key derived from WEB__SESSION_SECRET via PBKDF2-HMAC-SHA256.
    Re-deriving the same Fernet here unlocks the same MDK without the
    user re-entering the master password.
    """
    import base64

    from cryptography.fernet import Fernet, InvalidToken

    if not session_secret:
        raise ValueError(
            "session_secret is empty — pass --session-secret or set WEB__SESSION_SECRET"
        )
    if not session_key_path.exists():
        raise ValueError(f"{session_key_path} does not exist")

    salt = b"session-key-encrypt-v2"
    iterations = 480_000
    dk = hashlib.pbkdf2_hmac("sha256", session_secret.encode(), salt, iterations)
    fernet = Fernet(base64.urlsafe_b64encode(dk))
    try:
        return fernet.decrypt(session_key_path.read_bytes())
    except InvalidToken as exc:
        raise ValueError(
            "Could not unwrap session_key — WEB__SESSION_SECRET probably wrong "
            "(or session_key was created with a different secret)"
        ) from exc


def unlock_keyfile_v2(
    keyfile_path: Path, primary_key: str, key_file_data: bytes | None
) -> bytes:
    """Read v2 keyfile, derive wrapping key, unwrap MDK, verify."""
    data = keyfile_path.read_bytes()
    if not data or data[0] != 0x10:
        raise ValueError(f"{keyfile_path} is not a v2 keyfile (header byte != 0x10)")

    pk_salt = data[1:33]
    mdk_len = int.from_bytes(data[33:35], "big")
    encrypted_mdk = data[35 : 35 + mdk_len]
    verify_token = data[35 + mdk_len :]

    combined = primary_key
    if key_file_data is not None:
        combined = primary_key + hashlib.sha256(key_file_data).hexdigest()

    wrapping_key = derive_key(combined, pk_salt)
    try:
        mdk = decrypt_bytes(encrypted_mdk, wrapping_key)
    except Exception as exc:
        raise ValueError(f"Wrong primary key (mdk unwrap failed): {exc}") from None

    try:
        plaintext = decrypt_bytes(verify_token, mdk)
        if not secrets.compare_digest(plaintext, _VERIFY_PLAINTEXT):
            raise ValueError("verify token mismatch")
    except Exception as exc:
        raise ValueError(f"Unlocked MDK fails verification: {exc}") from None

    return mdk


# ── recovery flow ──────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Decrypt legacy FAGE-encrypted files.")
    parser.add_argument("--db", default="data/agent.db", help="path to agent.db")
    parser.add_argument(
        "--keyfile",
        default=None,
        help=(
            "path to encryption.key (v2 keyfile from feature/encryption-at-rest); "
            "defaults to encryption.key next to the --db file"
        ),
    )
    parser.add_argument(
        "--key-file-data",
        default=os.environ.get("KEY_FILE", ""),
        help="path to 2FA key file (defaults to $KEY_FILE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change, don't write",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="primary key (UNSAFE — leaks in process list). Prefer stdin prompt.",
    )
    parser.add_argument(
        "--via-session-key",
        action="store_true",
        help=(
            "Skip the master-password prompt and unwrap the MDK from a cached "
            "data/.session_key (the same way the running uvicorn does it on "
            "startup). Requires WEB__SESSION_SECRET to match the live config."
        ),
    )
    parser.add_argument(
        "--session-key-path",
        default=None,
        help="path to .session_key (defaults to .session_key next to --db)",
    )
    parser.add_argument(
        "--session-secret",
        default=os.environ.get("WEB__SESSION_SECRET", ""),
        help=(
            "WEB__SESSION_SECRET used to derive the Fernet that wraps the MDK. "
            "Defaults to $WEB__SESSION_SECRET. Loaded from .env if not in env."
        ),
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}", file=sys.stderr)
        return 2

    # ── two paths to obtain the MDK ──────────────────────────────────
    if args.via_session_key:
        sk_path = (
            Path(args.session_key_path).expanduser().resolve()
            if args.session_key_path
            else db_path.parent / ".session_key"
        )
        secret = args.session_secret
        if not secret:
            # Fallback: try to read .env next to the DB
            env_path = db_path.parent.parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("WEB__SESSION_SECRET="):
                        secret = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        if not secret:
            print(
                "ERROR: WEB__SESSION_SECRET not provided and not found in .env",
                file=sys.stderr,
            )
            return 2

        print(f"  using session_key: {sk_path}")
        print("  unwrapping MDK via Fernet/PBKDF2 (same as running uvicorn)...")
        try:
            mdk = unwrap_session_key(sk_path, secret)
        except ValueError as exc:
            print(f"\nFATAL: {exc}", file=sys.stderr)
            return 3
        print("  ✓ MDK obtained from session cache (no password prompt).")
    else:
        if args.keyfile:
            keyfile = Path(args.keyfile).expanduser().resolve()
        else:
            keyfile = db_path.parent / "encryption.key"
        if not keyfile.exists():
            print(f"ERROR: keyfile not found at {keyfile}", file=sys.stderr)
            print(f"       hint: pass --keyfile /absolute/path/to/encryption.key", file=sys.stderr)
            return 2
        print(f"  using keyfile: {keyfile}")

        key_file_data: bytes | None = None
        if args.key_file_data:
            kf = Path(args.key_file_data).expanduser()
            if kf.exists():
                key_file_data = kf.read_bytes()
                print(f"  using 2FA keyfile: {kf} ({len(key_file_data)} bytes)")
            else:
                print(f"WARNING: KEY_FILE points to {kf} but it does not exist", file=sys.stderr)

        password = args.password or getpass.getpass("Primary key (master password): ")
        if not password:
            print("ERROR: empty password", file=sys.stderr)
            return 2

        print("\nDeriving MDK from keyfile (Argon2id, ~0.5s)...")
        try:
            mdk = unlock_keyfile_v2(keyfile, password, key_file_data)
        except ValueError as exc:
            print(f"\nFATAL: {exc}", file=sys.stderr)
            return 3
        print("  ✓ MDK unlocked, verify token matches.")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    rows = cur.execute(
        "SELECT id, original_name, stored_path, summary, extracted_text, metadata_json "
        "FROM files WHERE encrypted=1"
    ).fetchall()
    print(f"\n{len(rows)} files to recover.\n")

    ok = 0
    fail = 0
    for r in rows:
        name = r["original_name"]
        try:
            # 1. Disk file
            disk_path = Path(r["stored_path"])
            if not disk_path.exists():
                raise FileNotFoundError(f"missing on disk: {disk_path}")
            blob = disk_path.read_bytes()
            if blob[:_MAGIC_LEN] != _MAGIC:
                # Already plain (or never encrypted) — just clear flag
                disk_plain = blob
            else:
                disk_plain = decrypt_bytes(blob, mdk)

            # 2. DB columns
            new_summary = decrypt_text(r["summary"] or "", mdk)
            new_text = decrypt_text(r["extracted_text"] or "", mdk)
            new_metadata = decrypt_text(r["metadata_json"] or "", mdk)
            # metadata is supposed to be JSON — sanity check
            if new_metadata:
                try:
                    json.loads(new_metadata)
                except json.JSONDecodeError:
                    # If it's not valid JSON post-decrypt, fall back to {}
                    new_metadata = "{}"

            if args.dry_run:
                print(f"  [dry] {name}: disk {len(blob)}→{len(disk_plain)} bytes, "
                      f"summary {len(new_summary)} chars, text {len(new_text)} chars")
            else:
                disk_path.write_bytes(disk_plain)
                cur.execute(
                    "UPDATE files SET summary=?, extracted_text=?, metadata_json=?, "
                    "encrypted=0, updated_at=datetime('now') WHERE id=?",
                    (new_summary, new_text, new_metadata, r["id"]),
                )
                con.commit()
                print(f"  ✓ {name}")
            ok += 1
        except Exception as exc:
            print(f"  ✗ {name}: {exc}", file=sys.stderr)
            fail += 1

    con.close()
    print(f"\nDone. {ok} recovered, {fail} failed.")
    if args.dry_run:
        print("(dry run — nothing written)")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
