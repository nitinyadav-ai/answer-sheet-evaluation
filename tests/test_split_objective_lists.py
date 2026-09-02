"""Offline unit tests for full_evaluator.split_objective_answer_lists (Phase 2 of the Qwen
segmentation fix). Zero network/API cost. Strings are derived from the real Science_Class_X
fixture that reproduced the MCQ-list collapse."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import full_evaluator as fe  # noqa: E402

# The real welded block: Qwen wrapped all six objective answers (A1..A6) under one [START_Q],
# then a budding-diagram tail followed on the same page.
WELDED = (
    "A1. 8\n(d) 1:8\n"
    "A2. (b) Al₂O₃ and MgO\n"
    "A3. (c) Weak acid, neutral, strong base and weak base.\n"
    "A4. (a) Salt and water are formed.\n"
    "A5. (c) It has weak electrostatic forces of attraction between its oppositely charged ions.\n"
    "A6. (d) Sodium and iron\n"
    "[DIAGRAM: Three sequential hand-drawn sketches of budding in Hydra, connected by arrows]"
)
QIDS = list(range(1, 40))
DB = {f"Q{n}": {"type": "MCQ", "marks": 1} for n in range(1, 21)}
DB["Q22"] = {"type": "Short Answer", "marks": 2}


def _ocr(answer, key="Q22"):
    return {key: {"answer": answer, "is_bad_handwriting": False}}


def test_splits_welded_mcq_list_into_q1_q6():
    ocr, split_map = fe.split_objective_answer_lists(_ocr(WELDED), DB, QIDS, None)
    assert split_map == {"Q22": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]}
    # Q2..Q6 are clean single options -> graded deterministically.
    assert fe._parse_option_id(ocr["Q2"]["answer"]) == "b"
    assert fe._parse_option_id(ocr["Q4"]["answer"]) == "a"
    assert fe._parse_option_id(ocr["Q6"]["answer"]) == "d"
    assert "Sodium and iron" in ocr["Q6"]["answer"]
    # provenance recorded
    assert ocr["Q3"]["split_from"] == "Q22"


def test_diagram_tail_re_homed_to_source_key():
    ocr, _ = fe.split_objective_answer_lists(_ocr(WELDED), DB, QIDS, None)
    assert "DIAGRAM" in ocr["Q22"]["answer"]
    assert "A1." not in ocr["Q22"]["answer"]  # the MCQ list is gone from Q22


def test_does_not_split_prose_even_when_numbers_are_mcq_typed():
    # Long prose '1. 2. 3.' chunks (a Short-Answer style list) must NOT split: the option-like gate
    # rejects them even though Q1/Q2/Q3 are MCQ-typed.
    prose = (
        "1. The first major reason metals corrode is oxidation in the presence of moisture and air "
        "over a long period of exposure.\n"
        "2. The second reason involves electrochemical cells forming across the metal surface.\n"
        "3. Thirdly, dissolved impurities accelerate the process considerably in industry."
    )
    ocr, split_map = fe.split_objective_answer_lists(_ocr(prose, key="Q27"), DB, QIDS, None)
    assert split_map == {}
    assert "Q1" not in ocr and ocr["Q27"]["answer"] == prose


def test_requires_at_least_three_groups():
    two = "A1. (a) yes\nA2. (b) no"
    ocr, split_map = fe.split_objective_answer_lists(_ocr(two), DB, QIDS, None)
    assert split_map == {} and "Q1" not in ocr


def test_out_of_set_number_blocks_split():
    # 41 is not in the question set -> the run is not a clean in-set objective list -> no split.
    bad = "A1. (a) x\nA2. (b) y\nA41. (c) z"
    ocr, split_map = fe.split_objective_answer_lists(_ocr(bad), DB, QIDS, None)
    assert split_map == {}


def test_never_clobbers_an_existing_captured_answer():
    ocr = _ocr(WELDED)
    ocr["Q1"] = {"answer": "(a) already captured separately", "is_bad_handwriting": False}
    ocr2, split_map = fe.split_objective_answer_lists(ocr, DB, QIDS, None)
    assert split_map == {}
    assert ocr2["Q1"]["answer"] == "(a) already captured separately"


def test_noop_when_no_mcq_types():
    ocr, split_map = fe.split_objective_answer_lists(_ocr(WELDED), {"Q22": {"type": "Short Answer"}}, QIDS, None)
    assert split_map == {}


def test_page_mapping_mirrored_to_new_keys():
    pm = {"/x/page_1.png": [{"question_id": "Q22", "image": "page_1.png"}]}
    fe.split_objective_answer_lists(_ocr(WELDED), DB, QIDS, pm)
    qids = {it["question_id"] for it in pm["/x/page_1.png"]}
    assert {"Q1", "Q6"}.issubset(qids)  # split keys inherit Q22's page image


# ---- label forms the splitter used to be blind to -------------------------------------------------
# _OBJ_LABEL_RE demanded a separator AFTER the number, so only 'Q16. (C)' / '16. (C)' / 'A1. (d)'
# parsed. On the real Maths_Class12 objective run the student wrote 'Q.16 (C) √74' .. 'Q.20 (C)' and
# NOT ONE of the six lines matched -- every other gate passed, so the splitter simply never fired.

import pytest  # noqa: E402


@pytest.mark.parametrize("line,num,chunk", [
    ("Q.16 (C) sqrt74", 16, "(C) sqrt74"),      # the real Maths_Class12 form -- dot, no separator
    ("Q16 (C) sqrt74", 16, "(C) sqrt74"),       # bare prefix, no punctuation at all
    ("16 (C) sqrt74", 16, "(C) sqrt74"),        # no prefix either
    ("Q16. (C) sqrt74", 16, "(C) sqrt74"),      # the form that already worked
    ("16. (C) sqrt74", 16, "(C) sqrt74"),
    ("A1. (d) 1:8", 1, "(d) 1:8"),
    ("1) (A) 960", 1, "(A) 960"),
])
def test_objective_labels_parse(line, num, chunk):
    m = fe._OBJ_LABEL_RE.match(line)
    assert m and int(m.group(1)) == num and m.group(2).strip() == chunk


@pytest.mark.parametrize("line", [
    "16 students were surveyed about their travel",   # prose opening with a number
    "Let x = 16 be the required value",
    "the answer is 16",
])
def test_prose_is_not_mistaken_for_an_objective_label(line):
    m = fe._OBJ_LABEL_RE.match(line)
    # Either no match at all, or the widened lookahead must not have fired (no bracket follows).
    assert not m or not m.group(2).lstrip().startswith(("(", "["))


# ---- the never-clobber guard must not count the host's own list against it ------------------------
# On the real blob the guard aborted the split because Q15 (the HOST, whose own answer is the first line
# of the list it holds) and Q16 (lifted OUT of Q15 by an earlier layer) were both filled. Neither is
# independent evidence that splitting is wrong.

REAL_BLOB = "Q15 (C) 12sqrt3\nQ.16 (C) sqrt74\n\nQ.17 (D) 43\n\nQ.18 (B)\n\nQ.19 (A)\n\nQ.20 (C)"


def _mcq_db(nums):
    return {f"Q{n}": {"type": "MCQ", "marks": 1, "answer": "(A)"} for n in nums}


def test_splits_when_only_the_host_and_a_slot_recovered_from_it_are_filled():
    ocr = {"Q15": {"answer": REAL_BLOB, "is_bad_handwriting": True},
           "Q16": {"answer": "Q.16 (C) sqrt74", "recovered_from": "Q15"}}
    out, smap = fe.split_objective_answer_lists(ocr, _mcq_db(range(15, 21)), list(range(15, 21)), None)
    assert smap == {"Q15": [f"Q{n}" for n in range(15, 21)]}
    assert out["Q17"]["answer"] == "(D) 43"
    assert out["Q20"]["answer"] == "(C)"
    assert out["Q15"]["answer"] == "(C) 12sqrt3"          # the host keeps only its OWN chunk


def test_still_refuses_when_an_independent_question_already_has_an_answer():
    """An unrelated slot in the range with its own captured answer must still block the split."""
    ocr = {"Q15": {"answer": REAL_BLOB, "is_bad_handwriting": True},
           "Q18": {"answer": "answered separately on another page"}}
    out, smap = fe.split_objective_answer_lists(ocr, _mcq_db(range(15, 21)), list(range(15, 21)), None)
    assert smap == {}
    assert out["Q18"]["answer"] == "answered separately on another page"
    assert out["Q15"]["answer"] == REAL_BLOB              # untouched


# ---- the shared negative corpus, applied to the objective splitter too ---------------------------
# The splitter has its own label regex (_OBJ_LABEL_RE). It must be held to the same bar as the header
# matcher, or widening one while forgetting the other reopens the false-positive surface.

from label_negatives import ALL_NEGATIVES  # noqa: E402


@pytest.mark.parametrize("line,num", ALL_NEGATIVES)
def test_negatives_never_parse_as_an_objective_label(line, num):
    """A line may parse as `<number><separator><text>` and still not be a label -- what matters is
    that it cannot form an objective GROUP. Assert the stronger, behavioural property: a block made of
    these lines never splits."""
    db = {f"Q{n}": {"type": "MCQ", "marks": 1, "answer": "(A)"} for n in range(1, 120)}
    ocr = {"Q50": {"answer": "\n".join([line] * 3), "is_bad_handwriting": False}}
    _out, smap = fe.split_objective_answer_lists(ocr, db, list(range(1, 120)), None)
    assert smap == {}, f"{line!r} formed an objective list"


@pytest.mark.parametrize("line,num,chunk", [
    ("Q-16 (C) sqrt74", 16, "(C) sqrt74"),
    ("Q:16 (C) sqrt74", 16, "(C) sqrt74"),
    ("Q No 16 (C) sqrt74", 16, "(C) sqrt74"),
    ("Q.No.16 (C) sqrt74", 16, "(C) sqrt74"),
    ("Ans 16) (C) sqrt74", 16, "(C) sqrt74"),
    ("Sol 16. (C) sqrt74", 16, "(C) sqrt74"),
])
def test_widened_separators_and_markers_parse(line, num, chunk):
    m = fe._OBJ_LABEL_RE.match(line)
    assert m and int(m.group(1)) == num and m.group(2).strip() == chunk
