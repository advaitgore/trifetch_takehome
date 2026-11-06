"""Navigation that survives session loss anywhere in a browser session."""
from __future__ import annotations

import logging
from typing import List, Optional

from playwright.async_api import Page, Response

from linda_agent.browser.auth import SessionLostError, login

logger = logging.getLogger(__name__)


async def goto_authenticated(
    page: Page,
    url: str,
    events: Optional[List[str]] = None,
    max_relogins: int = 2,
) -> Optional[Response]:
    """``page.goto(url)``; if the portal bounces to /login, sign in again and return to ``url``.

    Raises SessionLostError only if the session is lost ``max_relogins + 1`` times in a row.
    ``events`` collects human-readable recovery notes for the per-fax audit record.
    """
    for attempt in range(max_relogins + 1):
        response = await page.goto(url)
        await page.wait_for_load_state("networkidle")
        if "/login" not in page.url:
            return response
        msg = f"Session lost while opening {url}; re-authenticating (attempt {attempt + 1})"
        logger.warning(msg)
        if events is not None:
            events.append(msg)
        await login(page, in_place=True)
    raise SessionLostError(f"Still redirected to /login after {max_relogins} re-authentications ({url})")
