"""Integration tests demonstrating resilience against the portal's three simulated fault modes.

Each test launches the local portal subprocess configured with LINDA_FAULT_MODE
as specified in the exercise README, verifying autonomous error recovery.
"""
import asyncio
import os
import subprocess
import sys
import time
import pytest
from pathlib import Path
from playwright.async_api import async_playwright

from linda_agent.browser.session_a import SessionAWorker
from linda_agent.browser.session_b import SessionBWorker
from linda_agent.models import RoutingDecision, SelectedFields
from portal.app import conn

TEST_PORT = 8001
TEST_BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def reset_portal_db():
    """Resets the sandbox database to clean state."""
    with conn() as c:
        for t in ["audit_events", "routes", "downloads", "sessions", "fault_events", "faxes"]:
            c.execute(f"DELETE FROM {t}")
    from portal.app import init_db
    init_db()


class FaultServerContext:
    """Manages a dedicated portal server instance with injected fault mode."""

    def __init__(self, fault_mode: str):
        self.fault_mode = fault_mode
        self.process: subprocess.Popen | None = None

    def __enter__(self):
        reset_portal_db()
        env = {
            **os.environ,
            "LINDA_FAULT_MODE": self.fault_mode,
            "LINDA_PORTAL_URL": TEST_BASE_URL,
        }
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "portal.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(TEST_PORT),
            "--log-level",
            "warning",
        ]
        self.process = subprocess.Popen(cmd, env=env)
        time.sleep(1.0)  # Wait for server startup
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


@pytest.mark.asyncio
async def test_download_503_once_recovery(monkeypatch):
    """Verifies that SessionA catches the transient download 503 and retries successfully."""
    monkeypatch.setattr("linda_agent.config.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.session_a.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.auth.PORTAL_BASE_URL", TEST_BASE_URL)

    with FaultServerContext("download_503_once"):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            worker_a = SessionAWorker(browser)
            await worker_a.start()

            # Download FX-201 (first download attempt triggers simulated 503)
            pdf_path, retries = await worker_a.download_fax_pdf("FX-201")
            await worker_a.close()
            await browser.close()

            assert retries >= 1, "Expected at least 1 retry due to download_503_once"
            assert pdf_path.exists(), "PDF must exist"
            with open(pdf_path, "rb") as f:
                assert f.read(5).startswith(b"%PDF-"), "Downloaded file must be valid PDF"


@pytest.mark.asyncio
async def test_session_expire_on_route_once_recovery(monkeypatch):
    """Verifies that SessionB handles session expiration during submit, re-authenticates, and commits."""
    monkeypatch.setattr("linda_agent.config.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.session_a.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.auth.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.session_b.PORTAL_BASE_URL", TEST_BASE_URL)

    with FaultServerContext("session_expire_on_route_once"):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            
            # Step 1: Session A downloads FX-204
            worker_a = SessionAWorker(browser)
            await worker_a.start()
            await worker_a.download_fax_pdf("FX-204")
            await worker_a.close()

            # Step 2: Session B routes FX-204 (first submit triggers session expiration)
            worker_b = SessionBWorker(browser)
            await worker_b.start()

            decision = RoutingDecision(
                fax_id="FX-204",
                action="routed",
                selected_fields=SelectedFields(
                    assignee_slug="ethan",
                    assignee_name="Ethan Brooks - Authorizations",
                    doc_type="Prior Authorization",
                    folder="Authorizations / Pending",
                    notes="Recovery test",
                ),
            )

            verif_status, retries = await worker_b.route_and_confirm(decision)
            await worker_b.close()
            await browser.close()

            assert retries >= 1, "Expected at least 1 retry due to session_expire_on_route_once"
            assert verif_status == "verified_on_portal"


@pytest.mark.asyncio
async def test_route_commit_then_503_once_recovery(monkeypatch):
    """Verifies that SessionB detects a post-commit 503 and confirms completion via UI inspection."""
    monkeypatch.setattr("linda_agent.config.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.session_a.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.auth.PORTAL_BASE_URL", TEST_BASE_URL)
    monkeypatch.setattr("linda_agent.browser.session_b.PORTAL_BASE_URL", TEST_BASE_URL)

    with FaultServerContext("route_commit_then_503_once"):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            
            # Step 1: Session A downloads FX-205
            worker_a = SessionAWorker(browser)
            await worker_a.start()
            await worker_a.download_fax_pdf("FX-205")
            await worker_a.close()

            # Step 2: Session B routes FX-205 (submit succeeds but returns 503)
            worker_b = SessionBWorker(browser)
            await worker_b.start()

            decision = RoutingDecision(
                fax_id="FX-205",
                action="routed",
                selected_fields=SelectedFields(
                    assignee_slug="ava",
                    assignee_name="Ava Wilson - Nursing / Medication",
                    doc_type="Refill Request",
                    folder="Nursing / Medication",
                    notes="503 recovery test",
                ),
            )

            verif_status, retries = await worker_b.route_and_confirm(decision)
            await worker_b.close()
            await browser.close()

            assert verif_status == "verified_on_portal"
