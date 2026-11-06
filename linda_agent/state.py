"""State and checkpoint manager for idempotent execution and crash recovery.

File layout (checkpoints/progress_state.json):
    { "FX-201": {<FaxResult>}, ...,
      "__intents__": { "FX-205": {<FaxResult draft>} } }

``__intents__`` is written *before* a route is submitted. If the process dies after the
portal commits but before the result is recorded, the next run finds the portal route,
compares it with the intent and finalises the record instead of mislabeling it
"already routed by someone else".
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from linda_agent.config import CHECKPOINT_FILE
from linda_agent.models import FaxResult

logger = logging.getLogger(__name__)

INTENTS_KEY = "__intents__"
# Verifications that mean "nothing further to do for this fax".
COMPLETED_VERIFICATIONS = (
    "verified_on_portal",
    "verified_via_queue",
    "verified_mismatch",   # committed but divergent: reported loudly, never resubmitted
    "skipped",
    "not_applicable",
)


class CheckpointManager:
    """Manages durable state persistence across agent runs."""

    def __init__(self, checkpoint_path: Path = CHECKPOINT_FILE):
        self.checkpoint_path = checkpoint_path
        self.state: Dict[str, Dict[str, Any]] = {}
        self.intents: Dict[str, Dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        """Load state from disk if it exists."""
        self.state, self.intents = {}, {}
        if not self.checkpoint_path.exists():
            return
        try:
            with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.intents = raw.pop(INTENTS_KEY, {}) or {}
            self.state = raw
            logger.info(
                f"Loaded {len(self.state)} fax checkpoints and {len(self.intents)} pending "
                f"intents from {self.checkpoint_path}"
            )
        except Exception as e:
            logger.warning(f"Could not load checkpoint file: {e}. Starting fresh.")
            self.state, self.intents = {}, {}

    def save(self) -> None:
        """Atomically persist state to disk."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self.state)
        if self.intents:
            payload[INTENTS_KEY] = self.intents
        temp_path = self.checkpoint_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        temp_path.replace(self.checkpoint_path)

    # ------------------------------------------------------------------ results
    def record_result(self, result: FaxResult) -> None:
        """Record or update the result for a given fax (also clears any pending intent)."""
        self.state[result.fax_id] = result.model_dump()
        self.intents.pop(result.fax_id, None)
        self.save()

    def invalidate(self, fax_id: str) -> None:
        """Drop a checkpoint entry the portal does not corroborate."""
        if self.state.pop(fax_id, None) is not None:
            self.save()

    def is_completed(self, fax_id: str) -> bool:
        """True if the fax has been fully processed and needs no further action."""
        record = self.state.get(fax_id)
        return bool(record) and record.get("verification") in COMPLETED_VERIFICATIONS

    def get_result(self, fax_id: str) -> Optional[FaxResult]:
        data = self.state.get(fax_id)
        return FaxResult(**data) if data else None

    def all_results(self) -> Dict[str, FaxResult]:
        return {fid: FaxResult(**data) for fid, data in self.state.items()}

    def completed_results(self) -> Dict[str, FaxResult]:
        return {fid: r for fid, r in self.all_results().items() if self.is_completed(fid)}

    # ------------------------------------------------------------------ intents
    def record_intent(self, draft: FaxResult) -> None:
        """Persist what we are about to submit, before the submit happens."""
        self.intents[draft.fax_id] = draft.model_dump()
        self.save()

    def get_intent(self, fax_id: str) -> Optional[FaxResult]:
        data = self.intents.get(fax_id)
        return FaxResult(**data) if data else None

    def clear_intent(self, fax_id: str) -> None:
        if self.intents.pop(fax_id, None) is not None:
            self.save()
