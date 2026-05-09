# Encryption at Rest — Full Implementation Plan

**Date:** 2026-03-26
**Status:** Ready for implementation

## Context

Documents in the system (passports, medical records, financial docs) are stored in 3 places in plaintext:
1. **Files on disk** — `~/ai-agent-files/category/YYYY-MM/file.pdf` (plain bytes)
2. **SQLite** — `extracted_text`, `chat_history.content`, `metadata_json` (plaintext)
3. **Qdrant** — `payload.text` contains document text chunks (plaintext)

Anyone who gains access to the disk or backup sees everything.

## Decisions

- **SQLCipher** for full SQLite encryption + **AES-256-GCM** for files on disk
- Migrate existing data + encrypt new files
- **Recovery key** system for data loss protection
- Remove text from **Qdrant payload** (store only file_id + chunk_index + vectors)

## Performance Impact

Negligible:
- File upload (full pipeline): ~3-8 sec -> no change (encryption happens AFTER parsing/LLM)
- Semantic search: +1-2ms (text from SQLite instead of Qdrant)
- File write: +5-10ms (AES-256-GCM is hardware-accelerated)
- FTS5 search: no loss (SQLCipher encrypts the whole DB transparently)

---

## Step 1: Extend `app/utils/crypto.py` — AES-256-GCM + Recovery Key

Add functions (DO NOT touch existing Fernet code for secrets):

```python
import os, secrets, hashlib, base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM. Returns: nonce(12) + ciphertext + tag(16). Overhead: 28 bytes."""
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    return nonce + aesgcm.encrypt(nonce, data, None)

def decrypt_bytes(encrypted: bytes, key: bytes) -> bytes:
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(encrypted[:12], encrypted[12:], None)

def generate_encryption_key() -> str:
    return secrets.token_hex(32)  # 32 bytes = 256 bit

def generate_recovery_key(key: bytes) -> str:
    checksum = hashlib.sha256(key).digest()[:4]
    return base64.urlsafe_b64encode(key + checksum).decode()

def recover_key_from_recovery(recovery: str) -> bytes:
    payload = base64.urlsafe_b64decode(recovery)
    key, checksum = payload[:-4], payload[-4:]
    if hashlib.sha256(key).digest()[:4] != checksum:
        raise ValueError("Invalid recovery key")
    return key

def parse_encryption_key(hex_key: str) -> bytes:
    key = bytes.fromhex(hex_key)
    if len(key) != 32:
        raise ValueError(f"Key must be 32 bytes, got {len(key)}")
    return key
```

**File:** `app/utils/crypto.py` (currently 41 lines, adding ~40 lines)

---

## Step 2: Add `EncryptionConfig` to `app/config.py`

```python
class EncryptionConfig(BaseModel):
    enabled: bool = False
```

Add to `Settings`:
- `encryption_key: str = ""` — env var `ENCRYPTION_KEY`
- `encryption: EncryptionConfig = EncryptionConfig()`

**File:** `app/config.py` — around lines ~128, ~168

---

## Step 3: Create `app/storage/aiosqlcipher.py` — async SQLCipher wrapper

aiosqlite = sqlite3 in a dedicated thread + async API. Need the same for sqlcipher3.

Key classes:
- `Connection` — thread + queue + asyncio.Future, methods: `execute()`, `executescript()`, `commit()`, `close()`
- `Cursor` — async wrapper: `fetchone()`, `fetchall()`, `lastrowid`
- `Row` — re-export `sqlcipher3.Row` (compatible with sqlite3.Row)
- `connect(database, key)` — first command: `PRAGMA key="x'<hex>'"`

Pattern: callable is put into `queue.Queue`, thread picks up and executes, result via `loop.call_soon_threadsafe(future.set_result, ...)`.

~150-200 lines. **File:** `app/storage/aiosqlcipher.py` (new)

---

## Step 4: Modify `app/storage/db.py` — conditional connection

Changes ONLY in `__init__` and `connect()`:

```python
class Database:
    def __init__(self, db_path, encryption_key: str = ""):
        # ... existing ...
        self._encryption_key = encryption_key

    async def connect(self):
        if self._encryption_key:
            from app.storage.aiosqlcipher import connect as sc_connect
            self._db = await sc_connect(str(self.db_path), self._encryption_key)
        else:
            self._db = await aiosqlite.connect(str(self.db_path))
        # row_factory, PRAGMAs, schema — unchanged
```

Remaining 700+ lines of CRUD **stay unchanged** (same async API).

**File:** `app/storage/db.py` — lines 10, 170-187

---

## Step 5: File encryption in `app/storage/files.py`

```python
class FileStorage:
    def __init__(self, base_path, allowed_extensions=None, encryption_key: bytes | None = None):
        # ... existing ...
        self._encryption_key = encryption_key
```

- `save_from_bytes()` line 94: `target.write_bytes(encrypt_bytes(data, key))` if key present
- `save_from_path()` line 119: read + encrypt + write instead of `shutil.copy2`
- **New method** `read_file(path) -> bytes` — single read point with decryption
- `_hash_bytes`/`_hash_file` — hash BEFORE encryption (unchanged)

**File:** `app/storage/files.py`

---

## Step 6: Remove text from Qdrant payload in `app/storage/vectors.py`

- `upsert_document()` lines 283-313: remove `"text": ...` from payload. Keep `file_id`, `chunk_index`, `embedding_type`, `total_chunks`, `**meta`
- `search()` line 366: `SearchResult.text` will be `""` — OK, `app/llm/search.py:148-158` already prefers `full_text` from SQLite
- `find_similar()`: text also `""` — used only for dedup (needs file_id + score)

**File:** `app/storage/vectors.py`

---

## Step 7: Update all 4 file read points

### 7a. `app/web/routes.py` line 253
```python
# Was: content=file_path.read_bytes()
# Now: content=file_storage.read_file(file_path)
```
Line 257: `FileResponse(path=)` -> always `Response(content=file_storage.read_file(...))` (FileResponse can't decrypt).

### 7b. `app/bot/handlers.py` line 992
```python
# Was: document=open(file_path, "rb")
# Now: document=io.BytesIO(self.pipeline.file_storage.read_file(file_path))
```

### 7c. `app/mcp_server.py` line 255
```python
# Was: data = p.read_bytes()
# Now: data = file_storage.read_file(p)
```

### 7d. `app/storage/files.py` method `find_by_hash()` line 135
Not called anywhere in pipeline — mark as deprecated or update to decrypt before hashing.

---

## Step 8: Initialization in `app/main.py` (lifespan, line 47)

```python
encryption_key_hex = settings.encryption_key or os.environ.get("ENCRYPTION_KEY", "")
encryption_key_bytes = None
if encryption_key_hex:
    from app.utils.crypto import parse_encryption_key, generate_recovery_key
    encryption_key_bytes = parse_encryption_key(encryption_key_hex)

    # Recovery key — show once
    recovery_marker = Path("data/.recovery_shown")
    if not recovery_marker.exists():
        recovery = generate_recovery_key(encryption_key_bytes)
        logger.critical(
            "\n" + "=" * 60 +
            "\n  RECOVERY KEY — SAVE THIS NOW!\n" +
            f"  {recovery}\n" +
            "\n  Without this key, encrypted data is UNRECOVERABLE.\n" +
            "=" * 60
        )
        recovery_marker.touch()

# Pass keys:
db = Database(settings.database.path, encryption_key=encryption_key_hex)
file_storage = FileStorage(..., encryption_key=encryption_key_bytes)
```

**File:** `app/main.py` lines 47-72

---

## Step 9: Migration script `scripts/migrate_encryption.py`

### 9a. SQLite -> SQLCipher
```python
plain = sqlite3.connect(db_path)
enc = sqlcipher3.connect(db_path + ".encrypted")
enc.execute(f"PRAGMA key=\"x'{hex_key}'\"")
plain.backup(enc)  # SQLite C backup API
enc.close(); plain.close()
os.rename(db_path, db_path + ".plain.bak")
os.rename(db_path + ".encrypted", db_path)
```

### 9b. Files on disk
```python
for row in db.execute("SELECT stored_path FROM files"):
    path = Path(row[0])
    plaintext = path.read_bytes()
    encrypted = encrypt_bytes(plaintext, key)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(encrypted)
    tmp.rename(path)  # atomic on same filesystem
```

### 9c. Qdrant — remove text from payload
```python
# Scroll all points, delete "text" key
client.delete_payload(collection_name=..., keys=["text"], points=<all ids>)
```

**Safety:** `.plain.bak` is preserved, files use atomic rename.

**File:** `scripts/migrate_encryption.py` (new)

---

## Step 10: Dependencies + Docker

### requirements.txt
- Add: `sqlcipher3>=0.5.4`
- Do NOT remove `aiosqlite` (fallback without encryption)

### Dockerfile
- Add: `libsqlcipher-dev gcc` to `apt-get install`

---

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| sqlcipher3 won't compile on Python 3.14 | High | Test in Docker first. Fallback: column-level Fernet |
| FTS5 missing from SQLCipher build | Medium | Debian libsqlcipher-dev includes FTS5. Check PRAGMA compile_options |
| sqlite3.backup() -> sqlcipher3 incompatible | Medium | Test explicitly. Fallback: .dump() + executescript() |
| Lost ENCRYPTION_KEY = total data loss | Critical | Recovery key shown at first startup, saved by user |
| Migration interrupted midway | Medium | Atomic rename for files, plain DB backup kept |

---

## Implementation Order

1. `app/utils/crypto.py` — AES-256-GCM functions (no breaking changes)
2. `app/storage/aiosqlcipher.py` — async wrapper (isolated, testable)
3. `app/config.py` — EncryptionConfig
4. `app/storage/db.py` — conditional sqlcipher import (2 lines in connect)
5. `app/storage/files.py` — encryption_key + encrypt on write + read_file()
6. `app/storage/vectors.py` — remove text from payload
7. `app/main.py` — wire encryption key
8. File read consumers: routes.py, handlers.py, mcp_server.py
9. `scripts/migrate_encryption.py` — migration
10. Dockerfile + requirements.txt
11. Tests

## Verification

1. **Unit tests crypto:** roundtrip encrypt/decrypt, recovery key, wrong key -> exception
2. **Unit tests aiosqlcipher:** connect, execute, Row, FTS5, WAL
3. **Integration:** `ENCRYPTION_KEY=<test> make dev` -> upload file -> verify encrypted on disk
4. **Migration:** plain DB -> script -> verify access
5. **Recovery:** remove ENCRYPTION_KEY -> recover -> restore
6. **Backward compat:** no ENCRYPTION_KEY -> everything works as before
