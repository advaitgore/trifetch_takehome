# Linda Mini Fax Routing Agent: Architecture and Failure Notes

## 1. What it does

A Playwright agent that drives the Linda Mini portal through its UI: it reads the inbox, downloads
image-only scanned PDFs, OCRs every page locally, applies the routing SOP and exception rules,
submits assignments in a second, separate login session, and then reads the saved result back
from the portal before it reports success.

Nothing in the agent reads the portal's database, `portal/data/faxes`, or `__admin` endpoints.

## 2. Run shape

```
cycle 1
  Session A   login -> Fax Management -> Inbound Faxes (all pages) -> Routed Documents
              reconcile checkpoint against the portal
              download every fax that still needs work
              close
  offline     OCR (content-hash cached) -> extract -> duplicate analysis in arrival order -> SOP decisions
  barrier     >= 1.2 s
  Session B   NEW browser context + NEW login
              per fax: write intent -> open form -> fill -> Review Routing -> OK -> read back saved values
              close
cycle 2 (only if the portal answered 409 for some fax)
  Session A   fresh download session for just those faxes -> barrier -> fresh Session B
```

The cycle count is capped (`LINDA_MAX_CYCLES`, default 2). Faxes still blocked after the cap are
recorded as `failed` with the reason; the agent never loops.

### Why two sessions, and why the barrier

`route_save` in the portal accepts a route only if the fax has a download from a *different*
session whose `downloaded_at` is earlier than the routing session's `created_at`. Session B is a
new `browser.new_context()` (new cookie) created after Session A closed, and a 1.2 s sleep
removes any clock-resolution doubt.

### Why discovery is decoupled from routing

The inbox is listed newest first, so FX-210 (the resend, 08:49) appears on page 1 and FX-201
(the original, 08:02) on page 2. Acting while paginating would meet the resend first. The agent
indexes and OCRs everything first, then evaluates in ascending `received_at` order, and routes
oldest first.

## 3. Evidence and decisions

### OCR

RapidOCR (ONNX, offline) on pypdfium2 renders at 2x. Besides text, each page records the
minimum and mean line confidence. Results are cached under `checkpoints/ocr_cache/` keyed by
`sha256(pdf bytes)` and engine, so reruns and resumes do not re-OCR unchanged PDFs.

The scans lose inter-word spaces (`Nourgentflagprovided`, `Potassium6.3mmol/L`), so every pattern
is whitespace-insensitive and avoids `\b` where words may be glued together.

### Classification (deterministic)

| Document content | Assigned To (slug) | Document Type | Document Folder |
|---|---|---|---|
| Referral / consult request | olivia | Referral | Referrals / New |
| Routine lab result | noah | Lab Result | Clinical / Labs |
| Imaging / radiology report | noah | Imaging Report | Clinical / Imaging |
| Prior authorization request | ethan | Prior Authorization | Authorizations / Pending |
| Prescription refill request | ava | Refill Request | Nursing / Medication |
| Records release request | mia | Records Request | Records / Requests |
| EOB / remittance | liam | EOB | Billing / EOBs |
| Any exception below | jordan | Needs Review | Review / Unclassified |

The `<select>` option values are slugs; the form rejects display labels with HTTP 400.

Exception reasons are **collected, not short-circuited**, in a fixed order, and any one of them
sends the fax to Manual Review. The note lists all that apply (capped at the portal's 1000
characters, truncated visibly with `...`):

1. **Urgent / critical.** Scanned page by page. Specific phrases (`CRITICAL RESULT - CALLING
   PROVIDER`, `CRITICAL VALUE NOTIFICATION`, `PANIC VALUE`, `URGENT FLAG/REVIEW/...`) match
   case-insensitively; bare `CRITICAL`, `STAT`, `EMERGENCY`, `URGENT` count only as ALL-CAPS
   tokens. A match is negated only by a negator directly in front of it ("No urgent flag").
   A negated mention on one page never hides a flag on another; if both occur the packet is
   flagged and a conflicting-signal note is added. Matches contained in a longer match are
   dropped. The note carries a readable label for each flag phrase found (glued OCR text such as
   `Criticalvaluenotification` is normalised to `CRITICAL VALUE NOTIFICATION`) and, best-effort, the reported value
   (e.g. `Potassium 6.3 mmol/L`). The value never influences routing. The note also states that
   no clinical action was taken.
2. **Mixed packet.** Explicit "multiple enclosures" text or more than one document category
   across pages. The note lists each page's category and ID (e.g. `p2 Lab Result LAB-4012A;
   p3 Imaging Report IMG-4012B`).
3. **Evidence gap.** Any of: portal page count differs from OCR page count; a page with no
   recognised text; a page below the confidence floors; conflicting urgency signals; an
   uncorroborated or conflicting duplicate match.
4. **Incomplete / unclear.** Specific phrases only (`appears incomplete`, `missing continuation
   page`, `no readable document category`, `scan cut off`, `unknown/unidentified sender`).
   The bare word "unknown" in prose does not count.

### Confidence floors: how they were set

Measured on the 11 downloaded faxes (15 pages) with the real engine: lowest per-page minimum line
confidence **0.935**, lowest per-page mean **0.974**. The floors are **0.60** (line) and **0.85**
(mean), far enough below the observed minimum that all 12 outcomes are unchanged and the gate
trips only on genuinely degraded scans. They are env-configurable
(`LINDA_OCR_MIN_LINE_CONF`, `LINDA_OCR_MIN_MEAN_CONF`). A test asserts the observed minimum stays
well above the floors.

### Duplicates

A resend is *confirmed* only if the same source document ID appeared in an earlier fax **and**
there is corroboration: an explicit resend marker, or a matching patient name (compared with
spaces and punctuation stripped, since OCR drops spaces). The same ID with conflicting patients,
or with no corroboration, is *uncertain* and goes to Manual Review. It is never skipped silently
and never filed silently. Evidence for faxes finished in an earlier run comes from the checkpoint,
so detection survives a restart.

### Already routed

A fax already present in Routed Documents (or listed "Routed") is never modified. Its existing
assignment is recorded read-only in the result note.

## 4. Browser state machine

All branching is on HTTP status, not page text.

| Request | Status | Action |
|---|---|---|
| download | 200 + download event | validate `%PDF-` header and size, save |
| download | 503 | transient; exponential backoff (1.5 s, 3 s, 6 s), retry |
| download | 3xx | session expired; re-authenticate, retry |
| route POST | 303 to `?notice_type=success` / `already` | committed; read back saved values |
| route POST | 303 to `/login` | session expired; re-login, **reopen and refill the form from scratch** |
| route POST | 503 | outcome unknown; read the portal, never resubmit blindly. If not committed, resubmit |
| route POST | 409 | two-session rule; defer to a new Session A -> B cycle |
| route POST | 403 | stale form token; reopen the form |
| route POST | 400 / 404 | rejected; recorded `failed`, not retried |

Every navigation goes through `goto_authenticated`: if the portal bounces to `/login`, the agent
signs in again where it stands and returns to the target. This applies in both sessions and in
the download loop, not just at the start.

The confirmation modal is waited on (`#confirmation.show`) and its three summary fields are
compared with the intended values. A mismatch backs out of the modal and raises
`ModalMismatchError` (an explicit exception, not an `assert`).

### Verification

After any apparent commit the agent opens `/faxes/{id}`, parses the "Document routing" card
(scoped, because the page has two `dl.kv` blocks), and opens `/work-queue`. It checks that the
assigned-to label, document type and folder match the intent in both places and that the queue
has **exactly one** row for the fax. Outcomes: `verified_on_portal`, `verified_mismatch` (routed
but divergent; reported loudly, never resubmitted), `not_verified` (recorded `failed`).

## 5. Checkpoints, idempotency and restart

State is `checkpoints/progress_state.json`, written atomically after each fax. Each record holds the
outcome **and its extraction evidence** (source/original document ID, patient, DOB, resend marker,
page counts, confidences, uncertainty, recovery events).

* **Failed stays failed.** `failed` and `not_verified` are not "completed", so the next run retries
  them and the export reports the failure.
* **Intent before submit.** Immediately before a submit the agent persists what it is about to send.
  If the process dies after the portal commits but before the result is saved, the next run finds
  the route in the portal, compares it with the intent and finalises it as `routed`/`needs_review`
  (with a recovery note) instead of mislabeling it `already_routed`.
* **Startup reconciliation.** Each run reads Routed Documents first. A checkpoint entry claiming a
  route the portal does not show (for example after a sandbox reset) is invalidated and
  reprocessed. A route that differs from the checkpoint is marked `verified_mismatch`.
* **Resume.** Completed faxes are not re-downloaded or re-OCR'd, but their persisted evidence still
  feeds duplicate detection.
* **409 recovery.** A fresh Session B cannot route a fax whose earlier-session download is gone
  from the portal (it must be downloaded again in a session that is older than Session B). The
  agent therefore closes Session B, downloads those faxes again in a new Session A, waits out the
  barrier and opens a new Session B, bounded by the cycle cap.

## 6. Known gaps and deliberate deviations

* **Per-fax pages are reached by URL, not by clicking rows.** Login, the inbox walk and the Routed
  Documents link use the visible UI. Opening `/faxes/{id}` and `/faxes/{id}/route` uses `goto`. The
  fax ID is the matching key in both sessions. This was a deliberate scope choice.
* **The routing note cannot be verified through the UI.** The portal stores it (truncated to 1000
  characters) but never displays it. Read-back covers assignee, type and folder only.
* **OCR is imperfect.** On this data the engine produced `OoliverGrant` (FX-204), `SydneyPatel`
  (FX-205), `ElenaHart` (FX-210) and `PkT-4012` (FX-212, the brief lists it as `PKT-4012`).
  Document titles, which drive routing, were read correctly on all 11 downloaded faxes, and document
  IDs matched the brief apart from that letter case. Patient names are stored as OCR produced them.
  There is no accuracy figure to quote beyond these observations.
* **Keyword-based rules.** Classification is title/phrase matching tuned to this document family.
  An unfamiliar layout falls to Manual Review (not a wrong route), which is the intended failure
  direction, but recall on new layouts is untested.
* **Urgency is conservative.** Bare tokens need ALL CAPS. A lowercase or space-collapsed bare
  `stat` is not detected on its own; the specific phrases are.
* **Reported value capture is best-effort** and uses one `Result: <analyte> <number> <unit>` shape.
* **Crash window.** The intent is written before the submit. A crash before that write means nothing
  was submitted. A crash after the portal commits is recovered from the intent on the next run.
* **Pre-routed faxes are identified by the portal's own status.** A route made by this agent in a run
  whose checkpoint *and* intent were both lost is indistinguishable from a clinic-staff route, and
  is reported `already_routed` with the saved values in the note.
* **Single worker.** No locking against a second agent instance.
* **OCR speed** depends on the machine (observed from minutes to tens of seconds for the full set).
  Reruns skip OCR via the cache.

## 7. Tests

All tests run against an isolated portal (temp DB, temp work dir, free port) set up in
`tests/conftest.py` before any module reads its settings; a test asserts the real
`portal/portal.sqlite3` is untouched. Classifier tests use checked-in OCR fixtures
(`tests/fixtures/ocr`, regenerated with `tests/fixtures/regenerate.py` from PDFs the agent
downloaded through the UI), so the logic suite runs in about a second.

| File | Covers |
|---|---|
| `test_classifier.py` | all 12 outcomes, urgency negation/conflict, incomplete rules, enclosure notes, evidence gates |
| `test_duplicate_detector.py` | arrival order, corroboration, resume from checkpoint evidence |
| `test_state.py` | completion semantics, intents, old-format load, OCR cache |
| `test_two_session.py` | the portal's two-session contract |
| `test_fault_recovery.py` | the three injected faults; asserts audit events and exactly one route row |
| `test_agent_flow.py` | ground-truth run + idempotent rerun, failed-stays-failed, resume keeps duplicate skipped, stale checkpoint, divergent route, crash after commit, 409 second cycle, 409 cap |
| `test_session_and_verification.py` | session death mid-download, read-back mismatch, no second POST, modal mismatch, isolation |
