"""Unit tests for the SOP Classifier and Document Extractor."""
import pytest
from pathlib import Path

from linda_agent.logic.classifier import SOPClassifier
from linda_agent.logic.duplicate_detector import DuplicateDetector
from linda_agent.models import FaxMetadata
from linda_agent.ocr.extractor import DocumentExtractor
from linda_agent.ocr.rapid_ocr import RapidOCREngine

DATA_DIR = Path(__file__).resolve().parent.parent / "portal" / "data" / "faxes"


@pytest.fixture(scope="module")
def ocr_engine():
    return RapidOCREngine()


@pytest.fixture(scope="module")
def extractor():
    return DocumentExtractor()


def test_classify_all_twelve_faxes(ocr_engine, extractor):
    """Verifies that all 12 faxes in the dataset classify deterministically per the SOP."""
    metadata_list = [
        FaxMetadata(fax_id="FX-201", sender="Bayview Family Practice", received_at="2026-11-03 08:02", pages=2, status="New"),
        FaxMetadata(fax_id="FX-202", sender="Summit Diagnostic Labs", received_at="2026-11-03 08:06", pages=1, status="New"),
        FaxMetadata(fax_id="FX-203", sender="Blue Ridge Medical", received_at="2026-11-03 08:12", pages=1, status="New"),
        FaxMetadata(fax_id="FX-204", sender="Blue Ridge Medical", received_at="2026-11-03 08:12", pages=1, status="New"),
        FaxMetadata(fax_id="FX-205", sender="Harbor Community Pharmacy", received_at="2026-11-03 08:19", pages=1, status="New"),
        FaxMetadata(fax_id="FX-206", sender="Hillside Records Office", received_at="2026-11-03 08:25", pages=2, status="New"),
        FaxMetadata(fax_id="FX-207", sender="Example Health Plan", received_at="2026-11-03 08:31", pages=1, status="New"),
        FaxMetadata(fax_id="FX-208", sender="Summit Diagnostic Labs", received_at="2026-11-03 08:37", pages=1, status="New"),
        FaxMetadata(fax_id="FX-209", sender="Unidentified Fax Number", received_at="2026-11-03 08:42", pages=1, status="New"),
        FaxMetadata(fax_id="FX-210", sender="Bayview Family Practice", received_at="2026-11-03 08:49", pages=1, status="New"),
        FaxMetadata(fax_id="FX-211", sender="Northbridge Imaging", received_at="2026-11-03 08:54", pages=1, status="Routed"),
        FaxMetadata(fax_id="FX-212", sender="Central Care Associates", received_at="2026-11-03 09:03", pages=3, status="New"),
    ]

    extracted_docs = {}
    for meta in metadata_list:
        pdf_path = DATA_DIR / f"{meta.fax_id}.pdf"
        pages_text = ocr_engine.extract_text(pdf_path)
        doc = extractor.extract(meta.fax_id, pages_text)
        extracted_docs[meta.fax_id] = doc

    duplicates = DuplicateDetector.identify_duplicates(metadata_list, extracted_docs)
    assert "FX-210" in duplicates, "FX-210 must be recognized as a duplicate resend of RF-3001"
    assert "FX-201" not in duplicates, "FX-201 is the original and must not be marked duplicate"

    # 1. FX-201: Routine Referral
    d201 = SOPClassifier.classify(extracted_docs["FX-201"], is_duplicate=False)
    assert d201.action == "routed"
    assert d201.selected_fields.assignee_slug == "olivia"
    assert d201.selected_fields.doc_type == "Referral"
    assert d201.selected_fields.folder == "Referrals / New"

    # 2. FX-202: Urgent / Critical Lab Result
    d202 = SOPClassifier.classify(extracted_docs["FX-202"], is_duplicate=False)
    assert d202.action == "needs_review"
    assert d202.selected_fields.assignee_slug == "jordan"
    assert d202.selected_fields.doc_type == "Needs Review"
    assert "CRITICAL" in d202.selected_fields.notes

    # 3. FX-203: Diagnostic Imaging
    d203 = SOPClassifier.classify(extracted_docs["FX-203"], is_duplicate=False)
    assert d203.action == "routed"
    assert d203.selected_fields.assignee_slug == "noah"
    assert d203.selected_fields.doc_type == "Imaging Report"
    assert d203.selected_fields.folder == "Clinical / Imaging"

    # 4. FX-204: Prior Authorization
    d204 = SOPClassifier.classify(extracted_docs["FX-204"], is_duplicate=False)
    assert d204.action == "routed"
    assert d204.selected_fields.assignee_slug == "ethan"
    assert d204.selected_fields.doc_type == "Prior Authorization"
    assert d204.selected_fields.folder == "Authorizations / Pending"

    # 5. FX-205: Refill Request
    d205 = SOPClassifier.classify(extracted_docs["FX-205"], is_duplicate=False)
    assert d205.action == "routed"
    assert d205.selected_fields.assignee_slug == "ava"
    assert d205.selected_fields.doc_type == "Refill Request"
    assert d205.selected_fields.folder == "Nursing / Medication"

    # 6. FX-206: Records Request
    d206 = SOPClassifier.classify(extracted_docs["FX-206"], is_duplicate=False)
    assert d206.action == "routed"
    assert d206.selected_fields.assignee_slug == "mia"
    assert d206.selected_fields.doc_type == "Records Request"
    assert d206.selected_fields.folder == "Records / Requests"

    # 7. FX-207: EOB
    d207 = SOPClassifier.classify(extracted_docs["FX-207"], is_duplicate=False)
    assert d207.action == "routed"
    assert d207.selected_fields.assignee_slug == "liam"
    assert d207.selected_fields.doc_type == "EOB"
    assert d207.selected_fields.folder == "Billing / EOBs"

    # 8. FX-208: Routine Lab Report (No urgent flag)
    d208 = SOPClassifier.classify(extracted_docs["FX-208"], is_duplicate=False)
    assert d208.action == "routed"
    assert d208.selected_fields.assignee_slug == "noah"
    assert d208.selected_fields.doc_type == "Lab Result"
    assert d208.selected_fields.folder == "Clinical / Labs"

    # 9. FX-209: Incomplete Document
    d209 = SOPClassifier.classify(extracted_docs["FX-209"], is_duplicate=False)
    assert d209.action == "needs_review"
    assert d209.selected_fields.assignee_slug == "jordan"
    assert d209.selected_fields.doc_type == "Needs Review"

    # 10. FX-210: Duplicate Resend
    d210 = SOPClassifier.classify(extracted_docs["FX-210"], is_duplicate=True)
    assert d210.action == "skipped_duplicate"

    # 11. FX-211: Already Routed
    d211 = SOPClassifier.classify(extracted_docs["FX-211"], is_already_routed=True)
    assert d211.action == "already_routed"

    # 12. FX-212: Mixed Packet
    d212 = SOPClassifier.classify(extracted_docs["FX-212"], is_duplicate=False)
    assert d212.action == "needs_review"
    assert d212.selected_fields.assignee_slug == "jordan"
    assert d212.selected_fields.doc_type == "Needs Review"
