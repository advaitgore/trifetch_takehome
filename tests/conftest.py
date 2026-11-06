"""Test isolation.

Everything environment-dependent is pointed at throw-away locations *before* any
``linda_agent`` or ``portal`` module is imported (both read their settings at import time):

    LINDA_DB_PATH     portal SQLite DB      -> temp file (never portal/portal.sqlite3)
    LINDA_WORK_DIR    downloads/checkpoints/results -> temp dir (never the repo's run output)
    LINDA_PORTAL_URL  agent's portal URL    -> a free local port for the test server

so a test run cannot dirty the sandbox the agent is later demonstrated against.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TMP_ROOT = Path(tempfile.mkdtemp(prefix="linda_tests_"))
TEST_PORT = _free_port()
TEST_DB = TMP_ROOT / "portal.sqlite3"
TEST_WORK = TMP_ROOT / "work"
TEST_BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

os.environ["LINDA_DB_PATH"] = str(TEST_DB)
os.environ["LINDA_WORK_DIR"] = str(TEST_WORK)
os.environ["LINDA_PORTAL_URL"] = TEST_BASE_URL
os.environ["LINDA_RETRY_DELAY"] = "0.2"
os.environ.pop("LINDA_FAULT_MODE", None)

REAL_DB = ROOT / "portal" / "portal.sqlite3"
REAL_DB_MTIME_AT_START: Optional[float] = REAL_DB.stat().st_mtime if REAL_DB.exists() else None


def _wipe_db_files() -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            p.unlink()


def _reset_work_dir() -> None:
    shutil.rmtree(TEST_WORK, ignore_errors=True)
    (TEST_WORK / "downloads").mkdir(parents=True, exist_ok=True)
    (TEST_WORK / "checkpoints").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def fresh_sandbox():
    """Every test starts from a pristine portal DB and an empty work dir."""
    _wipe_db_files()
    _reset_work_dir()
    from portal.app import init_db  # imported after LINDA_DB_PATH is set

    init_db()
    yield


class PortalServer:
    """Spawns the real portal (uvicorn) on the test port, optionally with a fault mode."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None

    def start(self, fault: Optional[str] = None) -> str:
        self.stop()
        env = {**os.environ}
        env.pop("LINDA_FAULT_MODE", None)
        if fault:
            env["LINDA_FAULT_MODE"] = fault
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "portal.app:app", "--host", "127.0.0.1",
             "--port", str(TEST_PORT), "--log-level", "warning"],
            cwd=str(ROOT), env=env,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{TEST_BASE_URL}/login", timeout=1).read()
                return TEST_BASE_URL
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("test portal exited during startup")
                time.sleep(0.2)
        raise RuntimeError("test portal did not become ready")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


@pytest.fixture
def portal_server() -> Callable[[Optional[str]], str]:
    server = PortalServer()
    yield server.start
    server.stop()


@pytest.fixture
def db_conn():
    """Direct handle on the *test* DB for assertions (never the real sandbox DB)."""
    from portal.app import conn

    return conn


@pytest.fixture(scope="session")
def real_db_snapshot() -> Optional[float]:
    return REAL_DB_MTIME_AT_START


def all_routes(db_conn) -> List[dict]:
    with db_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM routes ORDER BY fax_id")]
