"""Marks GRANULARITY: every mark the pipeline reports is a multiple of 0.5.

Reported symptom: reports showed marks like 0.8, 0.3 and 0.7. Root cause: nothing rounded. The
grader's raw float was clamped to [0, Maximum Marks] and written straight out, and EVAL_POINTWISE
makes fractions reachable by construction -- it tells the model to split the scheme into value-points
"each worth a share of the marks", and a 4-mark question with 5 points is 0.8 a point.

These tests pin the rule at every layer that can write a mark: the quantizer itself, the LLM grading
path, the deterministic MCQ path, the diagram grader, the teacher-override path, and the answer-key
editor -- plus the upload check that REPORTS (never rewrites) a key whose maximum is illegal.

Offline: the LLM call is stubbed everywhere, so nothing here costs money or needs a network.
"""
import asyncio
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/diagram_evaluator/scripts"))

from marks_policy import MARK_STEP, is_valid_mark, quantize_mark  # noqa: E402

try:
    import evaluate as ev
except (ImportError, SystemExit) as e:                       # pragma: no cover
    ev = None
    _EV_ERR = str(e)

try:
    import upload_validation as uv
except Exception as e:                                       # pragma: no cover
    uv = None
    _UV_ERR = str(e)


# ---------------------------------------------------------------------------
# The quantizer itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0.8, 1),        # the three values from the bug report
    (0.3, 0.5),
    (0.7, 0.5),
    (0.0, 0),
    (0.1, 0),
    (0.4, 0.5),
    (0.6, 0.5),
    (0.9, 1),
    (1.2, 1),
    (2.25, 2.5),
    (2.9, 3),
    (3.33, 3.5),
])
def test_snaps_to_nearest_half(raw, expected):
    assert quantize_mark(raw) == expected


@pytest.mark.parametrize("legal", [0, 0.5, 1, 1.5, 2, 2.5, 3, 7.5, 20])
def test_already_legal_marks_are_untouched(legal):
    assert quantize_mark(legal) == legal


@pytest.mark.parametrize("tie,expected", [(0.25, 0.5), (0.75, 1), (1.25, 1.5), (2.75, 3)])
def test_exact_ties_round_up_in_the_students_favour(tie, expected):
    assert quantize_mark(tie) == expected


def test_is_not_bankers_rounding():
    """Python's round() is round-half-to-EVEN, which moves marks DOWN on exactly the values a
    per-point split produces most often. The quantizer must not inherit that."""
    assert round(0.25, 1) == 0.2 and quantize_mark(0.25) == 0.5
    assert round(2.5) == 2 and quantize_mark(2.5) == 2.5      # a legal value is never disturbed
    assert round(0.75, 1) == 0.8 and quantize_mark(0.75) == 1


def test_just_below_a_tie_still_rounds_down():
    assert quantize_mark(2.7499999) == 2.5


def test_clamped_to_the_maximum_after_snapping():
    assert quantize_mark(3.4, 3) == 3
    assert quantize_mark(0.8, 1) == 1
    assert quantize_mark(9, 2) == 2
    assert quantize_mark(0.3, 0) == 0                          # a 0-mark question stays 0


def test_never_negative():
    assert quantize_mark(-2) == 0
    assert quantize_mark(-0.4) == 0


@pytest.mark.parametrize("junk", [None, "abc", "", float("nan"), float("inf"), float("-inf"), {}, []])
def test_unusable_values_become_zero(junk):
    """A truncated or malformed grader reply must not put a mark on the sheet. Note "NaN" PARSES as a
    float -- the old min/max clamp let it through both guards and into the report."""
    assert quantize_mark(junk) == 0


def test_numeric_strings_are_accepted():
    assert quantize_mark("2.5") == 2.5
    assert quantize_mark("0.8") == 1


def test_whole_results_are_ints_so_mcqs_render_cleanly():
    """A 2-mark MCQ must still render "2 / 2", not "2.0 / 2"."""
    assert isinstance(quantize_mark(2), int)
    assert isinstance(quantize_mark(2.0), int)
    assert isinstance(quantize_mark(0.8), int)
    assert isinstance(quantize_mark(1.5), float)


def test_every_output_is_a_legal_mark_over_a_dense_sweep():
    for i in range(0, 2001):
        v = i / 100.0                                          # 0.00 .. 20.00 in 0.01 steps
        assert is_valid_mark(quantize_mark(v)), v


@pytest.mark.parametrize("v,ok", [
    (0, True), (0.5, True), (1, True), (1.5, True), (20, True), ("2.5", True),
    (0.8, False), (0.3, False), (-1, False), (float("nan"), False), ("x", False), (None, False),
])
def test_is_valid_mark(v, ok):
    assert is_valid_mark(v) is ok


# ---------------------------------------------------------------------------
# The LLM grading path (evaluate.evaluate_single)
# ---------------------------------------------------------------------------

def _stub_generate(monkeypatch, payload):
    """Make the grading call return `payload` as its JSON reply, with no network."""
    def fake(**kwargs):
        return json.dumps(payload), 10, 10
    monkeypatch.setattr(ev, "generate", fake)
    monkeypatch.setattr(ev, "strip_reasoning", lambda t: t)


def _grade(qid="Q1", awarded=0.8, mx=3, ocr=None, extra=None):
    body = {"Marks Awarded": awarded, "Maximum Marks": mx,
            "Student Wrote": "x", "Correct Answer": "y",
            "Justification": "j", "Feedback": "f",
            "Confidence (Low/Medium/High)": "High", "Needs Review (Yes/No)": "No"}
    body.update(extra or {})
    return body, {"answer": "x"} if ocr is None else ocr, {"marks": mx, "type": "Short Answer"}


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
@pytest.mark.parametrize("raw,expected", [(0.8, 1), (0.3, 0.5), (0.7, 0.5), (2.25, 2.5), (1.5, 1.5)])
def test_grader_reply_is_snapped(monkeypatch, raw, expected):
    body, ocr, db = _grade(awarded=raw)
    _stub_generate(monkeypatch, body)
    _i, _q, res = asyncio.run(ev.evaluate_single("Q1", ocr, db, 0))
    assert res["Marks Awarded"] == expected


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_grader_reply_above_the_maximum_is_still_capped(monkeypatch):
    body, ocr, db = _grade(awarded=4.4, mx=3)
    _stub_generate(monkeypatch, body)
    _i, _q, res = asyncio.run(ev.evaluate_single("Q1", ocr, db, 0))
    assert res["Marks Awarded"] == 3
    assert res["Needs Review (Yes/No)"] == "Yes"               # the existing over-award guard still fires


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_nan_mark_no_longer_reaches_the_report(monkeypatch):
    """"NaN" parses as a float, and float('nan') defeats BOTH sides of the old min/max clamp, so it
    used to land in the report as-is."""
    body, ocr, db = _grade(awarded="NaN")
    _stub_generate(monkeypatch, body)
    _i, _q, res = asyncio.run(ev.evaluate_single("Q1", ocr, db, 0))
    assert res["Marks Awarded"] == 0 and is_valid_mark(res["Marks Awarded"])


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_prompt_states_the_granularity_rule(monkeypatch):
    """The backstop must not be the only defence: the model is TOLD the rule, so its justification
    describes the mark it actually reports instead of a value that is silently snapped afterwards."""
    seen = {}

    def fake(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return json.dumps(_grade(awarded=2)[0]), 10, 10

    monkeypatch.setattr(ev, "generate", fake)
    monkeypatch.setattr(ev, "strip_reasoning", lambda t: t)
    asyncio.run(ev.evaluate_single("Q1", {"answer": "x"}, {"marks": 3, "type": "Short Answer"}, 0))
    p = seen["prompt"]
    # Assert the RULE text, not just the heading: the heading also appears in the JSON schema
    # annotation, so matching it alone would still pass with the whole block deleted.
    assert "marks are awarded in HALF-MARK" in p
    assert "round UP in the student's favour" in p
    assert "0, 0.5, 1, 1.5, 2, 2.5, 3" in p                    # the full ladder for a 3-mark question
    assert "0.8" in p                                          # named as an explicitly invalid value


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_prompt_falls_back_to_the_rule_for_a_large_maximum(monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return json.dumps(_grade(awarded=2)[0]), 10, 10

    monkeypatch.setattr(ev, "generate", fake)
    monkeypatch.setattr(ev, "strip_reasoning", lambda t: t)
    asyncio.run(ev.evaluate_single("Q1", {"answer": "x"}, {"marks": 80, "type": "Short Answer"}, 0))
    assert "multiple of 0.5" in seen["prompt"]                 # no 161-value ladder


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_pointwise_directive_no_longer_invites_arbitrary_fractions(monkeypatch):
    """EVAL_POINTWISE is the mechanism that made 0.8 reachable; it must now close with a rounding step."""
    seen = {}

    def fake(**kwargs):
        seen["prompt"] = kwargs.get("prompt", "")
        return json.dumps(_grade(awarded=2)[0]), 10, 10

    monkeypatch.setenv("EVAL_POINTWISE", "1")
    monkeypatch.setattr(ev, "generate", fake)
    monkeypatch.setattr(ev, "strip_reasoning", lambda t: t)
    asyncio.run(ev.evaluate_single("Q1", {"answer": "x"}, {"marks": 3, "type": "Short Answer"}, 0))
    assert "PARTIAL CREDIT" in seen["prompt"]
    assert "round that sum to the nearest legal mark" in seen["prompt"]


# ---------------------------------------------------------------------------
# The deterministic MCQ path (no LLM call at all)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_deterministic_mcq_awards_a_legal_whole_mark():
    ocr = {"Q1": {"answer": "(B)"}, "Q2": {"answer": "(C)"}}
    db = {"Q1": {"answer": "(B)", "marks": 2, "type": "MCQ"},
          "Q2": {"answer": "(B)", "marks": 2, "type": "MCQ"}}
    results = asyncio.run(ev.evaluate_all(ocr, db))
    by = {q: r for q, r in results}
    assert by["Q1"]["Marks Awarded"] == 2 and isinstance(by["Q1"]["Marks Awarded"], int)
    assert by["Q2"]["Marks Awarded"] == 0
    assert all(is_valid_mark(r["Marks Awarded"]) for r in by.values())


@pytest.mark.skipif(ev is None, reason="evaluate import unavailable in this env")
def test_deterministic_mcq_survives_an_illegal_key_maximum():
    """A key whose maximum was misparsed as 0.8 must not put 0.8 on the report."""
    ocr = {"Q1": {"answer": "(B)"}}
    db = {"Q1": {"answer": "(B)", "marks": 0.8, "type": "MCQ"}}
    results = asyncio.run(ev.evaluate_all(ocr, db))
    assert is_valid_mark(results[0][1]["Marks Awarded"])
    assert results[0][1]["Marks Awarded"] == 1


# ---------------------------------------------------------------------------
# The diagram grader
# ---------------------------------------------------------------------------

def test_diagram_grader_snaps_its_own_mark(monkeypatch):
    ed = pytest.importorskip("evaluate_diagrams")
    payload = {"marks_awarded": 0.8, "maximum_marks": 3, "student_diagram_features": "s",
               "correct_diagram_features": "c", "justification": "j", "feedback": "f",
               "confidence_score": 0.9}
    monkeypatch.setattr(ed, "generate", lambda **k: (json.dumps(payload), 5, 5))
    monkeypatch.setattr(ed, "strip_reasoning", lambda t: t)
    _qid, final, _i, _in, _out = ed.eval_single(
        "Q1", [], {"Q1": "features"}, {"Q1": {"answer": "expected", "marks": 3}}, 0)
    assert final["marks_awarded"] == 1 and is_valid_mark(final["marks_awarded"])


def test_diagram_grader_prompt_states_the_rule(monkeypatch):
    ed = pytest.importorskip("evaluate_diagrams")
    seen = []
    payload = {"marks_awarded": 2, "confidence_score": 0.9}

    def fake(**k):
        seen.append(k.get("prompt") or "".join(p.get("text", "") for p in (k.get("parts") or [])))
        return json.dumps(payload), 5, 5

    monkeypatch.setattr(ed, "generate", fake)
    monkeypatch.setattr(ed, "strip_reasoning", lambda t: t)
    ed.eval_single("Q1", [], {"Q1": "f"}, {"Q1": {"answer": "e", "marks": 3}}, 0)
    assert any("MARK GRANULARITY" in s for s in seen)


def test_diagram_merge_in_main_uses_the_quantizer():
    """The merge that copies a diagram mark onto the report lives inside main(), so it cannot be
    called in isolation. Guard the call site directly against a silent revert to raw min/max.

    Both candidates go through the quantizer now, not just the diagram one: best-of-two compares the
    diagram mark against the TEXT grader's mark, and comparing a snapped value against an unsnapped
    one would let 0.8-vs-1 decide the winner on a difference that cannot survive to the report."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert "_diag_mark = quantize_mark(d_awarded, q_max)" in src
    assert '_text_mark = quantize_mark(res.get("Marks Awarded", 0), q_max)' in src
    assert 'res["Marks Awarded"] = min(max(d_awarded, 0.0), q_max)' not in src


# ---------------------------------------------------------------------------
# Teacher overrides (server-side -- step="0.5" in the UI is only a browser hint)
# ---------------------------------------------------------------------------

def _ev_row(awarded=1, mx=3):
    return [["Q1", {"Marks Awarded": awarded, "Maximum Marks": mx, "Machine Marks": awarded}]]


@pytest.mark.parametrize("typed,expected", [(0.8, 1), (0.3, 0.5), (0.7, 0.5), (2.25, 2.5), (1.5, 1.5)])
def test_apply_corrections_snaps_a_typed_mark(typed, expected):
    rc = pytest.importorskip("review_corrections")
    updated, rows, total, _mx = rc.apply_corrections(
        _ev_row(), [{"question_id": "Q1", "decision": "reject", "corrected_marks": typed}])
    assert updated[0][1]["Marks Awarded"] == expected
    assert rows[0]["corrected_marks"] == expected
    assert total == expected


def test_apply_corrections_still_clamps_to_the_maximum():
    rc = pytest.importorskip("review_corrections")
    updated, _rows, _t, _m = rc.apply_corrections(
        _ev_row(mx=3), [{"question_id": "Q1", "decision": "reject", "corrected_marks": 99}])
    assert updated[0][1]["Marks Awarded"] == 3


@pytest.mark.parametrize("typed,expected", [(0.8, 1), (0.3, 0.5), (0.7, 0.5)])
def test_apply_decisions_snaps_a_typed_mark(typed, expected):
    rc = pytest.importorskip("review_corrections")
    updated, rows, _t, _m = rc.apply_decisions(
        _ev_row(), [{"question_id": "Q1", "decision": "reject", "corrected_marks": typed}])
    assert updated[0][1]["Marks Awarded"] == expected
    assert rows[0]["corrected_marks"] == expected


def test_accept_cannot_restore_an_illegal_legacy_mark():
    """Accepting the machine mark on a run graded BEFORE this rule existed must still land on a legal
    value -- the baseline is read straight off the archived file."""
    rc = pytest.importorskip("review_corrections")
    updated, _rows, _t, _m = rc.apply_decisions(
        _ev_row(awarded=0.8), [{"question_id": "Q1", "decision": "accept"}])
    assert updated[0][1]["Marks Awarded"] == 1


# ---------------------------------------------------------------------------
# Answer-key maximums: the editor SNAPS (the teacher is typing); a parsed key is FLAGGED
# ---------------------------------------------------------------------------

@pytest.mark.skipif(uv is None, reason="upload_validation import unavailable in this env")
def test_marks_editor_snaps_an_edited_maximum():
    key = {"questions": {"Q1": {"marks": 2}, "Q2": {"marks": 2}}}
    qs, _ch = uv.apply_marks_corrections(key, {}, {"marks": {"Q1": 0.8, "Q2": "0.3"}})
    assert qs["Q1"]["marks"] == 1 and qs["Q2"]["marks"] == 0.5


@pytest.mark.skipif(uv is None, reason="upload_validation import unavailable in this env")
def test_marks_editor_snaps_an_added_question():
    qs, _ch = uv.apply_marks_corrections({"questions": {}}, {}, {"added": [{"q": "Q9", "marks": 2.7}]})
    assert qs["Q9"]["marks"] == 2.5


@pytest.mark.skipif(uv is None, reason="upload_validation import unavailable in this env")
def test_parsed_key_with_an_illegal_maximum_is_flagged_not_rewritten():
    key = {"questions": {"Q1": {"marks": 2}, "Q2": {"marks": 0.8}, "Q3": {"marks": 1.5}}}
    issues = uv.validate_parsed_questions(key, "answer key")
    odd = [i for i in issues if i["code"] == "marks_not_half_step"]
    assert len(odd) == 1
    assert odd[0]["severity"] == uv.WARNING              # surfaced, but the run is still gradeable
    assert "Q2" in odd[0]["message"] and "Q3" not in odd[0]["message"]
    assert key["questions"]["Q2"]["marks"] == 0.8       # the teacher's key is NOT silently corrected


@pytest.mark.skipif(uv is None, reason="upload_validation import unavailable in this env")
def test_a_clean_key_raises_no_granularity_issue():
    key = {"questions": {"Q1": {"marks": 2}, "Q2": {"marks": 0.5}, "Q3": {"marks": 1.5}}}
    issues = uv.validate_parsed_questions(key, "answer key")
    assert not [i for i in issues if i["code"] == "marks_not_half_step"]


# ---------------------------------------------------------------------------
# The browser-side snap must agree with the server (they are two implementations of one rule)
# ---------------------------------------------------------------------------

def test_ui_snap_helper_matches_the_python_quantizer():
    """index.html reimplements the rule in JS so the teacher SEES the value that will be saved. Run the
    JS with node (skipped when node is absent) and compare it to the Python quantizer value-for-value."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = open(os.path.join(ROOT, "evaluation_app/templates/index.html")).read()
    start = html.index("function snapHalfInput(el)")
    end = html.index("\n    }", start) + len("\n    }")
    fn = html[start:end]
    cases = [0.8, 0.3, 0.7, 0.25, 0.75, 1.25, 2.9, 0, 1, 1.5, 2, 0.1, 3.33, 2.7499999]
    script = fn + """
    const out = [];
    for (const v of %s) {
        const el = { value: String(v), min: '0', max: '', dispatchEvent(){} };
        snapHalfInput(el);
        out.push(Number(el.value));
    }
    console.log(JSON.stringify(out));
    """ % json.dumps(cases)
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    check=True).stdout)
    assert got == [float(quantize_mark(c)) for c in cases]
