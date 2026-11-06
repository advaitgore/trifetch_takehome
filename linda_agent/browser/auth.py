"""Authentication helpers for Playwright browser sessions."""
from __future__ import annotations

import logging

from playwright.async_api import Page

from linda_agent.config import PASSWORD, PORTAL_BASE_URL, USERNAME

logger = logging.getLogger(__name__)


class SessionLostError(RuntimeError):
    """The portal kept redirecting to /login even after re-authentication."""


async def login(page: Page, username: str = USERNAME, password: str = PASSWORD, in_place: bool = False) -> None:
    """Signs in to the Linda Mini portal.

    With ``in_place=True`` and the page already on /login (a session-expiry redirect), the
    form is filled where it stands so the portal's ``next=`` target is preserved.
    """
    if not (in_place and "/login" in page.url):
        logger.info(f"Navigating to login page: {PORTAL_BASE_URL}/login")
        await page.goto(f"{PORTAL_BASE_URL}/login")
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("button[type='submit']")
    await page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        error_el = await page.query_selector("div.notice.error")
        msg = (await error_el.text_content() if error_el else "Unknown login failure").strip()
        raise RuntimeError(f"Login failed at {page.url}: {msg}")
    logger.info("Login completed successfully.")


async def is_logged_in(page: Page) -> bool:
    """Checks whether the current page session is authenticated."""
    content = await page.content()
    return "Sign out" in content or "/logout" in content
