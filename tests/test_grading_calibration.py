"""The grader was measurably over-strict: 18.7% of ATTEMPTED answers in the archived corpus scored
0, including answers carrying a complete and correct method.

Five independent defects caused it. The two big ones were invisible because both failed SILENTLY:

  1. `Rubric: {rubric[:2000]}` head-truncated every rubric file (31-89 KB), discarding 94-98% of it.
     Everything that awards partial credit -- step-mark allocations, the carry-forward rule, the 25%
     syntax cap, the half-mark value-point table -- lives below that cut. What survived was the
     preamble, whose loudest lines are "must behave as a STRICT ... evaluator" and "reduce or
     eliminate credit" for a directionally-correct answer.

  2. `get_rubric` routed on the question TYPE, matching the words "code"/"math"/"equation"/
     "calculation". The answer-key parser emits only: MCQ, Short Answer, Long Answer, Numerical,
     Fill in the Blank, True/False. None contain any of those words -- so code_rubric.md and
     equation_rubric.md routed to ZERO questions across the whole corpus, and all 342 Mathematics
     and 148 Computer Science answers were graded with the SUBJECTIVE rubric.

Both are the kind of bug a passing test suite happily coexists with, so the tests below assert the
DELIVERY (what reaches the model) and not merely that a file exists.
"""
import json
import glob
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REFS = os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/references")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit):                                    # pragma: no cover
    ev = None

import grading_calibration as gc

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate unavailable")

RUBRIC_FILES = ["subjective_rubric.md", "equation_rubric.md", "code_rubric.md", "objective_rubric.md"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test states its own calibration; a stray .env value must not decide the outcome."""
    monkeypatch.delenv("EVAL_GRADING_CALIBRATION", raising=False)


# --- the switch ----------------------------------------------------------------------------------

def test_v2_is_the_default():
    assert gc.is_v2() is True


def test_legacy_is_selectable(monkeypatch):
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    assert gc.is_v2() is False


@pytest.mark.parametrize("val", ["", "  ", "V2", "new", "nonsense", "LEGACYY"])
def test_unknown_values_fall_back_to_v2_not_to_legacy(monkeypatch, val):
    """The default has to be the safe one: a typo must never silently resurrect the over-strict path."""
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", val)
    assert gc.is_v2() is True


@pytest.mark.parametrize("val", ["legacy", "LEGACY", " Legacy "])
def test_legacy_is_case_and_space_insensitive(monkeypatch, val):
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", val)
    assert gc.is_v2() is False


def test_the_diagram_grader_reads_the_same_switch():
    """Two hand-rolled copies of the env check are how the halves of a switch drift apart, leaving a
    run that is half-calibrated and whose marks nobody can explain."""
    src = open(os.path.join(ROOT, "skills/diagram_evaluator/scripts/evaluate_diagrams.py")).read()
    assert "from grading_calibration import" in src
    assert 'os.environ.get("EVAL_GRADING_CALIBRATION"' not in src


# --- rubric delivery -----------------------------------------------------------------------------

@pytest.mark.parametrize("fn", RUBRIC_FILES)
def test_every_rubric_carries_a_directives_block(fn):
    text = open(os.path.join(REFS, fn)).read()
    assert ev._DIRECTIVES_RE.search(text), f"{fn} has no GRADER-DIRECTIVES block"


@pytest.mark.parametrize("kind,type_str,subject", [
    ("subjective", "Long Answer", "Artificial Intelligence"),
    ("equation", "Short Answer", "Mathematics"),
    ("code", "Short Answer", "Computer Science"),
    ("objective", "MCQ", "Mathematics"),
])
def test_the_model_receives_the_directives_not_the_preamble(kind, type_str, subject):
    got = ev.get_rubric(type_str, subject)
    assert got.startswith("GRADING DIRECTIVES"), f"{kind}: got the document preamble instead"
    assert "Document Purpose" not in got
    assert "strict, fair, and consistent" not in got


def test_directives_are_never_truncated():
    """The exact defect: the maths directives are longer than the old 2000-char slice, so a slice
    left anywhere on this path would silently cut the carry-forward rule back off."""
    maths = ev.get_rubric("Short Answer", "Mathematics")
    assert len(maths) > ev.RUBRIC_HEAD_CHARS
    assert "CARRY-FORWARD" in maths
    assert maths.rstrip().endswith(".")                    # not cut mid-sentence


def test_the_prompt_does_not_re_truncate_the_rubric():
    """Truncation lives in get_rubric (one rule, one place). A slice at the prompt would reinstate
    the bug for any directives block that grows past the limit."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert "Rubric: {rubric}" in src
    # The literal slice as INTERPOLATION, not as prose: the comment above get_rubric quotes the old
    # expression on purpose, and a bare substring check would fire on the explanation of the bug.
    assert not re.search(r"\{rubric\[\s*:", src)


@pytest.mark.parametrize("fn,needle", [
    ("subjective_rubric.md", "HALF"),                      # a directionally correct point earns half
    ("equation_rubric.md", "CARRY-FORWARD"),               # penalize the error once, not twice
    ("code_rubric.md", "CAPPED AT 25%"),                   # the syntax-penalty cap
    ("objective_rubric.md", "BINARY"),                     # MCQ stays all-or-nothing
])
def test_each_directives_block_carries_its_operative_rule(fn, needle):
    body = ev._DIRECTIVES_RE.search(open(os.path.join(REFS, fn)).read()).group(1)
    assert needle in body


@pytest.mark.parametrize("fn", ["subjective_rubric.md", "equation_rubric.md", "code_rubric.md"])
def test_partial_credit_rubrics_reserve_zero_rather_than_default_to_it(fn):
    body = ev._DIRECTIVES_RE.search(open(os.path.join(REFS, fn)).read()).group(1)
    assert "RESERVE ZERO" in body


# The load-bearing rules, named one by one.
#
# Two earlier versions of this check were too coarse and mutations walked straight through both:
#   * `"HALF" in body` -- "HALF" also labels the tier bullet, so deleting the actual rule sentence
#     ("A directionally correct point earns HALF, never zero.") left the needle behind.
#   * "at least one sentence mentions partial work AND a non-zero outcome" -- a FAMILY smoke test.
#     Each rubric states that guarantee more than once, so deleting any single rule still left a
#     sibling to satisfy it.
#
# So: enumerate the rules that carry the calibration, one row each. The regexes are tolerant of
# rewording (these blocks will be tuned as calibration continues) but every row fails if its rule is
# deleted or inverted -- which is the property the mutation run actually tests for.
LOAD_BEARING_RULES = [
    # subjective
    ("subjective_rubric.md", "half-credit tier exists",
     r"HALF\s*[-—]\s*the concept is present"),
    ("subjective_rubric.md", "a directionally correct point earns half, not zero",
     r"directionally correct point earns HALF"),
    ("subjective_rubric.md", "any partly-satisfied value point scores above zero",
     r"partly satisfied.{0,60}greater than zero"),
    ("subjective_rubric.md", "never deduct for presentation",
     r"NEVER DEDUCT"),
    ("subjective_rubric.md", "an error is charged once, not cumulatively",
     r"NO CUMULATIVE PENALTY"),
    # equation
    ("equation_rubric.md", "every correct step earns its mark",
     r"AWARD EVERY STEP THE STUDENT GETS RIGHT"),
    ("equation_rubric.md", "partly correct working scores above zero",
     r"partly\s+correct MUST score above zero"),
    ("equation_rubric.md", "carry-forward: penalize the error once, not twice",
     r"penali[sz]e the error once,? not twice"),
    ("equation_rubric.md", "alternative methods are valid",
     r"ALTERNATIVE METHODS"),
    ("equation_rubric.md", "equivalent forms are equal",
     r"EQUIVALENT FORMS ARE EQUAL"),
    # code
    ("code_rubric.md", "logic outweighs syntax",
     r"LOGIC (?:ALWAYS )?(?:OUTWEIGHS|EARNS)"),
    ("code_rubric.md", "the syntax penalty is capped at 25%",
     r"CAPPED AT 25%"),
    ("code_rubric.md", "partially correct output earns partial marks",
     r"PARTIALLY CORRECT OUTPUT earns partial marks"),
    ("code_rubric.md", "any correct alternative implementation earns full marks",
     r"ALTERNATIVE IMPLEMENTATION"),
    # objective
    ("objective_rubric.md", "objective questions stay binary",
     r"BINARY SCORING"),
    ("objective_rubric.md", "identifier OR text may match",
     r"EITHER the selected option identifier"),
]


@pytest.mark.parametrize("fn,rule,pattern",
                         LOAD_BEARING_RULES,
                         ids=[f"{f.split('_')[0]}:{r}" for f, r, _ in LOAD_BEARING_RULES])
def test_load_bearing_rule_is_present_in_what_the_model_reads(fn, rule, pattern):
    body = ev._DIRECTIVES_RE.search(open(os.path.join(REFS, fn)).read()).group(1)
    assert re.search(pattern, body, re.I | re.S), f"{fn}: lost the rule '{rule}'"


@pytest.mark.parametrize("fn", ["subjective_rubric.md", "equation_rubric.md", "code_rubric.md"])
def test_each_partial_credit_rubric_states_the_non_zero_guarantee(fn):
    """Belt-and-braces over the table above: whatever the wording, each partial-credit rubric must
    somewhere promise that partly-correct work scores above zero."""
    body = ev._DIRECTIVES_RE.search(open(os.path.join(REFS, fn)).read()).group(1)
    assert re.search(r"never zero|greater than zero|(?:MUST )?score above (?:zero|0)", body, re.I), fn


def test_no_directives_block_tells_the_grader_to_be_strict():
    """The old head-truncation fed the model 'must behave as a STRICT ... evaluator' and 'reduce or
    eliminate credit' with none of the partial-credit machinery. Those must not reappear in what the
    model actually reads."""
    for fn in RUBRIC_FILES:
        body = ev._DIRECTIVES_RE.search(open(os.path.join(REFS, fn)).read()).group(1)
        assert "eliminate credit" not in body.lower(), fn
        assert not re.search(r"\bpenali[sz]e\b.*\baggressive", body, re.I), fn
        assert not re.search(r"\bstrict\b", body, re.I), fn


def test_legacy_restores_the_head_truncation(monkeypatch):
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    got = ev.get_rubric("Long Answer", "Artificial Intelligence")
    assert len(got) == ev.RUBRIC_HEAD_CHARS
    assert got.startswith("# AI Board Examination")


def test_legacy_strips_the_directives_block_before_truncating(monkeypatch):
    """Otherwise 'legacy' would smuggle the new rules into the very mode that exists to exclude them,
    and the revert flag would revert nothing."""
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    for t, s in [("Long Answer", "AI"), ("Short Answer", "Mathematics"), ("MCQ", "")]:
        got = ev.get_rubric(t, s)
        assert "GRADING DIRECTIVES" not in got
        assert "GRADER-DIRECTIVES" not in got


def test_a_rubric_without_a_directives_block_still_grades(tmp_path, monkeypatch):
    """Fallback, not a crash -- but it must not be silent, because silent head-truncation is the
    defect this function exists to end."""
    assert ev._DIRECTIVES_RE.search("no block here") is None


# --- routing: the defect that made two rubrics dead code -----------------------------------------

@pytest.mark.parametrize("type_str,subject,expected", [
    # The six type values the answer-key parser actually emits, against real subjects.
    ("Short Answer", "Mathematics", "equation"),
    ("Long Answer", "Mathematics", "equation"),
    ("Numerical", "Mathematics", "equation"),
    ("Numerical", "Science", "equation"),               # "Numerical" missed the old "calculation" test
    ("Short Answer", "Computer Science", "code"),
    ("Long Answer", "Computer Science", "code"),
    ("Short Answer", "Informatics Practices", "code"),
    ("Long Answer", "Science", "subjective"),
    ("Short Answer", "Artificial Intelligence", "subjective"),
    # Objective wins outright, whatever the subject: a Mathematics MCQ is still binary.
    ("MCQ", "Mathematics", "objective"),
    ("MCQ", "Computer Science", "objective"),
    ("Assertion-Reason", "Science", "objective"),
])
def test_rubric_routing(type_str, subject, expected):
    assert ev.rubric_kind(type_str, subject) == expected


def test_the_old_routing_sent_maths_and_code_to_the_subjective_rubric():
    """Pins the actual bug so it cannot come back: none of the emitted type values contain the words
    the old router looked for."""
    for t in ("MCQ", "Short Answer", "Long Answer", "Numerical", "Fill in the Blank", "True/False"):
        assert "code" not in t.lower() and "programming" not in t.lower()
        assert not any(w in t.lower() for w in ("equation", "math", "calculation"))


@pytest.mark.parametrize("marks,expected", [(1, "objective"), (1.0, "objective"),
                                            (2, "code"), (4, "code"), (None, "code")])
def test_binary_form_types_are_binary_only_at_one_mark(marks, expected):
    """A 2-mark 'fill in the blanks' has two blanks and must be able to score 1 of them; grading it
    binary would be a NEW over-strictness bug introduced by the fix for the old one."""
    assert ev.rubric_kind("Fill in the Blank", "Computer Science", marks) == expected


def test_legacy_keeps_the_old_dead_routing(monkeypatch):
    """The revert has to be real: legacy never read the code or equation rubric, so it still must
    not -- otherwise the flag cannot reproduce the marking it exists to reproduce."""
    monkeypatch.setenv("EVAL_GRADING_CALIBRATION", "legacy")
    maths = ev.get_rubric("Short Answer", "Mathematics")
    assert maths.startswith("# AI Board Examination")            # the SUBJECTIVE file, as before
    assert "CBSE-Level Rubric for Evaluating Mathematical" not in maths


def test_every_archived_question_now_reaches_a_specific_rubric():
    """Corpus-level guard: with real keys on disk, no Mathematics or Computer Science question may
    fall through to the generic subjective rubric."""
    seen = 0
    for p in glob.glob(os.path.join(ROOT, "output/*/db_answers.json")):
        try:
            db = json.load(open(p))
        except Exception:
            continue
        for _q, d in db.items():
            if not isinstance(d, dict):
                continue
            subj = str(d.get("subject", "")).lower()
            if "mathematics" not in subj and "computer" not in subj:
                continue
            seen += 1
            kind = ev.rubric_kind(d.get("type", ""), d.get("subject", ""), d.get("marks"))
            assert kind in ("equation", "code", "objective"), \
                f"{d.get('subject')} / {d.get('type')} fell through to {kind}"
    if not seen:
        pytest.skip("no archived answer keys present")


# --- the remaining three defects ------------------------------------------------------------------

def test_off_topic_requires_disagreement_with_the_answer_key(monkeypatch):
    """The question text reaches the grader through OCR and can be garbled; when it is, every correct
    answer on the page looks off-topic. Measured false zeros: Maths Q26 (the key IS the student's
    integration by parts), Q23 (PQ vs QP -- same vector, opposite convention), Q28."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert "Expected Answer is the MORE RELIABLE signal" in src
    assert "matches NEITHER the question NOR the" in src


def test_diagram_grader_awards_per_feature_partial_credit():
    src = open(os.path.join(ROOT, "skills/diagram_evaluator/scripts/evaluate_diagrams.py")).read()
    assert "HALF share" in src and "MUST score above 0" in src


def test_a_diagram_verdict_cannot_discard_a_correct_written_answer():
    """Measured: Maths_Class12 Q24 -- 742 characters deriving |d1|=6 and |d2|=2sqrt(2), verbatim the
    answer key, recorded 0/2 because the sketch lacked axis labels (and the diagram grader was in
    fact reading a crop from Q23)."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert '_text_mark > _diag_mark' in src
    assert 'res["Marks Source"] = "written answer"' in src


def test_the_winning_mark_keeps_its_own_justification():
    """A report must never explain a mark nobody was awarded -- 2/2 beside 'the diagram fails to meet
    the requirements'."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert 'if res.get("Marks Source") != "written answer":' in src


def test_pointwise_partial_credit_is_on_by_default_under_v2():
    """It was OFF in code and only enabled through .env, so any run whose environment lacked the
    variable -- a fresh deploy, a subprocess without the overlay -- graded with no partial-credit
    directive at all."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert '_pointwise_default = "1" if grading_calibration_v2() else ""' in src
    assert 'os.environ.get("EVAL_POINTWISE", _pointwise_default)' in src
