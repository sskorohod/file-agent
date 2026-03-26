"""Parser factory — auto-detect file type, route to correct parser."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ParseResult
from .docx import DocxParser, TextParser
from .image import ImageParser
from .pdf import PDFParser

logger = logging.getLogger(__name__)


class ParserFactory:
    """Registry of parsers — picks the right one by file extension."""

    def __init__(self, vision_model: str | None = None):
        self.vision_model = vision_model
        self._parsers = [
            PDFParser(),
            ImageParser(),
            DocxParser(),
            TextParser(),
        ]

    def get_parser(self, file_path: Path):
        """Find a parser that can handle this file."""
        for parser in self._parsers:
            if parser.can_handle(file_path):
                return parser
        return None

    async def parse(self, file_path: Path) -> ParseResult:
        """Parse a file using the appropriate parser."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        parser = self.get_parser(path)
        if parser is None:
            logger.warning(f"No parser for {path.suffix}, returning empty result")
            return ParseResult(
                text="",
                metadata={"error": f"Unsupported file type: {path.suffix}"},
                parser_used="none",
            )

        try:
            # ImageParser needs vision_model kwarg
            if isinstance(parser, ImageParser):
                return await parser.parse(path, vision_model=self.vision_model)
            return await parser.parse(path)
        except Exception as e:
            logger.error(f"Parser {type(parser).__name__} failed on {path.name}: {e}")
            return ParseResult(
                text="",
                metadata={"error": str(e), "parser": type(parser).__name__},
                parser_used="error",
            )

    @property
    def supported_extensions(self) -> list[str]:
        exts = []
        for p in self._parsers:
            exts.extend(p.supported_extensions)
        return exts
