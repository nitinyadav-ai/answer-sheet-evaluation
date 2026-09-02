"""Non-degradation proof for scripts/qid_utils.py (Fix 1, audit E1/E6).

Captures the OLD extractors VERBATIM and asserts the new prefix-tolerant code is BYTE-IDENTICAL to
them on every non-prefixed tag (bare digits, sub-parts, roman-only, punctuation, empty), so the weld
decision and key alignment are unchanged on today's sheets. Only the previously-broken prefixed tags
('Q6'/'A6'/'Ans 6'/...) change (None -> correct number) -- a strict fix. Offline, zero cost."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import qid_utils as q  # noqa: E402


# --- the OLD logic, copied verbatim from run_ocr.py / full_evaluator.py before Fix 1 -------------
def OLD_leading_int(tag):
    m = re.match(r'\s*0*(\d+)', tag)
    return int(m.group(1)) if m else None


def OLD_has_suffix(tag):
    base = OLD_leading_int(tag)
    return base is not None and re.fullmatch(r'\s*0*\d+\s*', tag) is None


def OLD_normalize_qid(qid):
    if qid == "_instructions_":
        return qid
    s = str(qid).split("_", 1)[-1].strip()
    m = re.search(r'\d', s)
    if not m:
        return qid
    return "Q" + s[m.start():]


# Tags that do NOT begin with a label prefix -> new code MUST equal the old code exactly.
NON_PREFIXED = [
    "", "5", "05", "007", "0", "13", "99", "100", "999",
    "6.a", "6.ii", "6(a)", "6(iv)", "6 a)", "6-b", "12.iii", "5.b",
    "ii", "iv", "a", "I", "II", "(1)", "1)", "1.", "[1]", " 5", "5 ", "Section A",
]

# Tags that DID begin with a label prefix -> old dropped them (None); new resolves them.
PREFIXED_EXPECT = {
    "Q6": 6, "Q 6": 6, "Q.6": 6, "Q-6": 6, "A6": 6, "A 6": 6, "Ans 6": 6, "Ans6": 6,
    "Answer 6": 6, "Ques 6": 6, "Question 6": 6, "Sol 6": 6, "S6": 6, "Q06": 6, "Q6.a": 6,
}


def test_base_qnum_byte_identical_on_non_prefixed():
    for t in NON_PREFIXED:
        assert q.base_qnum(t) == OLD_leading_int(t), f"base_qnum mismatch on {t!r}"


def test_has_subpart_byte_identical_on_non_prefixed():
    for t in NON_PREFIXED:
        assert q.has_subpart(t) == OLD_has_suffix(t), f"has_subpart mismatch on {t!r}"


def test_prefixed_tags_now_resolve():
    for t, expected in PREFIXED_EXPECT.items():
        assert OLD_leading_int(t) is None, f"precondition: old should drop {t!r}"
        assert q.base_qnum(t) == expected, f"base_qnum({t!r}) should be {expected}"
    # a prefixed BARE number must NOT be seen as having a sub-part (the bug that welded it)
    assert q.has_subpart("Q6") is False
    assert q.has_subpart("Ans 6") is False
    assert q.has_subpart("Q6.a") is True  # genuine sub-part survives


def test_canonical_qid_matches_old_on_working_inputs():
    # inputs without leading zeros / stray wrapping punctuation -> identical to old normalize_qid
    working = ["A1", "Q1", "Ans 1", "Ans1", "Q.1", "13", "Q1(a)", "1(a)", "Ans 1(a)",
               "Q21(a)", "ii", "Section A", "_instructions_", "AI10_Q37", "Computer_Science_Q5"]
    for t in working:
        assert q.canonical_qid(t) == OLD_normalize_qid(t), f"canonical mismatch on {t!r}"


def test_canonical_qid_documented_fixes():
    # E3 leading zeros, E2 wrapping/trailing punctuation -> cleaned (old left them, causing BLANKs)
    assert q.canonical_qid("01") == "Q1"
    assert q.canonical_qid("1)") == "Q1"
    assert q.canonical_qid("(1)") == "Q1"
    assert q.canonical_qid("1.") == "Q1"
    assert q.canonical_qid("[1]") == "Q1"
    # all the label formats the user listed collapse to one key
    for t in ["Q1", "Ques 1", "Ans1", "A1", "1", "Sol 1", "Question 1"]:
        assert q.canonical_qid(t) == "Q1", f"{t!r} should canonicalize to Q1"
    # genuine sub-parts are preserved
    assert q.canonical_qid("Q1(a)") == "Q1(a)"
    assert q.canonical_qid("5.b") == "Q5.b"
