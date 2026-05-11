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

        # Step 0.3: Pre-check tag short-circuit before cognee. If the user
        # is clearly asking for a specific document type ("ssn", "паспорт",
        # "виза"), we want to surface the FILE with clickable buttons,
        # not a cognee dictionary definition without file_ids.
        likely_doc_query = False
        if self.db and 1 <= len(query.strip().split()) <= 3:
            ql = query.strip().lower()
            # 1. Literal tag match.
            try:
                _probe = await self.db.tag_search_files(ql, limit=1)
                likely_doc_query = bool(_probe)
            except Exception:
                pass
            # 2. Hard-coded synonyms + plural variants.
            if not likely_doc_query:
                known_doc_terms = {"ssn", "паспорт", "passport", "visa", "виза",
                                   "ead", "w-9", "w9", "i-94", "i94", "tax", "налог",
                                   "biometric", "biometrics", "uscis", "invoice",
                                   "счёт", "приём", "appointment",
                                   "driver license", "права", "социальное страхование"}
                likely_doc_query = ql in known_doc_terms
            # 3. Last-resort: FTS hit on the name/tag/summary path. If the
            # user typed a literal word from a file we own, this is a doc
            # lookup, not a "what is X" question for cognee.
            if not likely_doc_query:
                try:
                    _fts = await self.db.search_files(ql, limit=1)
                    if _fts:
                        # Confirm the hit is name/tag/summary, not just
                        # extracted body text (the body matches almost
                        # anything for English queries).
                        row = _fts[0]
                        name = (row.get("original_name") or "").lower()
                        tags = (row.get("tags") or "").lower()
                        summary = (row.get("summary") or "").lower()
                        if any(tok in name or tok in tags or tok in summary
                               for tok in ql.split() if len(tok) >= 3):
                            likely_doc_query = True
                except Exception:
                    pass

        # Step 0.5: Try cognee first when configured AND the query isn't
        # clearly a file lookup. Graceful fallback to vector_store path
        # if anything goes wrong, so search never silently breaks.
        if (
            self.cognee is not None
            and getattr(self.cognee.config, "use_for_search", False)
            and self.cognee.healthy
            and not likely_doc_query
        ):
            try:
                cog_result = await self._answer_via_cognee(query, top_k=top_k)
                if cog_result is not None:
                    if self.db:
                        await self._set_cache(query, cog_result)
                    return cog_result
            except Exception as e:
                logger.warning(f"cognee search failed, falling back to vector_store: {e}")

        # Step 0.7: Tag short-circuit. When the user types a single-word
        # query that's clearly a tag (ssn, passport, паспорт, w-9), pull
        # those files via JSON LIKE on `files.tags`. Vector + FTS still
        # run after, but starting with the exact-tag set means image-only
        # documents (where OCR fails) can't drop off the result list.
        tag_hits: list = []
        q_clean = query.strip().lower()
        if self.db and 1 <= len(q_clean.split()) <= 3 and len(q_clean) <= 40:
            # Map common synonyms to the canonical English tags used by
            # the classifier prompt.
            tag_aliases = {
                "паспорт": "passport",
                "passport": "passport",
                "ssn": "ssn",
                "соц страх": "ssn",
                "социальное страхование": "ssn",
                "номер социального страхования": "ssn",
                "виза": "visa",
                "visa": "visa",
                "налог": "tax",
                "tax": "tax",
                "налоги": "tax",
                "tin": "tin",
                "w-9": "w-9",
                "w9": "w-9",
                "ead": "employment-authorization",
                "i-94": "i-94",
                "i94": "i-94",
                "uscis": "uscis",
                "права": "driver-license",
                "driver license": "driver-license",
                "водительск": "driver-license",
            }
            target_tag = tag_aliases.get(q_clean, q_clean)
            try:
                tag_hits = await self.db.tag_search_files(target_tag, limit=top_k)
            except Exception as exc:
                logger.debug(f"tag short-circuit failed: {exc}")
                tag_hits = []

        # Step 1: Semantic search (wider net). Pull a few extra hits because
        # we'll drop anything without a real source row — vector chunks
        # whose payload doesn't tie back to either a file or a note are
        # garbage from old ingest experiments and can dominate top-1.
        results = await self.vector_store.search(query, top_k=top_k * 2)

        # Step 1.4: Splice tag short-circuit results in at a high score
        # (0.85) so they always beat weak vector near-matches.
        # For files already present in the vector results, BOOST their
        # score instead of duplicating — otherwise the score-prune step
        # would discard them in favour of unrelated high-score note hits
        # (the classic "Паспорт"-titled note beating the actual passport
        # PDF problem).
        if tag_hits:
            from app.storage.vectors import SearchResult
            by_file: dict[str, "SearchResult"] = {
                r.file_id: r for r in results
                if getattr(r, "file_id", "")
                and not (r.metadata or {}).get("type") == "note"
            }
            for row in tag_hits:
                fid = row.get("id") or ""
                if not fid:
                    continue
                if fid in by_file:
                    existing = by_file[fid]
                    if existing.score < 0.85:
                        existing.score = 0.85
                        existing.metadata = {
                            **(existing.metadata or {}),
                            "via": "tag",
                        }
                    continue
                results.append(SearchResult(
                    file_id=fid,
                    chunk_index=-4,
                    text=(row.get("summary") or row.get("original_name") or "")[:400],
                    score=0.85,
                    metadata={
                        "type": "file",
                        "original_name": row.get("original_name", ""),
                        "via": "tag",
                    },
                ))
            results.sort(key=lambda r: r.score, reverse=True)

        # Step 1.5: Hybrid — add FTS5 keyword hits.
        # Even with Gemini embeddings, image-only files (passports, SSN
        # cards) often have garbled OCR and miss vector queries entirely.
        # The keyword path catches them via tags / original_name / summary.
        # We splice synthetic SearchResult rows in at scores tuned so a
        # strong FTS hit on tags beats a weak vector near-match.
        if self.db:
            try:
                fts_rows = await self.db.search_files(query, limit=top_k * 2)
            except Exception as exc:
                logger.debug(f"FTS search skipped: {exc}")
                fts_rows = []
            if fts_rows:
                from app.storage.vectors import SearchResult
                by_file: dict[str, "SearchResult"] = {
                    r.file_id: r for r in results
                    if getattr(r, "file_id", "")
                    and not (r.metadata or {}).get("type") == "note"
                }
                # bm25 returns *negative* numbers (lower is better).
                # Convert via 1/(1+|bm25|) → (0, 1]; first row is strongest.
                for i, row in enumerate(fts_rows):
                    fid = row.get("id") or ""
                    if not fid:
                        continue
                    bm25 = abs(float(row.get("bm25_score") or i + 1))
                    fts_score = 1.0 / (1.0 + bm25 * 0.5)
                    # Tag-or-name hits get a bonus: the user typed a literal
                    # word that's in the filename or tag JSON, that's a
                    # strong signal regardless of vector distance.
                    q_low = query.lower()
                    name_l = (row.get("original_name") or "").lower()
                    tags_l = (row.get("tags") or "").lower()
                    if any(tok in name_l or tok in tags_l
                           for tok in q_low.split() if len(tok) >= 2):
                        fts_score = max(fts_score, 0.78)
                    if fid in by_file:
                        existing = by_file[fid]
                        if existing.score < fts_score:
                            existing.score = fts_score
                            existing.metadata = {
                                **(existing.metadata or {}),
                                "via": "fts",
                            }
                        continue
                    snippet = (row.get("summary") or
                               row.get("extracted_text") or "")[:400]
                    results.append(SearchResult(
                        file_id=fid,
                        chunk_index=-2,
                        text=snippet,
                        score=fts_score,
                        metadata={
                            "type": "file",
                            "original_name": row.get("original_name", ""),
                            "via": "fts",
                        },
                    ))
                # Re-sort by score so the merged pool keeps the right order.
                results.sort(key=lambda r: r.score, reverse=True)

        # Drop hits whose payload has no sane reference back to SQLite.
        # Two valid kinds:
        #   • file chunks: payload.type missing or 'file', `file_id` matches
        #     a row in the `files` table (the document path)
        #   • note chunks: payload.type == 'note', `note_id` matches a row
        #     in the `notes` table (the transcript path; PR #11 onward)
        if results and self.db:
            valid = []
            for r in results:
                pay = getattr(r, "metadata", {}) or {}
                rtype = (pay.get("type") or "").lower()
                if rtype == "note":
                    note_id = pay.get("note_id")
                    if not note_id:
                        continue
                    try:
                        cur = await self.db.db.execute(
                            "SELECT 1 FROM notes WHERE id=?", (note_id,)
                        )
                        if await cur.fetchone():
                            valid.append(r)
                    except Exception:
                        pass
                else:
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
            return {
                "text": "🔍 По вашему запросу ничего не найдено.",
                "file_ids": {}, "note_ids": {}, "cached": False,
            }

        # Step 2: Group by *source* (file or note), pick best chunk per
        # source. Note chunks have an empty file_id but a note_id in
        # payload; without this split every note collapsed into the
        # same "" bucket.
        filtered = [r for r in results if r.score >= MIN_SCORE]
        if not filtered:
            filtered = results[:1]

        from collections import defaultdict

        def _key(r):
            pay = r.metadata or {}
            if (pay.get("type") or "").lower() == "note":
                return ("note", str(pay.get("note_id") or ""))
            return ("file", r.file_id)

        by_src = defaultdict(list)
        for r in filtered:
            by_src[_key(r)].append(r)

        # Sort sources by best chunk score
        file_best = []
        for src_key, chunks in by_src.items():
            best = max(chunks, key=lambda c: c.score)
            file_best.append((src_key, best))
        file_best.sort(key=lambda x: x[1].score, reverse=True)

        # Drop chunks that don't actually clear the score threshold once we
        # have the best per file — a vector "near match" (score ~0.5) on
        # something only loosely related (e.g. birth certificate to a
        # passport query) shouldn't take a button slot.
        # Exception: keep any source whose match came via tag/FTS path
        # (chunk_index < -1) even if the absolute score is a touch lower
        # than the top vector hit — those carry the strongest signal that
        # the user wants THIS particular document.
        if file_best:
            top_score = file_best[0][1].score
            score_floor = max(MIN_SCORE, top_score - 0.10)
            file_best = [(fid, best) for (fid, best) in file_best
                         if best.score >= score_floor
                            or best.chunk_index in (-3, -4)
                            or best.metadata.get("via") in ("tag", "fts")]

        # Document-type-aware narrowing. When the user asks for a specific
        # document type ("найди паспорт", "pay stub", "W-9"), keep ONLY files
        # whose `metadata.document_type` matches that intent. Stops things
        # like the I-94 record or birth certificate from leaking into a
        # passport answer just because they share PII / immigration context.
        # Multi-passport / multi-payslip cases still surface (we filter by
        # type, not by uniqueness).
        TYPE_KEYWORDS = {
            "passport":           ("passport", "паспорт", "загранпасп"),
            "driver_license":     ("driver license", "водительск", "права",
                                   "driving licen", "dl"),
            "birth_certificate":  ("birth certif", "свидетельств", "о рожден"),
            "i94_record":         ("i-94", "i94"),
            "pay_stub":           ("pay stub", "pay-stub", "payslip",
                                   "расчётны", "расчетны", "зарпл"),
            "tax_form":           ("w-9", "w9", "w-2", "w2", "1099", "налогов"),
            "social_security_card": ("ssn", "social security card",
                                     "номер социального страхования",
                                     "снилс"),
            "ead": ("ead", "employment authorization"),
            "vehicle_registration": ("vehicle reg", "регистрация автомоб",
                                     "регистрация машин", "техпаспорт"),
            "lab_result": ("lab result", "анализ", "лабораторн"),
            "after_visit_summary": ("after visit", "выписк", "после визит"),
        }

        async def _file_doc_type(fid: str) -> str:
            if not self.db:
                return ""
            try:
                fr = await self.db.get_file(fid)
            except Exception:
                return ""
            if not fr:
                return ""
            try:
                meta = json.loads(fr.get("metadata_json") or "{}")
            except Exception:
                meta = {}
            return (meta.get("document_type") or "").lower()

        ql = query.strip().lower()
        wanted_types: set[str] = set()
        for dt, kws in TYPE_KEYWORDS.items():
            if any(kw in ql for kw in kws):
                wanted_types.add(dt)
        # Driver-license + passport requests also tolerate id_card matches
        # because users often photograph their EAD/SSN/DL all at once.
        # But we keep it strict for passport — that's the user's complaint.

        if wanted_types:
            narrowed: list = []
            for src_key, best in file_best:
                kind, sid = src_key
                if kind != "file":
                    # Notes don't carry a document_type — keep them only
                    # if the query was generic. For type-specific queries,
                    # docs must dominate.
                    continue
                dt = await _file_doc_type(sid)
                if dt in wanted_types or any(w in dt for w in wanted_types):
                    narrowed.append((src_key, best))
            # Only apply the narrow filter if it leaves at least one match —
            # otherwise we'd return an empty answer when the query keyword
            # doesn't perfectly line up with any stored document_type.
            if narrowed:
                file_best = narrowed
                logger.info(
                    f"document_type filter: kept {len(narrowed)} matching "
                    f"types {wanted_types}"
                )

        seen_files: dict[str, str] = {}
        seen_notes: dict[str, str] = {}
        for src_key, best in file_best[:MAX_CHUNKS_LLM]:
            kind, sid = src_key
            if kind == "note":
                title = best.metadata.get("title") or ""
                seen_notes[str(sid)] = title or best.text[:50]
            else:
                seen_files[sid] = (
                    best.metadata.get("filename")
                    or best.metadata.get("original_name")
                    or "file"
                )

        # Step 3: Build context, pulling full text from the right DB table
        context_parts = []
        for src_key, best in file_best[:MAX_CHUNKS_LLM]:
            kind, sid = src_key
            full_text = None
            display = ""

            if kind == "note" and self.db:
                try:
                    cur = await self.db.db.execute(
                        "SELECT title, content FROM notes WHERE id=?",
                        (int(sid),),
                    )
                    row = await cur.fetchone()
                    if row:
                        rd = dict(row)
                        full_text = rd.get("content") or ""
                        title = (rd.get("title") or "").strip()
                        display = title or "Note"
                except Exception:
                    pass
                marker = "Note"
            else:
                fname = best.metadata.get("filename", "unknown")
                if self.db:
                    try:
                        file_rec = await self.db.get_file(sid)
                        if file_rec:
                            full_text = file_rec.get("extracted_text", "")
                            fname = file_rec.get("original_name", fname)
                    except Exception:
                        pass
                display = fname
                marker = "Document"

            text_source = full_text if full_text else best.text
            words = (text_source or "").split()
            text = " ".join(words[:MAX_WORDS_PER_CHUNK])
            context_parts.append(f"[{marker}: {display}]\n{text}")

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
            result = {
                "text": response.text,
                "file_ids": seen_files,
                "note_ids": seen_notes,
                "cached": False,
            }

            # Save to cache
            if self.db:
                await self._set_cache(query, result)

            return result

        except Exception as e:
            logger.error(f"LLM search answer failed: {e}")
            lines = [f"🔍 Найдено по «{query}»:\n"]
            for i, r in enumerate(filtered[:3], 1):
                preview = r.text[:200] + "..." if len(r.text) > 200 else r.text
                lines.append(f"{i}. {r.metadata.get('filename', r.metadata.get('title', '?'))}\n{preview}")
            return {
                "text": "\n\n".join(lines),
                "file_ids": seen_files,
                "note_ids": seen_notes,
                "cached": False,
            }

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

        # Cognee likes to return confident "I don't see X in the graph"
        # answers even when the actual document is sitting in SQLite +
        # Qdrant. That tanked /search ssn for months: cognee said "no SSN
        # mentioned" → we returned that → user never saw the SSN card.
        # If the answer reads like a denial, fall back to vector search.
        low = text.lower()
        denial_markers = (
            "no ssn", "no mention", "is not mentioned", "no information",
            "no documents", "not found", "could not find",
            "не упомин", "не найден", "нет информации",
            "no explicit", "не содержит", "отсутству",
        )
        if any(m in low for m in denial_markers):
            logger.info(f"cognee returned denial for {query!r} — falling back to vector")
            return None

        # Cognee dictionary-style answers (a generic definition of the
        # query term, no reference to actual user data) are also useless
        # — the user wants their document, not Wikipedia. Heuristic:
        # answer starts with the bolded query and lacks any FAG-specific
        # signal like a file name, date, or person.
        q_low = query.lower().strip()
        if (text.lower().startswith(f"**{q_low}") or
                text.lower().startswith(f"a **{q_low}") or
                text.lower().startswith(f"the **{q_low}")):
            if not any(s in low for s in (".pdf", ".jpg", "20", "номер", "card", "passport id")):
                logger.info(f"cognee returned dictionary def for {query!r} — falling back")
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
