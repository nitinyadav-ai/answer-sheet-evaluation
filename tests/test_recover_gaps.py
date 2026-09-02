"""Offline unit tests for full_evaluator.recover_gaps_by_position (Phase 3). Zero cost. The host
string mirrors the real Science_Class_X Q35, which absorbed Q37's '37.'-headed content while Q36's
content sat under a '(b)(i)' sub-label with no '36.' header (so Q36 must stay BLANK)."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import full_evaluator as fe  # noqa: E402

# Host Q35: its own reproductive answer + Q36's refraction (under '(b)(i)', NO '36.' header) +
# Q37's content (under a literal '37.' header). Recovery should lift ONLY the 37. block.
HOST35 = (
    "(a)\n(i)\n(I) Production of ovum (egg) and female hormones.\n"
    "(ii)\nVasectomy - a surgical method...\n"
    "(b)\n(i) Refraction of a ray of light through a rectangular slab.\n"
    "[DIAGRAM: ray diagram through a glass slab]\n"
    "(ii)\nSnell's law: n2 = sin r / sin i\n"
    "\n\n37.\n(a)\n2NaCl + 2H2O  Electricity -> 2NaOH + Cl2 + H2\n"
    "(b)\nUses of NaOH: soaps, paper.\n"
    "Therefore, A = NaHCO3, B = Na2CO3."
)
DB = {f"Q{n}": {"type": "Short Answer", "marks": 4} for n in (35, 36, 37)}


def _state():
    return {
        "Q35": {"answer": HOST35, "is_bad_handwriting": False},
        "Q36": {"answer": "", "is_bad_handwriting": False},
        "Q37": {"answer": "", "is_bad_handwriting": False},
    }


def test_recovers_number_headed_fragment_into_gap():
    ocr, recovered, flagged, still = fe.recover_gaps_by_position(_state(), [36, 37], None, DB)
    assert recovered == [37]
    assert "2NaCl" in ocr["Q37"]["answer"]
    assert ocr["Q37"]["recovered_from"] == "Q35"
    # both the host and the recovered question are flagged for review
    assert flagged == [35, 37]


def test_host_keeps_its_own_content_and_is_never_emptied():
    ocr, _, _, _ = fe.recover_gaps_by_position(_state(), [36, 37], None, DB)
    assert "Production of ovum" in ocr["Q35"]["answer"]          # host retains its own answer
    assert "37." not in ocr["Q35"]["answer"]                     # the lifted block is gone
    assert ocr["Q35"]["answer"].strip()                          # never emptied


def test_gap_without_number_header_stays_blank():
    # Q36 has no '36.' header anywhere -> conservative recovery must NOT fabricate it.
    ocr, recovered, _, still = fe.recover_gaps_by_position(_state(), [36, 37], None, DB)
    assert 36 not in recovered
    assert 36 in still
    assert ocr["Q36"]["answer"] == ""


def test_noop_when_no_gaps():
    st = _state()
    st["Q36"]["answer"] = "x"
    st["Q37"]["answer"] = "y"
    ocr, recovered, flagged, still = fe.recover_gaps_by_position(st, [], None, DB)
    assert recovered == [] and flagged == [] and still == []


def test_does_not_steal_a_neighbours_unnumbered_content():
    # A gap whose number never appears as a header is left blank even though neighbours have content.
    st = {"Q35": {"answer": "(a) some unrelated answer with no 99 header"},
          "Q99": {"answer": ""}}
    ocr, recovered, _, _ = fe.recover_gaps_by_position(st, [99], None, {"Q99": {"type": "Short Answer"}})
    assert recovered == [] and ocr["Q99"]["answer"] == ""
    assert ocr["Q35"]["answer"] == "(a) some unrelated answer with no 99 header"


# --- Tier 1: cross-page-glue PREFIXED headers (real cases: UJJAWAL Q30, KRISHNA Q32) --------------
# The OCR glues a question that opens mid-page below a carried-over answer into the previous slot.
# When the student's label carries a Q/Ques prefix, it is recoverable even without a trailing
# separator ('Q30') or with a leading bracket + inline answer text ('(Ques 32) Aarush...').

def test_recovers_prefixed_header_without_separator():
    # UJJAWAL Q30: 'Q30' alone on its line (NO '.'/')' separator), glued at the tail of Q29.
    host = ("Q29. Let PA & PB be tangents from external point P.\nOA = OB (radii)\n"
            "Hence proved PA = PB.\nQ30\nr = 42 cm, θ = 30°\n"
            "Area of sector = 30/360 × 22/7 × 42 × 42 = 462 cm²\nmajor sector = 5082 cm²")
    st = {"Q29": {"answer": host, "is_bad_handwriting": False},
          "Q30": {"answer": "", "is_bad_handwriting": False}}
    db = {f"Q{n}": {"type": "Short Answer", "marks": 3} for n in (29, 30)}
    ocr, recovered, _, still = fe.recover_gaps_by_position(st, [30], None, db)
    assert recovered == [30]
    assert "sector" in ocr["Q30"]["answer"] and "462" in ocr["Q30"]["answer"]
    assert not ocr["Q30"]["answer"].lower().startswith("q30")            # header stripped
    assert "tangent" in ocr["Q29"]["answer"] and "sector" not in ocr["Q29"]["answer"].lower()


def test_recovers_bracketed_prefixed_header_with_inline_content():
    # KRISHNA Q32: '(Ques 32) Aarush : ...' -- leading bracket + prefix + answer text on the same line.
    host = ("Q31. Two dice thrown.\nP(sum=5)=4/36=1/9\nP(difference=3)=6/36=1/6\n"
            "(Ques 32) Aarush : 2 pencils + 3 choc = ₹11\nTanish : 1 pencil + 2 choc = ₹7\n2x+3y=11")
    st = {"Q31": {"answer": host, "is_bad_handwriting": False},
          "Q32": {"answer": "", "is_bad_handwriting": False}}
    db = {f"Q{n}": {"type": "Short Answer", "marks": 3} for n in (31, 32)}
    ocr, recovered, _, _ = fe.recover_gaps_by_position(st, [32], None, db)
    assert recovered == [32]
    assert "pencil" in ocr["Q32"]["answer"].lower()
    assert not ocr["Q32"]["answer"].startswith("(Ques")                  # bracketed header stripped
    assert "dice" in ocr["Q31"]["answer"].lower() and "pencil" not in ocr["Q31"]["answer"].lower()


def test_content_number_is_not_mistaken_for_a_prefixed_header():
    # '30' appears only as CONTENT (θ=30°, '= 30 × 22') with NO Q30/Ques 30 label -> the prefix
    # requirement keeps gap 30 BLANK and the host untouched (no false lift).
    host = ("Q29. tangent proof.\nradius r = 30 cm, angle θ = 30°\narea = 30 × 22/7 × 42\nHence proved.")
    st = {"Q29": {"answer": host, "is_bad_handwriting": False},
          "Q30": {"answer": "", "is_bad_handwriting": False}}
    db = {f"Q{n}": {"type": "Short Answer", "marks": 3} for n in (29, 30)}
    ocr, recovered, _, still = fe.recover_gaps_by_position(st, [30], None, db)
    assert recovered == [] and 30 in still and ocr["Q30"]["answer"] == ""
    assert ocr["Q29"]["answer"] == host                                 # host untouched


# ---- 'Q.<n>': the dot form, which the header matcher was blind to --------------------------------
# The old pattern was `(?:Q|Ques\.?|Question)` -- only 'Ques' could carry a dot, and the following
# `\s*` takes whitespace only -- so 'Q.17', the commonest handwritten label of all, matched NOTHING.
# Real case: Maths_Class12 page 3 shows 'Q.16 (C) √74' .. 'Q.20 (C)', all five welded into Q15's slot;
# Q17-Q20 were reported "No answer captured" and scored 0. Measured on the archived runs: 18 buried
# 'Q.<n>' headers invisible vs 43 matchable, and this fix recovers 10 answers.

@pytest.mark.parametrize("line", [
    "Q.17 (D) 43",          # the real Maths_Class12 form
    "Q. 17 (D) 43",         # space after the dot
    "(Q.17) 43",            # bracketed
    "q.17 answer text",     # lowercase
    "Q17 (D) 43",           # no dot -- must still work
    "Ques. 17 (D)",         # the one form that already worked
    "Question 17 (D)",
    "17) (D) 43",
])
def test_dot_and_plain_prefixes_both_open_a_slot(line):
    assert fe._qnum_header_idx([line], 17) == 0


@pytest.mark.parametrize("line", [
    "radius r = 17 cm, angle = 17°",   # bare content number
    "area = 17 × 22/7",
    "    x Q     17",                   # geometry variables, not a label
    "    P Q = 17 k",
    ".17 (D)",                          # a dot with no prefix must not qualify
])
def test_dot_form_does_not_loosen_the_content_number_guard(line):
    assert fe._qnum_header_idx([line], 17) is None


def test_recovers_the_real_maths_class12_glued_objective_run():
    """The exact Q15 blob from output/Maths_Class12/ocr_output/ocr_answers.json. Each of Q17-Q20 is
    headed by the student's own 'Q.<n>', so each must lift back into its own slot."""
    host = ("Q15 (C) 12√3\nQ.16 (C) √74\n\nQ.17 (D) 43\n\nQ.18 (B)\n\nQ.19 (A)\n\nQ.20 (C)")
    st = {"Q15": {"answer": host, "is_bad_handwriting": True}}
    st.update({f"Q{n}": {"answer": "", "is_bad_handwriting": False} for n in (17, 18, 19, 20)})
    db = {f"Q{n}": {"type": "MCQ", "marks": 1} for n in (15, 17, 18, 19, 20)}
    ocr, recovered, _flagged, _still = fe.recover_gaps_by_position(st, [17, 18, 19, 20], None, db)
    assert recovered == [17, 18, 19, 20]
    assert ocr["Q17"]["answer"] == "(D) 43"
    assert ocr["Q18"]["answer"] == "(B)"
    assert ocr["Q20"]["answer"] == "(C)"
    assert "12√3" in ocr["Q15"]["answer"]                               # host keeps its own answer
    # Recovered as a CHAIN (Q17 out of Q15, Q18 out of Q17, ...) because the run has no standalone
    # numeric header to bound each fragment. Every link must keep its provenance for the report badge.
    assert ocr["Q17"]["recovered_from"] == "Q15"
    assert all(ocr[f"Q{n}"].get("recovered_from") for n in (17, 18, 19, 20))


# ---- the widened label grammar --------------------------------------------------------------------
# Checked against a published Indian-exam label taxonomy. Its COVERAGE is right (every label shape in
# this project's corpus appears in it) but its reference regex is not adoptable: run over 3,603 real
# answer lines it produced 155 matches of which 56 were WRONG, because its roman-numeral branch eats
# letters inside ordinary words ('AC = 5cm' -> question 100). We take the coverage, not the pattern.

from label_negatives import ALL_NEGATIVES, PHYSICS_NEGATIVES, TAXONOMY_NEGATIVES  # noqa: E402


@pytest.mark.parametrize("line", [
    # separators -- all attested in real scripts
    "Q17 answer", "Q.17 answer", "Q 17 answer", "Q-17 answer", "Q:17 answer",
    "Q,17 answer", "Q#17 answer", "Q/17 answer", "Q_17 answer",
    # 'No' compounds
    "Q No 17 answer", "Q.No.17 answer", "Q. No. 17 answer", "Question No. 17 answer",
    "Question Number 17 answer",
    # answer-side markers -- the number after them is the QUESTION number, not an answer index
    "Ans 17) answer", "Ans. 17 answer", "Ans:17 answer", "Answer 17 answer",
    "Sol 17 answer", "Sol. 17 answer", "Soln 17 answer", "Solution 17 answer", "Att. 17 answer",
    "A17. answer", "A17) answer", "A. 17. answer",
    # forms that already worked -- regression cover
    "Ques. 17 (D)", "Question 17", "(Ques 17) text", "17.", "17)", "17. (a)", "17) (D) 43",
])
def test_widened_label_forms_open_a_slot(line):
    assert fe._qnum_header_idx([line], 17) == 0


@pytest.mark.parametrize("line,num", ALL_NEGATIVES)
def test_shared_negative_corpus_is_never_a_header(line, num):
    """Taxonomy section 13 + the false positives measured in this corpus. If any of these starts
    matching, the label grammar has been widened too far."""
    assert fe._qnum_header_idx([line], num) is None


@pytest.mark.parametrize("line,num", PHYSICS_NEGATIVES)
def test_charge_symbols_are_not_question_labels(line, num):
    """In physics 'Q1'/'Q2' are charges. A marker followed by '=' is a variable, not a label."""
    assert fe._qnum_header_idx([line], num) is None


def test_dotted_dates_do_not_open_a_question():
    """Regression: pat_inline's numeric sub-part branch used to read '12.' + '5' of '12.5.2024' as
    'question 12, sub-part 5'. Found by importing the taxonomy's negative set."""
    for d, n in [("12.5.2024", 12), ("12.5.24", 12), ("5.12.2024", 5), ("1.1.2025", 1)]:
        assert fe._qnum_header_idx([d], n) is None, d


def test_bare_A_needs_a_terminator_so_cofactors_are_safe():
    """'A1.' is a label; 'A11 = -2' is a matrix cofactor. Both appear in the archived Maths runs."""
    assert fe._qnum_header_idx(["A1. 8 (d) 1:8"], 1) == 0
    assert fe._qnum_header_idx(["A11 = -2, A12 = -(2)"], 11) is None
    assert fe._qnum_header_idx(["A21 = -(1) = -1"], 21) is None


def test_answer_side_label_is_stripped_from_the_lifted_fragment():
    """The strip pattern is built from the same slot constants as the matcher. If it lags behind, a
    fragment we just recognised keeps its own label as answer text."""
    host = ("Ans 20) my own answer to twenty\n"
            "Ans 21) alpha^3 beta + beta^3 alpha = alpha beta (alpha^2 + beta^2)")
    st = {"Q20": {"answer": host, "is_bad_handwriting": False},
          "Q21": {"answer": "", "is_bad_handwriting": False}}
    db = {f"Q{n}": {"type": "Short Answer", "marks": 3} for n in (20, 21)}
    ocr, recovered, _f, _s = fe.recover_gaps_by_position(st, [21], None, db)
    assert recovered == [21]
    assert ocr["Q21"]["answer"].startswith("alpha^3"), ocr["Q21"]["answer"]
    assert "Ans 21" not in ocr["Q21"]["answer"]
