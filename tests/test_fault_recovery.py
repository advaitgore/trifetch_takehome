"""The portal's three simulated faults, each against a real portal subprocess (isolated DB).

Assertions read the portal's own audit trail and routes table, so they prove what the
portal recorded (one commit, no blind resubmit), not just that a function returned.
"""
import pytest
from playwright.async_api import async_playwright

from conftest import all_routes
from linda_agent.browser.session_a import SessionAWorker
from linda_agent.browser.session_b import SessionBWorker
from linda_agent.models import RoutingDecision, SelectedFields


def audit_events(db_conn, event=None):
    with db_conn() as c:
        rows = [dict(r) for r in c.execute("SELECT event, fax_id FROM audit_events ORDER BY id")]
    return [r for r in rows if event is None or r["event"] == event]


def decision(fax_id, slug, name, dtype, folder, notes=""):
    return RoutingDecision(
        fax_id=fax_id, action="routed",
        selected_fields=SelectedFields(assignee_slug=slug, assignee_name=name, doc_type=dtype, folder=folder, notes=notes),
    )


@pytest.mark.asyncio
async def test_download_503_once_recovery(portal_server, db_conn):
    """First download returns HTTP 503; the agent backs off, retries and gets a valid PDF."""
    portal_server("download_503_once")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        worker = SessionAWorker(browser)
        await worker.start()
        outcome = await worker.download_fax_pdf("FX-201")
        await worker.close()
        await browser.close()

    assert outcome.retries == 1
    assert any("503" in e for e in outcome.events)
    assert outcome.path.read_bytes().startswith(b"%PDF-")
    assert len(audit_events(db_conn, "download_failed")) == 1   # the injected fault fired once
    assert len(audit_events(db_conn, "download")) == 1          # and exactly one real download followed


@pytest.mark.asyncio
async def test_session_expire_on_route_once_recovery(portal_server, db_conn):
    """The session dies during the first submit: re-login, refill from scratch, commit once."""
    portal_server("session_expire_on_route_once")
    d = decision("FX-204", "ethan", "Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending", "Recovery test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        a = SessionAWorker(browser)
        await a.start()
        await a.download_fax_pdf("FX-204")
        await a.close()

        b = SessionBWorker(browser)
        await b.start()
        out = await b.route_and_confirm(d)
        await b.close()
        await browser.close()

    assert out.verification == "verified_on_portal"
    assert out.retries >= 1 and any("Session expired" in e for e in out.events)
    assert len(audit_events(db_conn, "session_expired")) == 1
    routes = all_routes(db_conn)
    assert [r["fax_id"] for r in routes if r["fax_id"] == "FX-204"] == ["FX-204"]   # exactly one record
    assert routes[[r["fax_id"] for r in routes].index("FX-204")]["assignee"] == "ethan"


@pytest.mark.asyncio
async def test_route_commit_then_503_once_recovery(portal_server, db_conn):
    """Commit succeeds but the response is 503: verify via the UI, do NOT resubmit."""
    portal_server("route_commit_then_503_once")
    d = decision("FX-205", "ava", "Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication", "503 recovery test")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        a = SessionAWorker(browser)
        await a.start()
        await a.download_fax_pdf("FX-205")
        await a.close()

        b = SessionBWorker(browser)
        await b.start()
        out = await b.route_and_confirm(d)
        await b.close()
        await browser.close()

    assert out.verification == "verified_on_portal"
    assert any("503" in e for e in out.events) and any("had committed" in e for e in out.events)
    # The proof that matters: one commit, one lost response, and the agent never POSTed again.
    assert len(audit_events(db_conn, "route_committed")) == 1
    assert len(audit_events(db_conn, "response_lost")) == 1
    assert len(audit_events(db_conn, "repeated_route_blocked")) == 0
    assert [r["fax_id"] for r in all_routes(db_conn)].count("FX-205") == 1
