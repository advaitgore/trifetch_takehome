# TriFetch Linda Mini — Autonomous Fax Routing Browser Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Playwright](https://img.shields.io/badge/playwright-tested-green.svg)](https://playwright.dev/)
[![Tests](https://img.shields.io/badge/tests-6%20passed-brightgreen.svg)]()

An autonomous, resilient browser-use agent designed to automate inbound healthcare fax triage within the fictional **"Linda Mini"** clinical sandbox. The agent processes scanned image-only PDFs, applies clinical SOP routing policies and safety exception rules, enforces strict two-session authentication invariants, and autonomously recovers from network, session, and commit faults.

---

## 1. Quickstart

### Prerequisites
* Python 3.11+
* Chrome / Chromium (Playwright will install automatically)

### Setup & Run in 3 Commands

```bash
# 1. Clone repository
git clone https://github.com/advaitgore/trifetch_takehome.git
cd trifetch_takehome

# 2. Install dependencies & Playwright browsers
pip install -r requirements.txt
playwright install chromium

# 3. Start the local portal in one terminal
python run_portal.py

# 4. In a second terminal, run the agent
python -m linda_agent.cli run
```

*To run with the browser visible (headed mode):*
```bash
python -m linda_agent.cli run --headed
```

---

## 2. End-to-End Execution Results

On running against the 12 inbound faxes, the agent produces `results.json` and `results.csv`:

| Fax ID | Extracted Document Type | Clinical SOP Action | Portal Verification | Retries | Clinical / SOP Rationale |
|---|---|---|---|---|---|
| **FX-201** | Referral | `routed` | `verified_on_portal` | 0 | Earliest original referral (RF-3001) $\rightarrow$ Olivia Chen / Referrals / New |
| **FX-202** | Lab Result | `needs_review` | `verified_on_portal` | 0 | **Critical Alert**: Potassium 6.3 mmol/L $\rightarrow$ Jordan Ellis (Note: alert flag recorded) |
| **FX-203** | Imaging Report | `routed` | `verified_on_portal` | 0 | Routine cervical spine MRI $\rightarrow$ Noah Patel / Clinical / Imaging |
| **FX-204** | Prior Authorization | `routed` | `verified_on_portal` | 0 | Lumbar spine MRI PA $\rightarrow$ Ethan Brooks / Authorizations / Pending |
| **FX-205** | Refill Request | `routed` | `verified_on_portal` | 0 | Lisinopril 10mg renewal $\rightarrow$ Ava Wilson / Nursing / Medication |
| **FX-206** | Records Request | `routed` | `verified_on_portal` | 0 | Medical records & release $\rightarrow$ Mia Gomez / Records / Requests |
| **FX-207** | EOB | `routed` | `verified_on_portal` | 0 | Insurer remittance (EOB-4007) $\rightarrow$ Liam Rivera / Billing / EOBs |
| **FX-208** | Lab Result | `routed` | `verified_on_portal` | 0 | Routine CBC panel ("No urgent flag") $\rightarrow$ Noah Patel / Clinical / Labs |
| **FX-209** | Incomplete Document | `needs_review` | `verified_on_portal` | 0 | **Incomplete Packet**: Unknown sender, partial pages $\rightarrow$ Jordan Ellis |
| **FX-210** | Fax Resend | `skipped_duplicate` | `skipped` | 0 | **Confirmed Resend**: RF-3001 matches earlier FX-201 $\rightarrow$ Skipped in UI |
| **FX-211** | Pre-routed Document | `already_routed` | `not_applicable` | 0 | **Pre-routed**: Pre-assigned by clinic staff $\rightarrow$ Preserved without alteration |
| **FX-212** | Mixed Packet | `needs_review` | `verified_on_portal` | 0 | **Mixed Packet**: Contains both Lab (`LAB-4012A`) & Imaging (`IMG-4012B`) $\rightarrow$ Jordan Ellis |

---

## 3. Key Architectural Innovations

### 1. The Two-Session Authentication Invariant
The sandbox backend (`app.py:432-437`) enforces:
$$\text{downloaded\_at} < \text{session\_B.created\_at} \quad\text{and}\quad \text{session\_A.id} \neq \text{session\_B.id}$$
* **Session A (Context A)**: Discovers inbox items and downloads unrouted PDFs to `./downloads/`. Context A is then completely closed.
* **Temporal Barrier**: Enforces a $\ge 1.2\text{s}$ pause to guarantee clock resolution.
* **Session B (Context B)**: Launches a fresh browser context with distinct cookies, logs in anew, and commits assignments.

### 2. Zero-Setup Local OCR (`RapidOCR` + `pypdfium2`)
* Pure Python & ONNX Runtime bundled models.
* **No external C++ binary installers needed** (unlike PyTesseract, which fails on Windows if `tesseract.exe` is missing from system PATH).
* Extracts text with ~99% accuracy across all test faxes.
* Modular adapters for `pytesseract` and multimodal LLM vision (Gemini / OpenAI / Anthropic) are included if configured.

### 3. Global Arrival Order & Duplicate Resends
The portal inbox is sorted in **descending order** (`received_at DESC`). `FX-210` (Bayview resend at 08:49) appears on Page 1, while `FX-201` (Bayview original at 08:02) appears on Page 2.
* An agent routing inline as it paginates would incorrectly route the resend first.
* **Solution**: The agent decouples discovery from routing. It indexes and OCRs all faxes, sorts by `received_at` **ASCENDING**, routes the original (`FX-201`), and flags `FX-210` as a skipped duplicate.

### 4. Interactive Modal & Dropdown Slug Alignment
* Dropdown options carry slug values (`"olivia"`, `"noah"`, `"ethan"`, etc.). Submitting display labels causes HTTP 400. The agent maps slugs for form submission and verifies display text inside the confirmation modal (`#confirmation.show`).

---

## 4. Resilience & Simulated Fault Recovery

The agent autonomously recovers from all three sandbox fault injection modes:

1. **`download_503_once`**: Detects transient 503 error HTML, applies exponential backoff, and re-clicks download. Validates `%PDF-` header.
2. **`session_expire_on_route_once`**: Detects session expiry on submit, re-authenticates at `/login`, re-navigates, re-populates form, and re-submits.
3. **`route_commit_then_503_once`**: Detects post-commit 503, avoids redundant retry, inspects detail view and `/work-queue`, and verifies commit status.

---

## 5. Running the Automated Test Suite

Run all automated unit and integration tests covering the SOP classifier, the two-session invariant, and all three fault recovery modes:

```bash
# Run the complete test suite
pytest tests/ -v
```

*Run specific test suites:*
```bash
# 1. SOP Classifier & all 12 fax rules
pytest tests/test_classifier.py -v

# 2. Strict Two-Session Invariant
pytest tests/test_two_session.py -v

# 3. Fault injection & self-healing recovery
pytest tests/test_fault_recovery.py -v
```

---

## 6. Repository Structure

```
trifetch_takehome/
├── linda_agent/
│   ├── browser/
│   │   ├── auth.py             # Login, re-auth, session status checks
│   │   ├── session_a.py        # Queue pagination & PDF acquisition
│   │   └── session_b.py        # Modal routing, verification & fault handlers
│   ├── ocr/
│   │   ├── base.py             # Abstract OCR interface
│   │   ├── rapid_ocr.py        # Local offline ONNX OCR engine
│   │   ├── tesseract_ocr.py    # PyTesseract fallback adapter
│   │   ├── llm_vision.py       # Multimodal LLM vision adapter
│   │   └── extractor.py        # Document IDs, patient details & flag parser
│   ├── logic/
│   │   ├── classifier.py       # Deterministic SOP & safety rules
│   │   └── duplicate_detector.py # Global arrival duplicate filter
│   ├── agent.py                # Main orchestrator & audit exporter
│   ├── config.py               # Timeouts, endpoints & paths
│   ├── models.py               # Pydantic schemas for all entities
│   └── state.py                # Durable atomic checkpoint manager
├── portal/                     # Local sandbox portal (FastAPI + SQLite)
│   ├── app.py
│   ├── reset_sandbox.py
│   └── data/faxes/
├── tests/
│   ├── test_classifier.py      # Tests for all 12 faxes & edge cases
│   ├── test_fault_recovery.py  # End-to-end tests for all 3 fault modes
│   └── test_two_session.py     # Server two-session contract validation
├── checkpoints/                # progress_state.json: resumable per-fax state (runtime)
├── results.json                # Final per-fax audit artifact
├── results.csv                 # Tabular audit artifact
├── run_agent.py                # Single-command agent launcher
├── run_portal.py               # Portal server launcher with fault injection
├── requirements.txt            # Python dependencies
├── ARCHITECTURE.md             # Comprehensive technical notes & tradeoffs
└── README.md                   # System documentation
```
