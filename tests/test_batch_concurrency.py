"""Tests for sheet-level batch concurrency (BATCH_SHEET_CONCURRENCY).

Pins the contract that matters for "no degradation":
  * the serial default (concurrency 1) is the original in-process path;
  * the concurrent path returns IDENTICAL per-student results in INPUT order (dedup + ordering
    preserved) and a float-identical batch total, regardless of completion order;
  * the per-endpoint concurrency caps are SPLIT across in-flight sheets (aggregate load ~= one sheet);
  * env_overrides win over the .env overlay inside full_evaluate;
  * the --run-sheet subprocess entrypoint round-trips a result (and traps exceptions).
Everything is stubbed -- no real subprocess, no API, no PDF.
"""
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import batch_evaluator as be  # noqa: E402
import full_evaluator as fe  # noqa: E402


def _manifest(names):
    """One sheet per name; 2 pages each so the pages ranges differ."""
    sheets, p = [], 1
    for nm in names:
        sheets.append({"name": nm, "subject": "Mathematics", "start_page": p, "end_page": p + 1})
        p += 2
    return {"source_pdf": "/nonexistent/combined.pdf", "sheets": sheets}


def _fake_result(name, awarded, maximum, cost):
    return {"status": "success",
            "evaluations": [["Q1", {"Marks Awarded": awarded, "Maximum Marks": maximum}]],
            "cost": f"${cost:.6f}", "report_path": f"/reports/{name}.pdf",
            "student_details": {"name": name}, "review_id": name}


def _score_for(name):
    """Stable pseudo-marks/cost derived from the (unique) student name, so the serial and concurrent
    paths -- both keyed on the same unique name -- must compute identical records."""
    base = sum(ord(c) for c in name) % 5
    return _fake_result(name, float(base), 5.0, 0.01 * (base + 1))


# ---- _scaled_caps ---------------------------------------------------------------------------------
def test_scaled_caps_noop_for_serial():
    assert be._scaled_caps(1) == {}


def test_scaled_caps_splits_and_floors(monkeypatch):
    base = {"EVAL_MAX_CONCURRENCY": 24, "OCR_MAX_WORKERS": 20, "ANSWER_CROP_MAX_WORKERS": 8}
    monkeypatch.setattr(be, "_effective_int", lambda k, d: base[k])
    # n=3: 8//3=2 -> crops floored UP to 4. Crops run on the instruct model, not the thinking model
    # that bounds grading throughput, so they are deliberately throttled less than eval/OCR.
    assert be._scaled_caps(3) == {"EVAL_MAX_CONCURRENCY": "8", "OCR_MAX_WORKERS": "6",
                                  "ANSWER_CROP_MAX_WORKERS": "4"}
    # n=8: 24//8=3 -> eval floored to 4; 20//8=2 -> ocr floored to 6; 8//8=1 -> crops floored to 4
    assert be._scaled_caps(8) == {"EVAL_MAX_CONCURRENCY": "4", "OCR_MAX_WORKERS": "6",
                                  "ANSWER_CROP_MAX_WORKERS": "4"}


def test_scaled_caps_crop_floor_beats_eval_and_ocr_throttling(monkeypatch):
    """The crop floor must never fall below 4, at ANY sheet concurrency.

    Regression guard for the real failure mode: a low crop-worker count stretches the crop pass past
    the end of grading, at which point evaluate.py blocks on ANSWER_CROPS_SENTINEL and the display-only
    feature starts costing wall-clock on the critical path.
    """
    base = {"EVAL_MAX_CONCURRENCY": 24, "OCR_MAX_WORKERS": 20, "ANSWER_CROP_MAX_WORKERS": 8}
    monkeypatch.setattr(be, "_effective_int", lambda k, d: base[k])
    for n in range(2, 9):
        assert int(be._scaled_caps(n)["ANSWER_CROP_MAX_WORKERS"]) >= 4


def test_scaled_caps_never_raises_above_configured_value(monkeypatch):
    """A floor must not become a BOOST: with crops already configured below the floor, the split may
    not hand a concurrent batch more workers than the operator asked a single sheet to use."""
    base = {"EVAL_MAX_CONCURRENCY": 24, "OCR_MAX_WORKERS": 20, "ANSWER_CROP_MAX_WORKERS": 2}
    monkeypatch.setattr(be, "_effective_int", lambda k, d: base[k])
    assert int(be._scaled_caps(3)["ANSWER_CROP_MAX_WORKERS"]) <= 2


def test_sheet_concurrency_clamped(monkeypatch):
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "99")
    assert be._sheet_concurrency() == 8
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "0")
    assert be._sheet_concurrency() == 1
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "not-an-int")
    assert be._sheet_concurrency() == 1


# ---- serial vs concurrent equivalence (batch_evaluate) --------------------------------------------
@pytest.fixture
def stub_eval(monkeypatch):
    """No-op slice; serial calls full_evaluate, concurrent calls _run_sheet_subprocess -- both return
    the SAME deterministic result. The subprocess stub sleeps INVERSELY to submit order so later sheets
    finish first, exercising out-of-order completion."""
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: None)
    monkeypatch.setattr(be, "full_evaluate",
                        lambda input_file, student_name="Student", **k: _score_for(student_name))
    order = {"i": 0}

    def _fake_sub(run_id, kwargs, timeout):
        idx = order["i"]
        order["i"] += 1
        time.sleep(0.02 * max(0, 4 - idx))
        return _score_for(kwargs["student_name"])

    monkeypatch.setattr(be, "_run_sheet_subprocess", _fake_sub)


def test_serial_and_concurrent_agree(monkeypatch, stub_eval):
    names = ["Asha", "Ravi", "Asha", "Meena", "Ravi"]        # duplicates -> exercise dedup + ordering
    mf = _manifest(names)

    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "1")
    serial = be.batch_evaluate("batch_test", mf, "/key.json")

    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "3")
    concurrent = be.batch_evaluate("batch_test", mf, "/key.json")

    assert serial["students"] == concurrent["students"]      # identical records, in INPUT order
    assert serial["total_cost"] == concurrent["total_cost"]  # float-identical batch total
    assert [s["name"] for s in serial["students"]] == \
        ["Asha", "Ravi", "Asha (2)", "Meena", "Ravi (2)"]
    assert serial["students"][0]["pages"] == "1-2"           # per-sheet pages range preserved


def test_concurrent_progress_is_monotonic(monkeypatch, stub_eval):
    mf = _manifest(["A", "B", "C", "D"])
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "2")
    seen = []
    be.batch_evaluate("b", mf, "/key.json", status_cb=lambda d, t, n: seen.append((d, t)))
    dones = [d for d, _ in seen]
    assert dones == sorted(dones)          # never goes backwards despite out-of-order completion
    assert seen[-1] == (4, 4)              # final call is (total, total)
    assert all(t == 4 for _, t in seen)


# ---- serial vs concurrent equivalence (batch_resume_orientation) ----------------------------------
def test_resume_serial_and_concurrent_agree(monkeypatch):
    names = ["Sam", "Sam", "Nina"]
    mf = _manifest(names)
    monkeypatch.setattr(be, "resume_after_orientation",
                        lambda run_id, **k: _score_for(k["student_name"]))
    monkeypatch.setattr(be, "_run_sheet_subprocess",
                        lambda run_id, kwargs, timeout: _score_for(kwargs["student_name"]))

    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "1")
    serial = be.batch_resume_orientation("b", mf, {}, "/key.json")
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "3")
    conc = be.batch_resume_orientation("b", mf, {}, "/key.json")

    assert serial["students"] == conc["students"]
    assert [s["name"] for s in serial["students"]] == ["Sam", "Sam (2)", "Nina"]


def test_concurrent_one_bad_sheet_does_not_sink_batch(monkeypatch):
    """A sheet whose subprocess errors becomes an error record; the rest still succeed (batch resilience
    is preserved under concurrency)."""
    mf = _manifest(["Good1", "Bad", "Good2"])
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: None)

    def _sub(run_id, kwargs, timeout):
        if kwargs["student_name"] == "Bad":
            return {"status": "error", "error": "boom"}
        return _score_for(kwargs["student_name"])

    monkeypatch.setattr(be, "_run_sheet_subprocess", _sub)
    monkeypatch.setenv("BATCH_SHEET_CONCURRENCY", "3")
    res = be.batch_evaluate("b", mf, "/key.json")
    by_name = {s["name"]: s for s in res["students"]}
    assert by_name["Bad"]["status"] == "error" and by_name["Bad"]["error"] == "boom"
    assert by_name["Good1"]["status"] == "success" and by_name["Good2"]["status"] == "success"
    assert [s["name"] for s in res["students"]] == ["Good1", "Bad", "Good2"]   # order intact


# ---- --run-sheet subprocess entrypoint ------------------------------------------------------------
def test_run_sheet_entry_roundtrip(tmp_path, monkeypatch):
    result_path = str(tmp_path / "res.json")
    args_path = str(tmp_path / "args.json")
    with open(args_path, "w") as f:
        json.dump({"mode": "evaluate", "input_file": "/x.pdf", "student_name": "Zed",
                   "result_path": result_path, "env_overrides": {"EVAL_MAX_CONCURRENCY": "8"}}, f)

    captured = {}

    def _fake_full(input_file, **k):
        captured.update(k)
        captured["input_file"] = input_file
        return {"status": "success", "evaluations": [], "cost": "$0.000000", "review_id": "Zed"}

    monkeypatch.setattr(be, "full_evaluate", _fake_full)
    be._run_sheet_entry(args_path)

    with open(result_path) as f:
        res = json.load(f)
    assert res["status"] == "success" and res["review_id"] == "Zed"
    assert captured["student_name"] == "Zed"
    assert captured["env_overrides"] == {"EVAL_MAX_CONCURRENCY": "8"}   # passed straight through


def test_run_sheet_entry_traps_exception(tmp_path, monkeypatch):
    result_path = str(tmp_path / "res.json")
    args_path = str(tmp_path / "args.json")
    with open(args_path, "w") as f:
        json.dump({"mode": "evaluate", "input_file": "/x.pdf", "result_path": result_path}, f)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(be, "full_evaluate", _boom)
    be._run_sheet_entry(args_path)
    with open(result_path) as f:
        res = json.load(f)
    assert res["status"] == "error" and "kaboom" in res["error"]


# ---- env_overrides precedence inside full_evaluate ------------------------------------------------
def test_env_overrides_win_over_dotenv(monkeypatch):
    """full_evaluate must apply env_overrides AFTER the .env overlay, so a split cap reaches the stage
    subprocesses even though .env pins EVAL_MAX_CONCURRENCY. We halt at stage 1 after capturing env."""
    captured = {}

    def _halt(command, cwd=None, env=None):
        captured["env"] = env
        return False, "halt-after-capture"

    monkeypatch.setattr(fe, "run_command", _halt)
    stem = "zzz_env_override_probe"
    try:
        res = fe.full_evaluate(f"/tmp/{stem}.pdf",
                               env_overrides={"EVAL_MAX_CONCURRENCY": "7", "OCR_MAX_WORKERS": "5"})
    finally:
        import shutil
        shutil.rmtree(os.path.join(ROOT, "output", stem), ignore_errors=True)

    assert res.get("error") == "Ingestion failed"                 # halted at stage 1, as intended
    assert captured["env"]["EVAL_MAX_CONCURRENCY"] == "7"          # override beat the .env's value
    assert captured["env"]["OCR_MAX_WORKERS"] == "5"
