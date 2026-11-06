"""Read-only parsers for what the portal actually saved (detail page + Routed Documents)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from playwright.async_api import Page

from linda_agent.config import PORTAL_BASE_URL
from linda_agent.models import SelectedFields

logger = logging.getLogger(__name__)

VERIFIED = "verified_on_portal"
MISMATCH = "verified_mismatch"
NOT_VERIFIED = "not_verified"


@dataclass
class SavedRoute:
    """The three dropdown values the portal shows for a routed fax."""
    assignee_name: str
    doc_type: str
    folder: str

    def differences(self, expected: SelectedFields) -> List[str]:
        diffs = []
        if self.assignee_name != expected.assignee_name:
            diffs.append(f"assignee saved '{self.assignee_name}' != expected '{expected.assignee_name}'")
        if self.doc_type != expected.doc_type:
            diffs.append(f"doc type saved '{self.doc_type}' != expected '{expected.doc_type}'")
        if self.folder != expected.folder:
            diffs.append(f"folder saved '{self.folder}' != expected '{expected.folder}'")
        return diffs


async def read_detail_routing(page: Page) -> Optional[SavedRoute]:
    """Parse the 'Document routing' card of a fax detail page. None if the fax is not routed.

    The page has two ``dl.kv`` blocks (transmission info and routing), so the selector is
    scoped to the card whose heading is 'Document routing'.
    """
    card = page.locator("div.card", has=page.locator("h2", has_text="Document routing"))
    dts = [t.strip() for t in await card.locator("dl.kv dt").all_text_contents()]
    dds = [t.strip() for t in await card.locator("dl.kv dd").all_text_contents()]
    kv: Dict[str, str] = dict(zip(dts, dds))
    if kv.get("Routing status") != "Completed":
        return None
    return SavedRoute(
        assignee_name=kv.get("Assigned To", ""),
        doc_type=kv.get("Document Type", ""),
        folder=kv.get("Document Folder", ""),
    )


async def read_routed_rows(page: Page) -> Dict[str, List[SavedRoute]]:
    """Parse the Routed Documents table: fax_id -> list of rows (a list so duplicates are visible)."""
    rows = await page.query_selector_all("table[aria-label='Routed documents'] tbody tr")
    out: Dict[str, List[SavedRoute]] = {}
    for row in rows:
        cells = [(await c.text_content() or "").strip() for c in await row.query_selector_all("td")]
        if len(cells) < 5:
            continue
        out.setdefault(cells[0], []).append(
            SavedRoute(assignee_name=cells[2], doc_type=cells[1], folder=cells[3])
        )
    return out


async def open_work_queue(page: Page, events: Optional[List[str]] = None) -> Dict[str, List[SavedRoute]]:
    """Open /work-queue (re-authenticating if needed) and parse it."""
    from linda_agent.browser.navigation import goto_authenticated

    await goto_authenticated(page, f"{PORTAL_BASE_URL}/work-queue", events)
    await page.wait_for_selector("table[aria-label='Routed documents']")
    return await read_routed_rows(page)


async def verify_saved(
    page: Page,
    fax_id: str,
    expected: SelectedFields,
    events: Optional[List[str]] = None,
) -> str:
    """Compare what the portal saved with what we intended, from the detail page and the queue.

    Returns VERIFIED, MISMATCH (routed but values differ) or NOT_VERIFIED (not routed, or the
    two views disagree about whether it is).
    """
    from linda_agent.browser.navigation import goto_authenticated

    await goto_authenticated(page, f"{PORTAL_BASE_URL}/faxes/{fax_id}", events)
    detail = await read_detail_routing(page)
    queue = await open_work_queue(page, events)
    rows = queue.get(fax_id, [])

    if detail is None and not rows:
        return NOT_VERIFIED
    if detail is None or not rows:
        logger.error(f"{fax_id}: detail page and work queue disagree about routing state")
        return NOT_VERIFIED
    if len(rows) != 1:
        logger.error(f"{fax_id}: expected exactly one Routed Documents row, found {len(rows)}")
        return MISMATCH

    diffs = detail.differences(expected) + [f"queue: {d}" for d in rows[0].differences(expected)]
    if diffs:
        logger.error(f"{fax_id}: saved route differs from intended: {'; '.join(diffs)}")
        if events is not None:
            events.append("Saved route differs from intended: " + "; ".join(diffs))
        return MISMATCH

    logger.info(
        f"{fax_id}: verified saved route: assignee='{detail.assignee_name}', "
        f"type='{detail.doc_type}', folder='{detail.folder}' (exactly 1 queue row)"
    )
    return VERIFIED
