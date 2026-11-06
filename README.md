# TriFetch Linda Mini: Fax Routing Browser Agent

A Playwright agent that processes the 12 inbound faxes in the local "Linda Mini" sandbox. It
downloads each scanned (image-only) PDF through the portal UI, OCRs every page locally, applies
the routing SOP and its exception rules, submits assignments from a **second, separate login
session**, and verifies what the portal actually saved. It survives the portal's three injected
faults and can be stopped and restarted without duplicating or losing work.

Design details, failure handling and known gaps: [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/advaitgore/trifetch_takehome.git
cd trifetch_takehome

pip install -r requirements.txt
playwright install chromium

# terminal 1: the sandbox portal (http://127.0.0.1:8000)
python run_portal.py

# terminal 2: the agent
python -m linda_agent.cli run
```

`python run_agent.py` is equivalent. Add `--headed` to watch the browser, and
`--reset-checkpoint` to discard saved progress.

For a fresh sandbox between runs: stop the portal, run `python portal/reset_sandbox.py`
(deletes only `portal/portal.sqlite3`), delete `checkpoints/progress_state.json`, and start again.
A checkpoint that outlives a portal reset is detected and reprocessed, but starting clean is simplest.

Fault injection (restart the portal with one of these):

```bash
python run_portal.py --fault download_503_once
python run_portal.py --fault session_expire_on_route_once
python run_portal.py --fault route_commit_then_503_once
```

Outputs: `results.json` and `results.csv` (one record per fax), `checkpoints/progress_state.json`.

## Result on the sandbox

Produced by the run committed in this repo (real RapidOCR, fresh portal DB, no faults).
All 12 match the brief's ground truth; every routed fax was read back from the portal and
the work queue holds exactly one row per routed fax.

| Fax | Detected | Action | Assignee / Type / Folder | Why |
|---|---|---|---|---|
| FX-201 | Referral | routed | olivia / Referral / Referrals / New | earliest original of RF-3001 |
| FX-202 | Lab Result | needs_review | jordan / Needs Review / Review / Unclassified | critical flag; note carries the flag and `Potassium 6.3 mmol/L` |
| FX-203 | Imaging Report | routed | noah / Imaging Report / Clinical / Imaging | routine |
| FX-204 | Prior Authorization | routed | ethan / Prior Authorization / Authorizations / Pending | same sender and time as FX-203; matched by fax ID |
| FX-205 | Refill Request | routed | ava / Refill Request / Nursing / Medication | routine |
| FX-206 | Records Request | routed | mia / Records Request / Records / Requests | routine |
| FX-207 | EOB | routed | liam / EOB / Billing / EOBs | routine |
| FX-208 | Lab Result | routed | noah / Lab Result / Clinical / Labs | "no urgent flag" is a negation, not a flag |
| FX-209 | Incomplete Document | needs_review | jordan / Needs Review / Review / Unclassified | unknown sender, incomplete page (`U-4009`) |
| FX-210 | Fax Resend | skipped_duplicate | none | resend of RF-3001 from FX-201; never submitted |
| FX-211 | pre-routed | already_routed | none | already in Routed Documents; left untouched |
| FX-212 | Mixed Packet | needs_review | jordan / Needs Review / Review / Unclassified | lab (`LAB-4012A`) plus imaging (`IMG-4012B`) in one packet |

## How the hard parts are handled

* **Two sessions.** The portal only accepts a route if the fax was downloaded in an earlier,
  different login session. Session A downloads and closes; after a 1.2 s barrier, Session B opens
  as a new browser context with a new login and submits.
* **Resend before original.** The inbox is newest first, so FX-210 appears before FX-201. The agent
  indexes and OCRs everything, then decides in ascending arrival order. A resend is skipped only
  with a shared source-document ID **and** corroboration (resend marker or matching patient);
  otherwise it goes to Manual Review.
* **Dropdown slugs.** Option values are slugs (`olivia`, `noah`, ...). The confirmation modal is
  waited on and its summary is compared with the intended values before submitting.
* **Faults.** Branching is on HTTP status. `download_503_once`: backoff and retry.
  `session_expire_on_route_once`: re-login and refill the form from scratch.
  `route_commit_then_503_once`: read the portal state first and never resubmit blindly.
  If the portal says the two-session rule is unmet (409), the agent runs one bounded extra
  download-then-route cycle.
* **Verification.** After a submit the agent reads the fax's detail page and the Routed Documents
  table and compares the saved assignee, type and folder with the intent. It does not trust the HTTP
  response alone. The routing note is not displayed by the portal, so it cannot be read back.
* **Restart safety.** Progress, extraction evidence and a pre-submit intent are checkpointed.
  Failures stay failures and are retried. On startup the checkpoint is reconciled against the portal.

## Known gaps (read before relying on this)

* **The routing note is never read back.** The portal stores it (truncated to 1000 characters) but
  does not display it on any page. The agent verifies assignee, document type and folder; it cannot
  verify that the note text was saved.
* **Fax pages are opened by URL (deliberate deviation).** Login, the inbox walk and the Routed
  Documents link go through the visible UI. Opening `/faxes/{id}` and `/faxes/{id}/route` uses a
  direct `goto` instead of clicking the row. The fax ID is the key in both sessions. We chose this to
  limit scope; the brief asks for UI navigation, so this item is only partly met.
* **OCR makes mistakes.** The committed results contain `OoliverGrant` (FX-204), `SydneyPatel`
  (FX-205), `ElenaHart` (FX-210) and `PkT-4012` (FX-212; the brief has `PKT-4012`). These affect
  recorded evidence only; the document titles that drive routing were read correctly on all 11
  downloaded faxes. No accuracy percentage is claimed.
* **Rules are keyword based.** An unfamiliar layout falls to Manual Review, not to a wrong route.

More in [ARCHITECTURE.md](ARCHITECTURE.md#6-known-gaps-and-deliberate-deviations).

## Tests

```bash
pytest tests -q
```

45 tests, about 3.5 minutes (most of that is real browser runs). `pytest tests/test_classifier.py
tests/test_duplicate_detector.py tests/test_state.py -q` runs the logic suite in about a second.

Tests run against an isolated portal (temp DB, temp work directory, free port) and never touch
`portal/portal.sqlite3` or this repo's `downloads/`, `checkpoints/` or `results.*`. The classifier
tests use checked-in OCR fixtures in `tests/fixtures/ocr` (regenerate with
`python tests/fixtures/regenerate.py` after a run).

## Layout

```
linda_agent/
  agent.py              orchestrator: cycles, reconciliation, intents, export
  cli.py                python -m linda_agent.cli run
  config.py             paths, timeouts, confidence floors (env-overridable)
  models.py             pydantic models (FaxResult also carries extraction evidence)
  state.py              atomic checkpoint + pre-submit intents
  browser/
    auth.py             login (including in-place re-login)
    navigation.py       goto_authenticated: survives session loss on any navigation
    portal_reader.py    parses saved routes from the detail page and work queue
    session_a.py        inbox discovery, work-queue read, status-driven download
    session_b.py        modal handling, status-driven submit, verification
  ocr/
    rapid_ocr.py        offline ONNX OCR with per-page confidence
    cache.py            sha256-keyed OCR cache
    extractor.py        IDs, patient, urgency, mixed/incomplete, evidence gates
    tesseract_ocr.py    optional PyTesseract adapter
    llm_vision.py       optional multimodal adapter (not used in the committed run)
  logic/
    classifier.py       deterministic SOP + collected exception reasons
    duplicate_detector.py  arrival-order duplicate analysis with corroboration
portal/                 the provided sandbox
tests/                  45 tests + OCR fixtures
results.json, results.csv
```

## Disclosure

OCR is local and offline (RapidOCR with ONNX Runtime, pypdfium2 for rendering). No external AI
service, API key or paid model is used. The committed run does not use the optional vision adapter.
