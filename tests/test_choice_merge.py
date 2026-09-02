"""Granularity-tolerant choice-member resolution (the 99.5-vs-80 root-cause fix). A key parser may
name a choice alternative 'Q34(a)' while emitting it SPLIT into 'Q34(a)(i)', 'Q34(a)(ii)', ... The old
exact-id match failed to resolve the member, skipped the choice, and the additive collapse SUMMED both
alternatives (10 not 5). effective_choice_marks + the rewritten merge_choice_groups resolve members by
id-PREFIX, sum each alternative's sub-parts, take the MAX across alternatives, and ADD any shared parts
(a case study's (a),(b) with an OR only in (c)). Offline / no network."""
import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
except (ImportError, SystemExit) as e:  # pragma: no cover
    fe = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None, reason="full_evaluator.py unavailable in this env")


# ---- effective_choice_marks (pure) ---------------------------------------------------------------
def test_whole_question_choice_split_alternatives():
    # (a) OR (b), each split deeper than the member id -> max(5, 5) = 5 (NOT 10).
    leaves = {"Q34(a)(i)": 1, "Q34(a)(ii)": 1, "Q34(a)(iii)(I)": 1, "Q34(a)(iii)(II)": 1,
              "Q34(a)(iii)(III)": 1, "Q34(b)(i)": 2, "Q34(b)(ii)(I)": 1, "Q34(b)(ii)(II)": 1,
              "Q34(b)(ii)(III)": 1}
    assert fe.effective_choice_marks(leaves, ["Q34(a)", "Q34(b)"]) == 5


def test_case_study_shared_parts_plus_or():
    # (a)+(b) shared (answered regardless) + (c)(i) OR (c)(ii) -> 1 + 1 + max(2, 2) = 4.
    leaves = {"Q37(a)": 1, "Q37(b)": 1, "Q37(c)(i)": 2, "Q37(c)(ii)": 2}
    assert fe.effective_choice_marks(leaves, ["Q37(c)(i)", "Q37(c)(ii)"]) == 4


def test_or_alternative_split_deeper():
    # (c)(ii) itself split into (I),(II) -> still resolves to that alternative -> 1 + 1 + max(2,2) = 4.
    leaves = {"Q38(a)": 1, "Q38(b)": 1, "Q38(c)(i)": 2, "Q38(c)(ii)(I)": 1, "Q38(c)(ii)(II)": 1}
    assert fe.effective_choice_marks(leaves, ["Q38(c)(i)", "Q38(c)(ii)"]) == 4


def test_unequal_alternatives_take_max():
    leaves = {"Q28(a)": 3, "Q28(b)(i)": 2, "Q28(b)(ii)": 0.5}
    assert fe.effective_choice_marks(leaves, ["Q28(a)", "Q28(b)"]) == 3


def test_unresolvable_choice_returns_none():
    # Only one member resolves -> can't form a choice -> None (caller falls back to additive sum).
    assert fe.effective_choice_marks({"Q9(a)": 2}, ["Q9(a)", "Q9(b)"]) is None


def test_is_under_prefix_matching():
    assert fe._is_under("Q34(a)(i)", "Q34(a)") is True
    assert fe._is_under("Q34(a)", "Q34(a)") is True
    assert fe._is_under("Q34(b)(i)", "Q34(a)") is False
    assert fe._is_under("Q35(a)(i)", "Q34(a)") is False    # different base


# ---- merge_choice_groups (integration on temp files) ---------------------------------------------
def _run_merges(tmp_path, manual_db, choice_groups):
    ocr_path = os.path.join(str(tmp_path), "ocr.json")
    db_path = os.path.join(str(tmp_path), "db.json")
    bases = sorted({fe._base_qnum(fe.normalize_qid(k)) for k in manual_db}, key=lambda x: int(x))
    json.dump({f"Q{b}": {"answer": "s"} for b in bases}, open(ocr_path, "w"))
    json.dump(dict(manual_db), open(db_path, "w"))
    fe.merge_choice_groups(ocr_path, db_path, manual_db, choice_groups)
    fe.merge_additive_subparts(ocr_path, db_path)
    db = json.load(open(db_path))
    total = sum(fe._safe_float(v.get("marks")) for k, v in db.items()
                if isinstance(v, dict) and k != "_instructions_")
    return db, total


def test_merge_collapses_split_whole_question_choice(tmp_path):
    manual_db = {
        "Q34(a)(i)": {"marks": 2, "answer": "a1"}, "Q34(a)(ii)": {"marks": 3, "answer": "a2"},
        "Q34(b)(i)": {"marks": 5, "answer": "b1"},
    }
    db, total = _run_merges(tmp_path, manual_db, [{"parent": "Q34", "members": ["Q34(a)", "Q34(b)"], "required": 1}])
    assert total == 5                                  # max(5, 5), not 10
    assert "Q34" in db and db["Q34"]["marks"] == 5 and db["Q34"].get("is_choice") is True


def test_merge_keeps_shared_parts_of_case_study(tmp_path):
    manual_db = {
        "Q37(a)": {"marks": 1, "answer": "a"}, "Q37(b)": {"marks": 1, "answer": "b"},
        "Q37(c)(i)": {"marks": 2, "answer": "ci"}, "Q37(c)(ii)": {"marks": 2, "answer": "cii"},
    }
    db, total = _run_merges(tmp_path, manual_db, [{"parent": "Q37", "members": ["Q37(c)(i)", "Q37(c)(ii)"], "required": 1}])
    assert total == 4                                  # (a)+(b)+max((c)(i),(c)(ii)) = 1+1+2
    assert db["Q37"]["marks"] == 4


def test_merge_skips_unresolvable_choice(tmp_path):
    # Members that resolve to <2 alternatives -> not merged; entries survive (reconciler catches later).
    manual_db = {"Q9(a)": {"marks": 2, "answer": "a"}}
    db, _ = _run_merges(tmp_path, manual_db, [{"parent": "Q9", "members": ["Q9(a)", "Q9(b)"], "required": 1}])
    assert db.get("Q9", {}).get("is_choice") is not True


def test_merge_collapses_parent_less_group(tmp_path):
    # Regression: the marks-editor used to write choice groups WITHOUT a `parent`. merge_choice_groups
    # then skipped them and the additive merge SUMMED both alternatives -> the '70 confirmed, graded
    # out of 92' bug. The grader must now derive the parent from the members and collapse the pair.
    manual_db = {"Q22(A)": {"marks": 2, "answer": "a"}, "Q22(B)": {"marks": 2, "answer": "b"}}
    db, total = _run_merges(tmp_path, manual_db, [{"members": ["Q22(A)", "Q22(B)"]}])   # no parent, no required
    assert total == 2                                  # max(2, 2), NOT 2 + 2
    assert db.get("Q22", {}).get("marks") == 2 and db["Q22"].get("is_choice") is True
