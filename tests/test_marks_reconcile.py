"""Generalised answer-key integrity net: reconcile_marks_with_question_paper() cross-checks the parsed
answer key against the independently-parsed QUESTION PAPER and handles EVERY structural mismatch:
  (1) SHORTFALL  key<paper  -> auto-raise the single entry (the Class X Science case: Q37/38/39 came
      back worth 2 not 4, silently shrinking the max to 74) + review flag.
  (2) INFLATION  key>paper  -> FLAG (never silently lower).
  (3) DROPPED    in paper, not in key -> INJECT a placeholder at the paper's marks + review flag.
  (4) UNKNOWN    in key, not in paper -> FLAG.
Plus a grand-total assertion and a loud no-paper warning. Marks are auto-changed only in the two
confident directions (raise a shortfall, restore a dropped question); everything else is flagged.
Pure file I/O + arithmetic -- offline / no network."""
import json
import os
import sys

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


def _write(tmp_path, name, obj):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w") as f:
        json.dump(obj, f)
    return p


def _reconcile(tmp_path, db, qp, name="db.json", mode="raise"):
    """Run the reconciler on freshly-written temp files; return (summary_dict, reloaded_db)."""
    db_path = _write(tmp_path, name, db)
    qp_path = _write(tmp_path, "qp.json", qp)
    summary = fe.reconcile_marks_with_question_paper(db_path, qp_path, mode=mode)
    with open(db_path) as f:
        return summary, json.load(f)


# --- (1) shortfall: auto-raise ------------------------------------------------------------------
def test_raises_shortfall_and_flags(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"Q37": {"marks": 2, "answer": "(c) ...", "type": "Short Answer"}},
                         {"Q37": {"marks": 4, "question": "Case study..."}})
    assert res["adjusted"] == [("Q37", 2.0, 4.0)]
    assert res["flagged"] == [] and res["injected"] == []
    assert db["Q37"]["marks"] == 4
    assert db["Q37"]["marks_reconciled_from_qp"] is True
    assert "4" in db["Q37"]["reconcile_note"] and "2" in db["Q37"]["reconcile_note"]


def test_multiple_adjustments_and_total(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"Q37": {"marks": 2}, "Q38": {"marks": 2}, "Q39": {"marks": 2},
                          "Q1": {"marks": 1}},
                         {"Q37": {"marks": 4}, "Q38": {"marks": 4}, "Q39": {"marks": 4},
                          "Q1": {"marks": 1}})
    assert {q for q, _, _ in res["adjusted"]} == {"Q37", "Q38", "Q39"}
    assert sum(n - o for _, o, n in res["adjusted"]) == 6
    assert res["key_total"] == 13 and res["qp_total"] == 13     # 4+4+4+1, corrected
    assert db["Q1"]["marks"] == 1 and "marks_reconciled_from_qp" not in db["Q1"]


def test_prefixed_and_zeropadded_ids_map(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"AI10_Q07": {"marks": 1, "answer": "x"}},
                         {"Q7": {"marks": 3}})
    assert res["adjusted"] == [("AI10_Q07", 1.0, 3.0)]
    assert db["AI10_Q07"]["marks"] == 3


def test_ambiguous_siblings_flagged_not_raised(tmp_path):
    # Two key entries share a base and jointly fall short -> we can't know which to raise, so FLAG both
    # (marks unchanged) rather than guess.
    res, db = _reconcile(tmp_path,
                         {"Q10(a)": {"marks": 1}, "Q10(b)": {"marks": 1}},
                         {"Q10": {"marks": 5}})
    assert res["adjusted"] == []
    assert {q for q, _, _ in res["flagged"]} == {"Q10(a)", "Q10(b)"}
    assert db["Q10(a)"]["marks"] == 1 and "key_integrity_warning" in db["Q10(a)"]


# --- (2) inflation: flag, never lower -----------------------------------------------------------
def test_inflation_is_flagged_not_lowered(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"Q5": {"marks": 3, "answer": "x"}},
                         {"Q5": {"marks": 2}})
    assert res["adjusted"] == [] and res["injected"] == []
    assert res["flagged"] == [("Q5", 3.0, 2.0)]
    assert db["Q5"]["marks"] == 3                              # NOT lowered
    assert "marks_reconciled_from_qp" not in db["Q5"]
    assert "key_integrity_warning" in db["Q5"]


# --- (3) dropped question: inject ---------------------------------------------------------------
def test_missing_question_is_injected(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"Q1": {"marks": 1, "answer": "x"}},
                         {"Q1": {"marks": 1}, "Q40": {"marks": 4, "question": "Dropped Q text"}})
    assert res["injected"] == [("Q40", 0.0, 4.0)]
    assert res["adjusted"] == []
    assert db["Q40"]["marks"] == 4
    assert db["Q40"]["marks_reconciled_from_qp"] is True and db["Q40"]["key_parse_missing"] is True
    assert db["Q40"]["question"] == "Dropped Q text"          # placeholder carries the paper text
    assert res["key_total"] == 5 and res["qp_total"] == 5      # denominator now complete


# --- (4) unknown question: flag -----------------------------------------------------------------
def test_extra_key_question_is_flagged(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"Q1": {"marks": 1}, "Q99": {"marks": 2, "answer": "x"}},
                         {"Q1": {"marks": 1}})
    assert res["flagged"] == [("Q99", 2.0, None)]
    assert res["adjusted"] == [] and res["injected"] == []
    assert db["Q99"]["marks"] == 2 and "key_integrity_warning" in db["Q99"]


# --- clean cases / safety -----------------------------------------------------------------------
def test_equal_marks_no_change(tmp_path):
    res, db = _reconcile(tmp_path, {"Q10": {"marks": 5, "answer": "x"}}, {"Q10": {"marks": 5}})
    assert res["adjusted"] == [] and res["flagged"] == [] and res["injected"] == []
    assert "marks_reconciled_from_qp" not in db["Q10"] and "key_integrity_warning" not in db["Q10"]


def test_max_per_base_never_over_raises(tmp_path):
    # A paper that itself splits a question (Q8(a)=2, Q8(b)=2) -> max-per-base = 2; key already 2 -> no-op.
    res, db = _reconcile(tmp_path,
                         {"Q8": {"marks": 2, "answer": "x"}},
                         {"Q8(a)": {"marks": 2}, "Q8(b)": {"marks": 2}})
    assert res["adjusted"] == [] and res["flagged"] == []
    assert db["Q8"]["marks"] == 2


def test_no_question_paper_is_loud_noop(tmp_path):
    db_path = _write(tmp_path, "db.json", {"Q37": {"marks": 2}})
    r1 = fe.reconcile_marks_with_question_paper(db_path, None)
    r2 = fe.reconcile_marks_with_question_paper(db_path, os.path.join(str(tmp_path), "nope.json"))
    assert r1["checked"] is False and r2["checked"] is False
    with open(db_path) as f:
        assert json.load(f)["Q37"]["marks"] == 2             # untouched


def test_gating_off_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONCILE_KEY_MARKS_WITH_QP", "0")
    res, db = _reconcile(tmp_path, {"Q37": {"marks": 2}}, {"Q37": {"marks": 4}})
    assert res["checked"] is False and res["adjusted"] == []
    assert db["Q37"]["marks"] == 2


def test_instructions_and_nondict_entries_skipped(tmp_path):
    res, db = _reconcile(tmp_path,
                         {"_instructions_": ["answer any 1"], "Q2": "legacy-string",
                          "Q37": {"marks": 2}},
                         {"_instructions_": ["x"], "Q37": {"marks": 4}})
    assert res["adjusted"] == [("Q37", 2.0, 4.0)]
    assert db["_instructions_"] == ["answer any 1"]
    assert db["Q2"] == "legacy-string"


def test_integrity_summary_file_written(tmp_path):
    db_path = _write(tmp_path, "db.json", {"Q37": {"marks": 2}})
    qp_path = _write(tmp_path, "qp.json", {"Q37": {"marks": 4}})
    fe.reconcile_marks_with_question_paper(db_path, qp_path)
    integ = os.path.join(str(tmp_path), "key_integrity.json")
    assert os.path.exists(integ)
    with open(integ) as f:
        data = json.load(f)
    assert data["checked"] is True and data["adjusted"] == [["Q37", 2.0, 4.0]]
    assert data["qp_total"] == 4 and data["key_total"] == 4


# ---- marks-source MODES (the teacher's "which document is authoritative" choice) ----------------
def test_align_to_paper_lowers_inflation(tmp_path):
    # The 99.5-vs-80 case: a doubled 'answer any one' choice worth 10 -> lowered to the paper's 5.
    res, db = _reconcile(tmp_path, {"Q34": {"marks": 10, "answer": "x"}}, {"Q34": {"marks": 5}},
                         mode="align_to_paper")
    assert res["adjusted"] == [("Q34", 10.0, 5.0)] and res["flagged"] == []
    assert db["Q34"]["marks"] == 5 and db["Q34"]["marks_reconciled_from_qp"] is True


def test_align_to_paper_also_raises_shortfall(tmp_path):
    res, db = _reconcile(tmp_path, {"Q37": {"marks": 2}}, {"Q37": {"marks": 4}}, mode="align_to_paper")
    assert res["adjusted"] == [("Q37", 2.0, 4.0)] and db["Q37"]["marks"] == 4


def test_align_to_paper_matches_paper_total(tmp_path):
    key = {"Q34": {"marks": 10}, "Q35": {"marks": 10}, "Q1": {"marks": 1}}
    qp = {"Q34": {"marks": 5}, "Q35": {"marks": 5}, "Q1": {"marks": 1}}
    res, db = _reconcile(tmp_path, key, qp, mode="align_to_paper")
    assert res["key_total"] == res["qp_total"] == 11
    assert db["Q34"]["marks"] == 5 and db["Q35"]["marks"] == 5 and db["Q1"]["marks"] == 1


def test_raise_mode_leaves_inflation_flagged_not_lowered(tmp_path):
    # Default conservative mode: inflation is flagged, NEVER silently lowered.
    res, db = _reconcile(tmp_path, {"Q34": {"marks": 10}}, {"Q34": {"marks": 5}}, mode="raise")
    assert res["adjusted"] == [] and res["flagged"] == [("Q34", 10.0, 5.0)]
    assert db["Q34"]["marks"] == 10


def test_trust_key_makes_no_changes(tmp_path):
    res, db = _reconcile(tmp_path, {"Q34": {"marks": 10}, "Q40": {"marks": 4}},
                         {"Q34": {"marks": 5}, "Q99": {"marks": 3}}, mode="trust_key")
    assert res["adjusted"] == [] and res["flagged"] == [] and res["injected"] == []
    assert db["Q34"]["marks"] == 10 and "Q99" not in db     # nothing injected, nothing changed
