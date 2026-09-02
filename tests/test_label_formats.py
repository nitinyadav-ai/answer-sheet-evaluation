"""End-to-end proof of the E1 fix: a prefixed [START_Q] tag now OPENS its question through
assemble_answers instead of being welded into the previous one. Offline / no network."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

from qid_utils import canonical_qid  # noqa: E402
try:
    import run_ocr
except (ImportError, SystemExit) as e:
    run_ocr = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr import unavailable in this env")


def _r(i, text):
    return {"index": i, "image_path": f"/tmp/p{i}.png", "text": text,
            "tokens": {"prompt": 0, "completion": 0}, "error": None}


def _assemble(results, valid):
    ocr, _pm, _q2i, _ft, _coll = run_ocr.assemble_answers(results, "", valid)
    return {canonical_qid(k): v.get("answer", "") for k, v in ocr.items() if k != "_instructions_"}


@pytest.mark.parametrize("tag6", ["Q6", "A6", "Ans 6", "Ques 6", "Question 6", "Sol 6", "Q.6", "06"])
def test_prefixed_second_question_opens_not_welds(tag6):
    # page1 = Q5 (active), page2 = a PREFIXED Q6 tag. Before the fix, 'six' welded into Q5 and Q6
    # was BLANK; now Q6 opens.
    norm = _assemble([_r(0, "[START_Q: 5]\nfive answer\n[END_Q: 5]"),
                      _r(1, f"[START_Q: {tag6}]\nsix answer\n[END_Q: {tag6}]")], [5, 6, 7])
    assert "six answer" in norm.get("Q6", ""), f"{tag6!r} should open Q6"
    assert "six answer" not in norm.get("Q5", ""), f"{tag6!r} must not weld into Q5"


def test_all_label_styles_resolve_to_one_canonical_key():
    for tag in ["Q1", "Ques 1", "Ans1", "A1", "1", "Sol 1", "Question 1", "Q.1", "(1)", "01", "1)"]:
        norm = _assemble([_r(0, f"[START_Q: {tag}]\nthe answer\n[END_Q: {tag}]")], [1, 2, 3])
        assert "the answer" in norm.get("Q1", ""), f"{tag!r} should land in Q1, got keys {list(norm)}"


def test_clean_bare_digit_sequence_unchanged():
    # The common, already-working case stays exactly correct (regression guard).
    norm = _assemble([_r(0, "[START_Q: 1]\na1\n[END_Q: 1]"),
                      _r(1, "[START_Q: 2]\na2\n[END_Q: 2]"),
                      _r(2, "[START_Q: 3]\na3\n[END_Q: 3]")], [1, 2, 3])
    assert norm.get("Q1") == "a1" and norm.get("Q2") == "a2" and norm.get("Q3") == "a3"


def test_subpart_continuation_still_not_a_new_question():
    # A roman/letter sub-part at the top of a page must NOT open a question (unchanged behavior).
    norm = _assemble([_r(0, "[START_Q: 5]\nq5 part a\n[END_Q: 5]"),
                      _r(1, "[START_Q: ii]\nq5 part b continued\n[END_Q: ii]")], [5, 6, 7])
    assert "part b" in norm.get("Q5", "")        # welded back into the active Q5
    assert "Qii" not in norm and "Q2" not in norm  # 'ii' never became a question
