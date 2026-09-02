"""Replacing a previous evaluation when the corrected sheet has a DIFFERENT file name.

A same-name re-upload already reuses that student's folder, which the pipeline clears in place
(see test_reupload_reset.py). A different name produces a different `run_id`, so the superseded run
would otherwise linger with its own report. `replaces_run_id` on the upload makes the new evaluation
supersede the old one.

The ordering is the whole safety property: the old run is removed ONLY after the replacement has
graded successfully, so a failed re-upload never leaves the teacher with neither evaluation.

Offline: OUTPUT_BASE is redirected to a tmp dir; no grading runs.
"""
import os
import sys
import json

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "evaluation_app"))

try:
    import app as webapp
except (ImportError, SystemExit):  # pragma: no cover
    webapp = None

pytestmark = pytest.mark.skipif(webapp is None, reason="web app unavailable")


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_BASE", str(tmp_path))
    webapp.app.config["TESTING"] = True
    return tmp_path


def _graded_run(out, run_id, student="Asha", report_path="", questions=2):
    d = out / run_id
    (d / "images").mkdir(parents=True, exist_ok=True)
    (d / "images" / "p1.png").write_bytes(b"img")
    payload = {
        "student_details": {"name": student, "roll_no": "7"},
        "evaluations": [{"Question": f"Q{i}"} for i in range(1, questions + 1)],
        "report_path": str(report_path),
    }
    (d / "review_state.json").write_text(json.dumps(payload))
    return d


# ---- listing --------------------------------------------------------------------------------------
def test_previous_evaluations_lists_only_graded_runs(out):
    _graded_run(out, "Sheet_v1", student="Asha")
    (out / "half_done").mkdir()                       # mid-run: no review_state -> not offerable
    (out / "half_done" / "images").mkdir()
    rows = webapp.app.test_client().get("/previous-evaluations").get_json()["evaluations"]
    assert [r["run_id"] for r in rows] == ["Sheet_v1"]
    assert rows[0]["student_name"] == "Asha" and rows[0]["questions"] == 2


def test_listing_prefers_the_teachers_working_copy(out):
    d = _graded_run(out, "Sheet_v1", student="Asha")
    (d / "review_render.json").write_text(json.dumps(
        {"student_details": {"name": "Asha (corrected)"}, "evaluations": [{"Question": "Q1"}]}))
    rows = webapp.app.test_client().get("/previous-evaluations").get_json()["evaluations"]
    assert rows[0]["student_name"] == "Asha (corrected)"


# ---- validation -----------------------------------------------------------------------------------
def test_blank_means_not_replacing(out):
    assert webapp._validated_replaces("", "Sheet_v2") == (None, None)
    assert webapp._validated_replaces(None, "Sheet_v2") == (None, None)


def test_rejects_a_traversal_id(out):
    val, err = webapp._validated_replaces("../../etc", "Sheet_v2")
    assert val is None and err["error"] == "Invalid run id"


def test_rejects_an_unknown_run(out):
    val, err = webapp._validated_replaces("never_graded", "Sheet_v2")
    assert val is None and err["error"] == "Nothing to replace"


def test_replacing_itself_is_ignored_not_an_error(out):
    """Same filename already reuses and resets the folder; honouring it here would delete the run we
    are about to create."""
    _graded_run(out, "Sheet")
    assert webapp._validated_replaces("Sheet", "Sheet") == (None, None)


# ---- supersede ------------------------------------------------------------------------------------
def test_supersede_removes_the_old_run(out):
    _graded_run(out, "Sheet_v1")
    _graded_run(out, "Sheet_v2")
    res = webapp._supersede_run("Sheet_v1", "Sheet_v2")
    assert res["removed"] is True
    assert not (out / "Sheet_v1").exists()
    assert (out / "Sheet_v2").exists()


def test_supersede_deletes_an_orphaned_report(out, tmp_path):
    """Student renamed -> the new grading wrote a DIFFERENT pdf, so the old one is now an orphan."""
    old_pdf = tmp_path / "Asha.pdf"
    new_pdf = tmp_path / "Asha_Kumar.pdf"
    old_pdf.write_bytes(b"old-report")
    new_pdf.write_bytes(b"new-report")
    _graded_run(out, "Sheet_v1", report_path=old_pdf)
    res = webapp._supersede_run("Sheet_v1", "Sheet_v2", str(new_pdf))
    assert res["report_removed"] is True
    assert not old_pdf.exists()
    assert new_pdf.read_bytes() == b"new-report"


def test_supersede_keeps_the_report_when_the_path_is_unchanged(out, tmp_path):
    """THE dangerous case: same student name -> both runs write the same pdf, so the new grading has
    already overwritten it. Deleting 'the old report' here would delete the report just produced."""
    pdf = tmp_path / "Asha.pdf"
    pdf.write_bytes(b"NEW report written by the replacement run")
    _graded_run(out, "Sheet_v1", report_path=pdf)
    res = webapp._supersede_run("Sheet_v1", "Sheet_v2", str(pdf))
    assert res["report_removed"] is False
    assert pdf.read_bytes() == b"NEW report written by the replacement run"


def test_supersede_refuses_to_delete_the_new_run(out):
    _graded_run(out, "Sheet")
    assert webapp._supersede_run("Sheet", "Sheet")["removed"] is False
    assert (out / "Sheet").exists()


def test_supersede_survives_an_unremovable_dir(out, monkeypatch):
    """A failure here must not turn a successful grading into a reported error."""
    _graded_run(out, "Sheet_v1")
    monkeypatch.setattr(webapp.shutil, "rmtree", lambda p: (_ for _ in ()).throw(OSError("busy")))
    res = webapp._supersede_run("Sheet_v1", "Sheet_v2")     # must not raise
    assert res["removed"] is False


# ---- ordering: the safety property ---------------------------------------------------------------
def test_old_run_survives_a_failed_replacement(out, monkeypatch):
    """If grading fails, /evaluate must return the error AND leave the original evaluation intact."""
    _graded_run(out, "Sheet_v1")
    monkeypatch.setattr(webapp, "_eval_prereqs", lambda: (None, {
        "answer_key_path": "/k.json", "state": {"path": str(out), "class": "", "subject": ""},
        "marks_source": None}))
    calls = []
    monkeypatch.setattr(webapp, "_supersede_run", lambda *a, **k: calls.append(a) or {"removed": True})
    monkeypatch.setattr(webapp, "full_evaluate",
                        lambda *a, **k: {"status": "error", "error": "OCR failed"})
    monkeypatch.setattr(webapp.os.path, "exists", lambda p: True)

    client = webapp.app.test_client()
    r = client.post("/evaluate", data={
        "file": (__import__("io").BytesIO(b"%PDF-1.4"), "Sheet_v2.pdf"),
        "student_name": "Asha", "replaces_run_id": "Sheet_v1",
    }, content_type="multipart/form-data")

    assert r.get_json().get("status") == "error"
    assert calls == [], "the superseded run must NOT be deleted when the replacement fails"
    assert (out / "Sheet_v1").exists()
