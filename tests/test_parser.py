"""Tests for file parsers."""

import pytest
from pathlib import Path

from app.parser.base import ParseResult
from app.parser.docx import TextParser
from app.parser.factory import ParserFactory


class TestParseResult:
    def test_empty(self):
        r = ParseResult(text="")
        assert r.is_empty
        assert r.word_count == 0

    def test_truncated(self):
        r = ParseResult(text="word " * 1000)
        t = r.truncated(100)
        assert len(t) < 200
        assert "truncated" in t

    def test_not_truncated(self):
        r = ParseResult(text="short text")
        assert r.truncated(1000) == "short text"


class TestTextParser:
    @pytest.mark.asyncio
    async def test_parse_txt(self, sample_text_file):
        parser = TextParser()
        assert parser.can_handle(sample_text_file)
        result = await parser.parse(sample_text_file)
        assert "medical" in result.text
        assert result.parser_used == "text"

    def test_can_handle(self):
        parser = TextParser()
        assert parser.can_handle(Path("test.txt"))
        assert parser.can_handle(Path("test.csv"))
        assert not parser.can_handle(Path("test.pdf"))


class TestParserFactory:
    def test_supported_extensions(self):
        factory = ParserFactory()
        exts = factory.supported_extensions
        assert ".pdf" in exts
        assert ".txt" in exts
        assert ".docx" in exts
        assert ".jpg" in exts

    def test_get_parser_pdf(self):
        factory = ParserFactory()
        parser = factory.get_parser(Path("test.pdf"))
        assert parser is not None

    def test_get_parser_unknown(self):
        factory = ParserFactory()
        parser = factory.get_parser(Path("test.xyz"))
        assert parser is None

    @pytest.mark.asyncio
    async def test_parse_txt(self, sample_text_file):
        factory = ParserFactory()
        result = await factory.parse(sample_text_file)
        assert not result.is_empty
        assert "medical" in result.text

    @pytest.mark.asyncio
    async def test_parse_missing(self, tmp_dir):
        factory = ParserFactory()
        with pytest.raises(FileNotFoundError):
            await factory.parse(tmp_dir / "nonexistent.txt")

    @pytest.mark.asyncio
    async def test_parse_unsupported(self, tmp_dir):
        f = tmp_dir / "test.xyz"
        f.write_text("data")
        factory = ParserFactory()
        result = await factory.parse(f)
        assert result.is_empty
