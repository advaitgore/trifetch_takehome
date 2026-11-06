"""Central Orchestrator for the Linda Mini Fax Routing Agent."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Dict, List

from playwright.async_api import async_playwright

from linda_agent.browser.session_a import SessionAWorker
from linda_agent.browser.session_b import SessionBWorker
from linda_agent.config import (
    HEADLESS,
    OCR_ENGINE,
    RESULTS_CSV_FILE,
    RESULTS_JSON_FILE,
)
from linda_agent.logic.classifier import SOPClassifier
from linda_agent.logic.duplicate_detector import DuplicateDetector
from linda_agent.models import ExtractedDocument, FaxMetadata, FaxResult, RoutingDecision
from linda_agent.ocr.extractor import DocumentExtractor
from linda_agent.ocr.rapid_ocr import RapidOCREngine
from linda_agent.ocr.tesseract_ocr import TesseractOCREngine
from linda_agent.state import CheckpointManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("LindaAgent")


class LindaAgent:
    """End-to-end autonomous browser-use agent for clinical fax routing."""

    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self.state_mgr = CheckpointManager()
        self.extractor = DocumentExtractor()

        # Initialize selected OCR engine
        if OCR_ENGINE == "pytesseract":
            logger.info("Initializing PyTesseract OCR Engine...")
            self.ocr = TesseractOCREngine()
        else:
            logger.info("Initializing RapidOCR (ONNX) Local Engine...")
            self.ocr = RapidOCREngine()

    async def run(self) -> List[FaxResult]:
        """Executes the complete two-session workflow across the full inbox."""
        logger.info("=== Starting Linda Mini Fax Routing Agent ===")

        downloaded_paths: Dict[str, Path] = {}
        metadata_map: Dict[str, FaxMetadata] = {}
        download_retries: Dict[str, int] = {}

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)

            # -------------------------------------------------------------
            # PHASE 1: Session A - Discovery & Acquisition
            # -------------------------------------------------------------
            logger.info("\n--- PHASE 1: Discovery & Acquisition (Session A) ---")
            session_a = SessionAWorker(browser)
            await session_a.start()

            try:
                all_faxes = await session_a.discover_inbound_faxes()
                for meta in all_faxes:
                    metadata_map[meta.fax_id] = meta

                # Download PDFs for unrouted faxes
                for meta in all_faxes:
                    fax_id = meta.fax_id

                    # Check if already completed from a prior checkpoint
                    if self.state_mgr.is_completed(fax_id):
                        logger.info(f"Fax {fax_id} is already completed in checkpoint. Skipping download.")
                        continue

                    # If already marked Routed in the portal (e.g. FX-211)
                    if meta.status == "Routed":
                        logger.info(f"Fax {fax_id} is already routed in portal. Skipping download.")
                        continue

                    # Download PDF through portal UI
                    pdf_path, retries = await session_a.download_fax_pdf(fax_id)
                    downloaded_paths[fax_id] = pdf_path
                    download_retries[fax_id] = retries

            finally:
                await session_a.close()

            # -------------------------------------------------------------
            # PHASE 2: Local OCR & Global SOP Classification
            # -------------------------------------------------------------
            logger.info("\n--- PHASE 2: OCR & Decision Engine ---")
            extracted_docs: Dict[str, ExtractedDocument] = {}

            for fax_id, pdf_path in downloaded_paths.items():
                logger.info(f"Running OCR on {fax_id} ({pdf_path.name})...")
                pages_text = self.ocr.extract_text(pdf_path)
                doc = self.extractor.extract(fax_id, pages_text)
                extracted_docs[fax_id] = doc
                logger.info(f"  Extracted: {doc.evidence}")

            # Identify duplicates across global arrival order (received_at ASCENDING)
            duplicate_fax_ids = DuplicateDetector.identify_duplicates(
                list(metadata_map.values()),
                extracted_docs,
            )

            # Formulate routing decisions
            decisions: Dict[str, RoutingDecision] = {}
            for meta in metadata_map.values():
                fax_id = meta.fax_id
                is_already_routed = (meta.status == "Routed")
                is_duplicate = (fax_id in duplicate_fax_ids)
                doc = extracted_docs.get(fax_id)

                if doc:
                    decision = SOPClassifier.classify(
                        doc,
                        is_duplicate=is_duplicate,
                        is_already_routed=is_already_routed,
                    )
                else:
                    # For pre-routed faxes that didn't need OCR download
                    decision = RoutingDecision(
                        fax_id=fax_id,
                        action="already_routed",
                        reason="Pre-routed in portal before assessment",
                    )
                decisions[fax_id] = decision
                logger.info(f"Decision for {fax_id}: Action={decision.action}; Reason={decision.reason}")

            # -------------------------------------------------------------
            # PHASE 3: Session B - Assignment & Verification
            # -------------------------------------------------------------
            logger.info("\n--- PHASE 3: Routing & Verification (Session B) ---")
            session_b = SessionBWorker(browser)

            # Collect items requiring UI submission
            items_to_route = [
                dec for dec in decisions.values()
                if dec.action in ("routed", "needs_review") and not self.state_mgr.is_completed(dec.fax_id)
            ]

            if items_to_route:
                await session_b.start()
                try:
                    for decision in items_to_route:
                        fax_id = decision.fax_id
                        logger.info(f"\nProcessing routing for {fax_id}...")
                        doc = extracted_docs.get(fax_id)
                        retries = download_retries.get(fax_id, 0)

                        try:
                            verif_status, submit_retries = await session_b.route_and_confirm(decision)
                            total_retries = retries + submit_retries

                            # Record verified result
                            selected_dict = (
                                decision.selected_fields.model_dump()
                                if decision.selected_fields
                                else {}
                            )
                            result = FaxResult(
                                fax_id=fax_id,
                                extracted_type=doc.doc_type_detected if doc else "Unknown",
                                source_id_evidence=doc.evidence if doc else "",
                                action=decision.action,
                                selected_fields=selected_dict,
                                verification=verif_status,
                                errors_retries=total_retries,
                                notes=decision.selected_fields.notes if decision.selected_fields else "",
                            )
                            self.state_mgr.record_result(result)

                        except Exception as e:
                            logger.error(f"Failed routing for {fax_id}: {e}")
                            result = FaxResult(
                                fax_id=fax_id,
                                extracted_type=doc.doc_type_detected if doc else "Unknown",
                                source_id_evidence=doc.evidence if doc else "",
                                action="failed",
                                selected_fields={},
                                verification="failed",
                                errors_retries=retries + 1,
                                notes=str(e),
                            )
                            self.state_mgr.record_result(result)
                finally:
                    await session_b.close()
            else:
                logger.info("No faxes require new routing in Session B.")

            # Record non-routed items (skipped duplicates, already routed)
            for fax_id, decision in decisions.items():
                if not self.state_mgr.is_completed(fax_id):
                    doc = extracted_docs.get(fax_id)
                    verif = "skipped" if decision.action == "skipped_duplicate" else "not_applicable"
                    result = FaxResult(
                        fax_id=fax_id,
                        extracted_type=doc.doc_type_detected if doc else "Pre-routed Document",
                        source_id_evidence=doc.evidence if doc else "Pre-existing portal record",
                        action=decision.action,
                        selected_fields={},
                        verification=verif,
                        errors_retries=0,
                        notes=decision.reason,
                    )
                    self.state_mgr.record_result(result)

            await browser.close()

        # -------------------------------------------------------------
        # PHASE 4: Export Artifacts
        # -------------------------------------------------------------
        results = list(self.state_mgr.all_results().values())
        results.sort(key=lambda r: r.fax_id)
        self.export_results(results)
        self.print_summary(results)
        return results

    def export_results(self, results: List[FaxResult]) -> None:
        """Exports results to results.json and results.csv."""
        # Export JSON
        RESULTS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        json_data = [r.model_dump() for r in results]
        with open(RESULTS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)
        logger.info(f"Exported JSON results: {RESULTS_JSON_FILE}")

        # Export CSV
        with open(RESULTS_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "fax_id",
                "extracted_type",
                "source_id_evidence",
                "action",
                "selected_assignee",
                "selected_doc_type",
                "selected_folder",
                "verification",
                "errors_retries",
                "notes",
            ])
            for r in results:
                writer.writerow([
                    r.fax_id,
                    r.extracted_type,
                    r.source_id_evidence,
                    r.action,
                    r.selected_fields.get("assignee_slug", ""),
                    r.selected_fields.get("doc_type", ""),
                    r.selected_fields.get("folder", ""),
                    r.verification,
                    r.errors_retries,
                    r.notes,
                ])
        logger.info(f"Exported CSV results: {RESULTS_CSV_FILE}")

    def print_summary(self, results: List[FaxResult]) -> None:
        """Prints a clean summary table to stdout."""
        print("\n" + "=" * 90)
        print(f"{'FAX ID':<8} | {'EXTRACTED TYPE':<22} | {'ACTION':<18} | {'STATUS':<20} | {'RETRIES'}")
        print("-" * 90)
        for r in results:
            print(f"{r.fax_id:<8} | {r.extracted_type:<22} | {r.action:<18} | {r.verification:<20} | {r.errors_retries}")
        print("=" * 90 + "\n")
