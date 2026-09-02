"""Offline tests for the cheap-first grading CASCADE (EVAL_CASCADE). The fast pass (evaluate_single) and
the thinking grader (grade_with_consistency) are stubbed, so these run with no network/cost. They pin the
contract: subjective questions grade on the FAST model first and escalate to the thinking grader ONLY when
the mark is in the balance; MCQ-type questions skip the fast model entirely (its documented MCQ regression);
a clean full-mark / clear-zero grade stays fast; and the gate defaults OFF (byte-identical to today)."""
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


def _db(mx, type_="Short Answer"):
    return {"marks": mx, "type": type_}


def _fast_stub(result, calls):
    """Async stand-in for evaluate_single that records the (model, reasoning) it was called with."""
    async def stub(question_id, ocr_data, db_data, index, model=None, reasoning_effort=ev._UNSET):
        calls.append({"model": model, "reasoning": reasoning_effort})
        return index, question_id, dict(result)
    return stub


def _deep_stub(result, calls):
    """Async stand-in for grade_with_consistency (the thinking grader)."""
    async def stub(question_id, ocr_data, db_data, index):
        calls.append("deep")
        return index, question_id, dict(result)
    return stub


# ------------------------- gate -------------------------

def test_cascade_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("EVAL_CASCADE", raising=False)
    assert ev._cascade_on() is False                          # unset -> off -> today's path
    monkeypatch.setenv("EVAL_CASCADE", "1")
    assert ev._cascade_on() is True
    monkeypatch.setenv("EVAL_CASCADE", "0")
    assert ev._cascade_on() is False


# ------------------------- escalation predicate -------------------------

def test_partial_credit_alone_is_trusted(monkeypatch):
    """CHANGED: partial credit no longer escalates on its own.

    Measured against a teacher's marks (22 questions, subtotal 48.5): escalating partial credit moved
    marks AWAY from the human -- narrowed total 47.0 / MAE 0.43 vs old 46.0 / MAE 0.48 -- while costing
    9 extra thinking calls and 68% more output tokens. The expensive path was the less accurate one."""
    monkeypatch.delenv("EVAL_CASCADE_ESCALATE_PARTIAL", raising=False)
    monkeypatch.delenv("EVAL_GRADING_CALIBRATION", raising=False)
    assert ev._cascade_should_escalate(
        {"Marks Awarded": 2, "Confidence (Low/Medium/High)": "High"}, _db(5), {"answer": "x"}) is False


def test_partial_credit_escalation_is_restorable(monkeypatch):
    """The evidence is one sheet and one marker, so the old policy stays reachable."""
    monkeypatch.setenv("EVAL_CASCADE_ESCALATE_PARTIAL", "1")
    assert ev._cascade_should_escalate(
        {"Marks Awarded": 2, "Confidence (Low/Medium/High)": "High"}, _db(5), {"answer": "x"}) is True


def test_trust_full_marks_high_confidence():
    r = {"Marks Awarded": 5, "Confidence (Low/Medium/High)": "High", "Needs Review (Yes/No)": "No"}
    assert ev._cascade_should_escalate(r, _db(5), {"answer": "a correct answer"}) is False


def test_trust_blank_zero():
    r = {"Marks Awarded": 0, "Confidence (Low/Medium/High)": "High"}
    assert ev._cascade_should_escalate(r, _db(3), {"answer": ""}) is False        # blank -> the 0 is trusted


def test_escalate_substantive_zero_on_multimark():
    r = {"Marks Awarded": 0, "Confidence (Low/Medium/High)": "High"}
    assert ev._cascade_should_escalate(r, _db(3), {"answer": "a real written attempt here"}) is True


def test_recheck_single_mark_zero(monkeypatch):
    """A 1-mark zero USED to be trusted outright (threshold was 2 marks), which left it final on the
    word of the fast instruct model with reasoning off -- the cheapest place to hand out a 0 and the
    least likely to be re-examined. v2 re-checks it."""
    monkeypatch.delenv("EVAL_CASCADE_MIN_MARKS", raising=False)
    r = {"Marks Awarded": 0, "Confidence (Low/Medium/High)": "High"}
    assert ev._cascade_should_escalate(r, _db(1), {"answer": "a wrong one-mark attempt here"}) is True


def test_legacy_calibration_still_trusts_a_single_mark_zero(monkeypatch):
    """The revert flag has to actually revert: EVAL_GRADING_CALIBRATION=legacy restores the 2-mark
    threshold, so this is the behaviour difference the flag exists to give back."""
    monkeypatch.delenv("EVAL_CASCADE_MIN_MARKS", raising=False)
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    r = {"Marks Awarded": 0, "Confidence (Low/Medium/High)": "High"}
    assert ev._cascade_should_escalate(r, _db(1), {"answer": "a wrong one-mark attempt here"}) is False


def test_an_explicit_min_marks_env_still_wins(monkeypatch):
    """The threshold stays tunable: an operator who sets it explicitly is not overridden by v2."""
    monkeypatch.setenv("EVAL_CASCADE_MIN_MARKS", "3")
    r = {"Marks Awarded": 0, "Confidence (Low/Medium/High)": "High"}
    assert ev._cascade_should_escalate(r, _db(1), {"answer": "a wrong one-mark attempt here"}) is False
    assert ev._cascade_should_escalate(r, _db(3), {"answer": "a wrong three-mark attempt"}) is True


def test_escalate_low_confidence():
    assert ev._cascade_should_escalate(
        {"Marks Awarded": 3, "Confidence (Low/Medium/High)": "Low"}, _db(3), {"answer": "x"}) is True


def test_escalate_on_offtopic_and_injection():
    for f in ("Off-Topic (Yes/No)", "Prompt Injection Detected"):
        r = {"Marks Awarded": 3, "Confidence (Low/Medium/High)": "High", f: "Yes"}
        assert ev._cascade_should_escalate(r, _db(3), {"answer": "correct"}) is True, f


def test_legibility_flags_do_not_escalate():
    # Bad-handwriting / Needs-Review are OCR legibility flags -> the thinking grader reads the SAME text,
    # so a clean full-mark high-confidence grade STAYS FAST (the flag is preserved for the teacher).
    # (This is the fix: on KRISHNA 15/17 subjective answers were bad-hw -> everything escalated -> no gain.)
    for extra in ({"Needs Review (Yes/No)": "Yes"}, {"Bad Handwriting Flag": True},
                  {"Needs Review (Yes/No)": "Yes", "Bad Handwriting Flag": True}):
        r = {"Marks Awarded": 3, "Confidence (Low/Medium/High)": "High", **extra}
        assert ev._cascade_should_escalate(r, _db(3), {"answer": "correct"}) is False, extra


def test_escalate_on_unparseable_grade():
    assert ev._cascade_should_escalate("not a dict", _db(3), {}) is True
    assert ev._cascade_should_escalate(
        {"Marks Awarded": "NaN", "Confidence (Low/Medium/High)": "High"}, _db(3), {}) is True


# ------------------------- grade_cascade routing -------------------------

def test_full_marks_stays_fast(monkeypatch):
    monkeypatch.delenv("EVAL_CASCADE_FAST_MODEL", raising=False)
    fast, deep = [], []
    monkeypatch.setattr(ev, "evaluate_single", _fast_stub(
        {"Marks Awarded": 3, "Maximum Marks": 3, "Confidence (Low/Medium/High)": "High",
         "Needs Review (Yes/No)": "No"}, fast))
    monkeypatch.setattr(ev, "grade_with_consistency", _deep_stub({"Marks Awarded": 3}, deep))
    idx, qid, res = asyncio.run(ev.grade_cascade("Q30", {"answer": "correct"}, _db(3), 5))
    assert len(fast) == 1 and deep == []                       # ONLY the fast pass ran
    assert fast[0]["model"] == "qwen/qwen3-vl-235b-a22b-instruct" and fast[0]["reasoning"] == ""
    assert (idx, qid) == (5, "Q30") and res["Graded By"] == "fast" and res["Marks Awarded"] == 3


def test_partial_credit_keeps_the_fast_grade(monkeypatch):
    """End to end: a partially-credited answer is graded ONCE and the fast mark stands."""
    monkeypatch.delenv("EVAL_CASCADE_ESCALATE_PARTIAL", raising=False)
    monkeypatch.delenv("EVAL_GRADING_CALIBRATION", raising=False)
    fast, deep = [], []
    monkeypatch.setattr(ev, "evaluate_single", _fast_stub(
        {"Marks Awarded": 2, "Maximum Marks": 5, "Confidence (Low/Medium/High)": "High"}, fast))
    monkeypatch.setattr(ev, "grade_with_consistency", _deep_stub(
        {"Marks Awarded": 3, "Maximum Marks": 5}, deep))
    _i, _q, res = asyncio.run(ev.grade_cascade("Q31", {"answer": "partly right"}, _db(5, "Long Answer"), 0))
    assert len(fast) == 1 and deep == []                       # no thinking call at all
    assert res["Graded By"] == "fast" and res["Marks Awarded"] == 2


def test_a_real_signal_still_escalates_and_records_why(monkeypatch):
    """The escalations that remain must still happen AND say why, so the trigger mix is readable off
    real runs rather than needing another bespoke experiment."""
    monkeypatch.delenv("EVAL_CASCADE_ESCALATE_PARTIAL", raising=False)
    fast, deep = [], []
    monkeypatch.setattr(ev, "evaluate_single", _fast_stub(
        {"Marks Awarded": 2, "Maximum Marks": 5, "Confidence (Low/Medium/High)": "Low"}, fast))
    monkeypatch.setattr(ev, "grade_with_consistency", _deep_stub(
        {"Marks Awarded": 3, "Maximum Marks": 5}, deep))
    _i, _q, res = asyncio.run(ev.grade_cascade("Q31", {"answer": "partly right"}, _db(5, "Long Answer"), 0))
    assert len(fast) == 1 and deep == ["deep"]
    assert res["Graded By"] == "thinking" and res["Marks Awarded"] == 3
    assert res["Escalated Because"] == "low_confidence"
    assert res["Fast Marks"] == 2                              # the overridden mark is preserved


def test_mcq_skips_the_fast_model(monkeypatch):
    fast, deep = [], []
    monkeypatch.setattr(ev, "evaluate_single", _fast_stub({"Marks Awarded": 1}, fast))
    monkeypatch.setattr(ev, "grade_with_consistency", _deep_stub({"Marks Awarded": 1}, deep))
    asyncio.run(ev.grade_cascade("Q7", {"answer": "(b)"}, _db(1, "MCQ"), 0))
    assert fast == [] and deep == ["deep"]                     # MCQ never touches the fast model


def test_fast_error_falls_back_to_thinking(monkeypatch):
    deep = []
    async def boom(*a, **k):
        raise RuntimeError("fast backend down")
    monkeypatch.setattr(ev, "evaluate_single", boom)
    monkeypatch.setattr(ev, "grade_with_consistency", _deep_stub({"Marks Awarded": 4}, deep))
    _i, _q, res = asyncio.run(ev.grade_cascade("Q9", {"answer": "x"}, _db(4), 0))
    assert deep == ["deep"] and res["Marks Awarded"] == 4      # never crashes -> thinking grader
