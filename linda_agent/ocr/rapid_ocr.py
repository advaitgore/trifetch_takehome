"""RapidOCR implementation using ONNX Runtime and pypdfium2."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import numpy as np
import pypdfium2
from rapidocr_onnxruntime import RapidOCR

from linda_agent.ocr.base import BaseOCREngine, PageOCR

logger = logging.getLogger(__name__)


class RapidOCREngine(BaseOCREngine):
    """Local, offline, zero-binary OCR engine powered by RapidOCR (ONNX)."""

    def __init__(self):
        self.ocr = RapidOCR()

    def extract_pages(self, pdf_path: Path) -> List[PageOCR]:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        doc = pypdfium2.PdfDocument(str(pdf_path))
        pages: List[PageOCR] = []

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            # Render page at 2x resolution for optimal OCR text detection
            pil_img = page.render(scale=2.0).to_pil()
            img_arr = np.array(pil_img)

            result, _ = self.ocr(img_arr)
            if not result:
                # A page with no recognised text is an evidence gap, not an empty success.
                pages.append(PageOCR(text="", min_conf=0.0, mean_conf=0.0, line_count=0))
                continue

            # result format is [ [bbox, text, score], ... ]
            lines = [r[1] for r in result]
            scores = [float(r[2]) for r in result]
            full_text = "\n".join(lines)

            # Normalize common full-width Unicode characters that may arise in OCR
            normalized_text = full_text.replace("\uff08", "(").replace("\uff09", ")")
            pages.append(
                PageOCR(
                    text=normalized_text,
                    min_conf=min(scores),
                    mean_conf=sum(scores) / len(scores),
                    line_count=len(lines),
                )
            )

        return pages

    def extract_text(self, pdf_path: Path) -> List[str]:
        return [p.text for p in self.extract_pages(pdf_path)]
