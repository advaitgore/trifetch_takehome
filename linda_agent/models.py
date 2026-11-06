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


class PageEvidence(BaseModel):
    """Per-page evidence extracted from OCR."""
    page: int
    title: Optional[str] = None       # detected document category on this page
    doc_id: Optional[str] = None      # page-local document identifier, if labelled
    min_conf: Optional[float] = None
    mean_conf: Optional[float] = None
    line_count: int = 0


class ExtractedDocument(BaseModel):
    """Information extracted from the scanned PDF via OCR."""
    fax_id: str
    source_doc_id: Optional[str] = None
    original_doc_id: Optional[str] = None  # If marked as a resend
    doc_type_detected: str
    patient_name: Optional[str] = None
    dob: Optional[str] = None
    urgent_flags: List[str] = Field(default_factory=list)
    urgent_value: Optional[str] = None     # best-effort "Analyte value unit"; never drives routing
    is_resend_marker: bool = False         # document text explicitly says resend/retransmission
    is_mixed: bool = False
    is_incomplete: bool = False
    evidence: str = ""
    pages_text: List[str] = Field(default_factory=list)
    page_evidence: List[PageEvidence] = Field(default_factory=list)
    pages_portal: Optional[int] = None
    pages_ocr: int = 0
    ocr_min_confidence: Optional[float] = None
    ocr_mean_confidence: Optional[float] = None
    # Reasons the evidence is not trustworthy enough to auto-route (page-count mismatch,
    # empty page, low OCR confidence, conflicting urgency signals ...). Any entry => review.
    uncertainty: List[str] = Field(default_factory=list)


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
    """Final comprehensive record for per-fax JSON/CSV output.

    Besides the outcome it persists the extraction evidence, so a resumed run can
    reconstruct duplicate detection without re-downloading or re-OCRing completed faxes.
    All evidence fields are defaulted so older checkpoints still load.
    """
    fax_id: str
    extracted_type: str
    source_id_evidence: str
    action: str  # "routed", "needs_review", "skipped_duplicate", "already_routed", "failed"
    selected_fields: Dict[str, str] = Field(default_factory=dict)
    # "verified_on_portal", "verified_via_queue", "verified_mismatch", "not_verified",
    # "skipped", "not_applicable", "failed"
    verification: str
    errors_retries: int = 0
    notes: str = ""
    received_at: str = ""
    source_doc_id: Optional[str] = None
    original_doc_id: Optional[str] = None
    patient_name: Optional[str] = None
    dob: Optional[str] = None
    is_resend_marker: bool = False
    pages_portal: Optional[int] = None
    pages_ocr: Optional[int] = None
    ocr_min_confidence: Optional[float] = None
    ocr_mean_confidence: Optional[float] = None
    uncertainty: List[str] = Field(default_factory=list)
    recovery: List[str] = Field(default_factory=list)  # human-readable recovery events
