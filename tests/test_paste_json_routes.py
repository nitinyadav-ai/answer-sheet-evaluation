"""POST /paste-question-paper and /paste-answer-key — the teacher pastes the app's own JSON schema
instead of uploading a PDF. Offline: UPLOAD_FOLDER + the state-file paths are redirected to a tmp dir,
so no LLM parse runs and the real uploads/ is untouched. These lock:
  - pasted JSON is persisted to the SAME flat files (+ choices sidecar) the PDF path writes;
  - nested {metadata,questions}/{questions} AND flat {qid:{...}} are both accepted;
  - the shared _finalize_* helpers back both the /parse-* and /paste-* routes (refactor parity);
  - invalid JSON is a clean 400; the validation gates still fire.
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
    up = str(tmp_path)
    monkeypatch.setitem(webapp.app.config, "UPLOAD_FOLDER", up)
    monkeypatch.setattr(webapp, "QUESTION_PAPER_PATH", os.path.join(up, "current_question_paper.json"))
    monkeypatch.setattr(webapp, "ANSWER_KEY_PATH", os.path.join(up, "current_answer_key.json"))
    monkeypatch.setattr(webapp, "MARKS_SOURCE_PATH", os.path.join(up, "marks_source_state.json"))
    monkeypatch.setattr(webapp, "REPORT_STATE_PATH", os.path.join(up, "report_path_state.json"))
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    c._up = up
    return c


def _post(client, url, obj):
    return client.post(url, data=json.dumps(obj), content_type="application/json")


def _read(client, name):
    return json.load(open(os.path.join(client._up, name)))


QP = {"questions": {
    "Q1": {"question_id": "Q1", "question": "LCM of 960 and 240?", "marks": 1, "type": "MCQ"},
    "Q2": {"question_id": "Q2", "question": "Prove root 5 is irrational.", "marks": 3, "type": "Short Answer"},
}}

KEY = {"metadata": {"class": "Class X", "subject": "Mathematics",
                    "choice_groups": [{"parent": "Q22", "members": ["Q22(a)", "Q22(b)"], "required": 1}],
                    "inline_choice_ids": []},
       "questions": {
           "Q1": {"question_id": "Q1", "question": "…", "answer": "(A) 960", "type": "MCQ", "subject": "Mathematics", "marks": 1},
           "Q22(a)": {"question_id": "Q22(a)", "question": "…", "answer": "AC = 5 cm", "type": "Short Answer", "subject": "Mathematics", "marks": 2},
           "Q22(b)": {"question_id": "Q22(b)", "question": "…", "answer": "YR = 2.7 cm", "type": "Short Answer", "subject": "Mathematics", "marks": 2},
       }}


def test_paste_qp_writes_flat_file(client):
    r = _post(client, "/paste-question-paper", QP)
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success" and d["count"] == 2
    written = _read(client, "current_question_paper.json")
    assert set(written.keys()) == {"Q1", "Q2"}          # unwrapped to a flat map
    assert written["Q1"]["marks"] == 1


def test_paste_key_writes_flat_sidecar_and_state(client):
    # Post a QP first so the cross-check + marks-mismatch run against it.
    _post(client, "/paste-question-paper", {"questions": {
        "Q1": {"question_id": "Q1", "question": "…", "marks": 1, "type": "MCQ"},
        "Q22": {"question_id": "Q22", "question": "(a) … OR (b) …", "marks": 2, "type": "Short Answer"}}})
    r = _post(client, "/paste-answer-key", KEY)
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success"
    assert d["class"] == "Class X" and d["subject"] == "Mathematics"
    assert "suggested_path" in d and "marks_mismatch" in d
    key = _read(client, "current_answer_key.json")
    assert set(key.keys()) == {"Q1", "Q22(a)", "Q22(b)"}      # flat, metadata stripped
    ch = _read(client, "current_answer_key_choices.json")
    assert ch["choice_groups"][0]["parent"] == "Q22" and ch["inline_choice_ids"] == []
    assert os.path.exists(os.path.join(client._up, "marks_source_state.json"))
    assert os.path.exists(os.path.join(client._up, "report_path_state.json"))


def test_paste_flat_key_accepted_with_empty_sidecar(client):
    flat = {"Q1": {"question_id": "Q1", "question": "…", "answer": "(A) 960",
                   "type": "MCQ", "subject": "Science", "marks": 1}}
    r = _post(client, "/paste-answer-key", flat)
    assert r.status_code == 200 and r.get_json()["status"] == "success"
    assert _read(client, "current_answer_key_choices.json") == {"choice_groups": [], "inline_choice_ids": []}
    assert r.get_json()["subject"] == "Science"               # fell back from a question's subject


def test_nested_and_flat_qp_write_identical_files(client):
    _post(client, "/paste-question-paper", QP)                 # nested {questions}
    nested = _read(client, "current_question_paper.json")
    _post(client, "/paste-question-paper", QP["questions"])    # flat {qid:{...}}
    assert _read(client, "current_question_paper.json") == nested


def test_invalid_json_is_400(client):
    for url in ("/paste-question-paper", "/paste-answer-key"):
        r = client.post(url, data="{not valid json,", content_type="application/json")
        assert r.status_code == 400
        assert r.get_json()["status"] == "error"


def test_zero_marks_qp_is_blocked(client):
    bad = {"questions": {"Q1": {"question_id": "Q1", "question": "…", "marks": 0, "type": "MCQ"}}}
    d = _post(client, "/paste-question-paper", bad).get_json()
    assert d["status"] == "error"                              # >=50% missing marks -> blocking
    assert any(i.get("severity") == "error" for i in d.get("issues", []))


def test_empty_questions_is_blocked(client):
    d = _post(client, "/paste-question-paper", {"questions": {}}).get_json()
    assert d["status"] == "error"                              # no_questions


def test_finalize_helpers_back_both_routes(client):
    # The extracted helpers are what BOTH /parse-* and /paste-* now call — exercise them directly.
    with webapp.app.test_request_context():
        qp_body = webapp._finalize_question_paper(QP, []).get_json()
        key_body = webapp._finalize_answer_key(KEY, []).get_json()
    assert qp_body["status"] == "success" and qp_body["count"] == 2
    assert key_body["status"] == "success" and key_body["class"] == "Class X"
    # and the files they wrote match what the route wrote
    assert set(_read(client, "current_answer_key.json").keys()) == {"Q1", "Q22(a)", "Q22(b)"}
