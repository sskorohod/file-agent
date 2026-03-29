"""Google Drive storage backend."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.storage.backends.base import StorageBackend

logger = logging.getLogger(__name__)


class GDriveBackend(StorageBackend):
    """Store files in Google Drive via service account."""

    scheme = "gdrive"

    def __init__(
        self,
        credentials_json: str,
        folder_id: str,
        encryption_key: bytes | None = None,
    ):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        self._folder_id = folder_id
        self._encryption_key = encryption_key
        self._folder_cache: dict[str, str] = {}  # category → folder_id

        creds = Credentials.from_service_account_file(
            credentials_json,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self._service = build("drive", "v3", credentials=creds)
        logger.info(f"Google Drive backend: folder={folder_id}")

    def _get_or_create_folder(self, category: str) -> str:
        """Get or create a subfolder for the category."""
        if category in self._folder_cache:
            return self._folder_cache[category]

        # Search for existing folder
        q = (
            f"name='{category}' and '{self._folder_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = self._service.files().list(q=q, fields="files(id)").execute()
        files = results.get("files", [])

        if files:
            fid = files[0]["id"]
        else:
            meta = {
                "name": category,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [self._folder_id],
            }
            folder = self._service.files().create(body=meta, fields="id").execute()
            fid = folder["id"]

        self._folder_cache[category] = fid
        return fid

    async def write(
        self, data: bytes, category: str, original_name: str,
    ) -> str:
        if self._encryption_key:
            from app.utils.crypto import encrypt_bytes
            data = encrypt_bytes(data, self._encryption_key)

        from googleapiclient.http import MediaInMemoryUpload
        folder_id = await asyncio.to_thread(self._get_or_create_folder, category)
        name = Path(original_name).name
        media = MediaInMemoryUpload(data, resumable=False)
        meta = {"name": name, "parents": [folder_id]}
        result = await asyncio.to_thread(
            lambda: self._service.files().create(
                body=meta, media_body=media, fields="id",
            ).execute()
        )
        file_id = result["id"]
        return f"gdrive://{file_id}"

    async def read(self, uri: str) -> bytes:
        file_id = uri.removeprefix("gdrive://")
        resp = await asyncio.to_thread(
            lambda: self._service.files().get_media(fileId=file_id).execute()
        )
        data = resp if isinstance(resp, bytes) else bytes(resp)
        if self._encryption_key:
            from app.utils.crypto import decrypt_bytes, is_encrypted
            if is_encrypted(data):
                return decrypt_bytes(data, self._encryption_key)
        return data

    async def delete(self, uri: str) -> bool:
        file_id = uri.removeprefix("gdrive://")
        try:
            await asyncio.to_thread(
                lambda: self._service.files().delete(fileId=file_id).execute()
            )
            return True
        except Exception:
            return False

    async def exists(self, uri: str) -> bool:
        file_id = uri.removeprefix("gdrive://")
        try:
            await asyncio.to_thread(
                lambda: self._service.files().get(fileId=file_id, fields="id").execute()
            )
            return True
        except Exception:
            return False

    async def test_connection(self) -> dict:
        """Test Google Drive connectivity."""
        try:
            result = await asyncio.to_thread(
                lambda: self._service.files().get(
                    fileId=self._folder_id, fields="id,name",
                ).execute()
            )
            return {"status": "ok", "folder": result.get("name", self._folder_id)}
        except Exception as e:
            return {"status": "error", "error": str(e)}
