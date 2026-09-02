"""Skip-if-unchanged fast path for single-question re-grades (evaluate.py --regrade-one).

When a teacher clicks "re-evaluate" WITHOUT actually changing the OCR text (e.g. to clear a
false segmentation flag), the identical input would only reproduce the same marks -- so the
25-80s grader call is skipped and the answer is just confirmed. These guard the normalisation
and the unchanged predicate that decide that.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit):  # pragma: no cover
    ev = None

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate unavailable")


def test_norm_strips_trailing_ws_and_normalises_eol():
    assert ev._norm_regrade_text("a\r\nb  \n") == "a\nb"
    assert ev._norm_regrade_text("  x  ") == "x"
    assert ev._norm_regrade_text(None) == ""


def test_unchanged_true_for_whitespace_only_diff():
    # Trailing spaces / a stray newline / CRLF must NOT trigger a paid re-grade.
    assert ev._regrade_text_unchanged("def f():\n  return 1", "def f():\n  return 1  ")
    assert ev._regrade_text_unchanged("A. QP^-14", "A. QP^-14\n")
    assert ev._regrade_text_unchanged("line1\r\nline2", "line1\nline2")


def test_unchanged_false_for_a_real_edit():
    assert not ev._regrade_text_unchanged("QP^-14", "QP-14")        # a symbol changed
    assert not ev._regrade_text_unchanged("hello world", "hello")   # content changed
    assert not ev._regrade_text_unchanged("5", "6")                 # digit changed


def test_unchanged_false_when_edit_is_empty():
    # An empty edit is never "unchanged" -> always fall through to a real grade, never a silent skip.
    assert not ev._regrade_text_unchanged("", "")
    assert not ev._regrade_text_unchanged("   ", "anything")
