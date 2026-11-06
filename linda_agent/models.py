"""Data models and schemas for Linda Fax Routing Agent."""
from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class FaxMetadata(BaseModel):
    """Metadata retrieved from the portal Inbound Faxes table."""
    fax_id: str
    sender: str
    received_at: str
    pages: int
    status: str  # "New" or "Routed"


class ExtractedDocument(BaseModel):
    """Information extracted from the scanned PDF via OCR."""
    fax_id: str
    source_doc_id: Optional[str] = None
    original_doc_id: Optional[str] = None  # If marked as a resend
    doc_type_detected: str
    patient_name: Optional[str] = None
    dob: Optional[str] = None
    urgent_flags: List[str] = Field(default_factory=list)
    is_mixed: bool = False
    is_incomplete: bool = False
    evidence: str = ""
    pages_text: List[str] = Field(default_factory=list)


class SelectedFields(BaseModel):
    """Values selected in the portal UI for routing."""
    assignee_slug: str = ""
    assignee_name: str = ""
    doc_type: str = ""
    folder: str = ""
    notes: str = ""


class RoutingDecision(BaseModel):
    """The deterministic routing decision made by the SOP classifier."""
    fax_id: str
    action: str  # "routed", "needs_review", "skipped_duplicate", "already_routed", "failed"
    selected_fields: Optional[SelectedFields] = None
    reason: str = ""


class FaxResult(BaseModel):
    """Final comprehensive record for per-fax JSON/CSV output."""
    fax_id: str
    extracted_type: str
    source_id_evidence: str
    action: str  # "routed", "needs_review", "skipped_duplicate", "already_routed", "failed"
    selected_fields: Dict[str, str] = Field(default_factory=dict)
    verification: str  # "verified_on_portal", "verified_via_queue", "skipped", "not_applicable", "failed"
    errors_retries: int = 0
    notes: str = ""
