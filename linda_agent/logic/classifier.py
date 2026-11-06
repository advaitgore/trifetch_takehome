"""Deterministic SOP Classifier adhering to TriFetch Linda Mini Guidelines."""
from __future__ import annotations

import logging
from typing import Dict, Tuple

from linda_agent.models import ExtractedDocument, RoutingDecision, SelectedFields

logger = logging.getLogger(__name__)

# Dropdown mappings: (Assignee Slug, Assignee Display, Document Type, Folder)
SOP_MAPPING: Dict[str, Tuple[str, str, str, str]] = {
    "Referral": (
        "olivia",
        "Olivia Chen - Referrals",
        "Referral",
        "Referrals / New",
    ),
    "Lab Result": (
        "noah",
        "Noah Patel - Clinical Review",
        "Lab Result",
        "Clinical / Labs",
    ),
    "Imaging Report": (
        "noah",
        "Noah Patel - Clinical Review",
        "Imaging Report",
        "Clinical / Imaging",
    ),
    "Prior Authorization": (
        "ethan",
        "Ethan Brooks - Authorizations",
        "Prior Authorization",
        "Authorizations / Pending",
    ),
    "Refill Request": (
        "ava",
        "Ava Wilson - Nursing / Medication",
        "Refill Request",
        "Nursing / Medication",
    ),
    "Records Request": (
        "mia",
        "Mia Gomez - Medical Records",
        "Records Request",
        "Records / Requests",
    ),
    "EOB": (
        "liam",
        "Liam Rivera - Billing",
        "EOB",
        "Billing / EOBs",
    ),
    "Needs Review": (
        "jordan",
        "Jordan Ellis - Manual Review",
        "Needs Review",
        "Review / Unclassified",
    ),
}


class SOPClassifier:
    """Classifies extracted documents according to deterministic SOP rules."""

    @staticmethod
    def classify(doc: ExtractedDocument, is_duplicate: bool = False, is_already_routed: bool = False) -> RoutingDecision:
        fax_id = doc.fax_id

        # 1. Check if already routed in portal
        if is_already_routed:
            return RoutingDecision(
                fax_id=fax_id,
                action="already_routed",
                reason="Fax was pre-routed in portal before assessment run",
            )

        # 2. Check if duplicate / resend
        if is_duplicate:
            return RoutingDecision(
                fax_id=fax_id,
                action="skipped_duplicate",
                reason=f"Confirmed duplicate / resend of source document {doc.source_doc_id}",
            )

        # 3. Urgent / Critical Exception Rule
        # "Do not diagnose, clinically triage or send a clinical alert yourself.
        # Use Needs Review and include the fax's explicit flag in the routing note."
        if doc.urgent_flags:
            slug, name, dtype, folder = SOP_MAPPING["Needs Review"]
            flags_str = "; ".join(doc.urgent_flags)
            notes = f"URGENT / CRITICAL ALERT: {flags_str}"
            return RoutingDecision(
                fax_id=fax_id,
                action="needs_review",
                selected_fields=SelectedFields(
                    assignee_slug=slug,
                    assignee_name=name,
                    doc_type=dtype,
                    folder=folder,
                    notes=notes,
                ),
                reason=f"Urgent/critical flag detected: {flags_str}",
            )

        # 4. Mixed Packet Exception Rule
        # "A packet containing more than one document category must go to Manual Review"
        if doc.is_mixed:
            slug, name, dtype, folder = SOP_MAPPING["Needs Review"]
            notes = "Mixed packet containing multiple distinct document categories"
            return RoutingDecision(
                fax_id=fax_id,
                action="needs_review",
                selected_fields=SelectedFields(
                    assignee_slug=slug,
                    assignee_name=name,
                    doc_type=dtype,
                    folder=folder,
                    notes=notes,
                ),
                reason="Mixed packet containing multiple categories",
            )

        # 5. Incomplete / Unclear Classification Rule
        # "A document without enough reliable OCR evidence for classification must go to Manual Review"
        if doc.is_incomplete or doc.doc_type_detected not in SOP_MAPPING:
            slug, name, dtype, folder = SOP_MAPPING["Needs Review"]
            notes = "Incomplete packet or unclear classification requiring manual review"
            return RoutingDecision(
                fax_id=fax_id,
                action="needs_review",
                selected_fields=SelectedFields(
                    assignee_slug=slug,
                    assignee_name=name,
                    doc_type=dtype,
                    folder=folder,
                    notes=notes,
                ),
                reason="Incomplete document or insufficient classification evidence",
            )

        # 6. Standard Deterministic Routing
        slug, name, dtype, folder = SOP_MAPPING[doc.doc_type_detected]
        return RoutingDecision(
            fax_id=fax_id,
            action="routed",
            selected_fields=SelectedFields(
                assignee_slug=slug,
                assignee_name=name,
                doc_type=dtype,
                folder=folder,
                notes="",
            ),
            reason=f"Standard routing for {doc.doc_type_detected}",
        )
