"""Cancelling an in-flight evaluation.

A teacher who realises the sheet is wrong needs the work to STOP, not merely be ignored: grading is the
expensive stage and every second bills real API credit. Stages are subprocesses started with
start_new_session=True, so each owns a process group and cancelling SIGKILLs the whole tree.

The subtle properties pinned here:
  - a cancelled run refuses to START further (billable) stages;
  - the cancel flag is NOT cleared when the run is torn down -- a worker still unwinding would otherwise
    resurrect the folder that was just deleted via _write_orient_status;
  - a new run for the same file name clears it, so a corrected re-upload is not blocked.

Offline: no grading runs; the process kill is exercised against a real `sleep` child.
"""
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "evaluation_app"))

import full_evaluator as fe  # noqa: E402

try:
    import app as webapp
except (ImportError, SystemExit):  # pragma: no cover
    webapp = None


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for rid in ("R1", "R2", "Sheet", "Sheet_v1"):
        fe.clear_cancel(rid)


# ---- registry ------------------------------------------------------------------------------------
def test_cancel_marks_only_the_named_run():
    fe.request_cancel("R1")
    assert fe.is_cancelled("R1") is True
    assert fe.is_cancelled("R2") is False


def test_unknown_run_and_blank_are_safe():
    assert fe.request_cancel("never-started") == 0
    assert fe.request_cancel("") == 0
    assert fe.is_cancelled("") is False
    assert fe.is_cancelled(None) is False


def test_cancel_is_idempotent():
    fe.request_cancel("R1")
    assert fe.request_cancel("R1") == 0          # nothing left running to signal
    assert fe.is_cancelled("R1") is True


def test_clear_lets_the_same_run_id_be_used_again():
    """The corrected sheet usually has the SAME file name, so the run_id repeats."""
    fe.request_cancel("Sheet")
    fe.clear_cancel("Sheet")
    assert fe.is_cancelled("Sheet") is False


# ---- run_command honours cancellation ------------------------------------------------------------
def test_no_new_stage_starts_once_cancelled(monkeypatch):
    """The money property: a cancel landing between stages must cost nothing at all."""
    spawned = []
    monkeypatch.setattr(fe.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or pytest.fail("spawned after cancel"))
    fe.request_cancel("R1")
    ok, msg = fe.run_command([sys.executable, "anything.py"], env={"RUN_ID": "R1"})
    assert ok is False and msg == fe.CANCELLED_MSG
    assert spawned == []


def test_cancel_kills_a_running_stage_tree():
    """Real subprocess: a long sleep must die promptly, and the whole process GROUP is signalled."""
    import threading
    result = {}

    def run():
        result["out"] = fe.run_command([sys.executable, "-c", "import time; time.sleep(60)"],
                                       env={"RUN_ID": "R1"})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):                     # wait until the child is registered
        time.sleep(0.05)
        with fe._CANCEL_LOCK:
            if fe._RUN_PROCS.get("R1"):
                break
    t0 = time.time()
    assert fe.request_cancel("R1") == 1
    t.join(timeout=20)
    assert not t.is_alive(), "run_command did not return after the kill"
    assert time.time() - t0 < 20, "kill took far too long"
    ok, msg = result["out"]
    assert ok is False and msg == fe.CANCELLED_MSG


def test_a_normal_stage_is_unaffected():
    ok, out = fe.run_command([sys.executable, "-c", "print('hi')"], env={"RUN_ID": "R2"})
    assert ok is True and "hi" in out


def test_registry_is_emptied_after_a_stage_finishes():
    fe.run_command([sys.executable, "-c", "pass"], env={"RUN_ID": "R2"})
    with fe._CANCEL_LOCK:
        assert not fe._RUN_PROCS.get("R2")


# ---- route ---------------------------------------------------------------------------------------
pytestmark_app = pytest.mark.skipif(webapp is None, reason="web app unavailable")


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_BASE", str(tmp_path))
    webapp.app.config["TESTING"] = True
    return tmp_path


@pytestmark_app
def test_cancel_route_removes_the_run_and_reports_cancelled(out):
    run = out / "Sheet"
    (run / "images").mkdir(parents=True)
    (run / "images" / "p1.png").write_bytes(b"x")
    webapp._write_orient_status("Sheet", {"run_id": "Sheet", "phase": "evaluating"})

    body = webapp.app.test_client().post("/cancel-evaluation/Sheet").get_json()
    assert body["status"] == "cancelled"
    assert body["removed"] is True
    assert not run.exists(), "the wrong sheet must leave nothing behind"


@pytestmark_app
def test_cancel_route_rejects_a_traversal_id(out):
    r = webapp.app.test_client().post("/cancel-evaluation/..%2f..%2fetc")
    assert r.status_code in (400, 404)


@pytestmark_app
def test_cancel_route_is_safe_for_an_unknown_run(out):
    body = webapp.app.test_client().post("/cancel-evaluation/never_started").get_json()
    assert body["status"] == "cancelled"


@pytestmark_app
def test_flag_survives_teardown_so_a_late_worker_cannot_resurrect_the_run(out):
    """THE race: _write_orient_status recreates the run folder. If the flag were cleared during
    teardown, a worker unwinding a moment later would rebuild the directory just deleted and report a
    failure for work the teacher deliberately stopped."""
    (out / "Sheet").mkdir()
    webapp._write_orient_status("Sheet", {"run_id": "Sheet", "phase": "evaluating"})
    webapp.app.test_client().post("/cancel-evaluation/Sheet")

    assert fe.is_cancelled("Sheet") is True, "flag must persist past teardown"
    assert webapp._cancelled_now("Sheet") is True      # so the worker stays silent
    assert not (out / "Sheet").exists()


@pytestmark_app
def test_a_fresh_run_for_the_same_name_clears_the_flag(out):
    """Otherwise the corrected re-upload -- which usually has the SAME file name -- would be refused
    every stage by run_command and appear to hang."""
    fe.request_cancel("Sheet")
    assert fe.is_cancelled("Sheet") is True
    fe.clear_cancel("Sheet")                            # what /prepare-orientation does
    ok, out_txt = fe.run_command([sys.executable, "-c", "print('ran')"], env={"RUN_ID": "Sheet"})
    assert ok is True and "ran" in out_txt


@pytestmark_app
def test_worker_stays_silent_on_a_cancelled_result(out):
    assert webapp._cancelled_now("unrelated", {"status": "cancelled"}) is True
    assert webapp._cancelled_now("unrelated", {"status": "error"}) is False
