"""Duplicate and resend detector operating across global queue order.

A duplicate is *confirmed* only when the same source document ID appears in an earlier
fax AND there is corroborating evidence: an explicit resend marker in the later fax, or
a matching patient name. A shared ID with conflicting patients, or with no corroboration
at all, is reported as *uncertain*; the classifier then sends it to manual review. It is
never silently skipped and never silently filed.

Evidence for faxes that finished in an earlier run comes from the checkpoint (FaxResult),
so duplicate detection survives a restart without re-downloading or re-OCRing them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Set

from linda_agent.models import ExtractedDocument, FaxMetadata, FaxResult
from linda_agent.ocr.extractor import normalize_name

logger = logging.getLogger(__name__)


@dataclass
class DocEvidence:
    fax_id: str
    received_at: str
    doc_id: Optional[str]
    patient_key: str
    resend_marker: bool

    @classmethod
    def from_extracted(cls, meta: FaxMetadata, doc: ExtractedDocument) -> "DocEvidence":
        return cls(
            fax_id=meta.fax_id,
            received_at=meta.received_at,
            doc_id=doc.original_doc_id or doc.source_doc_id,
            patient_key=normalize_name(doc.patient_name),
            resend_marker=doc.is_resend_marker,
        )

    @classmethod
    def from_result(cls, meta: FaxMetadata, res: FaxResult) -> "DocEvidence":
        return cls(
            fax_id=meta.fax_id,
            received_at=meta.received_at or res.received_at,
            doc_id=res.original_doc_id or res.source_doc_id,
            patient_key=normalize_name(res.patient_name),
            resend_marker=res.is_resend_marker,
        )


@dataclass
class DuplicateReport:
    confirmed: Dict[str, str] = field(default_factory=dict)   # duplicate fax -> original fax
    uncertain: Dict[str, str] = field(default_factory=dict)   # fax -> reason it needs review


class DuplicateDetector:
    """Identifies confirmed resends using source document IDs plus corroborating evidence."""

    @staticmethod
    def analyze(
        metadata_list: List[FaxMetadata],
        extracted_docs: Mapping[str, ExtractedDocument],
        prior_results: Optional[Mapping[str, FaxResult]] = None,
    ) -> DuplicateReport:
        prior_results = prior_results or {}
        evidence: List[DocEvidence] = []
        for meta in metadata_list:
            if meta.fax_id in extracted_docs:
                evidence.append(DocEvidence.from_extracted(meta, extracted_docs[meta.fax_id]))
            elif meta.fax_id in prior_results:
                evidence.append(DocEvidence.from_result(meta, prior_results[meta.fax_id]))

        # Global arrival order, oldest first. The portal lists newest first, so a linear
        # walk of the UI would meet the resend before the original.
        evidence.sort(key=lambda e: (e.received_at, e.fax_id))

        report = DuplicateReport()
        first_seen: Dict[str, DocEvidence] = {}
        for ev in evidence:
            if not ev.doc_id:
                continue
            original = first_seen.get(ev.doc_id)
            if original is None:
                first_seen[ev.doc_id] = ev
                continue

            both_named = bool(ev.patient_key and original.patient_key)
            if both_named and ev.patient_key != original.patient_key:
                report.uncertain[ev.fax_id] = (
                    f"Shares source document ID {ev.doc_id} with {original.fax_id} but the "
                    f"patient names differ; identity uncertain"
                )
            elif ev.resend_marker or (both_named and ev.patient_key == original.patient_key):
                logger.info(
                    f"Fax {ev.fax_id} (received {ev.received_at}) is a confirmed duplicate/resend "
                    f"of source document {ev.doc_id} in fax {original.fax_id}."
                )
                report.confirmed[ev.fax_id] = original.fax_id
            else:
                report.uncertain[ev.fax_id] = (
                    f"Shares source document ID {ev.doc_id} with {original.fax_id} but there is no "
                    f"resend marker or matching patient to corroborate; identity uncertain"
                )
        return report

    @staticmethod
    def identify_duplicates(
        metadata_list: List[FaxMetadata],
        extracted_docs: Mapping[str, ExtractedDocument],
        prior_results: Optional[Mapping[str, FaxResult]] = None,
    ) -> Set[str]:
        """Fax IDs that are confirmed duplicate resends."""
        return set(DuplicateDetector.analyze(metadata_list, extracted_docs, prior_results).confirmed)
