"""Shared test helpers: OCR fixtures and a stub OCR engine."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from linda_agent.models import FaxMetadata
from linda_agent.ocr.base import PageOCR

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ocr"

# Portal facts for the 12 sandbox faxes (received_at as listed by the inbox, pages column).
PORTAL_FACTS: Dict[str, dict] = {
    "FX-201": dict(sender="Bayview Family Practice", received_at="2026-11-03 08:02", pages=2),
    "FX-202": dict(sender="Summit Diagnostic Labs", received_at="2026-11-03 08:06", pages=1),
    "FX-203": dict(sender="Blue Ridge Medical", received_at="2026-11-03 08:12", pages=1),
    "FX-204": dict(sender="Blue Ridge Medical", received_at="2026-11-03 08:12", pages=1),
    "FX-205": dict(sender="Harbor Community Pharmacy", received_at="2026-11-03 08:19", pages=1),
    "FX-206": dict(sender="Hillside Records Office", received_at="2026-11-03 08:25", pages=2),
    "FX-207": dict(sender="Example Health Plan", received_at="2026-11-03 08:31", pages=1),
    "FX-208": dict(sender="Summit Diagnostic Labs", received_at="2026-11-03 08:37", pages=1),
    "FX-209": dict(sender="Unidentified Fax Number", received_at="2026-11-03 08:42", pages=1),
    "FX-210": dict(sender="Bayview Family Practice", received_at="2026-11-03 08:49", pages=1),
    "FX-211": dict(sender="Northbridge Imaging", received_at="2026-11-03 08:54", pages=1),
    "FX-212": dict(sender="Central Care Associates", received_at="2026-11-03 09:03", pages=3),
}

# Ground truth from the take-home brief: (action, assignee slug, doc type, folder)
EXPECTED = {
    "FX-201": ("routed", "olivia", "Referral", "Referrals / New"),
    "FX-202": ("needs_review", "jordan", "Needs Review", "Review / Unclassified"),
    "FX-203": ("routed", "noah", "Imaging Report", "Clinical / Imaging"),
    "FX-204": ("routed", "ethan", "Prior Authorization", "Authorizations / Pending"),
    "FX-205": ("routed", "ava", "Refill Request", "Nursing / Medication"),
    "FX-206": ("routed", "mia", "Records Request", "Records / Requests"),
    "FX-207": ("routed", "liam", "EOB", "Billing / EOBs"),
    "FX-208": ("routed", "noah", "Lab Result", "Clinical / Labs"),
    "FX-209": ("needs_review", "jordan", "Needs Review", "Review / Unclassified"),
    "FX-210": ("skipped_duplicate", "", "", ""),
    "FX-211": ("already_routed", "", "", ""),
    "FX-212": ("needs_review", "jordan", "Needs Review", "Review / Unclassified"),
}


def meta(fax_id: str, status: str = "New") -> FaxMetadata:
    return FaxMetadata(fax_id=fax_id, status=status, **PORTAL_FACTS[fax_id])


def all_metadata() -> List[FaxMetadata]:
    return [meta(f, "Routed" if f == "FX-211" else "New") for f in PORTAL_FACTS]


def load_pages(fax_id: str) -> List[PageOCR]:
    data = json.loads((FIXTURE_DIR / f"{fax_id}.json").read_text(encoding="utf-8"))
    return [PageOCR(**p) for p in data["pages"]]


class StubOCR:
    """Drop-in for CachedOCR/RapidOCR: serves checked-in OCR output keyed by the PDF's fax ID."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def extract_pages(self, pdf_path) -> List[PageOCR]:
        fax_id = Path(pdf_path).stem
        self.calls.append(fax_id)
        return load_pages(fax_id)
