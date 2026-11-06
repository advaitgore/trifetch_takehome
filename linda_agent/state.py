"""State and checkpoint manager for idempotent execution and crash recovery."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from linda_agent.config import CHECKPOINT_FILE
from linda_agent.models import FaxResult

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Manages durable state persistence across agent runs."""

    def __init__(self, checkpoint_path: Path = CHECKPOINT_FILE):
        self.checkpoint_path = checkpoint_path
        self.state: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Load state from disk if it exists."""
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info(f"Loaded {len(self.state)} fax checkpoints from {self.checkpoint_path}")
            except Exception as e:
                logger.warning(f"Could not load checkpoint file: {e}. Starting fresh.")
                self.state = {}
        else:
            self.state = {}

    def save(self) -> None:
        """Atomically persist state to disk."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.checkpoint_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        temp_path.replace(self.checkpoint_path)

    def record_result(self, result: FaxResult) -> None:
        """Record or update the result for a given fax."""
        self.state[result.fax_id] = result.model_dump()
        self.save()

    def is_completed(self, fax_id: str) -> bool:
        """Check if a fax has already been fully processed and verified."""
        record = self.state.get(fax_id)
        if not record:
            return False
        return record.get("verification") in ("verified_on_portal", "verified_via_queue", "skipped", "not_applicable")

    def get_result(self, fax_id: str) -> Optional[FaxResult]:
        """Retrieve the persisted FaxResult if present."""
        data = self.state.get(fax_id)
        if data:
            return FaxResult(**data)
        return None

    def all_results(self) -> Dict[str, FaxResult]:
        """Return all persisted results as FaxResult objects."""
        return {fid: FaxResult(**data) for fid, data in self.state.items()}
