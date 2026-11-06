"""Whole-agent behaviour against a real portal subprocess (isolated DB, stub OCR from fixtures).

Covers: ground-truth run, idempotent rerun, failed-stays-failed (bug 1), resume keeps duplicate
detection (bug 2), stale-checkpoint reconciliation, crash-after-commit recovery, and the
bounded second cycle for the two-session (409) rule.
"""
import json

import pytest

from conftest import all_routes
from helpers import EXPECTED, StubOCR
from linda_agent.agent import LindaAgent
from linda_agent.browser.session_b import SessionBWorker
from linda_agent.config import CHECKPOINT_FILE, RESULTS_JSON_FILE
from linda_agent.state import CheckpointManager


def restrict_inbox(db_conn, keep):
    """Test-DB only: leave just ``keep`` in the inbox so a scenario runs in seconds."""
    with db_conn() as c:
        for (fax_id,) in c.execute("SELECT fax_id FROM faxes").fetchall():
            if fax_id not in keep:
                c.execute("DELETE FROM routes WHERE fax_id=?", (fax_id,))
                c.execute("DELETE FROM faxes WHERE fax_id=?", (fax_id,))


def results_by_id():
    return {r["fax_id"]: r for r in json.loads(RESULTS_JSON_FILE.read_text(encoding="utf-8"))}


def route_counts(db_conn):
    counts = {}
    for r in all_routes(db_conn):
        counts[r["fax_id"]] = counts.get(r["fax_id"], 0) + 1
    return counts


# ------------------------------------------------------------------ full run
@pytest.mark.asyncio
async def test_full_run_matches_ground_truth_and_is_idempotent(portal_server, db_conn):
    portal_server(None)
    ocr = StubOCR()
    await LindaAgent(ocr=ocr).run()
    res = results_by_id()

    assert set(res) == set(EXPECTED)
    for fax_id, (action, slug, dtype, folder) in EXPECTED.items():
        r = res[fax_id]
        assert r["action"] == action, fax_id
        if slug:
            sf = r["selected_fields"]
            assert (sf["assignee_slug"], sf["doc_type"], sf["folder"]) == (slug, dtype, folder), fax_id
            assert r["verification"] == "verified_on_portal", fax_id

    # exactly one route per routed fax, FX-211 untouched, FX-210 never submitted
    counts = route_counts(db_conn)
    assert all(v == 1 for v in counts.values())
    assert set(counts) == {f for f, e in EXPECTED.items() if e[0] in ("routed", "needs_review")} | {"FX-211"}
    assert "FX-210" not in counts
    assert "FX-211" not in ocr.calls                      # pre-routed fax is never downloaded/OCR'd
    assert res["FX-202"]["selected_fields"]["notes"].count("CRITICAL RESULT - CALLING PROVIDER") == 1
    assert "Potassium 6.3 mmol/L" in res["FX-202"]["selected_fields"]["notes"]
    assert "Imaging Report" in res["FX-211"]["notes"]     # existing assignment recorded read-only

    # rerun with the checkpoint intact: no OCR, no downloads, no new routes
    ocr2 = StubOCR()
    before = route_counts(db_conn)
    await LindaAgent(ocr=ocr2).run()
    assert ocr2.calls == []
    assert route_counts(db_conn) == before


# ------------------------------------------------------------------ bug 1
@pytest.mark.asyncio
async def test_failed_routing_stays_failed_and_is_retried(portal_server, db_conn, monkeypatch):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-203", "FX-205"})
    original = SessionBWorker.route_and_confirm

    async def flaky(self, decision):
        if decision.fax_id == "FX-205":
            raise RuntimeError("simulated routing failure")
        return await original(self, decision)

    monkeypatch.setattr(SessionBWorker, "route_and_confirm", flaky)
    await LindaAgent(ocr=StubOCR()).run()

    res = results_by_id()
    assert res["FX-203"]["action"] == "routed" and res["FX-203"]["verification"] == "verified_on_portal"
    assert res["FX-205"]["action"] == "failed"                  # not overwritten with a false success
    assert res["FX-205"]["verification"] == "failed"
    assert "simulated routing failure" in res["FX-205"]["notes"]
    assert not CheckpointManager(CHECKPOINT_FILE).is_completed("FX-205")
    assert "FX-205" not in route_counts(db_conn)

    monkeypatch.setattr(SessionBWorker, "route_and_confirm", original)   # fault cleared
    await LindaAgent(ocr=StubOCR()).run()
    res = results_by_id()
    assert res["FX-205"]["action"] == "routed" and res["FX-205"]["verification"] == "verified_on_portal"
    assert route_counts(db_conn) == {"FX-203": 1, "FX-205": 1}


# ------------------------------------------------------------------ bug 2
@pytest.mark.asyncio
async def test_resume_still_skips_the_duplicate(portal_server, db_conn):
    portal_server(None)
    with db_conn() as c:
        resend = dict(c.execute("SELECT * FROM faxes WHERE fax_id='FX-210'").fetchone())
    restrict_inbox(db_conn, {"FX-201"})             # run 1: only the original has arrived
    await LindaAgent(ocr=StubOCR()).run()
    assert route_counts(db_conn) == {"FX-201": 1}

    with db_conn() as c:                            # the resend arrives afterwards
        c.execute("INSERT INTO faxes VALUES(?,?,?,?,?)",
                  (resend["fax_id"], resend["sender"], resend["received_at"], resend["pages"], resend["filename"]))
    ocr2 = StubOCR()
    await LindaAgent(ocr=ocr2).run()

    assert ocr2.calls == ["FX-210"]                 # FX-201 was not re-downloaded or re-OCR'd
    res = results_by_id()
    assert res["FX-210"]["action"] == "skipped_duplicate"
    assert route_counts(db_conn) == {"FX-201": 1}   # never filed twice


# ------------------------------------------------------------------ reconciliation
@pytest.mark.asyncio
async def test_stale_checkpoint_is_invalidated_and_reprocessed(portal_server, db_conn):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-205"})
    await LindaAgent(ocr=StubOCR()).run()
    assert route_counts(db_conn) == {"FX-205": 1}

    with db_conn() as c:                            # sandbox reset: routes + downloads gone, checkpoint stays
        c.execute("DELETE FROM routes")
        c.execute("DELETE FROM downloads")
    assert CheckpointManager(CHECKPOINT_FILE).is_completed("FX-205")

    await LindaAgent(ocr=StubOCR()).run()
    assert route_counts(db_conn) == {"FX-205": 1}   # rebuilt from the portal's real state
    assert results_by_id()["FX-205"]["verification"] == "verified_on_portal"


@pytest.mark.asyncio
async def test_divergent_portal_route_is_flagged_not_resubmitted(portal_server, db_conn):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-205"})
    await LindaAgent(ocr=StubOCR()).run()
    with db_conn() as c:
        c.execute("UPDATE routes SET folder='Billing / EOBs' WHERE fax_id='FX-205'")
    await LindaAgent(ocr=StubOCR()).run()
    r = results_by_id()["FX-205"]
    assert r["verification"] == "verified_mismatch"
    assert any("differs" in e for e in r["recovery"])
    assert route_counts(db_conn) == {"FX-205": 1}


# ------------------------------------------------------------------ crash after commit
@pytest.mark.asyncio
async def test_crash_after_commit_is_finalised_on_restart(portal_server, db_conn, monkeypatch):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-205"})
    original = SessionBWorker.route_and_confirm

    async def commit_then_die(self, decision):
        await original(self, decision)               # route is really committed...
        raise KeyboardInterrupt                      # ...then the process is killed before recording it

    monkeypatch.setattr(SessionBWorker, "route_and_confirm", commit_then_die)
    with pytest.raises(KeyboardInterrupt):
        await LindaAgent(ocr=StubOCR()).run()

    cp = CheckpointManager(CHECKPOINT_FILE)
    assert cp.get_intent("FX-205") is not None       # intent survived the crash
    assert not cp.is_completed("FX-205")
    assert route_counts(db_conn) == {"FX-205": 1}

    monkeypatch.setattr(SessionBWorker, "route_and_confirm", original)
    ocr2 = StubOCR()
    await LindaAgent(ocr=ocr2).run()

    r = results_by_id()["FX-205"]
    assert r["action"] == "routed" and r["verification"] == "verified_on_portal"   # not "already_routed"
    assert any("interrupted run" in e for e in r["recovery"])
    assert ocr2.calls == []
    assert route_counts(db_conn) == {"FX-205": 1}


# ------------------------------------------------------------------ 409 second cycle
@pytest.mark.asyncio
async def test_409_triggers_a_second_cycle_that_succeeds(portal_server, db_conn, monkeypatch):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-205"})
    original_start = SessionBWorker.start
    wipes = []

    async def start_after_one_portal_reset(self):
        if not wipes:                                # portal "reset" between Session A and the first Session B
            with db_conn() as c:
                c.execute("DELETE FROM downloads")
            wipes.append(1)
        await original_start(self)

    monkeypatch.setattr(SessionBWorker, "start", start_after_one_portal_reset)
    await LindaAgent(ocr=StubOCR()).run()

    r = results_by_id()["FX-205"]
    assert r["action"] == "routed" and r["verification"] == "verified_on_portal"
    assert any("409" in e for e in r["recovery"])
    assert route_counts(db_conn) == {"FX-205": 1}
    with db_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE event='two_session_rule_blocked'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_permanent_409_stops_at_the_cycle_cap(portal_server, db_conn, monkeypatch):
    portal_server(None)
    restrict_inbox(db_conn, {"FX-205"})
    original_start = SessionBWorker.start

    async def always_reset(self):
        with db_conn() as c:
            c.execute("DELETE FROM downloads")
        await original_start(self)

    monkeypatch.setattr(SessionBWorker, "start", always_reset)
    await LindaAgent(ocr=StubOCR()).run()

    r = results_by_id()["FX-205"]
    assert r["action"] == "failed" and "409" in r["notes"]
    assert route_counts(db_conn) == {}
    with db_conn() as c:   # bounded: one blocked attempt per cycle, no loop
        assert c.execute("SELECT COUNT(*) FROM audit_events WHERE event='two_session_rule_blocked'").fetchone()[0] == 2
