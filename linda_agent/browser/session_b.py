"""Session B: Fresh authenticated session for routing submission and state verification."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List

from playwright.async_api import Browser, BrowserContext, Page

from linda_agent.browser.auth import login
from linda_agent.browser.navigation import goto_authenticated
from linda_agent.browser.portal_reader import MISMATCH, NOT_VERIFIED, VERIFIED, read_detail_routing, verify_saved
from linda_agent.config import (
    MAX_RETRIES,
    NAVIGATION_TIMEOUT_MS,
    PORTAL_BASE_URL,
    RETRY_DELAY_SECONDS,
    TWO_SESSION_BARRIER_SECONDS,
)
from linda_agent.models import RoutingDecision

logger = logging.getLogger(__name__)


class ModalMismatchError(RuntimeError):
    """The confirmation modal summarised different values than the ones we selected."""


class RouteRejectedError(RuntimeError):
    """The portal rejected the submission outright (400/404/...); retrying would not help."""

    def __init__(self, fax_id: str, status: int):
        super().__init__(f"Portal rejected route for {fax_id} with HTTP {status}")
        self.status = status


@dataclass
class RouteOutcome:
    """Result of one routing attempt sequence.

    ``verification`` is a portal read-back status (verified_on_portal / verified_mismatch /
    not_verified). ``deferred`` means the two-session rule (HTTP 409) blocked the submit and
    the fax needs a fresh Session A download + Session B (no route was committed).
    """
    verification: str = NOT_VERIFIED
    retries: int = 0
    deferred: bool = False
    events: List[str] = field(default_factory=list)


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
        await login(self.page)
        logger.info("Session B successfully authenticated.")

    async def close(self) -> None:
        """Close Browser Context B."""
        if self.context:
            await self.context.close()
            self.context = None
            self.page = None
            logger.info("Closed Browser Context B.")

    async def _cancel_modal(self) -> None:
        assert self.page is not None
        await self.page.click("#confirmation button:has-text('Back')")

    async def _check_modal_summary(self, decision: RoutingDecision) -> None:
        """Compare the confirmation modal against intent; raises ModalMismatchError (never `assert`)."""
        assert self.page is not None and decision.selected_fields is not None
        f = decision.selected_fields
        got = {
            "assignee": (await self.page.text_content("#summary-assignee") or "").strip(),
            "doc_type": (await self.page.text_content("#summary-type") or "").strip(),
            "folder": (await self.page.text_content("#summary-folder") or "").strip(),
        }
        want = {"assignee": f.assignee_name, "doc_type": f.doc_type, "folder": f.folder}
        if got != want:
            await self._cancel_modal()
            raise ModalMismatchError(
                f"Confirmation modal shows {got} but intended {want} for {decision.fax_id}"
            )

    async def route_and_confirm(self, decision: RoutingDecision) -> RouteOutcome:
        """Routes one fax through the UI modal, driven by the HTTP status of the submit.

        POST /faxes/{id}/route outcomes:
          303 -> ?notice_type=success|already : committed; verify saved values
          303 -> /login                        : session expired; re-login, refill from scratch
          503                                  : outcome UNKNOWN; read portal state, never blind-resubmit
          409                                  : two-session rule; defer to a new Session A -> B cycle
          403                                  : stale CSRF; reopen the form and retry
          400 / 404 / other                    : rejected; raise RouteRejectedError
        """
        assert self.page is not None
        page = self.page
        fields = decision.selected_fields
        fax_id = decision.fax_id
        if not fields:
            raise ValueError(f"No fields provided for routing fax {fax_id}")

        detail_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}"
        route_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}/route"
        outcome = RouteOutcome()

        for attempt in range(1, MAX_RETRIES + 1):
            # Always look before submitting: an earlier attempt or run may already have committed.
            await goto_authenticated(page, detail_url, outcome.events)
            if await read_detail_routing(page) is not None:
                outcome.events.append(
                    f"{fax_id} already routed in portal before submit (attempt {attempt}); verifying, not resubmitting"
                )
                logger.info(outcome.events[-1])
                outcome.verification = await verify_saved(page, fax_id, fields, outcome.events)
                return outcome

            logger.info(f"Opening route form for {fax_id} (attempt {attempt})...")
            await goto_authenticated(page, route_url, outcome.events)

            logger.info(
                f"Populating route form: Assignee='{fields.assignee_slug}', "
                f"Type='{fields.doc_type}', Folder='{fields.folder}'"
            )
            await page.select_option("#assignee", value=fields.assignee_slug)
            await page.select_option("#doc_type", value=fields.doc_type)
            await page.select_option("#folder", value=fields.folder)
            if fields.notes:
                await page.fill("#notes", fields.notes)

            await page.click("button:has-text('Review Routing')")
            await page.wait_for_selector("#confirmation.show", state="visible", timeout=5000)
            await self._check_modal_summary(decision)

            logger.info("Clicking 'OK - Submit Routing' in modal...")
            async with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith(f"/faxes/{fax_id}/route")
            ) as resp_info:
                await page.click("#confirm-ok")
            resp = await resp_info.value
            status = resp.status
            location = resp.headers.get("location", "")
            await page.wait_for_load_state("networkidle")
            logger.info(f"Route POST for {fax_id} returned HTTP {status} {location}".rstrip())

            if 300 <= status < 400:
                if "/login" in location:
                    outcome.retries += 1
                    outcome.events.append(
                        f"Session expired during submit of {fax_id} (303 to /login); re-authenticated, refilling form"
                    )
                    logger.warning(outcome.events[-1])
                    await login(page, in_place=True)
                    continue
                if "notice_type=already" in location:
                    outcome.events.append(f"Portal reports {fax_id} was already routed; verifying saved values")
                outcome.verification = await verify_saved(page, fax_id, fields, outcome.events)
                return outcome

            if status == 503:
                outcome.retries += 1
                outcome.events.append(
                    f"HTTP 503 after submitting {fax_id}: outcome unknown, reading portal state before any retry"
                )
                logger.warning(outcome.events[-1])
                verification = await verify_saved(page, fax_id, fields, outcome.events)
                if verification != NOT_VERIFIED:
                    outcome.events.append(f"{fax_id}: route had committed despite the 503")
                    outcome.verification = verification
                    return outcome
                outcome.events.append(f"{fax_id}: route not committed after the 503; safe to resubmit")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            if status == 409:
                outcome.deferred = True
                outcome.events.append(
                    f"HTTP 409 for {fax_id}: portal has no earlier-session download on record; deferring to a new session cycle"
                )
                logger.warning(outcome.events[-1])
                return outcome

            if status == 403:
                outcome.retries += 1
                outcome.events.append(f"HTTP 403 (stale form token) for {fax_id}; reopening the form")
                logger.warning(outcome.events[-1])
                continue

            raise RouteRejectedError(fax_id, status)

        raise RuntimeError(f"Failed to route and verify fax {fax_id} after {MAX_RETRIES} attempts.")
