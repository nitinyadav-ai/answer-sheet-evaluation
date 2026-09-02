"""Permanent robustness of the hybrid-MCQ gate: it may return a confident WRONG (False) ONLY when BOTH
the key and the student cleanly parse to a single option letter. If the KEY can't be parsed (an
answer-key format the regex doesn't know), the gate must DEFER (None) to the format-agnostic LLM grader
instead of silently scoring a correct answer 0. Offline / no network."""
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


def test_confident_correct_when_letters_match():
    assert ev._mcq_confident_verdict("A) Salt and water", "(a) salt and water") is True


def test_confident_wrong_when_both_clean_and_differ():
    # Both sides are clean single letters that differ, and text differs -> safe to say WRONG.
    assert ev._mcq_confident_verdict("A) foo", "(b) bar") is False


def test_defers_when_key_unparseable():
    # Key carries no extractable option letter -> must DEFER to the LLM, NOT score 0.
    assert ev._mcq_confident_verdict("The correct option is A", "(b) foo") is None
    assert ev._mcq_confident_verdict("Salt and water are formed", "(b) foo") is None  # pure text, no marker


def test_dash_key_is_handled_confidently():
    # With the en-dash separator fix, "A – text" parses cleanly, so a matching letter is confident-correct.
    assert ev._mcq_confident_verdict("A – Salt and water formed", "(a) Salt and water are formed") is True


def test_ambiguous_student_still_defers():
    # 2 markers (e.g. struck-out + rewritten) -> defer regardless of key.
    assert ev._mcq_confident_verdict("A) foo", "(a) foo (b) bar") is None
