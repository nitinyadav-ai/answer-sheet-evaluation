"""Offline tests for grade_with_consistency (self-consistency voting). The real grader is stubbed,
so these run with no network/cost. They pin the non-degradation contract: EVAL_VOTES=1 (default) is
a single call returning today's result; votes>1 takes the MEDIAN (fixing one-off harsh outliers) and
records the spread; a unanimous vote adds no spread note."""
import asyncio
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

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")


def _stub(marks_sequence):
    """An async stand-in for evaluate_single that yields successive marks from `marks_sequence`,
    so a test can control what each 'vote' returns. Counts calls."""
    state = {"i": 0}

    async def stub(question_id, ocr_data, db_data, index):
        m = marks_sequence[state["i"] % len(marks_sequence)]
        state["i"] += 1
        return index, question_id, {"Marks Awarded": m, "Maximum Marks": 3,
                                    "Justification": f"vote->{m}", "Needs Review (Yes/No)": "No"}
    return stub, state


def test_default_votes_1_is_a_single_call(monkeypatch):
    stub, state = _stub([2])
    monkeypatch.setattr(ev, "evaluate_single", stub)
    monkeypatch.delenv("EVAL_VOTES", raising=False)          # unset -> default 1
    idx, qid, res = asyncio.run(ev.grade_with_consistency("Q30", {}, {"marks": 3}, 7))
    assert state["i"] == 1                                   # exactly ONE grade (today's behaviour)
    assert (idx, qid, res["Marks Awarded"]) == (7, "Q30", 2)
    assert "Grading Spread" not in res


def test_votes_3_takes_median_and_fixes_harsh_outlier(monkeypatch):
    stub, state = _stub([0, 2, 2])                           # one harsh 0 among two 2s
    monkeypatch.setattr(ev, "evaluate_single", stub)
    monkeypatch.setenv("EVAL_VOTES", "3")
    idx, qid, res = asyncio.run(ev.grade_with_consistency("Q31", {}, {"marks": 3}, 0))
    assert state["i"] == 3
    assert res["Marks Awarded"] == 2                         # median, not the harsh 0
    assert "Grading Spread" in res and "median 2" in res["Grading Spread"]


def test_votes_3_majority_zero_stays_zero(monkeypatch):
    stub, _ = _stub([0, 0, 2])                               # genuinely-weak answer: 2 of 3 say 0
    monkeypatch.setattr(ev, "evaluate_single", stub)
    monkeypatch.setenv("EVAL_VOTES", "3")
    _i, _q, res = asyncio.run(ev.grade_with_consistency("Q30", {}, {"marks": 3}, 0))
    assert res["Marks Awarded"] == 0                         # median respects the majority -> not inflated


def test_unanimous_votes_have_no_spread_note(monkeypatch):
    stub, _ = _stub([3, 3, 3])
    monkeypatch.setattr(ev, "evaluate_single", stub)
    monkeypatch.setenv("EVAL_VOTES", "3")
    _i, _q, res = asyncio.run(ev.grade_with_consistency("Q1", {}, {"marks": 3}, 0))
    assert res["Marks Awarded"] == 3 and "Grading Spread" not in res


def test_bad_env_value_falls_back_to_single_call(monkeypatch):
    stub, state = _stub([1])
    monkeypatch.setattr(ev, "evaluate_single", stub)
    monkeypatch.setenv("EVAL_VOTES", "not-a-number")
    _i, _q, res = asyncio.run(ev.grade_with_consistency("Q5", {}, {"marks": 3}, 0))
    assert state["i"] == 1 and res["Marks Awarded"] == 1     # never crashes on a bad flag
