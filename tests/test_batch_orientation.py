"""Batch orientation gate — orchestration unit tests. Offline / no network / no real PDF.

The per-sheet pipeline (prepare_orientation / resume_after_orientation / full_evaluate / slice_pdf) is
stubbed, so these lock the batch-level contract that backs "no degradation":
  - run_id per sheet == _safe_stem(display_name, idx) for BOTH prepare and resume (so prepare, resume
    and the untouched skip-path all address the same output/<run_id>/ dir);
  - name de-duplication matches batch_evaluate;
  - resume routes each sheet's rotations by run_id; an unspecified sheet gets {} (zero-rotation);
  - batch_resume_orientation aggregates the SAME {students,total_cost} shape as batch_evaluate.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import batch_evaluator as be
except (ImportError, SystemExit) as e:  # pragma: no cover
    be = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(be is None, reason="batch_evaluator unavailable")

# Two sheets sharing a display name -> exercises the dedup ("Asha", "Asha (2)").
MANIFEST = {"source_pdf": "/tmp/nope.pdf", "num_pages": 6, "sheets": [
    {"id": "sheet_1", "name": "Asha", "subject": "Math", "start_page": 1, "end_page": 3},
    {"id": "sheet_2", "name": "Asha", "subject": "Math", "start_page": 4, "end_page": 6},
]}


def _success_result(name):
    return {"status": "success", "report_path": f"/r/{name}.pdf", "cost": "$0.010000",
            "evaluations": [["Q1", {"Marks Awarded": 2, "Maximum Marks": 3}]],
            "student_details": {"name": name}}


# ---- prepare -----------------------------------------------------------------

def test_prepare_run_ids_names_and_pages(monkeypatch):
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: a[-1])
    monkeypatch.setattr(be, "prepare_orientation", lambda sheet_pdf, **k: {
        "status": "orient_review",
        "pages": [{"index": 1, "file": "p1.png", "suggested_rot": 90, "confidence": "low"}]})
    out = be.batch_prepare_orientation("batch_x", MANIFEST, "/tmp/key.json")
    sheets = out["sheets"]
    assert [s["run_id"] for s in sheets] == [be._safe_stem("Asha", 1), be._safe_stem("Asha", 2)]
    assert [s["name"] for s in sheets] == ["Asha", "Asha (2)"]      # dedup matches batch_evaluate
    assert [s["pages_range"] for s in sheets] == ["1-3", "4-6"]
    assert all(len(s["pages"]) == 1 for s in sheets)


def test_prepare_error_sheet_recorded(monkeypatch):
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: a[-1])
    monkeypatch.setattr(be, "prepare_orientation", lambda *a, **k: {"error": "boom"})
    out = be.batch_prepare_orientation("batch_x", MANIFEST, "/tmp/key.json")
    assert all(s["pages"] == [] and s.get("error") == "boom" for s in out["sheets"])


# ---- resume ------------------------------------------------------------------

def test_resume_zero_rotation_and_aggregation(monkeypatch):
    got = {}
    def _stub(run_id, rotations=None, **k):
        got[run_id] = rotations
        return _success_result(k.get("student_name"))
    monkeypatch.setattr(be, "resume_after_orientation", _stub)
    res = be.batch_resume_orientation("batch_x", MANIFEST, {}, "/tmp/key.json")
    assert res["status"] == "success" and res["batch_id"] == "batch_x"
    assert [s["name"] for s in res["students"]] == ["Asha", "Asha (2)"]
    assert res["students"][0]["marks_awarded"] == 2 and res["students"][0]["marks_max"] == 3
    assert res["total_cost"] == "$0.020000"
    for rid in (be._safe_stem("Asha", 1), be._safe_stem("Asha", 2)):
        assert got[rid] == {}          # no rotations -> zero-rotation resume == today


def test_resume_routes_rotations_by_run_id(monkeypatch):
    got = {}
    monkeypatch.setattr(be, "resume_after_orientation",
                        lambda run_id, rotations=None, **k: (got.__setitem__(run_id, rotations),
                                                             _success_result("x"))[1])
    rid1 = be._safe_stem("Asha", 1)
    be.batch_resume_orientation("batch_x", MANIFEST, {rid1: {"1": 270}}, "/tmp/key.json")
    assert got[rid1] == {"1": 270}
    assert got[be._safe_stem("Asha", 2)] == {}     # unspecified sheet -> empty (no rotation)


def test_resume_shape_matches_batch_evaluate(monkeypatch):
    """The skip path (batch_evaluate) and the gated path (batch_resume_orientation) must produce the
    same per-student dict shape + total, so the results UI renders both identically."""
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: a[-1])
    monkeypatch.setattr(be, "full_evaluate", lambda sheet_pdf, **k: _success_result(k.get("student_name")))
    monkeypatch.setattr(be, "resume_after_orientation",
                        lambda run_id, rotations=None, **k: _success_result(k.get("student_name")))
    a = be.batch_evaluate("b", MANIFEST, "/tmp/key.json")
    b = be.batch_resume_orientation("b", MANIFEST, {}, "/tmp/key.json")
    assert set(a["students"][0].keys()) == set(b["students"][0].keys())
    assert [s["name"] for s in a["students"]] == [s["name"] for s in b["students"]]
    assert a["total_cost"] == b["total_cost"]
