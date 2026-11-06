"""Session A: Login, Inbound queue discovery, work-queue read and PDF acquisition."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from playwright.async_api import Browser, BrowserContext, Download, Page, Response

from linda_agent.browser.auth import login
from linda_agent.browser.navigation import goto_authenticated
from linda_agent.browser.portal_reader import SavedRoute, read_routed_rows
from linda_agent.config import (
    DOWNLOADS_DIR,
    MAX_RETRIES,
    NAVIGATION_TIMEOUT_MS,
    PORTAL_BASE_URL,
    RETRY_DELAY_SECONDS,
)
from linda_agent.models import FaxMetadata

logger = logging.getLogger(__name__)

DOWNLOAD_WAIT_SECONDS = 8.0


@dataclass
class DownloadOutcome:
    path: Path
    retries: int = 0
    events: List[str] = field(default_factory=list)


class SessionAWorker:
    """Manages Session A: Inbound fax discovery and PDF downloads."""

    def __init__(self, browser: Browser):
        self.browser = browser
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        """Launch isolated browser context for Session A."""
        logger.info("Starting Browser Context A (Discovery & Acquisition)...")
        self.context = await self.browser.new_context(accept_downloads=True)
        self.page = await self.context.new_page()
        self.page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    async def close(self) -> None:
        """Close Browser Context A."""
        if self.context:
            await self.context.close()
            self.context = None
            self.page = None
            logger.info("Closed Browser Context A.")

    async def login(self) -> None:
        assert self.page is not None
        await login(self.page)

    # ------------------------------------------------------------------ discovery
    async def discover_inbound_faxes(self) -> List[FaxMetadata]:
        """Signs in, then walks Fax Management -> Inbound Faxes through the visible UI, all pages."""
        assert self.page is not None
        await self.login()

        for restart in range(3):
            faxes = await self._walk_inbox()
            if faxes is not None:
                logger.info(f"Discovered total {len(faxes)} faxes across inbox.")
                return faxes
            logger.warning("Session lost during inbox discovery; re-authenticating and restarting")
            await login(self.page, in_place=True)
        raise RuntimeError("Could not complete inbox discovery: session kept expiring")

    async def _walk_inbox(self) -> List[FaxMetadata] | None:
        """One pass over the inbox; None if the session was lost part-way."""
        page = self.page
        assert page is not None
        logger.info("Navigating via visible UI: Fax Management -> Inbound Faxes")
        await page.click("a.nav:has-text('Fax Management')")
        await page.wait_for_load_state("networkidle")
        await page.click("a.nav:has-text('Inbound Faxes')")
        await page.wait_for_load_state("networkidle")

        all_faxes: List[FaxMetadata] = []
        page_num = 1
        while True:
            if "/login" in page.url:
                return None
            logger.info(f"Reading Inbound Faxes page {page_num}...")
            await page.wait_for_selector("table[aria-label='Inbound faxes'] tbody tr")
            rows = await page.query_selector_all("table[aria-label='Inbound faxes'] tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 6:
                    continue
                all_faxes.append(
                    FaxMetadata(
                        fax_id=(await cells[0].text_content() or "").strip(),
                        received_at=(await cells[1].text_content() or "").strip(),
                        sender=(await cells[2].text_content() or "").strip(),
                        pages=int((await cells[3].text_content() or "1").strip()),
                        status=(await cells[4].text_content() or "").strip(),
                    )
                )
            next_button = await page.query_selector("div.pagination a.btn:has-text('Next')")
            if not next_button:
                return all_faxes
            page_num += 1
            await next_button.click()
            await page.wait_for_load_state("networkidle")

    async def read_routed_documents(self) -> Dict[str, List[SavedRoute]]:
        """Open Fax Management -> Routed Documents via the sidebar and parse the table."""
        page = self.page
        assert page is not None
        await page.click("a.nav:has-text('Routed Documents')")
        await page.wait_for_load_state("networkidle")
        if "/login" in page.url:
            await login(page, in_place=True)
            await goto_authenticated(page, f"{PORTAL_BASE_URL}/work-queue")
        await page.wait_for_selector("table[aria-label='Routed documents']")
        return await read_routed_rows(page)

    # ------------------------------------------------------------------ download
    async def download_fax_pdf(self, fax_id: str) -> DownloadOutcome:
        """Download the scanned PDF for ``fax_id`` through the portal's Download button.

        Branches on the HTTP status of the download request, not on page text:
          200 + download event -> validate and save
          503                  -> transient (download_503_once): exponential backoff, retry
          3xx                  -> session expired: re-authenticate, retry
        """
        assert self.page is not None
        page = self.page
        detail_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}"
        download_suffix = f"/faxes/{fax_id}/download"
        save_path = DOWNLOADS_DIR / f"{fax_id}.pdf"
        outcome = DownloadOutcome(path=save_path)
        last_error = "unknown"

        for attempt in range(1, MAX_RETRIES + 1):
            await goto_authenticated(page, detail_url, outcome.events)

            done = asyncio.Event()
            box: Dict[str, object] = {}

            def on_response(resp: Response) -> None:
                if resp.url.split("?")[0].endswith(download_suffix):
                    box["status"] = resp.status
                    if resp.status >= 300:
                        done.set()

            def on_download(dl: Download) -> None:
                box["download"] = dl
                done.set()

            page.on("response", on_response)
            page.on("download", on_download)
            try:
                logger.info(f"Clicking Download Fax PDF for {fax_id} (attempt {attempt})...")
                await page.click("#download-fax")
                try:
                    await asyncio.wait_for(done.wait(), DOWNLOAD_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    pass
            finally:
                page.remove_listener("response", on_response)
                page.remove_listener("download", on_download)

            status = box.get("status")
            download = box.get("download")

            if download is not None and (status is None or status < 300):
                await download.save_as(save_path)  # type: ignore[union-attr]
                header = save_path.read_bytes()[:5] if save_path.exists() else b""
                if save_path.exists() and save_path.stat().st_size >= 100 and header.startswith(b"%PDF-"):
                    logger.info(f"Downloaded {save_path.name} ({save_path.stat().st_size} bytes).")
                    return outcome
                last_error = f"downloaded file is not a valid PDF (header {header!r})"
            elif status == 503:
                last_error = "HTTP 503 from download (transient)"
            elif isinstance(status, int) and 300 <= status < 400:
                last_error = f"HTTP {status} redirect (session expired)"
            else:
                last_error = f"no download and no response status within {DOWNLOAD_WAIT_SECONDS:.0f}s"

            outcome.retries += 1
            outcome.events.append(f"Download attempt {attempt} for {fax_id} failed: {last_error}")
            logger.warning(outcome.events[-1])
            if attempt < MAX_RETRIES:
                backoff = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))  # 1.5s, 3.0s, 6.0s ...
                logger.info(f"Retrying download in {backoff:.1f}s...")
                await asyncio.sleep(backoff)

        raise RuntimeError(f"Failed to download PDF for {fax_id} after {MAX_RETRIES} attempts ({last_error}).")
