"""Configuration settings for the Linda Mini Fax Routing Agent."""
from __future__ import annotations

import os
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
CHECKPOINTS_DIR = PROJECT_ROOT / "checkpoints"
CHECKPOINT_FILE = CHECKPOINTS_DIR / "progress_state.json"
RESULTS_JSON_FILE = PROJECT_ROOT / "results.json"
RESULTS_CSV_FILE = PROJECT_ROOT / "results.csv"

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
RETRY_DELAY_SECONDS = float(os.getenv("LINDA_RETRY_DELAY", "1.5"))
MAX_RETRIES = int(os.getenv("LINDA_MAX_RETRIES", "3"))

# OCR Engine
OCR_ENGINE = os.getenv("LINDA_OCR_ENGINE", "rapidocr").lower()

# Ensure directories exist
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
