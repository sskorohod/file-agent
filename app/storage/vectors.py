"""Vector store — Qdrant with Gemini multimodal + local embedding."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.config import QdrantConfig, EmbeddingConfig

logger = logging.getLogger(__name__)

# MIME types that Gemini Embedding 2 can process natively
GEMINI_MULTIMODAL_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf",
    "audio/mp3", "audio/mpeg", "audio/wav",
    "video/mp4", "video/quicktime",
}

# Max file sizes for multimodal embedding (Gemini limits)
_MAX_MULTIMODAL_BYTES = 20 * 1024 * 1024  # 20MB safety limit


@dataclass
class SearchResult:
    """Single search result from vector store."""
    file_id: str
    chunk_index: int
    text: str
    score: float
    metadata: dict


class GeminiEmbedder:
    """Wrapper for Google Gemini Embedding 2 API."""

    def __init__(self, model: str, api_key: str, vector_size: int = 768):
        self.model = model
        self.vector_size = vector_size
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
            logger.info(f"Initialized Gemini embedding client: {self.model}")
        return self._client

    def embed_texts(
        self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Embed text strings via Gemini API."""
        from google.genai import types
        client = self._get_client()
        result = client.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self.vector_size,
            ),
        )
        return [e.values for e in result.embeddings]

    def embed_multimodal(self, file_bytes: bytes, mime_type: str) -> list[float]:
        """Embed raw file bytes (image, PDF, audio, video) via Gemini API."""
        from google.genai import types
        client = self._get_client()
        part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        result = client.models.embed_content(
            model=self.model,
            contents=part,
            config=types.EmbedContentConfig(
                output_dimensionality=self.vector_size,
            ),
        )
        return result.embeddings[0].values


class VectorStore:
    """Manage vectors in remote Qdrant instance with Gemini + local embedding."""

    def __init__(
        self,
        qdrant_config: QdrantConfig,
        embedding_config: EmbeddingConfig,
        google_api_key: str = "",
        strip_text: bool = False,
    ):
        self.qdrant_config = qdrant_config
        self.embedding_config = embedding_config
        self._google_api_key = google_api_key
        self._strip_text = strip_text  # Don't store text in Qdrant payload
        self._client = None
        self._local_embedder = None
        self._gemini_embedder = None

    async def connect(self):
        """Initialize Qdrant client."""
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self._client = QdrantClient(
            url=f"http://{self.qdrant_config.host}:{self.qdrant_config.port}",
            prefer_grpc=False,
            timeout=10,
            api_key=self.qdrant_config.api_key or None,
        )

        # Ensure collection exists
        collections = self._client.get_collections().collections
        names = [c.name for c in collections]

        if self.qdrant_config.collection_name not in names:
            distance_map = {
                "Cosine": Distance.COSINE,
                "Euclid": Distance.EUCLID,
                "Dot": Distance.DOT,
            }
            self._client.create_collection(
                collection_name=self.qdrant_config.collection_name,
                vectors_config=VectorParams(
                    size=self.qdrant_config.vector_size,
                    distance=distance_map.get(self.qdrant_config.distance, Distance.COSINE),
                ),
            )
            logger.info(f"Created Qdrant collection: {self.qdrant_config.collection_name}")
        else:
            logger.info(f"Qdrant collection exists: {self.qdrant_config.collection_name}")

    @property
    def client(self):
        if not self._client:
            raise RuntimeError("VectorStore not connected. Call connect() first.")
        return self._client

    # ── Embedder factories ───────────────────────────────────────────────

    def _get_gemini_embedder(self) -> GeminiEmbedder:
        if self._gemini_embedder is None:
            self._gemini_embedder = GeminiEmbedder(
                model=self.embedding_config.model,
                api_key=self._google_api_key,
                vector_size=self.embedding_config.vector_size,
            )
        return self._gemini_embedder

    def _get_local_embedder(self):
        if self._local_embedder is None:
            from sentence_transformers import SentenceTransformer
            model_name = self.embedding_config.local_fallback_model
            self._local_embedder = SentenceTransformer(model_name)
            logger.info(f"Loaded local embedding model: {model_name}")
        return self._local_embedder

    # ── Chunking ────────────────────────────────────────────────────────

    def chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks by word count."""
        words = text.split()
        chunk_size = self.embedding_config.chunk_size_words
        overlap = self.embedding_config.chunk_overlap_words
        chunks = []
        start = 0

        while start < len(words):
            end = start + chunk_size
            chunk = " ".join(words[start:end])
            if chunk.strip():
                chunks.append(chunk)
            start += chunk_size - overlap

        return chunks if chunks else [text[:2000]] if text.strip() else []

    # ── Embedding ───────────────────────────────────────────────────────

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        """Embed using local sentence-transformers."""
        embedder = self._get_local_embedder()
        embeddings = embedder.encode(
            texts,
            batch_size=self.embedding_config.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return embeddings.tolist()

    def embed(
        self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Embed texts using configured provider (Gemini or local)."""
        import time as _time
        if self.embedding_config.provider == "gemini" and self._google_api_key:
            try:
                _start = _time.monotonic()
                result = self._get_gemini_embedder().embed_texts(texts, task_type=task_type)
                _ms = int((_time.monotonic() - _start) * 1000)
                # Log embedding usage
                self._log_embedding_usage(
                    role="embedding_text", model=self.embedding_config.model,
                    input_tokens=sum(len(t.split()) for t in texts),
                    latency_ms=_ms, count=len(texts),
                )
                return result
            except Exception as e:
                logger.warning(f"Gemini text embedding failed: {e}")
                raise
        return self._embed_local(texts)

    def embed_multimodal(self, file_bytes: bytes, mime_type: str) -> list[float] | None:
        """Embed raw file bytes using Gemini multimodal. Returns None if unsupported."""
        import time as _time
        if self.embedding_config.provider != "gemini" or not self._google_api_key:
            return None
        if mime_type not in GEMINI_MULTIMODAL_MIMES:
            return None
        if len(file_bytes) > _MAX_MULTIMODAL_BYTES:
            logger.warning(f"File too large for multimodal embedding: {len(file_bytes)} bytes")
            return None
        try:
            _start = _time.monotonic()
            result = self._get_gemini_embedder().embed_multimodal(file_bytes, mime_type)
            _ms = int((_time.monotonic() - _start) * 1000)
            self._log_embedding_usage(
                role="embedding_multimodal", model=self.embedding_config.model,
                input_tokens=len(file_bytes) // 1024,  # KB as proxy for tokens
                latency_ms=_ms, count=1,
            )
            return result
        except Exception as e:
            logger.warning(f"Multimodal embedding failed for {mime_type}: {e}")
            return None

    def _log_embedding_usage(self, role: str, model: str, input_tokens: int, latency_ms: int, count: int):
        """Log embedding API call to llm_usage table (async-safe fire-and-forget)."""
        try:
            import asyncio
            from app.main import get_state
            db = get_state("db")
            if db:
                # Gemini Embedding preview = free, but track calls
                # Pricing: $0.00 for preview, $0.0001/1K chars for GA
                coro = db.log_llm_usage(
                    role=role, model=model,
                    input_tokens=input_tokens, output_tokens=count,
                    cost_usd=0.0, latency_ms=latency_ms,
                )
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(coro)
                else:
                    loop.run_until_complete(coro)
        except Exception:
            pass  # non-fatal

    # ── CRUD ────────────────────────────────────────────────────────────

    async def upsert_document(
        self,
        file_id: str,
        text: str,
        metadata: dict | None = None,
        file_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> int:
        """Embed and upsert document into Qdrant.

        Creates:
        - Multimodal point (if file_bytes + supported MIME provided)
        - Text chunk points (if text is non-empty)
        """
        from qdrant_client.models import PointStruct

        points = []
        meta = metadata or {}

        # 1. Multimodal embedding (raw file bytes)
        if file_bytes and mime_type:
            mm_vector = self.embed_multimodal(file_bytes, mime_type)
            if mm_vector:
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{file_id}:mm"))
                points.append(PointStruct(
                    id=point_id,
                    vector=mm_vector,
                    payload={
                        "file_id": file_id,
                        "chunk_index": -1,  # -1 = multimodal point
                        **({"text": text[:500] if text else ""} if not self._strip_text else {}),
                        "embedding_type": "multimodal",
                        "mime_type": mime_type,
                        "total_chunks": 0,  # updated below
                        **meta,
                    },
                ))

        # 2. Text chunk embeddings
        chunks = self.chunk_text(text) if text.strip() else []
        if chunks:
            vectors = self.embed(chunks)
            for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{file_id}:{i}"))
                points.append(PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "file_id": file_id,
                        "chunk_index": i,
                        **({"text": chunk} if not self._strip_text else {}),
                        "embedding_type": "text",
                        "total_chunks": len(chunks),
                        **meta,
                    },
                ))

        # Update total_chunks in multimodal point
        if points and points[0].payload.get("embedding_type") == "multimodal":
            points[0].payload["total_chunks"] = len(chunks)

        if not points:
            return 0

        self.client.upsert(
            collection_name=self.qdrant_config.collection_name,
            points=points,
        )

        logger.info(
            f"Upserted {len(points)} points for {file_id} "
            f"(multimodal: {'yes' if any(p.payload.get('embedding_type') == 'multimodal' for p in points) else 'no'}, "
            f"text chunks: {len(chunks)})"
        )
        return len(points)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        file_id: str | None = None,
        category: str | None = None,
    ) -> list[SearchResult]:
        """Semantic search — embed query and find nearest chunks."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_vector = self.embed([query], task_type="RETRIEVAL_QUERY")[0]

        conditions = []
        if file_id:
            conditions.append(FieldCondition(key="file_id", match=MatchValue(value=file_id)))
        if category:
            conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))

        search_filter = Filter(must=conditions) if conditions else None

        response = self.client.query_points(
            collection_name=self.qdrant_config.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=search_filter,
            with_payload=True,
        )

        return [
            SearchResult(
                file_id=hit.payload.get("file_id", ""),
                chunk_index=hit.payload.get("chunk_index", 0),
                text=hit.payload.get("text", ""),
                score=hit.score,
                metadata={k: v for k, v in hit.payload.items()
                          if k not in ("file_id", "chunk_index", "text")},
            )
            for hit in response.points
        ]

    async def search_notes(
        self, query: str, top_k: int = 10, category: str = "",
    ) -> list[dict]:
        """Semantic search across note embeddings in Qdrant."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_vector = self.embed([query], task_type="RETRIEVAL_QUERY")[0]

        conditions = [FieldCondition(key="type", match=MatchValue(value="note"))]
        if category:
            conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))

        response = self.client.query_points(
            collection_name=self.qdrant_config.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=Filter(must=conditions),
            with_payload=True,
        )

        # Deduplicate by note_id (multiple chunks per note)
        seen_ids: set[int] = set()
        results = []
        for hit in response.points:
            note_id = hit.payload.get("note_id")
            if note_id and note_id not in seen_ids:
                seen_ids.add(note_id)
                results.append({
                    "note_id": note_id,
                    "score": hit.score,
                    "matched_text": hit.payload.get("text", ""),
                    "category": hit.payload.get("category", ""),
                })
        return results

    def get_file_vector(self, file_id: str) -> list[float] | None:
        """Retrieve the stored vector for a file (prefer multimodal point)."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        points, _ = self.client.scroll(
            collection_name=self.qdrant_config.collection_name,
            scroll_filter=Filter(must=[
                FieldCondition(key="file_id", match=MatchValue(value=file_id)),
            ]),
            limit=10,
            with_vectors=True,
            with_payload=True,
        )

        # Prefer multimodal point (chunk_index=-1)
        for p in points:
            if p.payload.get("chunk_index") == -1:
                return p.vector
        # Fallback: first text chunk
        if points:
            return points[0].vector
        return None

    def find_similar(
        self,
        vector: list[float],
        exclude_file_id: str,
        threshold: float = 0.94,
        top_k: int = 3,
    ) -> list[SearchResult]:
        """Find documents with similar embeddings, excluding a specific file_id."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue, HasIdCondition

        # Exclude points belonging to the same file
        search_filter = Filter(
            must_not=[FieldCondition(key="file_id", match=MatchValue(value=exclude_file_id))]
        )

        response = self.client.query_points(
            collection_name=self.qdrant_config.collection_name,
            query=vector,
            limit=top_k,
            query_filter=search_filter,
            with_payload=True,
            score_threshold=threshold,
        )

        return [
            SearchResult(
                file_id=hit.payload.get("file_id", ""),
                chunk_index=hit.payload.get("chunk_index", 0),
                text=hit.payload.get("text", ""),
                score=hit.score,
                metadata={k: v for k, v in hit.payload.items()
                          if k not in ("file_id", "chunk_index", "text")},
            )
            for hit in response.points
        ]

    async def update_document_metadata(self, file_id: str, payload_updates: dict) -> None:
        """Update payload fields on all points belonging to a file (no re-embedding).

        Uses Qdrant set_payload with a file_id filter to patch metadata in-place.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self.client.set_payload(
            collection_name=self.qdrant_config.collection_name,
            payload=payload_updates,
            points=Filter(
                must=[FieldCondition(key="file_id", match=MatchValue(value=file_id))]
            ),
        )
        logger.info(f"Updated Qdrant payload for {file_id}: {list(payload_updates.keys())}")

    async def delete_document(self, file_id: str) -> int:
        """Delete all points for a file (multimodal + text chunks)."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self.client.delete(
            collection_name=self.qdrant_config.collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="file_id", match=MatchValue(value=file_id))]
            ),
        )
        logger.info(f"Deleted vectors for file {file_id}")
        return 0

    async def health_check(self) -> dict:
        """Check Qdrant server health."""
        try:
            info = self.client.get_collection(self.qdrant_config.collection_name)
            return {
                "status": "healthy",
                "collection": self.qdrant_config.collection_name,
                "points_count": getattr(info, "points_count", 0),
                "embedding_provider": self.embedding_config.provider,
                "embedding_model": self.embedding_config.model,
                "vector_size": self.embedding_config.vector_size,
            }
        except Exception as e:
            try:
                cols = self.client.get_collections()
                return {"status": "healthy", "collections": len(cols.collections)}
            except Exception:
                return {"status": "unhealthy", "error": str(e)}

    async def close(self):
        """Close client connection."""
        if self._client:
            self._client.close()
            self._client = None
