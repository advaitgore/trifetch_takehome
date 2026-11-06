"""PyTesseract engine fallback adapter."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pypdfium2

from linda_agent.ocr.base import BaseOCREngine

logger = logging.getLogger(__name__)


class TesseractOCREngine(BaseOCREngine):
    """Optional PyTesseract OCR engine."""

    def __init__(self):
        try:
            import pytesseract
            self.pytesseract = pytesseract
        except ImportError:
            raise ImportError(
                "pytesseract is not installed. Run `pip install pytesseract` "
                "and ensure Tesseract OCR is installed on your OS PATH."
            )

    def extract_text(self, pdf_path: Path) -> List[str]:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        doc = pypdfium2.PdfDocument(str(pdf_path))
        pages_text: List[str] = []

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pil_img = page.render(scale=2.0).to_pil()
            text = self.pytesseract.image_to_string(pil_img)
            pages_text.append(text.strip())

        return pages_text
