"""Base parser interface — common contract for all file parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ParseResult:
    """Unified result from any parser."""
    text: str
    metadata: dict = field(default_factory=dict)
    pages: int = 0
    language: str = ""
    parser_used: str = ""
    confidence: float = 1.0

    @property
    def is_empty(self) -> bool:
        return len(self.text.strip()) == 0

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def truncated(self, max_chars: int = 4000) -> str:
        """Return truncated text for LLM prompts."""
        if len(self.text) <= max_chars:
            return self.text
        return self.text[:max_chars] + f"\n\n... [truncated, {len(self.text)} total chars]"


class BaseParser(Protocol):
    """Parser protocol — implement for each file type."""

    supported_extensions: list[str]

    async def parse(self, file_path: Path) -> ParseResult:
        """Parse a file and return extracted text + metadata."""
        ...

    def can_handle(self, file_path: Path) -> bool:
        """Check if this parser can handle the file."""
        ...
