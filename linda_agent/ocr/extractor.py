"""Document structure extractor that parses structured data and metadata from OCR text.

OCR on these scans routinely drops inter-word spaces ("Nourgentflagprovided",
"Potassium6.3mmol/L"), so every pattern here is whitespace-insensitive (``\\s*``) and avoids
``\\b`` anchors where adjacent letters may be glued together.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple, Union

from linda_agent.config import OCR_MIN_LINE_CONFIDENCE, OCR_MIN_MEAN_CONFIDENCE
from linda_agent.models import ExtractedDocument, PageEvidence
from linda_agent.ocr.base import PageOCR

PageInput = Union[str, PageOCR]


def normalize_name(name: Optional[str]) -> str:
    """Letters-only lowercase key, so 'Elena Hart' == 'ElenaHart' (OCR drops spaces)."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


class DocumentExtractor:
    """Parses raw OCR pages into an ExtractedDocument model."""

    # ---------------------------------------------------------------- identifiers
    LABELED_ID_PATTERN = re.compile(
        r"(?:Document\s*(?:ID|reference|number)|Records\s*request\s*ID|Accession(?:\s*Number)?|"
        r"Request\s*(?:ID|number)|Remittance\s*ID|Transmission\s*(?:ID|reference)|Report\s*ID)"
        r"\s*[:#]?\s*([A-Za-z0-9\-]+)",
        re.IGNORECASE,
    )
    # Unlabeled fallback. Lookarounds instead of \b because OCR glues words together.
    FALLBACK_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,4}-\d{3,5}[A-Z]?)(?![A-Za-z0-9])")
    ORIGINAL_DOC_ID_PATTERN = re.compile(
        r"Original\s*Document\s*ID\s*[:#]?\s*([A-Za-z0-9\-]+)", re.IGNORECASE
    )

    # ---------------------------------------------------------------- patient details
    PATIENT_PATTERN = re.compile(
        r"(?:Patient|Member)\s*[:#]\s*([A-Za-z\s\.\-]+?)"
        r"(?:\s+(?:DOB|Date|Referring|Ordering|Medication|I\s+authorize)|$|\n)",
        re.IGNORECASE,
    )
    DOB_PATTERN = re.compile(r"(?:DOB|Date\s*of\s*Birth)\s*[:#]?\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)

    # ---------------------------------------------------------------- urgency
    # Tier 1: specific phrases, matched case-insensitively.
    URGENT_SPECIFIC = [
        re.compile(r"CRITICAL\s*RESULT\s*[-\u2013\u2014:]?\s*CALLING\s*PROVIDER", re.IGNORECASE),
        re.compile(r"CRITICAL\s*VALUE\s*NOTIFICATION", re.IGNORECASE),
        re.compile(r"PANIC\s*VALUE", re.IGNORECASE),
        re.compile(r"URGENT\s*(?:FLAG|REVIEW|ATTENTION|RESULT)", re.IGNORECASE),
    ]
    # Tier 2: bare tokens count only as ALL-CAPS standalone tokens, so ordinary prose
    # ("critical thinking", "Status") does not trip them.
    URGENT_BARE = re.compile(r"(?<![A-Za-z])(?:CRITICAL|STAT|EMERGENCY|URGENT)(?![A-Za-z])")
    # A match is negated only by a negator directly in front of it ("No urgent flag provided").
    NEGATOR_BEFORE = re.compile(r"(?:^|[^A-Za-z])(?:no|not|without|nil)\s*$", re.IGNORECASE)
    URGENT_VALUE_PATTERN = re.compile(
        r"Result\s*:\s*([A-Za-z][A-Za-z +\-]*?)\s*(\d+(?:\.\d+)?)\s*([A-Za-z%]+(?:/[A-Za-z0-9]+)?)"
    )

    # ---------------------------------------------------------------- mixed / incomplete
    MIXED_PATTERNS = [
        re.compile(r"MULTIPLE\s*ENCLOSURES", re.IGNORECASE),
        re.compile(r"Contains\s*multiple\s*documents", re.IGNORECASE),
    ]
    INCOMPLETE_PATTERNS = [
        re.compile(r"appears\s*incomplete", re.IGNORECASE),
        re.compile(r"missing\s*continuation\s*page", re.IGNORECASE),
        re.compile(r"No\s*readable\s*document\s*category", re.IGNORECASE),
        re.compile(r"SCAN\s*CUT\s*OFF", re.IGNORECASE),
        re.compile(r"UNKNOWN\s*SENDER", re.IGNORECASE),
        re.compile(r"UNIDENTIFIED\s*(?:FAX|SENDER)", re.IGNORECASE),
    ]
    RESEND_PATTERN = re.compile(r"RESEND|RETRANSMISSION|DISREGARD\s*IF\s*ORIGINAL", re.IGNORECASE)

    # Primary Title Indicators
    TITLE_INDICATORS = [
        ("Referral", [r"\bNEW\s*PATIENT\s*REFERRAL\s*REQUEST\b", r"\bREFERRAL\s*REQUEST\b", r"\bCONSULTATION\s*REQUEST\b"]),
        ("Lab Result", [r"\bLABORATORY\s*RESULTS\b", r"\bLABORATORY\s*REPORT\b"]),
        ("Imaging Report", [r"\bDIAGNOSTIC\s*IMAGING\s*REPORT\b", r"\bFINAL\s*(?:IMAGING|RADIOLOGY)\s*REPORT\b", r"\bRADIOLOGY\s*REPORT\b"]),
        ("Prior Authorization", [r"\bPRIOR\s*AUTHORIZATION\s*REQUEST\b"]),
        ("Refill Request", [r"\bPRESCRIPTION\s*REFILL\s*REQUEST\b"]),
        ("Records Request", [r"\bREQUEST\s*FOR\s*MEDICAL\s*RECORDS\b", r"\bAUTHORIZATION\s*TO\s*RELEASE\s*INFORMATION\b"]),
        ("EOB", [r"\bEXPLANATION\s*OF\s*BENEFITS\b", r"\bREMITTANCE\s*ADVICE\b"]),
    ]

    # ------------------------------------------------------------------ helpers
    def _detect_page_title(self, page_text: str) -> Optional[str]:
        """Detects the primary document header / title from a page."""
        for cat, patterns in self.TITLE_INDICATORS:
            for pat in patterns:
                if re.search(pat, page_text, re.IGNORECASE):
                    return cat
        return None

    def _find_doc_id(self, text: str, own_fax_id: str) -> Optional[str]:
        """Labeled identifier first; unlabeled fallback never returns the fax's own ID."""
        m = self.LABELED_ID_PATTERN.search(text)
        if m:
            return m.group(1).strip()
        for fm in self.FALLBACK_ID_PATTERN.finditer(text):
            candidate = fm.group(1)
            if candidate == own_fax_id or candidate.startswith("FX-"):
                continue
            return candidate
        return None

    def _scan_urgency(self, page_text: str) -> Tuple[List[str], int]:
        """Returns (affirmed flags on this page, number of negated mentions)."""
        spans: List[Tuple[int, int, str]] = []
        for pat in self.URGENT_SPECIFIC:
            for m in pat.finditer(page_text):
                spans.append((m.start(), m.end(), m.group(0).strip()))
        for m in self.URGENT_BARE.finditer(page_text):
            spans.append((m.start(), m.end(), m.group(0).strip()))

        # Drop matches fully contained in a longer match (bare CRITICAL inside the specific phrase).
        kept = [
            s for s in spans
            if not any(o is not s and o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0])
                       for o in spans)
        ]
        kept.sort(key=lambda s: s[0])

        affirmed: List[str] = []
        negated = 0
        for start, _end, text in kept:
            window = page_text[max(0, start - 12):start]
            if self.NEGATOR_BEFORE.search(window):
                negated += 1
            elif text not in affirmed:
                affirmed.append(text)
        return affirmed, negated

    def _urgent_value(self, text: str) -> Optional[str]:
        """Best-effort 'Analyte value unit'. Informational only; never affects routing."""
        try:
            m = self.URGENT_VALUE_PATTERN.search(text)
            if m:
                return f"{m.group(1).strip()} {m.group(2)} {m.group(3)}"
        except Exception:
            pass
        return None

    @staticmethod
    def _coerce(pages: Sequence[PageInput]) -> List[PageOCR]:
        out: List[PageOCR] = []
        for p in pages:
            if isinstance(p, PageOCR):
                out.append(p)
            else:
                out.append(PageOCR(text=p, line_count=len([l for l in p.splitlines() if l.strip()])))
        return out

    # ------------------------------------------------------------------ main
    def extract(
        self,
        fax_id: str,
        pages: Sequence[PageInput],
        pages_portal: Optional[int] = None,
    ) -> ExtractedDocument:
        page_objs = self._coerce(pages)
        pages_text = [p.text for p in page_objs]
        full_text = "\n\n".join(pages_text)
        uncertainty: List[str] = []

        # 1. Original doc id (resends)
        original_doc_id: Optional[str] = None
        orig_match = self.ORIGINAL_DOC_ID_PATTERN.search(full_text)
        if orig_match:
            original_doc_id = orig_match.group(1).strip()

        # 2. Per-page evidence + document-level source id (first page that carries one)
        page_evidence: List[PageEvidence] = []
        source_doc_id: Optional[str] = None
        page_titles: List[str] = []
        for idx, p in enumerate(page_objs, start=1):
            title = self._detect_page_title(p.text)
            pid = self._find_doc_id(p.text, fax_id)
            if title:
                page_titles.append(title)
            if pid and not source_doc_id:
                source_doc_id = pid
            page_evidence.append(
                PageEvidence(
                    page=idx, title=title, doc_id=pid,
                    min_conf=p.min_conf, mean_conf=p.mean_conf, line_count=p.line_count,
                )
            )
        if original_doc_id:
            source_doc_id = original_doc_id  # a resend's identity is the original document

        # 3. Patient & DOB
        patient_name: Optional[str] = None
        p_match = self.PATIENT_PATTERN.search(full_text)
        if p_match:
            patient_name = p_match.group(1).strip()
        dob: Optional[str] = None
        dob_match = self.DOB_PATTERN.search(full_text)
        if dob_match:
            dob = dob_match.group(1).strip()

        # 4. Urgent / critical flags. Scanned page by page; a negated mention on one page
        #    ("No urgent flag provided" on a cover sheet) never suppresses a flag on another.
        urgent_flags: List[str] = []
        urgent_value: Optional[str] = None
        total_negated = 0
        for p in page_objs:
            affirmed, negated = self._scan_urgency(p.text)
            total_negated += negated
            for flag in affirmed:
                if flag not in urgent_flags:
                    urgent_flags.append(flag)
            if affirmed and urgent_value is None:
                urgent_value = self._urgent_value(p.text)
        if urgent_flags and total_negated:
            uncertainty.append(
                "Conflicting urgency signals: the document both denies and asserts an urgent/critical flag"
            )

        # 5. Mixed packet
        is_mixed = any(pat.search(full_text) for pat in self.MIXED_PATTERNS) or len(set(page_titles)) > 1

        # 6. Incomplete / unclear
        is_incomplete = any(pat.search(full_text) for pat in self.INCOMPLETE_PATTERNS)

        # 7. Resend marker
        is_resend_marker = bool(self.RESEND_PATTERN.search(full_text))

        # 8. Primary document type
        doc_type_detected = "Unknown"
        if is_mixed:
            doc_type_detected = "Mixed Packet"
        elif is_incomplete:
            doc_type_detected = "Incomplete Document"
        elif is_resend_marker:
            doc_type_detected = "Fax Resend"
        elif len(set(page_titles)) == 1:
            doc_type_detected = page_titles[0]
        else:
            for cat, patterns in self.TITLE_INDICATORS:
                if any(re.search(pat, full_text, re.IGNORECASE) for pat in patterns):
                    doc_type_detected = cat
                    break

        # 9. Evidence gates. Any entry sends the fax to manual review.
        if pages_portal is not None and len(page_objs) != pages_portal:
            uncertainty.append(
                f"Page count mismatch: portal lists {pages_portal} page(s), OCR read {len(page_objs)}"
            )
        for pe, p in zip(page_evidence, page_objs):
            if not p.text.strip():
                uncertainty.append(f"Page {pe.page}: no text recognised")
                continue
            low_line = pe.min_conf is not None and pe.min_conf < OCR_MIN_LINE_CONFIDENCE
            low_mean = pe.mean_conf is not None and pe.mean_conf < OCR_MIN_MEAN_CONFIDENCE
            if low_line or low_mean:
                uncertainty.append(
                    f"Page {pe.page}: low OCR confidence (min {pe.min_conf:.2f}, mean {pe.mean_conf:.2f})"
                )

        confs_min = [p.min_conf for p in page_objs if p.min_conf is not None]
        confs_mean = [p.mean_conf for p in page_objs if p.mean_conf is not None]

        evidence = (
            f"Type: {doc_type_detected}; DocID: {source_doc_id or 'None'}; "
            f"Patient: {patient_name or 'None'}; Urgent: {bool(urgent_flags)}; "
            f"Mixed: {is_mixed}; Incomplete: {is_incomplete}"
        )
        if uncertainty:
            evidence += "; Uncertainty: " + " | ".join(uncertainty)

        return ExtractedDocument(
            fax_id=fax_id,
            source_doc_id=source_doc_id,
            original_doc_id=original_doc_id,
            doc_type_detected=doc_type_detected,
            patient_name=patient_name,
            dob=dob,
            urgent_flags=urgent_flags,
            urgent_value=urgent_value,
            is_resend_marker=is_resend_marker,
            is_mixed=is_mixed,
            is_incomplete=is_incomplete,
            evidence=evidence,
            pages_text=pages_text,
            page_evidence=page_evidence,
            pages_portal=pages_portal,
            pages_ocr=len(page_objs),
            ocr_min_confidence=min(confs_min) if confs_min else None,
            ocr_mean_confidence=(sum(confs_mean) / len(confs_mean)) if confs_mean else None,
            uncertainty=uncertainty,
        )
