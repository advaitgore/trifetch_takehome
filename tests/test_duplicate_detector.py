"""Duplicate detection: arrival order, corroboration, and resume from checkpoint evidence."""
from helpers import PORTAL_FACTS, all_metadata, load_pages, meta
from linda_agent.logic.classifier import SOPClassifier
from linda_agent.logic.duplicate_detector import DuplicateDetector
from linda_agent.models import FaxResult
from linda_agent.ocr.base import PageOCR
from linda_agent.ocr.extractor import DocumentExtractor

ex = DocumentExtractor()


def _doc(fax_id, text, pages=1):
    return ex.extract(fax_id, [PageOCR(text=text, min_conf=0.97, mean_conf=0.97, line_count=5)], pages_portal=pages)


def _real(fax_id):
    return ex.extract(fax_id, load_pages(fax_id), pages_portal=PORTAL_FACTS[fax_id]["pages"])


def _pair():
    """Two synthetic faxes (FX-301 earlier, FX-302 later) with controllable content."""
    m1, m2 = meta("FX-201"), meta("FX-202")
    m1.fax_id, m2.fax_id = "FX-301", "FX-302"
    return [m1, m2]


def test_resend_is_found_even_though_portal_lists_it_first():
    """The inbox is newest-first, so FX-210 appears before FX-201; arrival order must decide."""
    docs = {f: _real(f) for f in ("FX-210", "FX-201")}  # insertion order: resend first
    metas = [meta("FX-210"), meta("FX-201")]
    report = DuplicateDetector.analyze(metas, docs)
    assert report.confirmed == {"FX-210": "FX-201"}
    assert DuplicateDetector.identify_duplicates(metas, docs) == {"FX-210"}


def test_resume_uses_checkpoint_evidence_for_completed_original():
    """Bug 2: FX-201 finished in an earlier run (no fresh OCR); FX-210 is new. Still a duplicate."""
    original = _real("FX-201")
    prior = {
        "FX-201": FaxResult(
            fax_id="FX-201", extracted_type="Referral", source_id_evidence="", action="routed",
            verification="verified_on_portal", received_at=PORTAL_FACTS["FX-201"]["received_at"],
            source_doc_id=original.source_doc_id, patient_name=original.patient_name,
        )
    }
    docs = {"FX-210": _real("FX-210")}
    metas = [meta("FX-201"), meta("FX-210")]

    assert DuplicateDetector.analyze(metas, docs, prior).confirmed == {"FX-210": "FX-201"}
    # Without the persisted evidence the earlier code let FX-210 fall through to a submit.
    assert DuplicateDetector.analyze(metas, docs, None).confirmed == {}


def test_same_id_without_corroboration_is_uncertain_not_skipped():
    a = _doc("FX-301", "REFERRALREQUEST\nDocumentID:RF-1\nPatient:Ann Poe")
    b = _doc("FX-302", "REFERRALREQUEST\nDocumentID:RF-1")  # no patient, no resend marker
    report = DuplicateDetector.analyze(_pair(), {"FX-301": a, "FX-302": b})
    assert report.confirmed == {}
    assert "FX-302" in report.uncertain
    d = SOPClassifier.classify(b, duplicate_uncertainty=report.uncertain["FX-302"])
    assert d.action == "needs_review"
    assert "identity uncertain" in d.selected_fields.notes


def test_same_id_with_conflicting_patients_is_uncertain_even_with_resend_marker():
    a = _doc("FX-301", "REFERRALREQUEST\nDocumentID:RF-1\nPatient:Ann Poe")
    b = _doc("FX-302", "FAXRESEND\nOriginal DocumentID:RF-1\nPatient:Bob Ray")
    report = DuplicateDetector.analyze(_pair(), {"FX-301": a, "FX-302": b})
    assert report.confirmed == {}
    assert "patient names differ" in report.uncertain["FX-302"]


def test_matching_patient_corroborates_without_resend_marker():
    a = _doc("FX-301", "REFERRALREQUEST\nDocumentID:RF-1\nPatient:Ann Poe")
    b = _doc("FX-302", "REFERRALREQUEST\nDocumentID:RF-1\nPatient:AnnPoe")  # OCR dropped the space
    assert DuplicateDetector.analyze(_pair(), {"FX-301": a, "FX-302": b}).confirmed == {"FX-302": "FX-301"}


def test_all_twelve_only_one_duplicate():
    docs = {f: _real(f) for f in PORTAL_FACTS if f != "FX-211"}
    assert DuplicateDetector.identify_duplicates(all_metadata(), docs) == {"FX-210"}
