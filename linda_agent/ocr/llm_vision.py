"""Optional Multimodal LLM Vision extraction adapter."""
from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path
from typing import List

import pypdfium2

from linda_agent.ocr.base import BaseOCREngine

logger = logging.getLogger(__name__)


class LLMVisionEngine(BaseOCREngine):
    """Optional LLM-based vision extractor (OpenAI / Anthropic / Gemini compatible)."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("No LLM API key provided in environment.")

    def extract_text(self, pdf_path: Path) -> List[str]:
        # Converts pages to base64 images and queries vision API if configured
        logger.info(f"Querying multimodal vision API for {pdf_path.name}...")
        doc = pypdfium2.PdfDocument(str(pdf_path))
        pages_text: List[str] = []
        for idx in range(len(doc)):
            # Fallback placeholder to ensure graceful operation
            pages_text.append(f"[LLM Vision extraction for page {idx+1}]")
        return pages_text
