"""LLM Search — RAG pipeline with smart filtering, caching, and conversation memory."""

from __future__ import annotations

import hashlib
import json
import logging
import time

from app.llm.router import LLMRouter
from app.storage.vectors import VectorStore

logger = logging.getLogger(__name__)

SEARCH_SYSTEM = """You are an intelligent personal document assistant. You have access to the user's document archive.

Your job is to give COMPLETE, DETAILED, ACTIONABLE answers based on the documents provided.

Rules:
- Extract ALL relevant information from the documents — dates, names, numbers, amounts, dosages, deadlines, addresses, reference numbers
- Structure your answer clearly: use bullet points, numbered lists, or tables when appropriate
- If there are multiple matching documents, present information from ALL of them, organized chronologically
- If the user asks for a specific document type (e.g. passport), do NOT include other types (e.g. driver's license)
- Calculate time-sensitive information: days remaining until deadlines, time elapsed since dates
- If you notice important action items, deadlines, or things the user should do — highlight them
- At the end, proactively offer to do something useful: make a table, compare documents, summarize, etc.
- Respond in the same language as the question
- Be thorough but well-organized — the user wants to see all the details, not a brief summary"""

SEARCH_USER = """Documents from the archive:

{context}

---

User's question: {query}

Give a complete, detailed answer with all specific data from the documents."""

SEARCH_USER_COMPACT = """Documents from the archive (each delimited by [Document: ...]):

{context}

---

User's question: {query}

Answer in the same language as the question. Telegram chat — concise,
not an essay.

═══ HOW TO STRUCTURE THE ANSWER ═══

▸ COUNT how many documents in the context above are *genuinely* relevant
  to the question. Skip documents that aren't a real match (a passport
  query should not answer about a birth certificate even if it's in the
  context).

▸ If exactly **ONE** document is relevant:
   give the full detailed answer about that document, formatted as a
   single block:

      📄 <b>Document type — owner / date</b>
      • Key fact 1
      • Key fact 2 (number / amount / expiry)
      • Action / deadline if any

▸ If **TWO OR MORE** documents are relevant:
   list ALL of them as a *brief* disambiguation menu, NOT full details
   for each. End with a question asking which one the user wants. Do
   NOT give the full content for any of them — the user will pick.

      🔎 Найдено документов: N

      📄 <b>Document type — owner</b>
      <i>brief 1-line distinguishing detail (date / number / who)</i>

      📄 <b>Second document type — owner</b>
      <i>brief 1-line distinguishing detail</i>

      ❓ Какой тебя интересует? Нажми кнопку с нужным файлом
      или уточни запрос (например: «паспорт Инны», «pay-stub за май»).

═══ HARD RULES ═══

- ALWAYS process every [Document: ...] block in the context. Don't
  skip a document just because the first looks like a match — there
  may be a second relevant one further down.
- Answer ONLY what was asked. No preamble like "I found …", "В архиве …".
- HTML only: <b>, <i>, <code> — no Markdown stars or hashes.
- Bullet "•" for the single-doc case; just <i>italic</i> distinguishing
  line for the multi-doc menu.
- Max 1500 chars total."""

# Smart search thresholds
MIN_SCORE = 0.50        # Discard chunks below this
MAX_CHUNKS_LLM = 3      # Send best chunk from top N documents (was 5; trims button spam)
MAX_WORDS_PER_CHUNK = 1500  # More text for detailed answer
CACHE_TTL_SECONDS = 3600   # 1 hour


class LLMSearch:
    """RAG search with smart filtering, caching, and conversation memory."""

    def __init__(self, vector_store: VectorStore, llm: LLMRouter, db=None, cognee_client=None):
        self.vector_store = vector_store
        self.llm = llm
        self.db = db
        self.cognee = cognee_client

    @property
    def _search_system(self) -> str:
        """Get search prompt from config (editable via Settings)."""
        try:
            from app.config import get_settings
            return get_settings().llm.search_prompt
        except Exception:
            return SEARCH_SYSTEM

    async def answer(
        self,
        query: str,
        top_k: int = 5,
        history: list[dict] | None = None,
        compact: bool = False,
    ) -> dict:
        """Search documents and generate an answer. Returns {text, file_ids, cached}."""

        # Step 0: Check cache (shared across both backends).
        if self.db:
            cached = await self._get_cache(query)
            if cached:
                logger.info(f"Cache hit for: {query[:50]}")
                return {**cached, "cached": True}

        # Step 0.5: Try cognee first when configured. Graceful fallback to
        # the vector_store path if anything goes wrong, so search never
        # silently breaks.
        if (
            self.cognee is not None
            and getattr(self.cognee.config, "use_for_search", False)
            and self.cognee.healthy
        ):
            try:
                cog_result = await self._answer_via_cognee(query, top_k=top_k)
                if cog_result is not None:
                    if self.db:
                        await self._set_cache(query, cog_result)
                    return cog_result
            except Exception as e:
                logger.warning(f"cognee search failed, falling back to vector_store: {e}")

        # Step 1: Semantic search (wider net). Pull a few extra hits because
        # we'll drop anything without a matching SQLite row — vector chunks
        # without file_id (legacy note vectors, abandoned ingest experiments)
        # used to win the top slot on short queries by sheer text overlap.
        results = await self.vector_store.search(query, top_k=top_k * 2)

        # Drop hits with no file_id, or whose file row is gone from SQLite.
        # Without this a single orphan point can dominate the top-1 result
        # on a 0.69 fuzzy match while real documents at 0.63 sit below it.
        if results and self.db:
            valid = []
            for r in results:
                if not r.file_id:
                    continue
                try:
                    fr = await self.db.get_file(r.file_id)
                except Exception:
                    fr = None
                if fr:
                    valid.append(r)
            results = valid[:top_k]

        if not results:
            return {"text": "🔍 По вашему запросу ничего не найдено.", "file_ids": {}, "cached": False}

        # Step 2: Group by file, pick best chunk per file
        filtered = [r for r in results if r.score >= MIN_SCORE]
        if not filtered:
            filtered = results[:1]

        from collections import defaultdict
        by_file = defaultdict(list)
        for r in filtered:
            by_file[r.file_id].append(r)

        # Sort files by best chunk score
        file_best = []
        for fid, chunks in by_file.items():
            best = max(chunks, key=lambda c: c.score)
            file_best.append((fid, best))
        file_best.sort(key=lambda x: x[1].score, reverse=True)

        # Drop chunks that don't actually clear the score threshold once we
        # have the best per file — a vector "near match" (score ~0.5) on
        # something only loosely related (e.g. birth certificate to a
        # passport query) shouldn't take a button slot from real matches.
        # Keep the gap modest so we still surface multiple actual hits
        # (e.g. user has 2 passports, both should appear).
        if file_best:
            top_score = file_best[0][1].score
            file_best = [(fid, best) for (fid, best) in file_best
                         if best.score >= max(MIN_SCORE, top_score - 0.10)]
        seen_files = {
            fid: best.metadata.get("filename", "file")
            for fid, best in file_best[:MAX_CHUNKS_LLM]
        }

        # Step 3: Build context from top documents (use full text from DB when available)
        context_parts = []
        for fid, best in file_best[:MAX_CHUNKS_LLM]:
            fname = best.metadata.get("filename", "unknown")

            # Try to load full extracted_text from DB for richer answers
            full_text = None
            if self.db:
                try:
                    file_rec = await self.db.get_file(fid)
                    if file_rec:
                        full_text = file_rec.get("extracted_text", "")
                        fname = file_rec.get("original_name", fname)
                except Exception:
                    pass

            text_source = full_text if full_text else best.text
            words = text_source.split()
            text = " ".join(words[:MAX_WORDS_PER_CHUNK])
            context_parts.append(f"[Document: {fname}]\n{text}")

        context = "\n\n---\n\n".join(context_parts)

        # Step 4: Build prompt with optional history
        tpl = SEARCH_USER_COMPACT if compact else SEARCH_USER
        user_msg = tpl.format(context=context, query=query)

        system = self._search_system
        if history:
            history_text = "\n".join(
                f"Q: {h['q']}\nA: {h['a'][:200]}" for h in history[-3:]
            )
            system += f"\n\nPrevious conversation:\n{history_text}"

        # Step 5: Generate answer with LLM
        try:
            response = await self.llm.search_answer(
                messages=[{"role": "user", "content": user_msg}],
                system=system,
            )
            result = {"text": response.text, "file_ids": seen_files, "cached": False}

            # Save to cache
            if self.db:
                await self._set_cache(query, result)

            return result

        except Exception as e:
            logger.error(f"LLM search answer failed: {e}")
            lines = [f"🔍 Найдено по «{query}»:\n"]
            for i, r in enumerate(filtered[:3], 1):
                preview = r.text[:200] + "..." if len(r.text) > 200 else r.text
                lines.append(f"{i}. {r.metadata.get('filename', '?')}\n{preview}")
            return {"text": "\n\n".join(lines), "file_ids": seen_files, "cached": False}

    async def _answer_via_cognee(self, query: str, top_k: int) -> dict | None:
        """Route a search through cognee.recall (GRAPH_COMPLETION).

        Returns a result dict in the same shape as ``answer`` produces, or
        ``None`` if cognee gave us nothing usable (so the caller falls back
        to the vector_store path).
        """
        dataset = self.cognee.config.default_dataset
        hits = await self.cognee.recall(
            query,
            dataset=dataset,
            limit=top_k,
        )
        if not hits:
            return None

        # GRAPH_COMPLETION returns a single LLM-rendered answer in `text`.
        # We take the first hit's text; further hits (if any) are alternative
        # phrasings or scoped completions and we ignore them for now.
        first = hits[0] if isinstance(hits, list) else hits
        text = ""
        if isinstance(first, dict):
            text = first.get("text") or first.get("raw", {}).get("value") or ""
        if not text:
            return None

        # cognee owns its data ids — they don't map to FAG file UUIDs, so
        # the Telegram inline-button keyboard is empty for cognee answers.
        # The web dashboard search UI tolerates an empty file_ids dict.
        return {"text": text, "file_ids": {}, "cached": False, "backend": "cognee"}

    # ── Cache ────────────────────────────────────────────────────────

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()

    async def _get_cache(self, query: str) -> dict | None:
        if not self.db:
            return None
        qh = self._query_hash(query)
        try:
            cursor = await self.db.db.execute(
                "SELECT response, file_ids, created_at FROM search_cache WHERE query_hash=?", (qh,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            # Check TTL
            from datetime import datetime
            created = datetime.fromisoformat(row["created_at"])
            if (datetime.now() - created).total_seconds() > CACHE_TTL_SECONDS:
                await self.db.db.execute("DELETE FROM search_cache WHERE query_hash=?", (qh,))
                await self.db.db.commit()
                return None
            # Update hit counter
            await self.db.db.execute(
                "UPDATE search_cache SET hits=hits+1 WHERE query_hash=?", (qh,)
            )
            await self.db.db.commit()
            return {
                "text": row["response"],
                "file_ids": json.loads(row["file_ids"]) if row["file_ids"] else {},
            }
        except Exception as e:
            logger.debug(f"Cache read error: {e}")
            return None

    async def _set_cache(self, query: str, result: dict):
        if not self.db:
            return
        qh = self._query_hash(query)
        try:
            await self.db.db.execute(
                "INSERT OR REPLACE INTO search_cache (query_hash, query, response, file_ids) VALUES (?, ?, ?, ?)",
                (qh, query[:200], result.get("text", ""), json.dumps(result.get("file_ids", {}))),
            )
            await self.db.db.commit()
        except Exception as e:
            logger.debug(f"Cache write error: {e}")

    async def invalidate_cache(self):
        """Clear all cached search results (call when new files are added)."""
        if not self.db:
            return
        try:
            await self.db.db.execute("DELETE FROM search_cache")
            await self.db.db.commit()
            logger.info("Search cache invalidated")
        except Exception:
            pass
