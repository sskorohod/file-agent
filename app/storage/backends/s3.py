"""S3 storage backend (AWS S3, MinIO, Wasabi, Backblaze B2)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.storage.backends.base import StorageBackend

logger = logging.getLogger(__name__)


class S3Backend(StorageBackend):
    """Store files in S3-compatible object storage."""

    scheme = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "fileagent",
        region: str = "us-east-1",
        access_key_id: str = "",
        secret_access_key: str = "",
        endpoint_url: str = "",
        encryption_key: bytes | None = None,
    ):
        import boto3

        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._encryption_key = encryption_key

        client_kwargs = {
            "service_name": "s3",
            "region_name": region,
        }
        if access_key_id and secret_access_key:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._client = boto3.client(**client_kwargs)
        logger.info(f"S3 backend: bucket={bucket}, prefix={prefix}")

    def _build_key(self, category: str, original_name: str) -> str:
        now = datetime.now()
        name = Path(original_name).name
        uid = uuid4().hex[:8]
        return f"{self._prefix}/{category}/{now.strftime('%Y-%m')}/{uid}_{name}"

    async def write(
        self, data: bytes, category: str, original_name: str,
        encrypt: bool = False,
    ) -> str:
        payload = data
        if encrypt:
            if self._encryption_key:
                from app.utils.crypto import encrypt_bytes
                payload = encrypt_bytes(data, self._encryption_key)
            else:
                logger.warning(
                    "encrypt=True requested but no encryption key — writing plaintext (%s)",
                    original_name,
                )

        key = self._build_key(category, original_name)
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=payload,
        )
        return f"s3://{self._bucket}/{key}"

    async def read(self, uri: str) -> bytes:
        bucket, key = self._parse_uri(uri)
        resp = await asyncio.to_thread(
            self._client.get_object, Bucket=bucket, Key=key,
        )
        data = resp["Body"].read()
        if self._encryption_key:
            from app.utils.crypto import decrypt_bytes, is_encrypted
            if is_encrypted(data):
                return decrypt_bytes(data, self._encryption_key)
        return data

    async def delete(self, uri: str) -> bool:
        bucket, key = self._parse_uri(uri)
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=bucket, Key=key,
            )
            return True
        except Exception:
            return False

    async def exists(self, uri: str) -> bool:
        bucket, key = self._parse_uri(uri)
        try:
            await asyncio.to_thread(
                self._client.head_object, Bucket=bucket, Key=key,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str]:
        """Parse 's3://bucket/key' → (bucket, key)."""
        path = uri.removeprefix("s3://")
        bucket, _, key = path.partition("/")
        return bucket, key

    async def test_connection(self) -> dict:
        """Test S3 connectivity. Returns status dict."""
        try:
            resp = await asyncio.to_thread(
                self._client.head_bucket, Bucket=self._bucket,
            )
            return {"status": "ok", "bucket": self._bucket}
        except Exception as e:
            return {"status": "error", "error": str(e)}
