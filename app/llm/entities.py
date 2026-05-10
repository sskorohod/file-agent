"""LLM-driven entity extraction.

Sprint G replacement for the regex-based ``extract_entities`` in
``scripts/build_wiki.py``. The regex caught false positives like
"После Визита" and couldn't fold "Скороход Вячеслав" /
"Vyacheslav Skorokhod" / "Слава" into one canonical entity.

This module:

* hits the local openai-oauth proxy (`gpt-5.4-mini`) with a small
  prompt asking for `[{name, kind, aliases[]}]`
* normalises results through the SQLite ``entity_aliases`` table so a
  rerun returns the same canonical names
* caches the per-source result keyed by content sha256 so a noop
  rebuild costs ~zero LLM tokens

The expected callsite is ``scripts/build_wiki.py`` walking every
file/note row and pasting the result into the YAML frontmatter +
backlinks. A real cognee-driven entity layer (entire graph instead of
per-source extraction) is the bigger plan in
docs/memory-audit-2026-05-10.md and remains out of scope here — this
module bridges us until cognee gets a usable entity-recall API.

Public API:

    extractor = EntityExtractor(db)
    entities = await extractor.extract(text, content_hash=sha256)
    # entities: list[Entity(name, kind)] — already alias-resolved.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """Ты выделяешь сущности из персональных заметок и
описаний документов. Возвращай ТОЛЬКО JSON-список объектов:

  [{"name": "...", "kind": "person|org|place|topic", "aliases": ["..."]}, ...]

Правила:
- Бери только реальные сущности: имена людей (полные ФИО или часть),
  компании / организации (USCIS, IRS, DMV, Kaiser Permanente, FAG),
  места (страны, города, штаты), темы (passport, MRI, immigration,
  pay stub, CRM).
- НЕ бери действия, прилагательные, фразы из служебной разметки
  ("После Визита", "Тренировка нет").
- Группируй варианты одного человека/орг в одну запись через
  `aliases` (например `name: "Vyacheslav Skorokhod"`,
  `aliases: ["Скороход Вячеслав", "Слава", "Viacheslav"]`).
- Игнорируй обобщённые слова без идентификации ("друг", "клиент"
  без имени, "человек").
- Максимум 12 сущностей на одну заметку. Если меньше — окей.

Если сущностей нет — верни []."""


@dataclass(frozen=True)
class Entity:
    name: str         # canonical name as it appears in entity_aliases
    kind: str         # person / org / place / topic
    confidence: float = 1.0


class EntityExtractor:
    """Extract + resolve entities via the proxy + ``entity_aliases``."""

    def __init__(self, db, model: str = "openai/gpt-5.4-mini",
                 api_base: str = "http://127.0.0.1:10531/v1"):
        self.db = db
        self.model = model
        self.api_base = api_base

    async def extract(
        self, text: str, *, content_hash: str | None = None,
    ) -> list[Entity]:
        if not text or not text.strip():
            return []

        h = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = await self._cache_get(h)
        if cached is not None:
            return cached

        raw = await self._llm_extract(text)
        canonical = await self._canonicalise(raw)
        await self._cache_put(h, canonical)
        return canonical

    # ── LLM call ─────────────────────────────────────────────────────

    async def _llm_extract(self, text: str) -> list[dict]:
        """Async LLM call. Uses litellm.acompletion (not the sync
        completion) so concurrent extractions don't block the event
        loop and tower hours of wall time. Includes a hard 20-second
        deadline per call — slow responses just degrade to regex."""
        import asyncio as _asyncio
        import litellm
        clipped = text[:3500]
        try:
            resp = await _asyncio.wait_for(
                litellm.acompletion(
                    model=self.model,
                    api_base=self.api_base,
                    api_key="dummy",
                    max_tokens=600,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": clipped},
                    ],
                ),
                timeout=20.0,
            )
            body = resp.choices[0].message.content.strip()
            if body.startswith("```"):
                body = body.split("```")[1].lstrip("json").strip()
            data = json.loads(body)
        except Exception as exc:
            logger.warning(f"entity LLM extraction failed: {exc}")
            return []
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict) and d.get("name")]

    # ── Alias resolution via ``entity_aliases`` ──────────────────────

    async def _canonicalise(self, raw: list[dict]) -> list[Entity]:
        """For each LLM-suggested entity, look up an existing canonical
        in ``entity_aliases`` (or upsert one). Returns deduplicated list."""
        seen: dict[str, Entity] = {}
        for d in raw:
            name = (d.get("name") or "").strip()
            kind = (d.get("kind") or "topic").strip().lower()
            if not name or len(name) > 80:
                continue
            aliases = [a.strip() for a in d.get("aliases", [])
                       if isinstance(a, str) and 0 < len(a) <= 80]

            canonical = await self._find_canonical(kind, [name, *aliases])
            if not canonical:
                canonical = name
                # remember the new canonical and all suggested aliases
                for a in {name, *aliases}:
                    try:
                        await self.db.db.execute(
                            "INSERT OR IGNORE INTO entity_aliases "
                            "(entity_type, alias, canonical_value) "
                            "VALUES (?, ?, ?)",
                            (kind, a, canonical),
                        )
                    except Exception:
                        pass
                try:
                    await self.db.db.commit()
                except Exception:
                    pass
            else:
                # ensure the alias side is also recorded
                for a in aliases:
                    try:
                        await self.db.db.execute(
                            "INSERT OR IGNORE INTO entity_aliases "
                            "(entity_type, alias, canonical_value) "
                            "VALUES (?, ?, ?)",
                            (kind, a, canonical),
                        )
                    except Exception:
                        pass
                try:
                    await self.db.db.commit()
                except Exception:
                    pass

            seen.setdefault(
                canonical.lower(),
                Entity(name=canonical, kind=kind,
                       confidence=float(d.get("confidence") or 0.9)),
            )
        return list(seen.values())

    async def _find_canonical(self, kind: str, names: list[str]) -> str | None:
        if not names:
            return None
        placeholders = ",".join("?" * len(names))
        cur = await self.db.db.execute(
            f"SELECT canonical_value FROM entity_aliases "
            f"WHERE entity_type=? AND alias IN ({placeholders}) "
            f"COLLATE NOCASE LIMIT 1",
            (kind, *names),
        )
        row = await cur.fetchone()
        return dict(row).get("canonical_value") if row else None

    # ── tiny content-hash cache (in entity_aliases by side-channel) ──
    # Stored as entity_type='_cache', alias=hash, canonical_value=JSON.
    # Keeps one table; not worth a new schema for this.

    async def _cache_get(self, content_hash: str) -> list[Entity] | None:
        try:
            cur = await self.db.db.execute(
                "SELECT canonical_value FROM entity_aliases "
                "WHERE entity_type='_entity_cache' AND alias=?",
                (content_hash,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            data = json.loads(dict(row).get("canonical_value", "[]"))
            return [Entity(**e) for e in data]
        except Exception:
            return None

    async def _cache_put(self, content_hash: str, entities: list[Entity]):
        try:
            payload = json.dumps(
                [{"name": e.name, "kind": e.kind, "confidence": e.confidence}
                 for e in entities],
                ensure_ascii=False,
            )
            await self.db.db.execute(
                "INSERT OR REPLACE INTO entity_aliases "
                "(entity_type, alias, canonical_value) VALUES "
                "('_entity_cache', ?, ?)",
                (content_hash, payload),
            )
            await self.db.db.commit()
        except Exception:
            pass
