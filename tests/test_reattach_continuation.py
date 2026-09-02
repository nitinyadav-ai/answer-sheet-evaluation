"""Layer 2 -- reattach_leading_continuation. Pins the page-break drift fix (Q34's (a)(iii) equations
swallowed at the top of Q35) AND its non-degradation contract: only an answer that OPENS with a later
sub-part before its own '(a)' is split, the dangling prefix goes back to the PREVIOUS question, and a
well-formed answer is never touched. Offline / no network."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
except (ImportError, SystemExit) as e:
    fe = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None, reason="full_evaluator import unavailable in this env")


def _a(text):
    return {"answer": text, "is_bad_handwriting": False}


# ---- pure helpers -------------------------------------------------------------------------------

def test_starts_with_later_subpart():
    f = fe._starts_with_later_subpart
    assert f("(iii)\n(I) eq") is True
    assert f("(ii) foo") is True
    assert f("(b) bar") is True
    assert f("(2) baz") is True
    assert f("(a) opener") is False           # opener
    assert f("(i) opener") is False           # opener
    assert f("(I) sub-enumerator") is False   # 'I' lowercases to 'i' -> opener, never triggers
    assert f("Plain prose answer") is False   # no leading marker


def test_first_top_level_a_pos_ignores_sub_enumerators():
    g = fe._first_top_level_a_pos
    # The '(I)/(II)/(III)' equation enumerators must NOT be taken as the split point; only '(a)' is.
    text = "(iii)\n(I) eq1\n(II) eq2\n(a)\n(i) real start"
    pos = g(text)
    assert pos is not None and text[pos:].startswith("(a)")
    assert g("(ii) only roman\n(iii) more") is None   # no part-(a) -> no split point


# ---- function integration -----------------------------------------------------------------------

def test_page_break_swallowed_continuation_is_rehomed():
    ocr = {
        "Q34": _a("(a)\n(i) butene isomers\n(ii) naming"),
        "Q35": _a("(iii)\n(I) equation one\n(II) equation two\n(a)\n(i) reproduction answer"),
    }
    ocr, moved, flagged = fe.reattach_leading_continuation(ocr, [33, 34, 35, 36])
    assert moved == [35]
    assert "equation one" in ocr["Q34"]["answer"]        # equations moved back to Q34
    assert "equation one" not in ocr["Q35"]["answer"]    # removed from Q35
    assert ocr["Q35"]["answer"].startswith("(a)")        # Q35 keeps its own (a)(i)
    assert "reproduction answer" in ocr["Q35"]["answer"]
    assert flagged == [34, 35]                           # both flagged for review


def test_well_formed_answer_is_untouched():
    ocr = {"Q34": _a("(a) q34 answer"),
           "Q35": _a("(a) q35 own answer\n(b) more of q35")}
    before = dict(ocr["Q35"])
    ocr, moved, flagged = fe.reattach_leading_continuation(ocr, [34, 35])
    assert moved == [] and flagged == []
    assert ocr["Q35"] == before                          # byte-identical
    assert "q35" not in ocr["Q34"]["answer"]


def test_later_subpart_without_part_a_is_untouched():
    # Opens with a later sub-part but has NO '(a)' to split at -> leave it (conservative, no fabrication).
    ocr = {"Q34": _a("(a) q34"),
           "Q35": _a("(ii) only roman parts here\n(iii) and more")}
    ocr, moved, flagged = fe.reattach_leading_continuation(ocr, [34, 35])
    assert moved == []
    assert "only roman parts" in ocr["Q35"]["answer"]
    assert "only roman parts" not in ocr["Q34"]["answer"]


def test_no_move_when_previous_question_absent():
    # Q35 has a swallowed prefix but Q34 was never captured -> no owner to move it to -> leave as-is.
    ocr = {"Q35": _a("(iii)\n(I) eq\n(a)\n(i) own answer")}
    ocr, moved, flagged = fe.reattach_leading_continuation(ocr, [34, 35])
    assert moved == []
    assert ocr["Q35"]["answer"].startswith("(iii)")


def test_unanchored_is_a_no_op():
    ocr = {"Q34": _a("(a) x"), "Q35": _a("(iii)\n(I) eq\n(a)\n(i) y")}
    ocr2, moved, flagged = fe.reattach_leading_continuation(ocr, None)
    assert moved == [] and flagged == []
