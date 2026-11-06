"""Abstract Base Class for OCR engines."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List


class BaseOCREngine(ABC):
    """Abstract interface for extracting text from a PDF file."""

    @abstractmethod
    def extract_text(self, pdf_path: Path) -> List[str]:
        """Extract text from each page of the PDF.
        
        Returns:
            List[str]: A list of strings, where each element is the extracted text for that page.
        """
        pass
