"""Orphan-page rescue: a scanned page whose [START_Q] the model never emitted must not be discarded.

Before this, assemble_answers dropped such a page ENTIRELY whenever no question was open yet -- the
`if active_qid:` at the top of the per-page loop had no else. Measured on real data:
maths_Ans_sheet__merged page 2 legibly holds six answered objective questions ('1) (A) 960' ..
'6) (D) a1/a2 = b1/b2 != c1/c2'), its page_mapping entry is [], and all six graded 0 with
"No answer captured".

The text is now parked under run_ocr.UNASSIGNED_QID. These tests pin the whole chain:
  * held, not lost -- and the page keeps its image association
  * a page WITH an active question still welds exactly as before (no regression)
  * blank pages create nothing; the kill switch restores the old behaviour byte-for-byte
  * the holder key stays DIGIT-FREE (a numbered one canonicalises to a real question -- see below)
  * end-to-end: the existing split_objective_answer_lists fans the holder out into Q1..Q6, removes it,
    and leaves the FOLLOWING question's own answer untouched (the regression that ruled out the
    alternative design of welding orphan text onto the next question)
All offline; no vision call, no cost.
"""
import copy
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

import full_evaluator as fe  # noqa: E402

try:
    import run_ocr
except (ImportError, SystemExit) as e:  # pragma: no cover
    run_ocr = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr unavailable in this env")

# The real discarded page, transcribed from output/maths_Ans_sheet__merged/preprocessed/..._page_2.png
ORPHAN_PAGE = ("Section - A\n"
               "1) (A) 960\n"
               "2) (D) Neither prime nor composite\n"
               "3) (B) 5\n"
               "4) (D) 3\n"
               "5) (B) 2/3\n"
               "6) (D) a1/a2 = b1/b2 =/= c1/c2")
NEXT_PAGE = "[START_Q: 7]\n(c) and (d)\n[END_Q: 7]"


def _page(path, text):
    return {"error": None, "image_path": path, "text": text}


def _assemble(results, valid):
    return run_ocr.assemble_answers(results, "AI10", valid_base_numbers=valid)


# ---- the fix ------------------------------------------------------------------------------------

def test_orphan_page_is_held_not_discarded():
    ocr, pm, _q2i, _ft, _cb = _assemble(
        [_page("p2.png", ORPHAN_PAGE), _page("p3.png", NEXT_PAGE)], [1, 2, 3, 4, 5, 6, 7])
    held = ocr[run_ocr.UNASSIGNED_QID]["answer"]
    assert "1) (A) 960" in held and "6) (D) a1/a2" in held      # nothing lost
    assert "Section - A" not in held                            # printed banner still stripped
    assert [i["question_id"] for i in pm["p2.png"]] == [run_ocr.UNASSIGNED_QID]


def test_following_question_is_untouched():
    ocr, _pm, _q2i, _ft, _cb = _assemble(
        [_page("p2.png", ORPHAN_PAGE), _page("p3.png", NEXT_PAGE)], [1, 2, 3, 4, 5, 6, 7])
    assert ocr["AI10_Q7"]["answer"] == "(c) and (d)"            # never contaminated by the orphan


def test_leading_text_with_no_open_question_is_held():
    """A page that opens with un-numbered text and THEN starts a question: the leading part used to be
    dropped when nothing was active. It must be held, and must not leak into the question it precedes."""
    ocr, _pm, _q2i, _ft, _cb = _assemble(
        [_page("p1.png", "stray opening line with no question number\n[START_Q: 5]\nQ5 body\n[END_Q: 5]")],
        [5])
    assert "stray opening line" in ocr[run_ocr.UNASSIGNED_QID]["answer"]
    assert "stray opening line" not in ocr["AI10_Q5"]["answer"]


# ---- no regression on the paths that already worked ---------------------------------------------

def test_page_with_an_active_question_still_welds():
    """The existing cross-page continuation weld must be unchanged -- this branch never had the bug."""
    ocr, _pm, _q2i, _ft, _cb = _assemble(
        [_page("p1.png", "[START_Q: 5]\nQ5 body\n[END_Q: 5]"), _page("p2.png", "tail of Q5")], [5, 6])
    assert "tail of Q5" in ocr["AI10_Q5"]["answer"]
    assert run_ocr.UNASSIGNED_QID not in ocr


def test_blank_orphan_page_creates_no_holder():
    """A cover page / unfilled OMR sheet must behave exactly as before: nothing recorded, no mapping."""
    ocr, pm, _q2i, _ft, _cb = _assemble(
        [_page("p1.png", "[BLANK PAGE]"), _page("p2.png", "[START_Q: 1]\nx\n[END_Q: 1]")], [1])
    assert run_ocr.UNASSIGNED_QID not in ocr
    assert pm["p1.png"] == []


def test_kill_switch_restores_the_old_discard(monkeypatch):
    monkeypatch.setattr(run_ocr, "_KEEP_ORPHAN_PAGES", False)
    ocr, pm, _q2i, _ft, _cb = _assemble(
        [_page("p2.png", ORPHAN_PAGE), _page("p3.png", NEXT_PAGE)], [1, 2, 3, 4, 5, 6, 7])
    assert run_ocr.UNASSIGNED_QID not in ocr
    assert pm["p2.png"] == []


# ---- the holder must never look like a question -------------------------------------------------

def test_holder_key_is_digit_free():
    """normalize_qid('_unassigned_p2') is 'Q2' -- a numbered holder would masquerade as question 2 and
    silently overwrite it. The key must carry no digit at all."""
    assert not any(ch.isdigit() for ch in run_ocr.UNASSIGNED_QID)
    assert fe._base_qnum(run_ocr.UNASSIGNED_QID) is None
    assert fe.normalize_qid(run_ocr.UNASSIGNED_QID) == run_ocr.UNASSIGNED_QID
    assert fe.normalize_qid("_unassigned_p2") == "Q2"          # the trap this guards against


def test_holder_is_not_counted_as_a_captured_answer():
    st = {"Q7": {"answer": "x"}, run_ocr.UNASSIGNED_QID: {"answer": "1) (A) 960"}}
    assert fe._recompute_gaps(dict(st), [1, 2, 7]) == [1, 2]   # holder fills no question's gap


# ---- end-to-end: the existing splitter fans the holder out --------------------------------------

def _mcq_db(n):
    return {f"Q{i}": {"type": "MCQ", "marks": 1, "answer": "(A)"} for i in range(1, n + 1)}


def test_holder_is_split_into_its_questions_and_then_removed():
    db = dict(_mcq_db(6), Q7={"type": "MCQ", "marks": 1, "answer": "(c) and (d)"})
    st = {run_ocr.UNASSIGNED_QID: {"answer": ORPHAN_PAGE.split("\n", 1)[1], "is_bad_handwriting": False},
          "Q7": {"answer": "(c) and (d)", "is_bad_handwriting": False}}
    out, smap = fe.split_objective_answer_lists(copy.deepcopy(st), db, list(range(1, 8)), None)
    assert smap == {run_ocr.UNASSIGNED_QID: ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]}
    assert out["Q1"]["answer"] == "(A) 960"
    assert out["Q6"]["answer"] == "(D) a1/a2 = b1/b2 =/= c1/c2"
    assert run_ocr.UNASSIGNED_QID not in out                    # holder consumed
    assert out["Q7"]["answer"] == "(c) and (d)"                 # untouched


def test_split_targets_inherit_the_orphan_page_image():
    """_mirror_page_mapping used to bail when the source key had no base number, so questions split out
    of the holder would have had NO page image -- answer crops and diagram detection would find nothing."""
    pm = {"/x/page_2.png": [{"question_id": run_ocr.UNASSIGNED_QID, "image": "page_2.png"}]}
    fe._mirror_page_mapping(pm, run_ocr.UNASSIGNED_QID, ["Q1", "Q2"])
    got = [i["question_id"] for i in pm["/x/page_2.png"]]
    assert "Q1" in got and "Q2" in got
    assert all(i["image"] == "page_2.png" for i in pm["/x/page_2.png"])


def test_numeric_source_mirroring_is_unchanged():
    """The base-number path (the only one that existed) must behave exactly as before."""
    pm = {"/x/p.png": [{"question_id": "Q22.a", "image": "p.png"}]}
    fe._mirror_page_mapping(pm, "Q22", ["Q23"])
    assert [i["question_id"] for i in pm["/x/p.png"]] == ["Q22.a", "Q23"]


# ---- the holder must never be graded as a question ----------------------------------------------

def test_evaluate_skips_underscore_prefixed_holders():
    sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))
    ev = pytest.importorskip("evaluate")
    db = {"Q1": {"type": "MCQ", "marks": 1, "answer": "(A)"}}
    ocr = {"Q1": {"answer": "(A)"}, "_instructions_": ["x"],
           run_ocr.UNASSIGNED_QID: {"answer": "text we could not place"}}
    import asyncio
    graded = asyncio.run(ev.evaluate_all(ocr, db))
    assert [q for q, _ in graded] == ["Q1"]                     # no phantom rows
