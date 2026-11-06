"""Configuration settings for the Linda Mini Fax Routing Agent."""
from __future__ import annotations

import os
from pathlib import Path

# Paths. LINDA_WORK_DIR relocates all runtime artifacts (downloads, checkpoints, results);
# the test-suite points it at a temp dir so tests never touch the repo's real run output.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.getenv("LINDA_WORK_DIR", str(PROJECT_ROOT))).resolve()
DOWNLOADS_DIR = WORK_DIR / "downloads"
CHECKPOINTS_DIR = WORK_DIR / "checkpoints"
CHECKPOINT_FILE = CHECKPOINTS_DIR / "progress_state.json"
RESULTS_JSON_FILE = WORK_DIR / "results.json"
RESULTS_CSV_FILE = WORK_DIR / "results.csv"

# Portal credentials and URL
PORTAL_BASE_URL = os.getenv("LINDA_PORTAL_URL", "http://127.0.0.1:8000").rstrip("/")
USERNAME = os.getenv("LINDA_USERNAME", "agent.demo")
PASSWORD = os.getenv("LINDA_PASSWORD", "demo-password-123")

# Browser settings
HEADLESS = os.getenv("LINDA_HEADLESS", "true").lower() in ("true", "1", "yes")
BROWSER_TIMEOUT_MS = int(os.getenv("LINDA_BROWSER_TIMEOUT_MS", "30000"))
NAVIGATION_TIMEOUT_MS = int(os.getenv("LINDA_NAV_TIMEOUT_MS", "15000"))

# Timing and Invariant settings
TWO_SESSION_BARRIER_SECONDS = float(os.getenv("LINDA_BARRIER_SECONDS", "1.2"))
RETRY_DELAY_SECONDS = float(os.getenv("LINDA_RETRY_DELAY", "1.5"))  # base of the exponential backoff
MAX_RETRIES = int(os.getenv("LINDA_MAX_RETRIES", "3"))

# OCR Engine
OCR_ENGINE = os.getenv("LINDA_OCR_ENGINE", "rapidocr").lower()
OCR_CACHE_DIR = CHECKPOINTS_DIR / "ocr_cache"

# Evidence gates. Measured on the 12 sandbox faxes (see ARCHITECTURE.md): the lowest
# per-page line confidence was 0.935 and the lowest per-page mean was 0.974. The floors
# below sit far under those values so the gate only trips on genuinely degraded scans.
OCR_MIN_LINE_CONFIDENCE = float(os.getenv("LINDA_OCR_MIN_LINE_CONF", "0.60"))
OCR_MIN_MEAN_CONFIDENCE = float(os.getenv("LINDA_OCR_MIN_MEAN_CONF", "0.85"))

# Maximum Session A -> Session B cycles per run (a 409 two-session rejection needs a second one).
MAX_CYCLES = int(os.getenv("LINDA_MAX_CYCLES", "2"))
ROUTE_NOTE_MAX_CHARS = 1000  # the portal truncates notes at 1000 characters

# Ensure directories exist
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
