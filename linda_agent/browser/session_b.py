"""Session B: Fresh authenticated session for routing submission and state verification."""
from __future__ import annotations

import asyncio
import logging
from typing import Tuple

from playwright.async_api import Browser, BrowserContext, Page

from linda_agent.browser.auth import login
from linda_agent.config import (
    MAX_RETRIES,
    NAVIGATION_TIMEOUT_MS,
    PORTAL_BASE_URL,
    RETRY_DELAY_SECONDS,
    TWO_SESSION_BARRIER_SECONDS,
)
from linda_agent.models import RoutingDecision

logger = logging.getLogger(__name__)


class SessionBWorker:
    """Manages Session B: Fresh login context, assignment submission, and UI verification."""

    def __init__(self, browser: Browser):
        self.browser = browser
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        """Enforces the temporal barrier and launches isolated browser context for Session B."""
        logger.info(
            f"Enforcing temporal barrier ({TWO_SESSION_BARRIER_SECONDS}s) to guarantee "
            "Session B created_at strictly exceeds all Session A download timestamps..."
        )
        await asyncio.sleep(TWO_SESSION_BARRIER_SECONDS)

        logger.info("Starting FRESH Browser Context B (Assignment & Verification)...")
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        # Authenticate fresh session
        await login(self.page)
        logger.info("Session B successfully authenticated.")

    async def close(self) -> None:
        """Close Browser Context B."""
        if self.context:
            await self.context.close()
            self.context = None
            self.page = None
            logger.info("Closed Browser Context B.")

    async def verify_routing_completed(self, fax_id: str, expected_doc_type: str = "") -> bool:
        """Checks whether the fax is committed as completed on the portal detail view and work queue."""
        assert self.page is not None
        detail_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}"
        logger.info(f"Inspecting detail view for completion: {detail_url}")
        await self.page.goto(detail_url)
        await self.page.wait_for_load_state("networkidle")

        content = await self.page.content()
        if "Routing completed" in content or "This fax cannot be submitted again" in content:
            logger.info(f"Verified 'Routing completed' notice on {detail_url}")
            return True

        # Fallback check on /work-queue (Routed Documents)
        queue_url = f"{PORTAL_BASE_URL}/work-queue"
        logger.info(f"Cross-checking work queue at {queue_url}")
        await self.page.goto(queue_url)
        await self.page.wait_for_load_state("networkidle")

        row = await self.page.query_selector(f"table[aria-label='Routed documents'] tr:has-text('{fax_id}')")
        if row:
            row_text = await row.text_content() or ""
            logger.info(f"Verified fax {fax_id} in work queue: {row_text.strip()}")
            return True

        return False

    async def route_and_confirm(self, decision: RoutingDecision) -> Tuple[str, int]:
        """Routes a single fax using the interactive UI modal, handling synthetic faults.
        
        Returns:
            Tuple[str, int]: (verification_status, retries_count)
        """
        assert self.page is not None
        fax_id = decision.fax_id
        fields = decision.selected_fields

        if not fields:
            raise ValueError(f"No fields provided for routing fax {fax_id}")

        route_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}/route"
        detail_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}"
        retries = 0

        # First, check if already routed before touching the form
        if await self.verify_routing_completed(fax_id):
            logger.info(f"Fax {fax_id} was already completed. Skipping routing submit.")
            return "verified_on_portal", 0

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"Opening route form for {fax_id} (attempt {attempt})...")
            await self.page.goto(route_url)
            await self.page.wait_for_load_state("networkidle")

            # Check if redirected back to detail because it's already routed
            if self.page.url == detail_url or "already" in self.page.url:
                if await self.verify_routing_completed(fax_id):
                    return "verified_on_portal", retries

            # Populate the form using exact option slugs
            logger.info(
                f"Populating route form: Assignee='{fields.assignee_slug}', "
                f"Type='{fields.doc_type}', Folder='{fields.folder}'"
            )
            await self.page.select_option("#assignee", value=fields.assignee_slug)
            await self.page.select_option("#doc_type", value=fields.doc_type)
            await self.page.select_option("#folder", value=fields.folder)
            if fields.notes:
                await self.page.fill("#notes", fields.notes)

            # Click 'Review Routing' to open confirmation modal
            logger.info("Clicking 'Review Routing' button...")
            await self.page.click("button:has-text('Review Routing')")

            # Wait for modal backdrop to gain .show class
            await self.page.wait_for_selector("#confirmation.show", state="visible", timeout=5000)

            # Verify modal summary fields reflect selected options
            summary_assignee = (await self.page.text_content("#summary-assignee") or "").strip()
            summary_type = (await self.page.text_content("#summary-type") or "").strip()
            summary_folder = (await self.page.text_content("#summary-folder") or "").strip()

            assert summary_assignee == fields.assignee_name, (
                f"Modal summary assignee '{summary_assignee}' does not match expected '{fields.assignee_name}'"
            )
            assert summary_type == fields.doc_type, (
                f"Modal summary doc_type '{summary_type}' does not match expected '{fields.doc_type}'"
            )
            assert summary_folder == fields.folder, (
                f"Modal summary folder '{summary_folder}' does not match expected '{fields.folder}'"
            )

            # Click 'OK - Submit Routing'
            logger.info("Clicking 'OK - Submit Routing' in modal...")
            await self.page.click("#confirm-ok")
            await self.page.wait_for_load_state("networkidle")

            # Check for Fault Mode 1: session_expire_on_route_once
            if "/login" in self.page.url:
                retries += 1
                logger.warning(
                    f"Session expired on routing submission for {fax_id} (session_expire_on_route_once). "
                    "Re-authenticating and re-attempting..."
                )
                await login(self.page)
                # Re-loop will re-open route_url with fresh CSRF token and fill again
                continue

            # Check for Fault Mode 2: route_commit_then_503_once
            page_content = await self.page.content()
            if "503" in page_content or "Routing unavailable" in page_content or "Gateway error" in page_content:
                retries += 1
                logger.warning(
                    f"Received 503 response on submit for {fax_id} (route_commit_then_503_once). "
                    "Do NOT retry POST blindly. Inspecting portal UI state to verify commit..."
                )
                if await self.verify_routing_completed(fax_id, fields.doc_type):
                    return "verified_on_portal", retries
                else:
                    logger.error(f"Route was not committed after 503 for {fax_id}. Retrying...")
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue

            # Check for standard success notice
            if "notice_type=success" in self.page.url or "Routing completed" in page_content:
                logger.info(f"Route submission committed and verified for {fax_id}.")
                return "verified_on_portal", retries

            # If separate session was blocked (Two-session invariant failure)
            if "Separate session required" in page_content:
                raise RuntimeError(
                    f"Portal rejected route for {fax_id}: Two-session rule blocked! "
                    "Session B must be created after Session A download."
                )

            # If we reached here, verify on portal
            if await self.verify_routing_completed(fax_id, fields.doc_type):
                return "verified_on_portal", retries

        raise RuntimeError(f"Failed to route and verify fax {fax_id} after {MAX_RETRIES} attempts.")
