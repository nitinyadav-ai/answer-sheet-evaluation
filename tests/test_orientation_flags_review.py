"""Review wiring for the orientation probe: evaluate._apply_orientation_flags joins orientation_flags.json
to page_mapping.json and raises Needs Review on the affected questions WITHOUT changing any mark. Fully
offline (temp sidecars, no network)."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate
except (ImportError, SystemExit) as e:  # pragma: no cover
    evaluate = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(evaluate is None, reason="evaluate module unavailable in this env")


def _setup(tmp_path, flags, page_mapping):
    ocr_path = os.path.join(tmp_path, "ocr_answers.json")
    with open(ocr_path, "w") as f:
        json.dump({}, f)
    with open(os.path.join(tmp_path, "orientation_flags.json"), "w") as f:
        json.dump(flags, f)
    with open(os.path.join(tmp_path, "page_mapping.json"), "w") as f:
        json.dump(page_mapping, f)
    return ocr_path


def test_uncertain_flag_raises_review_on_pages_questions(tmp_path):
    tmp_path = str(tmp_path)
    # Flag page 3 uncertain; page_mapping puts Q5 + Q6 on that page image (different dir -> basename join).
    flags = [{"index": 2, "image_path": "/scan/preprocessed_page_3.png", "action": "uncertain",
              "from": 0, "to": 180}]
    page_mapping = {
        "/abs/run/preprocessed_page_3.png": [{"question_id": "Q5"}, {"question_id": "Q6"}],
        "/abs/run/preprocessed_page_4.png": [{"question_id": "Q7"}],
    }
    ocr_path = _setup(tmp_path, flags, page_mapping)
    results = [("Q5", {"Marks Awarded": 3, "Needs Review (Yes/No)": "No"}),
               ("Q6", {"Marks Awarded": 0, "Needs Review (Yes/No)": "No"}),
               ("Q7", {"Marks Awarded": 2, "Needs Review (Yes/No)": "No"})]
    out = evaluate._apply_orientation_flags(results, ocr_path)

    by = dict(out)
    assert by["Q5"]["Needs Review (Yes/No)"] == "Yes" and "could not be confidently oriented" in by["Q5"]["Orientation Warning"]
    assert by["Q6"]["Needs Review (Yes/No)"] == "Yes"
    assert by["Q7"]["Needs Review (Yes/No)"] == "No"           # different page -> untouched
    assert by["Q5"]["Marks Awarded"] == 3 and by["Q6"]["Marks Awarded"] == 0   # marks never changed


def test_rotated_flag_uses_rotated_message_and_prefix_tolerant(tmp_path):
    tmp_path = str(tmp_path)
    flags = [{"index": 0, "image_path": "/x/preprocessed_page_1.png", "action": "rotated",
              "from": 0, "to": 180}]
    page_mapping = {"/y/preprocessed_page_1.png": [{"question_id": "Q1"}]}
    ocr_path = _setup(tmp_path, flags, page_mapping)
    # results qid carries a subject prefix -> base-number match must still hit.
    results = [("SCI10_Q1", {"Marks Awarded": 1, "Needs Review (Yes/No)": "No"})]
    out = evaluate._apply_orientation_flags(results, ocr_path)
    res = out[0][1]
    assert res["Needs Review (Yes/No)"] == "Yes"
    assert "auto-rotated" in res["Orientation Warning"]


def test_no_sidecar_is_noop(tmp_path):
    tmp_path = str(tmp_path)
    ocr_path = os.path.join(tmp_path, "ocr_answers.json")
    with open(ocr_path, "w") as f:
        json.dump({}, f)
    results = [("Q1", {"Needs Review (Yes/No)": "No"})]
    out = evaluate._apply_orientation_flags(results, ocr_path)
    assert out[0][1]["Needs Review (Yes/No)"] == "No"          # nothing to apply -> unchanged


def test_empty_flags_is_noop(tmp_path):
    tmp_path = str(tmp_path)
    ocr_path = _setup(tmp_path, [], {"/y/preprocessed_page_1.png": [{"question_id": "Q1"}]})
    results = [("Q1", {"Needs Review (Yes/No)": "No"})]
    out = evaluate._apply_orientation_flags(results, ocr_path)
    assert out[0][1]["Needs Review (Yes/No)"] == "No"
