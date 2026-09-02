"""Segmentation-safety off-topic flag: an answer the grader judges OFF-TOPIC for its question (a likely
scanning/segmentation mis-assignment) is forced to manual review WITHOUT changing its marks. Pure,
offline -- no grading call is made."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit) as e:
    ev = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate.py unavailable in this env")


def test_off_topic_yes_forces_review_and_notes():
    r = {"Marks Awarded": 0, "Needs Review (Yes/No)": "No", "Off-Topic (Yes/No)": "Yes"}
    ev._apply_off_topic_review(r)
    assert r["Needs Review (Yes/No)"] == "Yes"
    assert "Capture Status" in r


def test_off_topic_no_leaves_review_untouched():
    r = {"Marks Awarded": 1, "Needs Review (Yes/No)": "No", "Off-Topic (Yes/No)": "No"}
    ev._apply_off_topic_review(r)
    assert r["Needs Review (Yes/No)"] == "No"
    assert "Capture Status" not in r


def test_off_topic_absent_is_noop():
    # Feature gated off -> the grader never emits the field; must not touch the result.
    r = {"Marks Awarded": 2, "Needs Review (Yes/No)": "No"}
    ev._apply_off_topic_review(r)
    assert r["Needs Review (Yes/No)"] == "No" and "Capture Status" not in r


def test_off_topic_never_changes_marks():
    r = {"Marks Awarded": 0, "Needs Review (Yes/No)": "No", "Off-Topic (Yes/No)": "Yes"}
    ev._apply_off_topic_review(r)
    assert r["Marks Awarded"] == 0     # marks are the grader's; the flag only affects review
