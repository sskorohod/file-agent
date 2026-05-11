"""Sprint P — extract future-commitment tasks from a note's text.

Calls `gpt-5.4-mini` via the local openai-oauth proxy with a tight
modal-verb prompt. Returns a list of `Task` dicts with `description`,
`due_text`, `priority`, `confidence`, `source_span`, `rationale`.

Empty-array fast-path: if the text is short and contains no modal-verb
trigger, we skip the LLM call entirely. This keeps every voice note
from costing a token round-trip when the user just says "съел блинчики".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# RU + EN modal verbs / commitment markers. Any one of these in the text
# is enough to send it to the LLM. Pre-compiled \b-bounded regex.
_MODAL_TRIGGERS = re.compile(
    r"\b("
    r"надо|нужно|должен|должна|должны|должно|"
    r"запланир\w+|план(?:ирую|ирует|ирую\w*)?|"
    r"напомни\w*|надо\s+бы|нужно\s+бы|"
    r"завтра|послезавтра|сегодня|"
    r"купить|позвон\w+|написать|отправить|оплат\w+|"
    r"забронир\w+|записаться|встретиться|"
    r"will|should|must|need\s+to|have\s+to|gonna|"
    r"todo|to-do|remind|tomorrow|tonight|today"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


@dataclass
class ExtractedTask:
    description: str
    due_text: str = ""
    priority: str = "medium"           # high|medium|low
    confidence: str = "implicit"       # explicit|implicit
    source_span: tuple[int, int] | None = None
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "due_text": self.due_text,
            "priority": self.priority,
            "confidence": self.confidence,
            "source_span": list(self.source_span) if self.source_span else None,
            "rationale": self.rationale,
        }


_SYSTEM_PROMPT = """You extract FUTURE COMMITMENTS (tasks/reminders) from a
short note. Output ONLY a JSON array of objects with fields:
- "description": imperative ≤80 chars (e.g. "Позвонить Анне")
- "due_text": raw time phrase as found in input or "" (e.g. "завтра в 9",
  "через 2 часа", "до 9 мая 2026"). DO NOT invent dates.
- "priority": "high" | "medium" | "low"
- "confidence": "explicit" if the user clearly commits with a modal verb
  (надо/нужно/должен/will/should/must/напомни) or imperative; "implicit"
  if it's a vague aspirational mention.
- "source_span": [start, end] character offsets into the note text
- "rationale": one short clause why this is a task (modal verb, deadline)

Rules:
- ONLY future commitments. NEVER past-tense ("я звонил", "I called").
- NEVER feelings/observations ("устал", "блинчики вкусные").
- If no tasks → output []
- Output JSON ONLY. No markdown fences. No prose.
"""


class TaskExtractor:
    def __init__(self,
                 model: str = "openai/gpt-5.4-mini",
                 api_base: str = "http://127.0.0.1:10531/v1"):
        self.model = model
        self.api_base = api_base

    async def extract(self, text: str, language: str = "ru") -> list[ExtractedTask]:
        if not text:
            return []
        clipped = text.strip()
        if len(clipped) < 4:
            return []
        # Fast-path: short text without any modal trigger almost never
        # carries a task. Saves ~159 LLM calls on a normal day.
        word_count = len(clipped.split())
        if word_count < 30 and not _MODAL_TRIGGERS.search(clipped):
            return []
        return await self._llm_extract(clipped[:3500], language)

    async def _llm_extract(self, text: str, language: str) -> list[ExtractedTask]:
        import litellm
        try:
            resp = await asyncio.wait_for(
                litellm.acompletion(
                    model=self.model,
                    api_base=self.api_base,
                    api_key="dummy",
                    max_tokens=700,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                ),
                timeout=20.0,
            )
            body = resp.choices[0].message.content.strip()
            if body.startswith("```"):
                body = body.split("```", 2)[1].lstrip("json").strip()
            data = json.loads(body)
        except Exception as exc:
            logger.warning(f"task LLM extraction failed: {exc}")
            return []
        if not isinstance(data, list):
            return []
        out: list[ExtractedTask] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            desc = (d.get("description") or "").strip()
            if not desc:
                continue
            span = d.get("source_span")
            try:
                span_t = (int(span[0]), int(span[1])) if span and len(span) == 2 else None
            except Exception:
                span_t = None
            out.append(ExtractedTask(
                description=desc[:200],
                due_text=(d.get("due_text") or "").strip()[:120],
                priority=(d.get("priority") or "medium")
                    if d.get("priority") in {"high", "medium", "low"} else "medium",
                confidence=(d.get("confidence") or "implicit")
                    if d.get("confidence") in {"explicit", "implicit"} else "implicit",
                source_span=span_t,
                rationale=(d.get("rationale") or "")[:200],
            ))
        return out


# Module-level convenience for callers that don't want to manage instances.
_default: Optional[TaskExtractor] = None


async def extract_tasks(text: str, language: str = "ru") -> list[ExtractedTask]:
    global _default
    if _default is None:
        _default = TaskExtractor()
    return await _default.extract(text, language)
