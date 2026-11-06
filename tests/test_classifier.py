"""Unit tests for the extractor, SOP classifier and evidence gates.

Inputs are checked-in OCR fixtures (tests/fixtures/ocr), generated from the PDFs the agent
itself downloaded through the portal UI. The tests never read the seeded portal data.
"""
import pytest

from helpers import EXPECTED, PORTAL_FACTS, all_metadata, load_pages
from linda_agent.logic.classifier import SOPClassifier
from linda_agent.logic.duplicate_detector import DuplicateDetector
from linda_agent.models import ExtractedDocument
from linda_agent.ocr.base import PageOCR
from linda_agent.ocr.extractor import DocumentExtractor

OCR_FAXES = [f for f in PORTAL_FACTS if f != "FX-211"]  # FX-211 is pre-routed: never downloaded


@pytest.fixture(scope="module")
def extractor():
    return DocumentExtractor()


@pytest.fixture(scope="module")
def docs(extractor):
    return {
        f: extractor.extract(f, load_pages(f), pages_portal=PORTAL_FACTS[f]["pages"])
        for f in OCR_FAXES
    }


def _page(text: str, conf: float = 0.97) -> PageOCR:
    return PageOCR(text=text, min_conf=conf, mean_conf=conf, line_count=len(text.splitlines()))


def test_classify_all_twelve_faxes(docs):
    """All 12 faxes land on the ground-truth action / assignee / type / folder."""
    report = DuplicateDetector.analyze(all_metadata(), docs)
    assert set(report.confirmed) == {"FX-210"}
    assert report.confirmed["FX-210"] == "FX-201"
    assert not report.uncertain

    for fax_id, (action, slug, dtype, folder) in EXPECTED.items():
        if fax_id == "FX-211":
            # Pre-routed: decided from portal status alone, no document needed.
            stub = ExtractedDocument(fax_id=fax_id, doc_type_detected="Unknown")
            decision = SOPClassifier.classify(stub, is_already_routed=True)
        else:
            decision = SOPClassifier.classify(docs[fax_id], is_duplicate=fax_id in report.confirmed)
        assert decision.action == action, fax_id
        if slug:
            f = decision.selected_fields
            assert (f.assignee_slug, f.doc_type, f.folder) == (slug, dtype, folder), fax_id


def test_all_twelve_have_clean_evidence(docs):
    """The gates must not flip any real fax: no uncertainty on any of the 11 OCR'd faxes."""
    for fax_id, doc in docs.items():
        assert doc.uncertainty == [], f"{fax_id}: {doc.uncertainty}"
        assert doc.pages_ocr == PORTAL_FACTS[fax_id]["pages"]


# ---------------------------------------------------------------- urgency (Task 5)
def test_fx202_note_has_flag_and_reported_value(docs):
    d = SOPClassifier.classify(docs["FX-202"])
    assert d.action == "needs_review"
    note = d.selected_fields.notes
    assert "CRITICAL RESULT-CALLING PROVIDER" in note
    assert "Potassium 6.3 mmol/L" in note
    assert "no clinical action" in note
    # contained duplicate removed: a bare CRITICAL inside the longer phrase is not listed twice
    assert "; CRITICAL" not in note.replace("CRITICAL RESULT-CALLING PROVIDER", "")
    assert len(note) <= 1000


def test_fx208_negated_urgency_is_not_urgent(docs):
    assert docs["FX-208"].urgent_flags == []
    assert SOPClassifier.classify(docs["FX-208"]).action == "routed"


def test_negating_cover_sheet_does_not_hide_critical_page(extractor):
    """'No urgent flag' on one page must never suppress a critical flag on another."""
    pages = [
        _page("CENTRALCARE\nFAXCOVER\nNourgentflagprovided"),
        _page("LABORATORYRESULTS\nDocumentreference:LAB-9\nPatient:Ann Poe\nResult:Potassium6.8mmol/L\nCRITICAL RESULT-CALLING PROVIDER"),
    ]
    doc = extractor.extract("FX-900", pages, pages_portal=2)
    assert doc.urgent_flags, "critical flag on page 2 must be detected"
    assert any("Conflicting urgency" in u for u in doc.uncertainty)
    d = SOPClassifier.classify(doc)
    assert d.action == "needs_review"
    assert "Conflicting" in d.selected_fields.notes


def test_value_capture_failure_never_changes_routing(extractor):
    pages = [_page("LABORATORYRESULTS\nDocumentreference:LAB-9\nPatient:Ann Poe\nCRITICAL RESULT-CALLING PROVIDER")]
    doc = extractor.extract("FX-901", pages, pages_portal=1)
    assert doc.urgent_value is None
    d = SOPClassifier.classify(doc)
    assert d.action == "needs_review"
    assert "CRITICAL RESULT-CALLING PROVIDER" in d.selected_fields.notes
    assert "Reported value" not in d.selected_fields.notes


def test_ordinary_prose_does_not_trigger_bare_tokens(extractor):
    pages = [_page("LABORATORYREPORT\nReportID:LAB-5\nPatient:Bo Li\nStatus:Final\nCritical thinking not required. Statement of fact.")]
    assert extractor.extract("FX-902", pages, pages_portal=1).urgent_flags == []


# ---------------------------------------------------------------- incomplete / ids (Task 6)
def test_fx209_incomplete_with_transmission_id(docs):
    assert docs["FX-209"].is_incomplete
    assert docs["FX-209"].source_doc_id == "U-4009"
    assert docs["FX-209"].patient_name is None
    assert SOPClassifier.classify(docs["FX-209"]).action == "needs_review"


def test_word_unknown_in_prose_is_not_incomplete(extractor):
    pages = [_page("LABORATORYREPORT\nReportID:LAB-7\nPatient:Cy Dee\nCause of anemia unknown to the patient.")]
    doc = extractor.extract("FX-903", pages, pages_portal=1)
    assert not doc.is_incomplete
    assert SOPClassifier.classify(doc).action == "routed"


def test_fallback_id_never_returns_the_fax_id(extractor):
    doc = extractor.extract("FX-904", [_page("PRESCRIPTIONREFILLREQUEST\nTransmissionFX-904Page1of1\nRef RX-4005 below")], pages_portal=1)
    assert doc.source_doc_id == "RX-4005"


def test_fx212_note_names_its_enclosures(docs):
    d = SOPClassifier.classify(docs["FX-212"])
    assert d.action == "needs_review"
    note = d.selected_fields.notes
    assert "LAB-4012A" in note and "IMG-4012B" in note and "PkT-4012" in note


# ---------------------------------------------------------------- evidence gates (Task 7)
def test_page_count_mismatch_goes_to_review(extractor):
    pages = load_pages("FX-212")[:2]  # OCR only read 2 of the 3 portal pages
    doc = extractor.extract("FX-212", pages, pages_portal=3)
    assert any("Page count mismatch" in u for u in doc.uncertainty)

    clean = extractor.extract("FX-206", load_pages("FX-206")[:1], pages_portal=2)
    d = SOPClassifier.classify(clean)
    assert d.action == "needs_review"
    assert "Page count mismatch" in d.selected_fields.notes


def test_low_confidence_page_goes_to_review(extractor):
    pages = [PageOCR(text=p.text, min_conf=0.30, mean_conf=0.55, line_count=p.line_count)
             for p in load_pages("FX-203")]
    doc = extractor.extract("FX-203", pages, pages_portal=1)
    assert any("low OCR confidence" in u for u in doc.uncertainty)
    assert SOPClassifier.classify(doc).action == "needs_review"


def test_empty_page_goes_to_review(extractor):
    pages = load_pages("FX-203") + [PageOCR(text="", min_conf=0.0, mean_conf=0.0, line_count=0)]
    doc = extractor.extract("FX-203", pages, pages_portal=2)
    assert any("no text recognised" in u for u in doc.uncertainty)
    assert SOPClassifier.classify(doc).action == "needs_review"


def test_measured_confidences_sit_well_above_the_floors(docs):
    """Documents the threshold's basis: observed minimum is far above the configured floors."""
    from linda_agent.config import OCR_MIN_LINE_CONFIDENCE, OCR_MIN_MEAN_CONFIDENCE

    assert min(d.ocr_min_confidence for d in docs.values()) > OCR_MIN_LINE_CONFIDENCE + 0.25
    assert min(d.ocr_mean_confidence for d in docs.values()) > OCR_MIN_MEAN_CONFIDENCE + 0.08
