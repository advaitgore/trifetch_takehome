"""Central Orchestrator for the Linda Mini Fax Routing Agent.

Run shape (a Session A -> Session B *cycle*, repeated at most MAX_CYCLES times):

    Session A  login, discover inbox (UI), read Routed Documents, reconcile checkpoint,
               download every fax that still needs work, close
    (offline)  OCR (cached) -> extract -> duplicate analysis in arrival order -> SOP decisions
    barrier    >= TWO_SESSION_BARRIER_SECONDS
    Session B  fresh context + login; for each fax: write intent, submit, read back saved values
    cycle 2    only for faxes the portal rejected with 409 (two-session rule), e.g. after a
               portal reset: new download session, barrier, new Session B. Hard-capped.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import Browser, async_playwright

from linda_agent.browser.portal_reader import MISMATCH, NOT_VERIFIED, SavedRoute
from linda_agent.browser.session_a import DownloadOutcome, SessionAWorker
from linda_agent.browser.session_b import SessionBWorker
from linda_agent.config import (
    HEADLESS,
    MAX_CYCLES,
    OCR_CACHE_DIR,
    OCR_ENGINE,
    RESULTS_CSV_FILE,
    RESULTS_JSON_FILE,
)
from linda_agent.logic.classifier import SOPClassifier
from linda_agent.logic.duplicate_detector import DuplicateDetector
from linda_agent.models import (
    ExtractedDocument,
    FaxMetadata,
    FaxResult,
    RoutingDecision,
    SelectedFields,
)
from linda_agent.ocr.cache import CachedOCR
from linda_agent.ocr.extractor import DocumentExtractor
from linda_agent.state import CheckpointManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("LindaAgent")


def _build_ocr():
    """Instantiate the configured OCR engine wrapped in the content-addressed cache."""
    if OCR_ENGINE == "pytesseract":
        from linda_agent.ocr.tesseract_ocr import TesseractOCREngine

        logger.info("Initializing PyTesseract OCR Engine...")
        return CachedOCR(TesseractOCREngine(), OCR_CACHE_DIR, "pytesseract")
    from linda_agent.ocr.rapid_ocr import RapidOCREngine

    logger.info("Initializing RapidOCR (ONNX) Local Engine...")
    return CachedOCR(RapidOCREngine(), OCR_CACHE_DIR, "rapidocr")


class LindaAgent:
    """End-to-end autonomous browser-use agent for clinical fax routing."""

    def __init__(self, headless: bool = HEADLESS, ocr=None, state_mgr: Optional[CheckpointManager] = None):
        self.headless = headless
        self.state_mgr = state_mgr or CheckpointManager()
        self.extractor = DocumentExtractor()
        self.ocr = ocr if ocr is not None else _build_ocr()

        # Per-run working data
        self.metadata_map: Dict[str, FaxMetadata] = {}
        self.extracted_docs: Dict[str, ExtractedDocument] = {}
        self.decisions: Dict[str, RoutingDecision] = {}
        self.download_info: Dict[str, DownloadOutcome] = {}

    # ================================================================== helpers
    def _result(
        self,
        fax_id: str,
        decision: Optional[RoutingDecision],
        verification: str,
        *,
        action: Optional[str] = None,
        retries: int = 0,
        notes: str = "",
        recovery: Optional[List[str]] = None,
        extracted_type: Optional[str] = None,
        evidence: Optional[str] = None,
    ) -> FaxResult:
        meta = self.metadata_map.get(fax_id)
        doc = self.extracted_docs.get(fax_id)
        fields = decision.selected_fields.model_dump() if decision and decision.selected_fields else {}
        return FaxResult(
            fax_id=fax_id,
            extracted_type=extracted_type or (doc.doc_type_detected if doc else "Unknown"),
            source_id_evidence=evidence if evidence is not None else (doc.evidence if doc else ""),
            action=action or (decision.action if decision else "failed"),
            selected_fields=fields,
            verification=verification,
            errors_retries=retries,
            notes=notes,
            received_at=meta.received_at if meta else "",
            source_doc_id=doc.source_doc_id if doc else None,
            original_doc_id=doc.original_doc_id if doc else None,
            patient_name=doc.patient_name if doc else None,
            dob=doc.dob if doc else None,
            is_resend_marker=doc.is_resend_marker if doc else False,
            pages_portal=meta.pages if meta else None,
            pages_ocr=doc.pages_ocr if doc else None,
            ocr_min_confidence=doc.ocr_min_confidence if doc else None,
            ocr_mean_confidence=doc.ocr_mean_confidence if doc else None,
            uncertainty=list(doc.uncertainty) if doc else [],
            recovery=list(recovery or []),
        )

    @staticmethod
    def _describe(route: SavedRoute) -> str:
        return f"{route.doc_type} | {route.assignee_name} | {route.folder}"

    def _reconcile(self, portal_routes: Dict[str, List[SavedRoute]]) -> None:
        """Trust the portal over the checkpoint.

        * A checkpoint entry claiming a route the portal does not show is invalidated (stale
          checkpoint, e.g. after a sandbox reset) and will be reprocessed.
        * A route present in the portal but different from what we recorded is flagged
          ``verified_mismatch``, never resubmitted.
        * An intent (written before a submit) whose route is now in the portal means a previous
          run was interrupted after the commit: finalise it instead of calling it pre-routed.
        """
        sm = self.state_mgr
        for fax_id, res in list(sm.all_results().items()):
            if fax_id not in self.metadata_map:
                continue
            in_portal = fax_id in portal_routes
            if res.action in ("routed", "needs_review") and sm.is_completed(fax_id):
                if not in_portal:
                    logger.warning(f"Checkpoint says {fax_id} is routed but the portal has no route; reprocessing")
                    sm.invalidate(fax_id)
                else:
                    diffs = portal_routes[fax_id][0].differences(SelectedFields(**res.selected_fields))
                    if diffs and res.verification != MISMATCH:
                        logger.error(f"{fax_id}: portal route differs from checkpoint: {diffs}")
                        res.verification = MISMATCH
                        res.recovery.append("Startup reconciliation: portal route differs from checkpoint: " + "; ".join(diffs))
                        sm.record_result(res)
            elif res.action == "already_routed" and not in_portal:
                logger.warning(f"Checkpoint says {fax_id} was pre-routed but the portal has no route; reprocessing")
                sm.invalidate(fax_id)

        for fax_id in list(sm.intents):
            intent = sm.get_intent(fax_id)
            if intent is not None and fax_id in portal_routes and not sm.is_completed(fax_id):
                diffs = portal_routes[fax_id][0].differences(SelectedFields(**intent.selected_fields))
                verdict = MISMATCH if diffs else "verified_on_portal"
                intent.verification = verdict
                intent.recovery.append(
                    "Recovered from an interrupted run: route was found committed in the portal"
                    + (f" but differs: {'; '.join(diffs)}" if diffs else " and matches the recorded intent")
                )
                logger.info(f"Finalised interrupted submit for {fax_id}: {verdict}")
                sm.record_result(intent)
            else:
                sm.clear_intent(fax_id)

    # ================================================================== main run
    async def run(self) -> List[FaxResult]:
        logger.info("=== Starting Linda Mini Fax Routing Agent ===")
        deferred: List[str] = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)
            try:
                for cycle in range(1, MAX_CYCLES + 1):
                    logger.info(f"\n=== CYCLE {cycle}/{MAX_CYCLES} ===")
                    if cycle == 1:
                        to_route = await self._cycle_one_session_a(browser)
                    else:
                        to_route = await self._followup_session_a(browser, deferred)
                    deferred = await self._session_b(browser, to_route) if to_route else []
                    if not deferred:
                        break
                    logger.warning(f"{len(deferred)} fax(es) deferred by the two-session rule: {deferred}")
            finally:
                await browser.close()

        for fax_id in deferred:
            dec = self.decisions.get(fax_id)
            info = self.download_info.get(fax_id)
            self.state_mgr.record_result(
                self._result(
                    fax_id, dec, "failed", action="failed",
                    retries=info.retries if info else 0,
                    notes=f"Two-session rule (HTTP 409) still blocking after {MAX_CYCLES} cycle(s)",
                    recovery=info.events if info else [],
                )
            )

        results = sorted(self.state_mgr.all_results().values(), key=lambda r: r.fax_id)
        self.export_results(results)
        self.print_summary(results)
        return results

    # ------------------------------------------------------------------ cycle 1
    async def _cycle_one_session_a(self, browser: Browser) -> List[RoutingDecision]:
        sm = self.state_mgr
        logger.info("\n--- PHASE 1: Discovery & Acquisition (Session A) ---")
        session_a = SessionAWorker(browser)
        await session_a.start()
        try:
            all_faxes = await session_a.discover_inbound_faxes()
            self.metadata_map = {m.fax_id: m for m in all_faxes}
            portal_routes = await session_a.read_routed_documents()
            self._reconcile(portal_routes)

            targets: List[str] = []
            for meta in all_faxes:
                fid = meta.fax_id
                if sm.is_completed(fid):
                    logger.info(f"{fid}: already completed in checkpoint and confirmed by the portal; skipping")
                elif fid in portal_routes or meta.status == "Routed":
                    logger.info(f"{fid}: already routed in portal; will not be modified")
                else:
                    targets.append(fid)

            for fid in targets:
                try:
                    self.download_info[fid] = await session_a.download_fax_pdf(fid)
                except Exception as e:
                    logger.error(f"Download failed for {fid}: {e}")
                    sm.record_result(
                        self._result(fid, None, "failed", action="failed",
                                     retries=3, notes=f"Download failed: {e}")
                    )
        finally:
            await session_a.close()

        # ----- OCR + decisions (offline, no browser session open)
        logger.info("\n--- PHASE 2: OCR & Decision Engine ---")
        for fid in targets:
            info = self.download_info.get(fid)
            if not info:
                continue
            logger.info(f"Running OCR on {fid} ({info.path.name})...")
            pages = self.ocr.extract_pages(info.path)
            doc = self.extractor.extract(fid, pages, pages_portal=self.metadata_map[fid].pages)
            self.extracted_docs[fid] = doc
            logger.info(f"  Extracted: {doc.evidence}")

        report = DuplicateDetector.analyze(
            list(self.metadata_map.values()), self.extracted_docs, sm.completed_results()
        )

        for fid, doc in self.extracted_docs.items():
            dec = SOPClassifier.classify(
                doc,
                is_duplicate=fid in report.confirmed,
                duplicate_uncertainty=report.uncertain.get(fid),
            )
            self.decisions[fid] = dec
            logger.info(f"Decision for {fid}: Action={dec.action}; Reason={dec.reason}")

        # ----- record faxes that need no browser submit
        for fid, dec in self.decisions.items():
            if dec.action == "skipped_duplicate":
                orig = report.confirmed.get(fid, "")
                sm.record_result(
                    self._result(fid, dec, "skipped", notes=f"{dec.reason} (original: {orig})",
                                 retries=self.download_info[fid].retries,
                                 recovery=self.download_info[fid].events)
                )
        for meta in all_faxes:
            fid = meta.fax_id
            if sm.is_completed(fid) or fid in self.decisions or fid in targets:
                continue
            if fid in portal_routes or meta.status == "Routed":
                existing = (
                    "Pre-routed in portal: " + self._describe(portal_routes[fid][0])
                    if fid in portal_routes else "Pre-routed in portal"
                )
                sm.record_result(
                    self._result(
                        fid, RoutingDecision(fax_id=fid, action="already_routed", reason=existing),
                        "not_applicable", notes=existing,
                        extracted_type="Pre-routed Document", evidence="Pre-existing portal record",
                    )
                )

        route_items = [
            d for d in self.decisions.values()
            if d.action in ("routed", "needs_review") and not sm.is_completed(d.fax_id)
        ]
        # Oldest first: originals are always handled before anything that could be their resend.
        route_items.sort(key=lambda d: (self.metadata_map[d.fax_id].received_at, d.fax_id))
        return route_items

    # ------------------------------------------------------------------ cycle >= 2
    async def _followup_session_a(self, browser: Browser, deferred: List[str]) -> List[RoutingDecision]:
        """A fresh download session for faxes the portal refused to route (HTTP 409)."""
        logger.info("\n--- FOLLOW-UP Session A: re-downloading deferred faxes ---")
        session_a = SessionAWorker(browser)
        await session_a.start()
        redo: List[RoutingDecision] = []
        try:
            await session_a.login()
            for fid in deferred:
                try:
                    previous = self.download_info.get(fid)
                    fresh = await session_a.download_fax_pdf(fid)
                    if previous:  # keep the audit trail (incl. the 409) across cycles
                        fresh.events = previous.events + fresh.events
                        fresh.retries += previous.retries
                    self.download_info[fid] = fresh
                    redo.append(self.decisions[fid])
                except Exception as e:
                    logger.error(f"Follow-up download failed for {fid}: {e}")
                    self.state_mgr.record_result(
                        self._result(fid, self.decisions.get(fid), "failed", action="failed",
                                     notes=f"Follow-up download failed: {e}")
                    )
        finally:
            await session_a.close()
        return redo

    # ------------------------------------------------------------------ Session B
    async def _session_b(self, browser: Browser, items: List[RoutingDecision]) -> List[str]:
        """Route ``items`` in a fresh Session B. Returns fax IDs deferred by HTTP 409."""
        sm = self.state_mgr
        logger.info("\n--- PHASE 3: Routing & Verification (Session B) ---")
        deferred: List[str] = []
        session_b = SessionBWorker(browser)
        await session_b.start()
        try:
            for dec in items:
                fid = dec.fax_id
                logger.info(f"\nProcessing routing for {fid}...")
                info = self.download_info.get(fid)
                dl_retries = info.retries if info else 0
                dl_events = list(info.events) if info else []
                notes = dec.selected_fields.notes if dec.selected_fields else ""

                # Intent first: if we die after the portal commits, the next run can finalise this.
                sm.record_intent(self._result(fid, dec, "pending", retries=dl_retries,
                                              notes=notes, recovery=dl_events))
                try:
                    out = await session_b.route_and_confirm(dec)
                except Exception as e:
                    logger.error(f"Failed routing for {fid}: {e}")
                    sm.record_result(
                        self._result(fid, dec, "failed", action="failed", retries=dl_retries + 1,
                                     notes=f"{type(e).__name__}: {e}", recovery=dl_events)
                    )
                    continue

                if out.deferred:
                    sm.clear_intent(fid)
                    if info:
                        info.events.extend(out.events)
                    deferred.append(fid)
                    continue

                if out.verification == NOT_VERIFIED:
                    sm.record_result(
                        self._result(fid, dec, "not_verified", action="failed",
                                     retries=dl_retries + out.retries,
                                     notes="Submit finished but the portal does not show the route",
                                     recovery=dl_events + out.events)
                    )
                else:
                    sm.record_result(
                        self._result(fid, dec, out.verification, retries=dl_retries + out.retries,
                                     notes=notes, recovery=dl_events + out.events)
                    )
        finally:
            await session_b.close()
        return deferred

    # ================================================================== export
    def export_results(self, results: List[FaxResult]) -> None:
        """Exports results to results.json and results.csv."""
        RESULTS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RESULTS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump([r.model_dump() for r in results], f, indent=2)
        logger.info(f"Exported JSON results: {RESULTS_JSON_FILE}")

        with open(RESULTS_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "fax_id", "extracted_type", "source_id_evidence", "action",
                "selected_assignee", "selected_doc_type", "selected_folder",
                "verification", "errors_retries", "notes",
                "source_doc_id", "patient_name", "pages_portal", "pages_ocr",
                "ocr_min_confidence", "uncertainty", "recovery",
            ])
            for r in results:
                writer.writerow([
                    r.fax_id, r.extracted_type, r.source_id_evidence, r.action,
                    r.selected_fields.get("assignee_slug", ""),
                    r.selected_fields.get("doc_type", ""),
                    r.selected_fields.get("folder", ""),
                    r.verification, r.errors_retries, r.notes,
                    r.source_doc_id or "", r.patient_name or "",
                    "" if r.pages_portal is None else r.pages_portal,
                    "" if r.pages_ocr is None else r.pages_ocr,
                    "" if r.ocr_min_confidence is None else f"{r.ocr_min_confidence:.3f}",
                    " | ".join(r.uncertainty), " | ".join(r.recovery),
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
