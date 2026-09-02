"""Report/UX changes (offline, no network):
  #1 live step-progress: full_evaluator._write_progress writes progress.json; /orient-status surfaces it.
  #2 Question field: evaluate._question_for returns the real question, or '' when it's missing or equals
     the answer (so the report falls back to the question number instead of showing the answer).
  #4 downloaded PDF excludes diagram images by default (they remain in the online report only).
"""
import os
import sys
import json

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "evaluation_app"))

try:
    import evaluate as ev
except Exception:  # pragma: no cover
    ev = None
try:
    import full_evaluator as fe
except Exception:  # pragma: no cover
    fe = None
try:
    import app as webapp
except (ImportError, SystemExit):  # pragma: no cover
    webapp = None


# ------------------------- #2 Question field -------------------------

@pytest.mark.skipif(ev is None, reason="evaluate unavailable")
def test_question_for_returns_real_question():
    assert ev._question_for({"question": "Define a stack.", "answer": "A LIFO structure"}) == "Define a stack."


@pytest.mark.skipif(ev is None, reason="evaluate unavailable")
def test_question_for_blank_when_missing_or_equals_answer():
    assert ev._question_for({"question": "", "answer": "x"}) == ""          # no question text
    assert ev._question_for({"question": "True", "answer": "True"}) == ""   # objective: stored the answer
    assert ev._question_for({"answer": "x"}) == ""
    assert ev._question_for({}) == ""
    assert ev._question_for("not a dict") == ""


# ------------------------- #1 progress writer -------------------------

@pytest.mark.skipif(fe is None, reason="full_evaluator unavailable")
def test_write_progress_writes_valid_json(tmp_path):
    fe._write_progress(str(tmp_path), 1)
    p = json.load(open(os.path.join(str(tmp_path), "progress.json")))
    assert p["index"] == 1 and p["total"] == len(fe._PROGRESS_STEPS)
    assert p["label"] == fe._PROGRESS_STEPS[0] and p["steps"] == fe._PROGRESS_STEPS


@pytest.mark.skipif(fe is None, reason="full_evaluator unavailable")
def test_write_progress_clamps_out_of_range_index(tmp_path):
    fe._write_progress(str(tmp_path), 99)
    assert json.load(open(os.path.join(str(tmp_path), "progress.json")))["index"] == len(fe._PROGRESS_STEPS)


# ------------------------- #4 PDF excludes diagram images -------------------------

@pytest.mark.skipif(ev is None, reason="evaluate unavailable")
def test_pdf_excludes_diagram_images_by_default():
    # The downloaded PDF must not embed diagram crops (they stay in the online report only).
    assert ev._PDF_DIAGRAM_IMAGES is False


# ------------------------- #1 /orient-status surfaces progress -------------------------

@pytest.mark.skipif(webapp is None, reason="web app unavailable")
def test_orient_status_includes_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_BASE", str(tmp_path))
    run_id = "testrun"
    d = os.path.join(str(tmp_path), run_id)
    os.makedirs(d, exist_ok=True)
    json.dump({"run_id": run_id, "phase": "evaluating"}, open(os.path.join(d, "orient_status.json"), "w"))
    json.dump({"index": 2, "total": 3, "label": "Analyzing diagrams",
               "steps": ["Reading handwriting (OCR)", "Analyzing diagrams", "Grading & building report"]},
              open(os.path.join(d, "progress.json"), "w"))
    webapp.app.config["TESTING"] = True
    r = webapp.app.test_client().get(f"/orient-status/{run_id}").get_json()
    assert r["phase"] == "evaluating"
    assert r["progress"]["index"] == 2 and r["progress"]["label"] == "Analyzing diagrams"


@pytest.mark.skipif(webapp is None, reason="web app unavailable")
def test_orient_status_no_progress_when_done(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_BASE", str(tmp_path))
    run_id = "donerun"
    d = os.path.join(str(tmp_path), run_id)
    os.makedirs(d, exist_ok=True)
    json.dump({"run_id": run_id, "phase": "done", "result": {}}, open(os.path.join(d, "orient_status.json"), "w"))
    json.dump({"index": 3}, open(os.path.join(d, "progress.json"), "w"))
    webapp.app.config["TESTING"] = True
    r = webapp.app.test_client().get(f"/orient-status/{run_id}").get_json()
    assert r["phase"] == "done" and "progress" not in r     # progress only while evaluating
