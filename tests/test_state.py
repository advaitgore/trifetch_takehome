"""Checkpoint manager: completion semantics, intents, backward compatibility, OCR cache."""
import json

from linda_agent.models import FaxResult
from linda_agent.ocr.base import BaseOCREngine, PageOCR
from linda_agent.ocr.cache import CachedOCR
from linda_agent.state import CheckpointManager


def _res(fax_id="FX-205", verification="verified_on_portal", action="routed", **kw):
    return FaxResult(fax_id=fax_id, extracted_type="Refill Request", source_id_evidence="e",
                     action=action, verification=verification, **kw)


def test_failed_is_not_completed_and_stays_failed(tmp_path):
    sm = CheckpointManager(tmp_path / "s.json")
    sm.record_result(_res(action="failed", verification="failed"))
    assert not sm.is_completed("FX-205")
    assert sm.get_result("FX-205").action == "failed"


def test_completed_verifications(tmp_path):
    sm = CheckpointManager(tmp_path / "s.json")
    for i, v in enumerate(["verified_on_portal", "verified_mismatch", "skipped", "not_applicable"]):
        sm.record_result(_res(fax_id=f"FX-30{i}", verification=v))
        assert sm.is_completed(f"FX-30{i}"), v
    for i, v in enumerate(["failed", "not_verified", "pending"]):
        sm.record_result(_res(fax_id=f"FX-31{i}", verification=v))
        assert not sm.is_completed(f"FX-31{i}"), v


def test_intent_round_trip_and_cleared_by_result(tmp_path):
    path = tmp_path / "s.json"
    sm = CheckpointManager(path)
    sm.record_intent(_res(verification="pending"))
    reloaded = CheckpointManager(path)
    assert reloaded.get_intent("FX-205") is not None
    assert not reloaded.is_completed("FX-205")
    assert "FX-205" not in reloaded.all_results()   # the intents key is not mistaken for a fax

    reloaded.record_result(_res())
    assert reloaded.get_intent("FX-205") is None
    assert CheckpointManager(path).is_completed("FX-205")


def test_old_format_checkpoint_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"FX-201": {
        "fax_id": "FX-201", "extracted_type": "Referral", "source_id_evidence": "x",
        "action": "routed", "selected_fields": {"assignee_slug": "olivia"},
        "verification": "verified_on_portal", "errors_retries": 0, "notes": "",
    }}), encoding="utf-8")
    sm = CheckpointManager(path)
    r = sm.get_result("FX-201")
    assert r.source_doc_id is None and r.uncertainty == [] and r.recovery == []
    assert sm.is_completed("FX-201")


def test_evidence_fields_round_trip(tmp_path):
    sm = CheckpointManager(tmp_path / "s.json")
    sm.record_result(_res(source_doc_id="RF-3001", patient_name="Elena Hart", pages_ocr=2,
                          ocr_min_confidence=0.93, uncertainty=["u"], recovery=["r"]))
    r = CheckpointManager(tmp_path / "s.json").get_result("FX-205")
    assert (r.source_doc_id, r.patient_name, r.pages_ocr, r.uncertainty, r.recovery) == \
        ("RF-3001", "Elena Hart", 2, ["u"], ["r"])


class _CountingEngine(BaseOCREngine):
    def __init__(self):
        self.calls = 0

    def extract_text(self, pdf_path):
        return ["x"]

    def extract_pages(self, pdf_path):
        self.calls += 1
        return [PageOCR(text="hello", min_conf=0.9, mean_conf=0.95, line_count=1)]


def test_ocr_cache_hit_miss_and_content_addressing(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1 one")
    engine = _CountingEngine()
    cache = CachedOCR(engine, tmp_path / "cache", "stub")

    first = cache.extract_pages(pdf)
    second = cache.extract_pages(pdf)
    assert engine.calls == 1 and (cache.hits, cache.misses) == (1, 1)
    assert first[0].text == second[0].text == "hello" and second[0].mean_conf == 0.95

    pdf.write_bytes(b"%PDF-1 two")        # different content -> different key
    cache.extract_pages(pdf)
    assert engine.calls == 2

    for f in (tmp_path / "cache").glob("*.json"):   # a corrupt entry is recomputed, not trusted
        f.write_text("{not json")
    cache.extract_pages(pdf)
    assert engine.calls == 3
