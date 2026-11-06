"""Session-loss recovery in Session A, read-back verification, and test-suite isolation."""
import pytest
from playwright.async_api import async_playwright

import conftest
from conftest import all_routes
from linda_agent.browser.portal_reader import MISMATCH, NOT_VERIFIED, VERIFIED, verify_saved
from linda_agent.browser.session_a import SessionAWorker
from linda_agent.browser.session_b import ModalMismatchError, SessionBWorker
from linda_agent.models import RoutingDecision, SelectedFields

AVA = SelectedFields(assignee_slug="ava", assignee_name="Ava Wilson - Nursing / Medication",
                     doc_type="Refill Request", folder="Nursing / Medication")


def decision(fields=AVA, fax_id="FX-205"):
    return RoutingDecision(fax_id=fax_id, action="routed", selected_fields=fields)


async def download_then_open_b(browser, fax_id="FX-205"):
    a = SessionAWorker(browser)
    await a.start()
    await a.download_fax_pdf(fax_id)
    await a.close()
    b = SessionBWorker(browser)
    await b.start()
    return b


@pytest.mark.asyncio
async def test_session_a_recovers_when_session_dies_mid_download_loop(portal_server, db_conn):
    portal_server(None)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        a = SessionAWorker(browser)
        await a.start()
        first = await a.download_fax_pdf("FX-201")
        assert first.path.read_bytes().startswith(b"%PDF-")

        with db_conn() as c:                      # the portal drops every session
            c.execute("DELETE FROM sessions")

        second = await a.download_fax_pdf("FX-202")
        await a.close()
        await browser.close()

    assert second.path.read_bytes().startswith(b"%PDF-")
    assert any("re-authenticating" in e for e in second.events)
    with db_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM downloads WHERE fax_id='FX-202'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_verification_reads_back_saved_values(portal_server, db_conn):
    portal_server(None)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        b = await download_then_open_b(browser)
        out = await b.route_and_confirm(decision())
        assert out.verification == VERIFIED

        wrong = SelectedFields(assignee_slug="liam", assignee_name="Liam Rivera - Billing",
                               doc_type="EOB", folder="Billing / EOBs")
        events = []
        assert await verify_saved(b.page, "FX-205", wrong, events) == MISMATCH   # saved != intended
        assert any("differs" in e for e in events)
        assert await verify_saved(b.page, "FX-203", AVA) == NOT_VERIFIED          # never routed
        await b.close()
        await browser.close()


@pytest.mark.asyncio
async def test_second_route_attempt_does_not_post_again(portal_server, db_conn):
    portal_server(None)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        b = await download_then_open_b(browser)
        first = await b.route_and_confirm(decision())
        second = await b.route_and_confirm(decision())
        await b.close()
        await browser.close()

    assert first.verification == second.verification == VERIFIED
    assert any("already routed" in e for e in second.events)
    with db_conn() as c:
        events = [r[0] for r in c.execute("SELECT event FROM audit_events")]
    assert events.count("route_committed") == 1
    assert events.count("repeated_route_blocked") == 0   # the portal never even saw a second POST
    assert [r["fax_id"] for r in all_routes(db_conn)].count("FX-205") == 1


@pytest.mark.asyncio
async def test_modal_mismatch_raises_and_commits_nothing(portal_server, db_conn):
    portal_server(None)
    bad = SelectedFields(assignee_slug="olivia", assignee_name="Noah Patel - Clinical Review",   # label != slug
                         doc_type="Referral", folder="Referrals / New")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        b = await download_then_open_b(browser)
        with pytest.raises(ModalMismatchError):
            await b.route_and_confirm(decision(bad))
        modal_open = await b.page.locator("#confirmation.show").count()
        await b.close()
        await browser.close()

    assert modal_open == 0                      # the modal was backed out of, not left open
    assert all_routes(db_conn) == [r for r in all_routes(db_conn) if r["fax_id"] == "FX-211"]


def test_suite_never_touches_the_real_sandbox_db(real_db_snapshot):
    """The real portal DB (portal/portal.sqlite3) must be exactly as the run started."""
    real = conftest.REAL_DB
    after = real.stat().st_mtime if real.exists() else None
    assert after == real_db_snapshot
    assert conftest.TEST_DB != real
