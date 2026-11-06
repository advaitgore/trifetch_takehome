"""RapidOCR implementation using ONNX Runtime and pypdfium2."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import numpy as np
import pypdfium2
from rapidocr_onnxruntime import RapidOCR

from linda_agent.ocr.base import BaseOCREngine

logger = logging.getLogger(__name__)


class RapidOCREngine(BaseOCREngine):
    """Local, offline, zero-binary OCR engine powered by RapidOCR (ONNX)."""

    def __init__(self):
        self.ocr = RapidOCR()

    def extract_text(self, pdf_path: Path) -> List[str]:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        doc = pypdfium2.PdfDocument(str(pdf_path))
        pages_text: List[str] = []

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            # Render page at 2x resolution for optimal OCR text detection
            pil_img = page.render(scale=2.0).to_pil()
            img_arr = np.array(pil_img)

            result, _ = self.ocr(img_arr)
            if not result:
                pages_text.append("")
                continue

            # result format is [ [bbox, text, score], ... ]
            lines = [r[1] for r in result]
            full_text = "\n".join(lines)
            
            # Normalize common full-width Unicode characters that may arise in OCR
            normalized_text = full_text.replace("\uff08", "(").replace("\uff09", ")")
            pages_text.append(normalized_text)

        return pages_text
