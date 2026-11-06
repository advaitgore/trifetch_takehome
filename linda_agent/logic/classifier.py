"""Deterministic SOP Classifier adhering to TriFetch Linda Mini Guidelines."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from linda_agent.config import ROUTE_NOTE_MAX_CHARS
from linda_agent.models import ExtractedDocument, RoutingDecision, SelectedFields

logger = logging.getLogger(__name__)

# Dropdown mappings: (Assignee Slug, Assignee Display, Document Type, Folder)
SOP_MAPPING: Dict[str, Tuple[str, str, str, str]] = {
    "Referral": ("olivia", "Olivia Chen - Referrals", "Referral", "Referrals / New"),
    "Lab Result": ("noah", "Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    "Imaging Report": ("noah", "Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    "Prior Authorization": ("ethan", "Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    "Refill Request": ("ava", "Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    "Records Request": ("mia", "Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    "EOB": ("liam", "Liam Rivera - Billing", "EOB", "Billing / EOBs"),
    "Needs Review": ("jordan", "Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified"),
}


def _truncate(note: str) -> str:
    """The portal silently truncates notes at 1000 characters; do it visibly instead."""
    if len(note) <= ROUTE_NOTE_MAX_CHARS:
        return note
    return note[: ROUTE_NOTE_MAX_CHARS - 3] + "..."


def _enclosure_summary(doc: ExtractedDocument) -> str:
    """'p1 packet ref PkT-4012; p2 Lab Result LAB-4012A; ...' from per-page evidence."""
    parts: List[str] = []
    for pe in doc.page_evidence:
        if pe.title:
            parts.append(f"p{pe.page} {pe.title}" + (f" {pe.doc_id}" if pe.doc_id else ""))
        elif pe.doc_id:
            parts.append(f"p{pe.page} packet ref {pe.doc_id}")
    return "; ".join(parts)


class SOPClassifier:
    """Classifies extracted documents according to deterministic SOP rules.

    Exception reasons are *collected*, not short-circuited, so a packet that is both
    urgent and low-confidence says so. Order is fixed: urgent, mixed, evidence gap,
    incomplete/unclear. Any reason routes to Manual Review.
    """

    @staticmethod
    def _review(fax_id: str, notes: List[str], reasons: List[str]) -> RoutingDecision:
        slug, name, dtype, folder = SOP_MAPPING["Needs Review"]
        return RoutingDecision(
            fax_id=fax_id,
            action="needs_review",
            selected_fields=SelectedFields(
                assignee_slug=slug,
                assignee_name=name,
                doc_type=dtype,
                folder=folder,
                notes=_truncate(" | ".join(notes)),
            ),
            reason="; ".join(reasons),
        )

    @staticmethod
    def classify(
        doc: ExtractedDocument,
        is_duplicate: bool = False,
        is_already_routed: bool = False,
        duplicate_uncertainty: Optional[str] = None,
    ) -> RoutingDecision:
        fax_id = doc.fax_id

        # 1. Already routed in the portal: never modify.
        if is_already_routed:
            return RoutingDecision(
                fax_id=fax_id,
                action="already_routed",
                reason="Fax was pre-routed in portal before assessment run",
            )

        # 2. Confirmed duplicate / resend: report, do not submit.
        if is_duplicate:
            return RoutingDecision(
                fax_id=fax_id,
                action="skipped_duplicate",
                reason=f"Confirmed duplicate / resend of source document {doc.source_doc_id}",
            )

        notes: List[str] = []
        reasons: List[str] = []

        # 3. Urgent / critical: Manual Review with the fax's own flag in the note. No clinical action.
        if doc.urgent_flags:
            flags = "; ".join(doc.urgent_flags)
            note = f"URGENT / CRITICAL FLAG on fax: {flags}"
            if doc.urgent_value:
                note += f" | Reported value: {doc.urgent_value}"
            note += " | Automation took no clinical action; manual review required"
            notes.append(note)
            reasons.append(f"Urgent/critical flag detected: {flags}")

        # 4. Mixed packet: more than one document category must not be filed as one type.
        if doc.is_mixed:
            note = "Mixed packet containing multiple distinct document categories"
            encl = _enclosure_summary(doc)
            if encl:
                note += f" ({encl})"
            notes.append(note)
            reasons.append("Mixed packet containing multiple categories")

        # 5. Evidence gaps: page count, empty/low-confidence pages, conflicting signals, uncertain duplicate.
        gaps = list(doc.uncertainty)
        if duplicate_uncertainty:
            gaps.append(duplicate_uncertainty)
        if gaps:
            notes.append("Insufficient or conflicting evidence: " + "; ".join(gaps))
            reasons.append("Evidence gap: " + "; ".join(gaps))

        # 6. Incomplete or unclassifiable.
        if (doc.is_incomplete or doc.doc_type_detected not in SOP_MAPPING) and not doc.is_mixed:
            notes.append("Incomplete packet or unclear classification requiring manual review")
            reasons.append("Incomplete document or insufficient classification evidence")

        if reasons:
            return SOPClassifier._review(fax_id, notes, reasons)

        # 7. Standard deterministic routing.
        slug, name, dtype, folder = SOP_MAPPING[doc.doc_type_detected]
        return RoutingDecision(
            fax_id=fax_id,
            action="routed",
            selected_fields=SelectedFields(
                assignee_slug=slug, assignee_name=name, doc_type=dtype, folder=folder, notes="",
            ),
            reason=f"Standard routing for {doc.doc_type_detected}",
        )
