"""compute_review_progress — the card-badge counters. Offline / pure.

reviewed  = questions a teacher acted on (accept / reject / regrade)
needs_review = machine 'Needs Review' flag, excluding bad-handwriting (matches the single report)
injection = machine 'Prompt Injection Detected' flag
Malformed [qid, dict] items are ignored.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import review_corrections as rc  # noqa: E402


def _ev(qid, **extra):
    d = {"Marks Awarded": 1, "Maximum Marks": 2}
    d.update(extra)
    return [qid, d]


def test_counts_reviewed_needs_review_injection():
    evals = [
        _ev("Q1"),                                                    # untouched
        _ev("Q2", **{"Teacher Reviewed": True}),                      # reviewed (accept/reject)
        _ev("Q3", **{"Teacher Re-evaluated": "Yes"}),                 # reviewed (regrade)
        _ev("Q4", **{"Needs Review (Yes/No)": "Yes"}),                # needs review
        _ev("Q5", **{"Needs Review (Yes/No)": "Yes",
                     "Bad Handwriting Flag": True}),                  # excluded (bad handwriting)
        _ev("Q6", **{"Prompt Injection Detected": "Yes"}),           # injection
    ]
    p = rc.compute_review_progress(evals)
    assert p == {"reviewed": 2, "total": 6, "needs_review": 1, "injection": 1}


def test_teacher_corrected_counts_as_reviewed():
    p = rc.compute_review_progress([_ev("Q1", **{"Teacher Corrected": True})])
    assert p["reviewed"] == 1 and p["total"] == 1


def test_guards_malformed_items():
    p = rc.compute_review_progress([_ev("Q1"), ["bad"], "junk", [1, 2, 3], None])
    assert p["total"] == 1


def test_empty():
    assert rc.compute_review_progress([]) == {"reviewed": 0, "total": 0,
                                              "needs_review": 0, "injection": 0}
