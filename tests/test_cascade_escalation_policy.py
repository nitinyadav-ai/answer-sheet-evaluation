"""The cascade must not spend a thinking call on partial credit alone.

The trigger `0 < mark < maximum` escalated EVERY partially-credited answer. After the grading
calibration, partial credit became the COMMON outcome rather than the exception, so the cascade's
cheap path stopped being the cheap path -- 68% of LLM-graded answers on one sheet.

Measured against a teacher's own per-question marks (Computer Science, 22 LLM-graded questions,
teacher subtotal 48.5), through the real `grade_cascade`:

    narrowed (this default)  total 47.0   MAE 0.43   2 escalations   19,038 output tokens
    old (escalate on all)    total 46.0   MAE 0.48  11 escalations   58,591 output tokens

Escalating moved marks AWAY from the teacher, so this was never a speed-versus-accuracy trade: the
expensive path was also the less accurate one. Both differing questions (Q31, Q37) landed closer to
the teacher under the narrowed policy.

Repeat passes showed each tier scoring 21 of 22 questions identically (mean drift 0.02 marks), so a
fast mark is stable rather than lucky -- re-grading it bought variance reduction that was not needed.

Caveat pinned by these tests: the evidence is ONE sheet and one marker, so the old behaviour stays
reachable and every escalation records WHY, letting more marked sheets settle it without another
bespoke experiment.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit):                                    # pragma: no cover
    ev = None

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate unavailable")


def _db(marks=3, type_str="Short Answer"):
    return {"marks": marks, "type": type_str, "subject": "Computer Science"}


def _res(mark, conf="High", **extra):
    r = {"Marks Awarded": mark, "Confidence (Low/Medium/High)": conf}
    r.update(extra)
    return r


SUBSTANTIVE = {"answer": "x" * 60}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("EVAL_CASCADE_ESCALATE_PARTIAL", "EVAL_GRADING_CALIBRATION", "EVAL_CASCADE_MIN_MARKS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --- the change itself ----------------------------------------------------------------------------

@pytest.mark.parametrize("mark,mx", [(1.5, 3), (0.5, 1), (2.5, 5), (4.5, 5), (0.5, 2)])
def test_partial_credit_alone_no_longer_escalates(mark, mx):
    assert ev._cascade_escalation_reason(_res(mark), _db(mx), SUBSTANTIVE) is None
    assert ev._cascade_should_escalate(_res(mark), _db(mx), SUBSTANTIVE) is False


def test_partial_credit_default_is_off():
    assert ev._escalate_partial_credit() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_old_behaviour_is_restorable(_clean, val):
    """One sheet, one marker -- the previous policy must stay reachable."""
    _clean.setenv("EVAL_CASCADE_ESCALATE_PARTIAL", val)
    assert ev._escalate_partial_credit() is True
    assert ev._cascade_escalation_reason(_res(1.5), _db(3), SUBSTANTIVE) == "partial_credit"


def test_legacy_calibration_keeps_the_old_escalation(_clean):
    """legacy must reproduce the old marking end to end, escalation included."""
    _clean.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    assert ev._escalate_partial_credit() is True
    assert ev._cascade_escalation_reason(_res(1.5), _db(3), SUBSTANTIVE) == "partial_credit"


@pytest.mark.parametrize("val", ["", "   "])
def test_blank_env_does_not_read_as_enabled(_clean, val):
    """A dashboard that 'clears' a variable leaves an empty string; it must mean unset, not on."""
    _clean.setenv("EVAL_CASCADE_ESCALATE_PARTIAL", val)
    assert ev._escalate_partial_credit() is False


# --- everything that MUST still escalate ----------------------------------------------------------

@pytest.mark.parametrize("res,expected", [
    (_res(1.5, conf="Low"), "low_confidence"),
    (_res(1.5, conf=""), "low_confidence"),
    (_res(1.5, conf="unknown"), "low_confidence"),
    (_res(0, **{"Off-Topic (Yes/No)": "Yes"}), "off_topic"),
    (_res(3, **{"Prompt Injection Detected": "Yes"}), "prompt_injection"),
    (_res(float("nan")), "non_finite_mark"),
    (_res(float("inf")), "non_finite_mark"),
    (_res("abc"), "non_numeric_mark"),
    (_res(0), "substantive_zero"),
])
def test_real_signals_still_escalate(res, expected):
    assert ev._cascade_escalation_reason(res, _db(3), SUBSTANTIVE) == expected


def test_a_non_dict_result_escalates():
    assert ev._cascade_escalation_reason(None, _db(3), SUBSTANTIVE) == "unparseable_result"


def test_an_unattempted_zero_is_still_trusted():
    """A blank answer is a genuine 0; re-grading it cannot change that."""
    assert ev._cascade_escalation_reason(_res(0), _db(3), {"answer": ""}) is None


def test_full_marks_are_still_trusted():
    assert ev._cascade_escalation_reason(_res(3), _db(3), SUBSTANTIVE) is None


# --- attribution: the number has to be the trigger's MARGINAL cost --------------------------------

def test_partial_credit_is_reported_only_when_it_is_the_sole_reason(_clean):
    """Ordering matters for the DATA, not the behaviour. An answer that is both low-confidence and
    partially credited would escalate regardless, so crediting it to partial_credit would overstate
    that trigger's cost and skew the next tuning decision. (The original ordering did exactly that.)"""
    _clean.setenv("EVAL_CASCADE_ESCALATE_PARTIAL", "1")
    both = _res(1.5, conf="Low")
    assert ev._cascade_escalation_reason(both, _db(3), SUBSTANTIVE) == "low_confidence"
    only = _res(1.5, conf="High")
    assert ev._cascade_escalation_reason(only, _db(3), SUBSTANTIVE) == "partial_credit"


def test_red_flags_outrank_partial_credit_in_attribution(_clean):
    _clean.setenv("EVAL_CASCADE_ESCALATE_PARTIAL", "1")
    r = _res(1.5, **{"Off-Topic (Yes/No)": "Yes"})
    assert ev._cascade_escalation_reason(r, _db(3), SUBSTANTIVE) == "off_topic"


# --- the reason reaches the result, so production runs are self-measuring -------------------------

def test_grade_cascade_records_why_it_escalated():
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert '_reason = _cascade_escalation_reason(fast, db_data, ocr_data)' in src
    assert 'deep.setdefault("Escalated Because", _reason)' in src
    # the fast mark it overrode is kept, so agreement can be measured off real runs
    assert 'deep.setdefault("Fast Marks"' in src


def test_the_boolean_predicate_still_exists():
    """Kept as the public form; other call sites and older tests use it."""
    assert ev._cascade_should_escalate(_res(3), _db(3), SUBSTANTIVE) is False
    assert ev._cascade_should_escalate(_res(0), _db(3), SUBSTANTIVE) is True


# --- the substantive-zero threshold is untouched by this change -----------------------------------

def test_substantive_zero_threshold_is_unchanged(_clean):
    _clean.delenv("EVAL_CASCADE_MIN_MARKS", raising=False)
    assert ev._cascade_escalation_reason(_res(0), _db(1), SUBSTANTIVE) == "substantive_zero"
    _clean.setenv("EVAL_CASCADE_MIN_MARKS", "3")
    assert ev._cascade_escalation_reason(_res(0), _db(1), SUBSTANTIVE) is None
