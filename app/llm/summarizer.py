"""File Summarizer — dedicated summary generation with richer context than classifier."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = """Ты — ассистент для создания описаний документов.

Напиши краткое описание документа (2-4 предложения на русском):
- Что это за документ
- О чём он (основная тема)
- Для чего нужен / чем полезен

Контекст:
- Файл: {filename}
- Тип: {document_type}
- Категория: {category}
- Длина текста: {text_length} символов

Правила:
- Только факты из текста, не выдумывай
- Для гайдов/мануалов: указывай тему и практическое назначение
- Для счетов/чеков: суммы, даты, контрагенты
- Для медицинских: тип анализа, ключевые показатели
- Не начинай с 'Данный документ' или 'Этот документ'"""


@dataclass
class SummaryContext:
    filename: str
    document_type: str
    category: str
    text_excerpt: str
    text_length: int


@dataclass
class SummaryResult:
    summary: str
    model: str
    context_chars: int
    latency_ms: int


def build_summary_context(text: str) -> str:
    """Prepare text excerpt based on document length.

    Strategy:
    - <= 4000 chars: full text
    - <= 12000 chars: first 6000 + last 2000
    - > 12000 chars: first 6000 + middle 2000 + last 2000
    """
    if not text or not text.strip():
        return ""

    length = len(text)

    if length <= 4000:
        return text

    if length <= 12000:
        return text[:6000] + "\n\n[...]\n\n" + text[-2000:]

    mid_start = length // 2 - 1000
    return (
        text[:6000]
        + "\n\n[...]\n\n"
        + text[mid_start:mid_start + 2000]
        + "\n\n[...]\n\n"
        + text[-2000:]
    )


class FileSummarizer:
    """Generate human-readable file summaries via LLM with richer context."""

    def __init__(self, llm_router, prompt_template: str = ""):
        self.llm = llm_router
        self._prompt = prompt_template or _DEFAULT_PROMPT

    async def summarize(self, context: SummaryContext) -> SummaryResult:
        """Generate summary. Raises on LLM failure (caller handles as non-fatal)."""
        start = time.monotonic()

        system = self._prompt.format(
            filename=context.filename,
            document_type=context.document_type,
            category=context.category,
            text_length=context.text_length,
        )

        messages = [{"role": "user", "content": context.text_excerpt}]

        response = await self.llm.complete(
            role="analysis",
            messages=messages,
            system=system,
            max_tokens=512,
            temperature=0.1,
        )

        summary = response.text.strip()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        model = response.model if hasattr(response, "model") else ""

        logger.info(
            f"Summary generated: {len(summary)} chars, "
            f"{len(context.text_excerpt)} context chars, {elapsed_ms}ms"
        )

        return SummaryResult(
            summary=summary,
            model=model,
            context_chars=len(context.text_excerpt),
            latency_ms=elapsed_ms,
        )
