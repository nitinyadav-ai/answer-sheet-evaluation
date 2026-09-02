"""GET /student-report/<run_id> — the read-only route that powers the batch card -> full report open
(and single-mode reload recovery). Offline: OUTPUT_BASE is redirected to a tmp dir; no grading runs.
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
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "OUTPUT_BASE", str(tmp_path))
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _write(run_dir, name, payload):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, name), "w") as f:
        json.dump(payload, f)


def test_returns_report_shape_and_progress(client, tmp_path):
    run = "sheet_1_ASHA"
    _write(os.path.join(str(tmp_path), run), "review_state.json", {
        "student_details": {"name": "Asha"}, "report_path": "/r/Asha.pdf",
        "evaluations": [
            ["Q1", {"Marks Awarded": 1, "Maximum Marks": 2, "Needs Review (Yes/No)": "Yes"}],
            ["Q2", {"Marks Awarded": 2, "Maximum Marks": 2, "Prompt Injection Detected": "Yes",
                    "Teacher Reviewed": True}]]})
    r = client.get(f"/student-report/{run}")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success" and d["review_id"] == run
    assert d["student_details"]["name"] == "Asha" and len(d["evaluations"]) == 2
    assert d["review_progress"] == {"reviewed": 1, "total": 2, "needs_review": 1, "injection": 1}


def test_prefers_working_copy_over_state(client, tmp_path):
    run = "sheet_2_BINA"
    d = os.path.join(str(tmp_path), run)
    _write(d, "review_state.json", {"evaluations": [["Q1", {"Marks Awarded": 0, "Maximum Marks": 2}]]})
    _write(d, "review_render.json", {"evaluations": [["Q1", {"Marks Awarded": 2, "Maximum Marks": 2}]]})
    r = client.get(f"/student-report/{run}")
    assert r.get_json()["evaluations"][0][1]["Marks Awarded"] == 2


def test_missing_run_404(client):
    assert client.get("/student-report/sheet_9_NOBODY").status_code == 404


def test_bad_run_id_400(client):
    assert client.get("/student-report/a~b").status_code == 400   # '~' not in the safe-id charset
