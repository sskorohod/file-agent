"""Image parser — Vision API (Gemini/GPT-4o) with Tesseract fallback."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from .base import ParseResult

logger = logging.getLogger(__name__)

VISION_PROMPT = """Analyze this image. Extract ALL text visible in the image.
If this is a document (receipt, invoice, ID, medical form, etc.), extract all fields and values.
If this is a photo, describe what you see and extract any visible text.

Return format:
EXTRACTED TEXT:
<all text found>

DESCRIPTION:
<brief description of the image content>

DOCUMENT TYPE:
<type if identifiable, e.g. receipt, invoice, passport, medical_report, photo, screenshot>
"""


class ImageParser:
    """Parse images using Vision API with Tesseract OCR fallback."""

    supported_extensions = [".png", ".jpg", ".jpeg", ".heic", ".webp", ".bmp", ".tiff"]

    def can_handle(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self.supported_extensions

    async def parse(self, file_path: Path, vision_model: str | None = None) -> ParseResult:
        # Try Vision API first
        text = ""
        metadata = {}
        parser_used = ""

        if vision_model:
            try:
                text, metadata = await self._vision_api(file_path, vision_model)
                parser_used = "vision_api"
            except Exception as e:
                logger.warning(f"Vision API failed, falling back to OCR: {e}")

        # Fallback to Tesseract
        if not text.strip():
            text = await self._tesseract_fallback(file_path)
            parser_used = "tesseract" if text.strip() else "none"

        language = self._detect_language(text)

        return ParseResult(
            text=text,
            metadata=metadata,
            pages=1,
            language=language,
            parser_used=parser_used,
        )

    async def _vision_api(self, file_path: Path, model: str) -> tuple[str, dict]:
        """Extract text using Vision API via litellm."""
        import litellm

        img_bytes = file_path.read_bytes()
        b64 = base64.b64encode(img_bytes).decode("utf-8")

        suffix = file_path.suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".heic": "image/heic",
            ".bmp": "image/bmp",
        }
        mime = mime_map.get(suffix, "image/jpeg")

        response = await litellm.acompletion(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            max_tokens=2048,
            temperature=0.1,
        )

        result_text = response.choices[0].message.content or ""

        # Parse structured output
        metadata = {
            "vision_model": model,
            "file_size": len(img_bytes),
        }
        if "DOCUMENT TYPE:" in result_text:
            doc_type = result_text.split("DOCUMENT TYPE:")[-1].strip().split("\n")[0].strip()
            metadata["document_type"] = doc_type

        # Extract the text portion
        if "EXTRACTED TEXT:" in result_text:
            text_part = result_text.split("EXTRACTED TEXT:")[1]
            if "DESCRIPTION:" in text_part:
                text_part = text_part.split("DESCRIPTION:")[0]
            text = text_part.strip()
        else:
            text = result_text

        return text, metadata

    async def _tesseract_fallback(self, file_path: Path) -> str:
        """OCR via Tesseract."""
        try:
            from PIL import Image
            import pytesseract

            img = Image.open(file_path)
            text = pytesseract.image_to_string(img, lang="eng+rus")
            return text.strip()
        except ImportError:
            logger.warning("Tesseract/PIL not available")
            return ""
        except Exception as e:
            logger.error(f"Tesseract OCR failed: {e}")
            return ""

    def _detect_language(self, text: str) -> str:
        if not text:
            return "unknown"
        sample = text[:2000]
        cyrillic = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
        latin = sum(1 for c in sample if "a" <= c.lower() <= "z")
        total = cyrillic + latin
        if total == 0:
            return "unknown"
        return "ru" if cyrillic / total > 0.5 else "en"
