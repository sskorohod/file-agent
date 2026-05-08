"""Async HTTP client for the Cognee sidecar.

The Cognee instance runs as a separate process (see infra/cognee/README.md).
This module is the only place in FAG that talks to it — everything goes
through ``CogneeClient``.

Two key contracts that callers can rely on:

* ``health_check`` returns ``True`` only when the sidecar answered. Anywhere a
  caller is about to do work that depends on the sidecar, ``self.healthy``
  should already be ``True`` (set by the lifespan probe in ``app/main.py``).
* When ``cognee.enabled`` is ``False`` or the sidecar is unreachable,
  every call returns a sentinel result instead of raising. The pipeline
  treats that as "skip this step" rather than failing the whole ingest.

The exact REST contract for cognee 1.0.x endpoints is still being verified
in spike-2 (Phase 1). Methods documented here describe intent — actual paths
and request bodies will be tightened against ``GET /openapi.json`` of a live
sidecar before Phase 2 starts using them.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import CogneeConfig

logger = logging.getLogger(__name__)


class CogneeError(Exception):
    """Sidecar reached but returned an error response."""


class CogneeUnavailable(CogneeError):
    """Sidecar unreachable (network error, timeout, or disabled)."""


class CogneeClient:
    """Thin async wrapper over the Cognee sidecar HTTP API."""

    def __init__(self, config: CogneeConfig):
        self.config = config
        self.healthy: bool = False
        self._client: httpx.AsyncClient | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Create the HTTP client and probe the sidecar.

        Never raises — failures are logged and ``self.healthy`` stays False.
        """
        if not self.config.enabled:
            logger.info("Cognee disabled in config — client is a no-op")
            return

        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        self._client = httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=self.config.request_timeout_s,
        )
        await self.health_check()

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        """Probe the sidecar root endpoint. Updates ``self.healthy``."""
        if not self.config.enabled or self._client is None:
            self.healthy = False
            return False
        try:
            response = await self._client.get("/", timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning(
                "Cognee sidecar unreachable at %s (%s) — memory features disabled",
                self.config.base_url,
                exc.__class__.__name__,
            )
            self.healthy = False
            return False
        if response.status_code != 200:
            logger.warning(
                "Cognee sidecar at %s returned HTTP %d on health check",
                self.config.base_url,
                response.status_code,
            )
            self.healthy = False
            return False
        self.healthy = True
        logger.info("Cognee sidecar healthy at %s", self.config.base_url)
        return True

    # ── memory ops (Phase 2+ will start calling these) ───────────────────

    async def add(
        self,
        content: str,
        *,
        dataset: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send raw text to the sidecar for ingestion.

        After this call returns, the content is staged but not yet processed
        into the graph — call ``cognify`` to do that.
        """
        target_dataset = dataset or self.config.default_dataset
        body: dict[str, Any] = {"data": content, "dataset_name": target_dataset}
        if metadata:
            body["metadata"] = metadata
        return await self._post("/api/v1/add", body)

    async def cognify(self, *, dataset: str | None = None) -> dict[str, Any]:
        """Run the cognification pipeline (LLM extraction → graph + vectors).

        Long-running — uses ``cognify_timeout_s`` instead of the default.
        """
        target_dataset = dataset or self.config.default_dataset
        return await self._post(
            "/api/v1/cognify",
            {"datasets": [target_dataset]},
            timeout=self.config.cognify_timeout_s,
        )

    async def search(
        self,
        query: str,
        *,
        query_type: str = "GRAPH_COMPLETION",
        dataset: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """V1 search API — returns LLM-rendered answers / chunks."""
        body: dict[str, Any] = {
            "query": query,
            "query_type": query_type,
            "top_k": limit,
        }
        if dataset:
            body["datasets"] = [dataset]
        result = await self._post("/api/v1/search", body)
        if isinstance(result, list):
            return result
        return result.get("results", []) if isinstance(result, dict) else []

    async def recall(
        self,
        query: str,
        *,
        dataset: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """v1.0 recall API — preferred over ``search`` for retrieval."""
        body: dict[str, Any] = {"query": query, "top_k": limit}
        if dataset:
            body["dataset_name"] = dataset
        result = await self._post("/api/v1/recall", body)
        if isinstance(result, list):
            return result
        return result.get("memories", []) if isinstance(result, dict) else []

    async def forget(
        self,
        *,
        dataset: str | None = None,
        content_ref: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if dataset:
            body["dataset_name"] = dataset
        if content_ref:
            body["content_ref"] = content_ref
        return await self._post("/api/v1/forget", body)

    async def list_datasets(self) -> list[dict[str, Any]]:
        result = await self._get("/api/v1/datasets")
        if isinstance(result, list):
            return result
        return result.get("datasets", []) if isinstance(result, dict) else []

    # ── transport ────────────────────────────────────────────────────────

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path, json_body=None)

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> Any:
        return await self._request("POST", path, json_body=body, timeout=timeout)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> Any:
        if not self.config.enabled or self._client is None or not self.healthy:
            raise CogneeUnavailable(
                f"Cognee sidecar not available (enabled={self.config.enabled}, "
                f"healthy={self.healthy})"
            )
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                timeout=timeout if timeout is not None else self.config.request_timeout_s,
            )
        except httpx.HTTPError as exc:
            self.healthy = False
            raise CogneeUnavailable(f"{method} {path}: {exc}") from exc

        if response.status_code >= 400:
            raise CogneeError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:500]}"
            )

        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text
