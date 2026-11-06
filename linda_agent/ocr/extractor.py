"""Document structure extractor that parses structured data and metadata from OCR text."""
from __future__ import annotations

import re
from typing import List, Optional

from linda_agent.models import ExtractedDocument


class DocumentExtractor:
    """Parses raw OCR pages text into an ExtractedDocument model."""

    # Patterns for document IDs
    SOURCE_ID_PATTERNS = [
        re.compile(r"(?:Document\s*(?:ID|reference|number)|Records\s*request\s*ID|Accession(?:\s*Number)?|Request\s*(?:ID|number)|Remittance\s*ID|Transmission\s*reference|Report\s*ID)\s*[:#]?\s*([A-Za-z0-9\-]+)", re.IGNORECASE),
        re.compile(r"\b([A-Z]{2,4}-\d{3,5}[A-Z]?)\b"),
    ]
    ORIGINAL_DOC_ID_PATTERN = re.compile(r"Original\s*Document\s*ID\s*[:#]?\s*([A-Za-z0-9\-]+)", re.IGNORECASE)

    # Patterns for patient details
    PATIENT_PATTERN = re.compile(r"(?:Patient|Member)\s*[:#]\s*([A-Za-z\s\.\-]+?)(?:\s+(?:DOB|Date|Referring|Ordering|Medication|I\s+authorize)|$|\n)", re.IGNORECASE)
    DOB_PATTERN = re.compile(r"(?:DOB|Date\s*of\s*Birth)\s*[:#]?\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)

    # Patterns for urgency & alerts
    URGENT_PATTERNS = [
        re.compile(r"\b(CRITICAL\s+RESULT\s*-\s*CALLING\s+PROVIDER)\b", re.IGNORECASE),
        re.compile(r"\b(CRITICAL\s+VALUE\s+NOTIFICATION)\b", re.IGNORECASE),
        re.compile(r"\b(CRITICAL|STAT|EMERGENCY|URGENT\s+FLAG)\b", re.IGNORECASE),
    ]

    # Primary Title Indicators (typically in top half of document)
    TITLE_INDICATORS = [
        ("Referral", [r"\bNEW\s*PATIENT\s*REFERRAL\s*REQUEST\b", r"\bREFERRAL\s*REQUEST\b", r"\bCONSULTATION\s*REQUEST\b"]),
        ("Lab Result", [r"\bLABORATORY\s*RESULTS\b", r"\bLABORATORY\s*REPORT\b"]),
        ("Imaging Report", [r"\bDIAGNOSTIC\s*IMAGING\s*REPORT\b", r"\bFINAL\s*(?:IMAGING|RADIOLOGY)\s*REPORT\b", r"\bRADIOLOGY\s*REPORT\b"]),
        ("Prior Authorization", [r"\bPRIOR\s*AUTHORIZATION\s*REQUEST\b"]),
        ("Refill Request", [r"\bPRESCRIPTION\s*REFILL\s*REQUEST\b"]),
        ("Records Request", [r"\bREQUEST\s*FOR\s*MEDICAL\s*RECORDS\b", r"\bAUTHORIZATION\s*TO\s*RELEASE\s*INFORMATION\b"]),
        ("EOB", [r"\bEXPLANATION\s*OF\s*BENEFITS\b", r"\bREMITTANCE\s*ADVICE\b"]),
    ]

    def _detect_page_title(self, page_text: str) -> Optional[str]:
        """Detects the primary document header / title from a page."""
        for cat, patterns in self.TITLE_INDICATORS:
            for pat in patterns:
                if re.search(pat, page_text, re.IGNORECASE):
                    return cat
        return None

    def extract(self, fax_id: str, pages_text: List[str]) -> ExtractedDocument:
        full_text = "\n\n".join(pages_text)
        
        # 1. Check for original doc id (Resends)
        original_doc_id: Optional[str] = None
        orig_match = self.ORIGINAL_DOC_ID_PATTERN.search(full_text)
        if orig_match:
            original_doc_id = orig_match.group(1).strip()

        # 2. Extract source document ID
        source_doc_id: Optional[str] = None
        for pattern in self.SOURCE_ID_PATTERNS:
            match = pattern.search(full_text)
            if match:
                source_doc_id = match.group(1).strip()
                break

        # If resend, the primary source doc ID is the original document ID
        if original_doc_id:
            source_doc_id = original_doc_id

        # 3. Extract Patient Name & DOB
        patient_name: Optional[str] = None
        p_match = self.PATIENT_PATTERN.search(full_text)
        if p_match:
            patient_name = p_match.group(1).strip()

        dob: Optional[str] = None
        dob_match = self.DOB_PATTERN.search(full_text)
        if dob_match:
            dob = dob_match.group(1).strip()

        # 4. Check for Urgent / Critical flags
        urgent_flags: List[str] = []
        # Check specifically to avoid false positives on negative phrases like "No urgent flag"
        if not re.search(r"No\s*urgent\s*flag", full_text, re.IGNORECASE):
            for up in self.URGENT_PATTERNS:
                for match in up.finditer(full_text):
                    flag_text = match.group(0).strip()
                    if flag_text not in urgent_flags:
                        urgent_flags.append(flag_text)

        # 5. Check for Mixed Packet
        is_mixed = False
        if re.search(r"MULTIPLE\s*ENCLOSURES", full_text, re.IGNORECASE) or \
           re.search(r"Contains\s*multiple\s*documents", full_text, re.IGNORECASE):
            is_mixed = True
        
        # Check if individual pages contain distinct title categories
        page_titles = set()
        for p_text in pages_text:
            t = self._detect_page_title(p_text)
            if t:
                page_titles.add(t)

        if len(page_titles) > 1:
            is_mixed = True

        # 6. Check for Incomplete / Unclear
        is_incomplete = False
        if re.search(r"Attached\s*page\s*appears\s*incomplete", full_text, re.IGNORECASE) or \
           "UNKNOWN" in full_text.upper():
            is_incomplete = True

        # 7. Detect primary document type
        doc_type_detected = "Unknown"
        if is_mixed:
            doc_type_detected = "Mixed Packet"
        elif is_incomplete:
            doc_type_detected = "Incomplete Document"
        elif re.search(r"RESEND", full_text, re.IGNORECASE):
            doc_type_detected = "Fax Resend"
        else:
            # Check detected page titles first
            if len(page_titles) == 1:
                doc_type_detected = list(page_titles)[0]
            else:
                for cat, patterns in self.TITLE_INDICATORS:
                    for pat in patterns:
                        if re.search(pat, full_text, re.IGNORECASE):
                            doc_type_detected = cat
                            break
                    if doc_type_detected != "Unknown":
                        break

        evidence = (
            f"Type: {doc_type_detected}; DocID: {source_doc_id or 'None'}; "
            f"Patient: {patient_name or 'None'}; Urgent: {bool(urgent_flags)}; "
            f"Mixed: {is_mixed}; Incomplete: {is_incomplete}"
        )

        return ExtractedDocument(
            fax_id=fax_id,
            source_doc_id=source_doc_id,
            original_doc_id=original_doc_id,
            doc_type_detected=doc_type_detected,
            patient_name=patient_name,
            dob=dob,
            urgent_flags=urgent_flags,
            is_mixed=is_mixed,
            is_incomplete=is_incomplete,
            evidence=evidence,
            pages_text=pages_text,
        )
