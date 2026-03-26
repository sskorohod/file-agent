"""PDF parser — PyMuPDF with Tesseract OCR fallback."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ParseResult

logger = logging.getLogger(__name__)


class PDFParser:
    """Parse PDF files using PyMuPDF, with OCR fallback for scanned documents."""

    supported_extensions = [".pdf"]

    def can_handle(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.supported_extensions

    async def parse(self, file_path: Path) -> ParseResult:
        import fitz  # PyMuPDF

        doc = fitz.open(str(file_path))
        metadata = self._extract_metadata(doc)
        pages = len(doc)

        # Try text extraction first
        text_parts = []
        empty_pages = 0
        for page in doc:
            page_text = page.get_text("text").strip()
            if page_text:
                text_parts.append(page_text)
            else:
                empty_pages += 1

        text = "\n\n".join(text_parts)

        # If >50% pages are empty, likely a scanned doc — try OCR
        if pages > 0 and empty_pages / pages > 0.5:
            logger.info(f"PDF appears scanned ({empty_pages}/{pages} empty). Trying OCR...")
            ocr_text = await self._ocr_fallback(file_path)
            if ocr_text and len(ocr_text) > len(text):
                text = ocr_text
                metadata["ocr_used"] = True

        doc.close()

        language = self._detect_language(text)

        return ParseResult(
            text=text,
            metadata=metadata,
            pages=pages,
            language=language,
            parser_used="pymupdf" + ("+tesseract" if metadata.get("ocr_used") else ""),
        )

    def _extract_metadata(self, doc) -> dict:
        meta = doc.metadata or {}
        return {
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "subject": meta.get("subject", ""),
            "creator": meta.get("creator", ""),
            "creation_date": meta.get("creationDate", ""),
            "mod_date": meta.get("modDate", ""),
            "page_count": len(doc),
        }

    async def _ocr_fallback(self, file_path: Path) -> str:
        """OCR via Tesseract on rasterized pages."""
        try:
            import fitz
            from PIL import Image
            import pytesseract
            import io

            doc = fitz.open(str(file_path))
            ocr_parts = []

            for i, page in enumerate(doc):
                if i >= 20:  # Limit OCR to first 20 pages
                    break
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                page_text = pytesseract.image_to_string(img, lang="eng+rus")
                if page_text.strip():
                    ocr_parts.append(page_text.strip())

            doc.close()
            return "\n\n".join(ocr_parts)

        except ImportError:
            logger.warning("Tesseract/PIL not available for OCR fallback")
            return ""
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return ""

    def _detect_language(self, text: str) -> str:
        """Simple language detection based on character analysis."""
        if not text:
            return "unknown"
        sample = text[:2000]
        cyrillic = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
        latin = sum(1 for c in sample if "a" <= c.lower() <= "z")
        total = cyrillic + latin
        if total == 0:
            return "unknown"
        if cyrillic / total > 0.5:
            return "ru"
        return "en"
