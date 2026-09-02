"""Batch results must carry review_id per student (= _safe_stem == the output/<run_id>/ dir), so the
frontend can open each student's report and scope corrections/regrades. Offline; the per-sheet
pipeline is stubbed. Also re-asserts the skip path and the orientation path share one key set.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import batch_evaluator as be
except (ImportError, SystemExit):  # pragma: no cover
    be = None

pytestmark = pytest.mark.skipif(be is None, reason="batch_evaluator unavailable")

MANIFEST = {"source_pdf": "/tmp/nope.pdf", "num_pages": 6, "sheets": [
    {"id": "s1", "name": "Asha", "subject": "Math", "start_page": 1, "end_page": 3},
    {"id": "s2", "name": "Asha", "subject": "Math", "start_page": 4, "end_page": 6},
]}


def _result(rid, name):
    return {"status": "success", "report_path": f"/r/{rid}.pdf", "cost": "$0.010000",
            "evaluations": [["Q1", {"Marks Awarded": 1, "Maximum Marks": 2}]],
            "student_details": {"name": name}, "review_id": rid}


def _stub_full(sheet_pdf, **k):
    rid = os.path.splitext(os.path.basename(sheet_pdf))[0]   # full_evaluate's run_id == the stem
    return _result(rid, k.get("student_name"))


def _stub_resume(run_id, rotations=None, **k):
    return _result(run_id, k.get("student_name"))


def test_batch_evaluate_carries_review_id(monkeypatch):
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: a[-1])
    monkeypatch.setattr(be, "full_evaluate", _stub_full)
    res = be.batch_evaluate("b", MANIFEST, "/tmp/key.json")
    assert [s["review_id"] for s in res["students"]] == \
           [be._safe_stem("Asha", 1), be._safe_stem("Asha", 2)]


def test_batch_resume_carries_review_id(monkeypatch):
    monkeypatch.setattr(be, "resume_after_orientation", _stub_resume)
    res = be.batch_resume_orientation("b", MANIFEST, {}, "/tmp/key.json")
    assert [s["review_id"] for s in res["students"]] == \
           [be._safe_stem("Asha", 1), be._safe_stem("Asha", 2)]


def test_both_paths_same_keys_including_review_id(monkeypatch):
    monkeypatch.setattr(be, "slice_pdf", lambda *a, **k: a[-1])
    monkeypatch.setattr(be, "full_evaluate", _stub_full)
    monkeypatch.setattr(be, "resume_after_orientation", _stub_resume)
    a = be.batch_evaluate("b", MANIFEST, "/tmp/key.json")
    b = be.batch_resume_orientation("b", MANIFEST, {}, "/tmp/key.json")
    assert set(a["students"][0].keys()) == set(b["students"][0].keys())
    assert "review_id" in a["students"][0]
