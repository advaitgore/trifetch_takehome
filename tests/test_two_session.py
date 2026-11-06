"""Tests for the Two-Session Invariant enforced by the Linda Mini portal."""
import pytest
import time
import httpx
from portal.app import app, conn


@pytest.mark.asyncio
async def test_two_session_rejection_in_same_session():
    """Verifies that attempting to route within the same session as the download is rejected (HTTP 409)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Step 1: Login in Session A
        login_res = await client.post("/login", data={"username": "agent.demo", "password": "demo-password-123"})
        assert login_res.status_code == 303

        # Step 2: Download FX-201 in Session A
        dl_res = await client.get("/faxes/FX-201/download")
        assert dl_res.status_code == 200

        # Step 3: Get CSRF token
        form_res = await client.get("/faxes/FX-201/route")
        assert form_res.status_code == 200
        # Parse csrf from HTML
        import re
        csrf_match = re.search(r'name=["\']csrf["\']\s+value=["\']([^"\']+)["\']', form_res.text)
        assert csrf_match, "CSRF token not found"
        csrf = csrf_match.group(1)

        # Step 4: Attempt to route FX-201 in the SAME Session A
        route_res = await client.post(
            "/faxes/FX-201/route",
            data={
                "csrf": csrf,
                "assignee": "olivia",
                "doc_type": "Referral",
                "folder": "Referrals / New",
                "notes": "Testing same-session routing",
            },
        )
        # Server MUST reject with 409 Conflict
        assert route_res.status_code == 409, f"Expected 409 Conflict, got {route_res.status_code}"
        assert "Separate session required" in route_res.text


@pytest.mark.asyncio
async def test_two_session_success_in_distinct_subsequent_session():
    """Verifies that routing succeeds when Session B logs in strictly after Session A's download."""
    transport = httpx.ASGITransport(app=app)

    # Session A: Download FX-203
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a:
        login_res = await client_a.post("/login", data={"username": "agent.demo", "password": "demo-password-123"})
        assert login_res.status_code == 303
        dl_res = await client_a.get("/faxes/FX-203/download")
        assert dl_res.status_code == 200

    # Enforce clock resolution
    time.sleep(0.05)

    # Session B: Fresh login after download
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:
        login_res = await client_b.post("/login", data={"username": "agent.demo", "password": "demo-password-123"})
        assert login_res.status_code == 303

        form_res = await client_b.get("/faxes/FX-203/route")
        assert form_res.status_code == 200
        import re
        csrf_match = re.search(r'name=["\']csrf["\']\s+value=["\']([^"\']+)["\']', form_res.text)
        assert csrf_match
        csrf = csrf_match.group(1)

        route_res = await client_b.post(
            "/faxes/FX-203/route",
            data={
                "csrf": csrf,
                "assignee": "noah",
                "doc_type": "Imaging Report",
                "folder": "Clinical / Imaging",
                "notes": "Valid separate session routing",
            },
        )
        # Server MUST accept and redirect with success
        assert route_res.status_code == 303
        assert "/faxes/FX-203?notice_type=success" in route_res.headers["location"]
