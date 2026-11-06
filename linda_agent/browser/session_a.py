"""Session A: Login, Inbound queue discovery, and PDF acquisition."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Tuple

from playwright.async_api import Browser, BrowserContext, Page

from linda_agent.browser.auth import is_logged_in, login
from linda_agent.config import (
    DOWNLOADS_DIR,
    MAX_RETRIES,
    NAVIGATION_TIMEOUT_MS,
    PORTAL_BASE_URL,
    RETRY_DELAY_SECONDS,
)
from linda_agent.models import FaxMetadata

logger = logging.getLogger(__name__)


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

    async def discover_inbound_faxes(self) -> List[FaxMetadata]:
        """Navigates to Inbound Faxes and traverses all pages to index all transmissions."""
        assert self.page is not None

        # Sign in
        await login(self.page)

        # Navigate via visible UI: Fax Management -> Inbound Faxes
        logger.info("Navigating via visible UI: Fax Management -> Inbound Faxes")
        await self.page.click("a.nav:has-text('Fax Management')")
        await self.page.wait_for_load_state("networkidle")
        await self.page.click("a.nav:has-text('Inbound Faxes')")
        await self.page.wait_for_load_state("networkidle")

        all_faxes: List[FaxMetadata] = []
        page_num = 1

        while True:
            logger.info(f"Reading Inbound Faxes page {page_num}...")
            # Wait for table rows to be present
            await self.page.wait_for_selector("table[aria-label='Inbound faxes'] tbody tr")
            rows = await self.page.query_selector_all("table[aria-label='Inbound faxes'] tbody tr")

            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 6:
                    continue
                fax_id = (await cells[0].text_content() or "").strip()
                received_at = (await cells[1].text_content() or "").strip()
                sender = (await cells[2].text_content() or "").strip()
                pages_str = (await cells[3].text_content() or "1").strip()
                status_text = (await cells[4].text_content() or "").strip()

                meta = FaxMetadata(
                    fax_id=fax_id,
                    sender=sender,
                    received_at=received_at,
                    pages=int(pages_str),
                    status=status_text,
                )
                all_faxes.append(meta)

            # Check for pagination 'Next' link
            next_button = await self.page.query_selector("div.pagination a.btn:has-text('Next')")
            if next_button:
                page_num += 1
                await next_button.click()
                await self.page.wait_for_load_state("networkidle")
            else:
                break

        logger.info(f"Discovered total {len(all_faxes)} faxes across inbox.")
        return all_faxes

    async def download_fax_pdf(self, fax_id: str) -> Tuple[Path, int]:
        """Downloads the scanned PDF for the given fax_id, handling transient 503 errors.
        
        Returns:
            Tuple[Path, int]: (saved_pdf_path, retry_count)
        """
        assert self.page is not None
        if not await is_logged_in(self.page):
            await login(self.page)

        target_url = f"{PORTAL_BASE_URL}/faxes/{fax_id}"
        save_path = DOWNLOADS_DIR / f"{fax_id}.pdf"
        retries = 0

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"Navigating to {target_url} to download PDF (attempt {attempt})...")
            await self.page.goto(target_url)
            await self.page.wait_for_load_state("networkidle")

            try:
                async with self.page.expect_download(timeout=3500) as download_info:
                    await self.page.click("#download-fax")

                download = await download_info.value
                await download.save_as(save_path)

                # Verify file integrity
                if not save_path.exists() or save_path.stat().st_size < 100:
                    raise ValueError(f"Downloaded file {save_path} is empty or missing.")

                with open(save_path, "rb") as f:
                    header = f.read(5)
                    if not header.startswith(b"%PDF-"):
                        raise ValueError(f"File {save_path} is not a valid PDF (header: {header}).")

                logger.info(f"Successfully downloaded {save_path.name} ({save_path.stat().st_size} bytes).")
                return save_path, retries

            except Exception as e:
                retries += 1
                logger.warning(f"Download attempt {attempt} failed for {fax_id}: {e}")
                
                # Check for 503 error notice in DOM (download_503_once fault)
                error_notice = await self.page.query_selector("div.notice.error")
                if error_notice:
                    notice_text = await error_notice.text_content() or ""
                    logger.warning(f"Detected portal error notice: {notice_text.strip()}")

                if attempt < MAX_RETRIES:
                    backoff = RETRY_DELAY_SECONDS * attempt
                    logger.info(f"Retrying download in {backoff} seconds...")
                    await asyncio.sleep(backoff)
                else:
                    raise RuntimeError(f"Failed to download PDF for {fax_id} after {MAX_RETRIES} attempts.") from e

        return save_path, retries
