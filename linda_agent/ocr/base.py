"""Abstract Base Class for OCR engines."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class PageOCR:
    """OCR output for a single PDF page.

    ``min_conf`` / ``mean_conf`` are None when the engine does not report
    confidences (e.g. the Tesseract adapter as used here).
    """

    text: str
    min_conf: Optional[float] = None
    mean_conf: Optional[float] = None
    line_count: int = 0


class BaseOCREngine(ABC):
    """Abstract interface for extracting text from a PDF file."""

    @abstractmethod
    def extract_text(self, pdf_path: Path) -> List[str]:
        """Extract text from each page of the PDF.

        Returns:
            List[str]: A list of strings, where each element is the extracted text for that page.
        """

    def extract_pages(self, pdf_path: Path) -> List[PageOCR]:
        """Extract text plus per-page confidence. Engines without scores return None confidences."""
        return [PageOCR(text=t, line_count=len([l for l in t.splitlines() if l.strip()]))
                for t in self.extract_text(pdf_path)]
