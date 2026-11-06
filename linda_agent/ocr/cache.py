"""Content-addressed OCR cache so reruns and resumes never re-OCR an unchanged PDF."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import List

from linda_agent.ocr.base import BaseOCREngine, PageOCR

logger = logging.getLogger(__name__)


class CachedOCR:
    """Wraps any OCR engine; results are keyed by sha256(pdf bytes) + engine name."""

    def __init__(self, engine: BaseOCREngine, cache_dir: Path, engine_name: str = ""):
        self.engine = engine
        self.cache_dir = cache_dir
        self.engine_name = engine_name or type(engine).__name__
        self.hits = 0
        self.misses = 0

    def _key(self, pdf_path: Path) -> Path:
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        return self.cache_dir / f"{self.engine_name}-{digest}.json"

    def extract_pages(self, pdf_path: Path) -> List[PageOCR]:
        path = self._key(pdf_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pages = [PageOCR(**p) for p in data["pages"]]
                self.hits += 1
                logger.info(f"OCR cache hit for {pdf_path.name}")
                return pages
            except Exception as e:  # corrupt cache entry: fall through and recompute
                logger.warning(f"Ignoring unreadable OCR cache entry {path.name}: {e}")

        pages = self.engine.extract_pages(pdf_path)
        self.misses += 1
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"pages": [p.__dict__ for p in pages]}, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return pages
