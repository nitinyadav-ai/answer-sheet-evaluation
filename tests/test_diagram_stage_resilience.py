"""A stalled diagram crop must not take the whole stage -- or the diagram grades -- down with it.

Measured on the Science_Class_X run: `extract_features.py` ran for 420.02s against a watchdog set to
exactly 420s, so it was KILLED. NINE of its ten crops had already succeeded; all nine were discarded
with the one straggler, `student_features.json` was never written, `diagram_evals.json` never
followed, and the run produced a report with no diagram grades and no indication that anything was
missing.

Two independent defects, pinned separately below:

  1. `generate()`'s parameter-rejection retry fired on TIMEOUTS too, multiplying the caller's budget
     by another full round: 90s x (1 sdk retry + 1) x (1 manual retry + 1) = 360s for a caller that
     asked for 90s. Re-issuing an identical request is the right answer to a 400 and the wrong answer
     to a timeout.
  2. The stage was ALL-OR-NOTHING: no future-level budget, so a wedged connection that never returns
     left its future pending forever and every completed crop died with it.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import llm_client as lc
except Exception:                                                    # pragma: no cover
    lc = None
try:
    import full_evaluator as fe
except Exception:                                                    # pragma: no cover
    fe = None

lc_only = pytest.mark.skipif(lc is None, reason="llm_client unavailable")
fe_only = pytest.mark.skipif(fe is None, reason="full_evaluator unavailable")

EXTRACT = os.path.join(ROOT, "skills/feature-extracter/scripts/extract_features.py")


# --- defect 1: a timeout must not be re-issued ----------------------------------------------------

class _Timeout(Exception):
    pass


@lc_only
@pytest.mark.parametrize("exc", [
    type("APITimeoutError", (Exception,), {})("request timed out"),
    type("Timeout", (Exception,), {})("boom"),
    type("APIConnectionError", (Exception,), {})("connection error"),
    type("SomeError", (Exception,), {})("Request timed out."),
])
def test_timeout_like_errors_are_recognised(exc):
    assert lc._is_timeout_error(exc) is True


@lc_only
@pytest.mark.parametrize("exc", [
    type("BadRequestError", (Exception,), {})("response_format is not supported"),
    type("APIStatusError", (Exception,), {})("400 unknown parameter: reasoning"),
    ValueError("nope"),
])
def test_parameter_rejections_are_not_treated_as_timeouts(exc):
    """These are exactly what the retry EXISTS for -- it must still fire for them."""
    assert lc._is_timeout_error(exc) is False


@lc_only
def test_is_timeout_error_never_raises():
    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("unprintable")
    with pytest.raises(RuntimeError):
        str(Nasty())                       # confirms the fixture really is hostile
    try:
        lc._is_timeout_error(Nasty())
    except RuntimeError:
        pytest.fail("_is_timeout_error must not propagate an exception from str(exc)")


@lc_only
def test_generate_does_not_reissue_on_timeout():
    """Guards the budget arithmetic: with the re-issue, a 90s caller could wait 360s."""
    src = open(os.path.join(ROOT, "scripts", "llm_client.py")).read()
    assert "if _is_timeout_error(_first_err):" in src
    assert "raise" in src.split("if _is_timeout_error(_first_err):")[1][:60]


# --- defect 2: the stage keeps what finished -------------------------------------------------------

def test_stage_has_a_future_level_budget():
    """The per-call timeout is not enough: a wedged connection can leave a future pending forever,
    which is exactly what happened. The stage must bound its own wait."""
    src = open(EXTRACT).read()
    assert "concurrent.futures.wait(" in src
    assert "timeout=_budget" in src
    assert "DIAGRAM_FEATURES_STAGE_TIMEOUT" in src


def test_completed_crops_are_kept_when_others_stall():
    src = open(EXTRACT).read()
    assert "for future in done:" in src
    assert "if pending:" in src
    # the abandoned questions are NAMED, so missing grades are explainable afterwards
    assert "were abandoned" in src and "stalled" in src


def test_stage_cannot_hang_at_interpreter_exit():
    """ThreadPoolExecutor threads are NON-DAEMON and Python's atexit JOINS them, so a wedged worker
    would hang the process at exit even after shutdown(wait=False) -- the watchdog would still kill
    the stage and the results would still be lost."""
    src = open(EXTRACT).read()
    assert "shutdown(wait=False, cancel_futures=True)" in src
    assert "os._exit(0)" in src
    assert "sys.stdout.flush()" in src


def test_a_worker_that_raises_does_not_abort_the_stage():
    src = open(EXTRACT).read()
    assert "except Exception as e:" in src and "Feature extraction worker failed" in src


# --- the gap is recorded so a missing diagram grade is visible -------------------------------------

@fe_only
def test_unassessed_diagrams_are_recorded(tmp_path):
    crops = tmp_path / "crops.json"
    crops.write_text(json.dumps([
        {"question_id": "Q22", "image": "/x/a.png"},
        {"question_id": "Q35", "image": "/x/b.png"},
        {"question_id": "Q36", "image": "/x/c.png"},
    ]))
    feats = json.dumps({"Q22": "a circuit with two cells", "Q36": "a ray diagram"})
    fe._flag_unassessed_diagrams(str(tmp_path), str(crops), features_json=feats)
    out = json.loads((tmp_path / fe.DIAGRAM_UNASSESSED_FILE).read_text())
    assert set(out) == {"Q35"}
    assert "written answer" in out["Q35"]


@fe_only
def test_error_placeholders_count_as_unassessed(tmp_path):
    """extract_single returns '[SYSTEM ERROR: ...]' on failure -- that is not a diagram reading."""
    crops = tmp_path / "crops.json"
    crops.write_text(json.dumps([{"question_id": "Q7", "image": "/x/a.png"}]))
    fe._flag_unassessed_diagrams(str(tmp_path), str(crops),
                                 features_json=json.dumps({"Q7": "[SYSTEM ERROR: Failed]"}))
    out = json.loads((tmp_path / fe.DIAGRAM_UNASSESSED_FILE).read_text())
    assert set(out) == {"Q7"}


@fe_only
def test_nothing_written_when_every_diagram_was_assessed(tmp_path):
    crops = tmp_path / "crops.json"
    crops.write_text(json.dumps([{"question_id": "Q1", "image": "/x/a.png"}]))
    fe._flag_unassessed_diagrams(str(tmp_path), str(crops), features_json=json.dumps({"Q1": "ok"}))
    assert not (tmp_path / fe.DIAGRAM_UNASSESSED_FILE).exists()


@fe_only
def test_total_failure_flags_every_diagram_question(tmp_path):
    crops = tmp_path / "crops.json"
    crops.write_text(json.dumps([{"question_id": "Q1", "image": "/a"},
                                 {"question_id": "Q2", "image": "/b"}]))
    fe._flag_unassessed_diagrams(str(tmp_path), str(crops), features_json=None)
    out = json.loads((tmp_path / fe.DIAGRAM_UNASSESSED_FILE).read_text())
    assert set(out) == {"Q1", "Q2"}


@fe_only
def test_flagging_never_raises_on_bad_input(tmp_path):
    """Diagnostics must never be able to fail a grading run."""
    fe._flag_unassessed_diagrams(str(tmp_path), "/does/not/exist.json", features_json="{oops")
    fe._flag_unassessed_diagrams(str(tmp_path), str(tmp_path), features_json=None)


def test_report_side_reads_the_sidecar_and_forces_review(tmp_path):
    """Behavioural, not textual: an un-assessed diagram must BOTH carry the note and be pushed into
    the review queue. A mutation that kept the note but dropped the review flag slipped past the
    earlier source-text version of this test -- and that is the failure that matters, because a note
    nobody is sent to look at is the same as no note."""
    ev = pytest.importorskip("evaluate", reason="evaluate unavailable")
    run = tmp_path / "run"
    (run / "ocr_output").mkdir(parents=True)
    ocr_path = run / "ocr_output" / "ocr_answers.json"
    ocr_path.write_text("{}")
    (run / "diagram_unassessed.json").write_text(json.dumps({"Q35": "diagram could not be read"}))

    results = [("Q35", {"Marks Awarded": 2, "Needs Review (Yes/No)": "No"}),
               ("Q22", {"Marks Awarded": 2, "Needs Review (Yes/No)": "No"})]
    out = dict(ev._apply_unassessed_diagram_flags(results, str(ocr_path)))
    assert out["Q35"]["Diagram Warning"]
    assert out["Q35"]["Needs Review (Yes/No)"] == "Yes", "flagged but never surfaced for review"
    assert "Diagram Warning" not in out["Q22"]
    assert out["Q22"]["Needs Review (Yes/No)"] == "No"


def test_report_side_is_wired_into_the_pipeline():
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert "results_ordered = _apply_unassessed_diagram_flags(results_ordered, ocr_path)" in src


# --- defect 3: a RUNAWAY generation, which no timeout can bound ------------------------------------
#
# What the 210s stage budget above was actually firing on. Replaying the stalled crops:
#
#     Q31 (stalled)   625.3s   out=16384 tokens     <- the provider's default cap
#     Q22 (healthy)    19.6s   out=  615 tokens
#     Q36 (healthy)    29.8s   out=  816 tokens
#     Q37 (healthy)    23.4s   out=  591 tokens
#
# The generation RATE was normal (26 vs 31 tok/s) -- the model simply would not stop. `timeout` is an
# httpx READ timeout (longest allowed SILENCE between bytes), so a steady stream never trips it: a
# probe with timeout=200 ran 625s to completion. Only a token cap ends a runaway, and this was the one
# vision call in the pipeline without one.

PROVIDER_DEFAULT_CAP = 16384      # what the runaway actually generated, unbounded
LARGEST_HEALTHY_READ = 816        # largest observed good response, in tokens


def _load_extract_features():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ef_under_test", EXTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_feature_extraction_sends_a_token_cap():
    """Behavioural, not textual: call extract_single with generate() stubbed and read the kwargs."""
    mod = _load_extract_features()
    seen = {}

    def _fake_generate(**kwargs):
        seen.update(kwargs)
        return "features", 10, 20

    mod.generate = _fake_generate
    qid, text, index, in_tok, out_tok = mod.extract_single("Q31", "/nonexistent.png", 0)
    assert text == "features", "the stub should have been reached; guard rewrote the result"
    assert "max_tokens" in seen, "the runaway is only bounded by max_tokens -- timeout cannot do it"
    assert isinstance(seen["max_tokens"], int) and seen["max_tokens"] > 0


def test_token_cap_is_below_the_cap_the_runaway_reached():
    mod = _load_extract_features()
    assert mod._FEAT_MAX_TOKENS < PROVIDER_DEFAULT_CAP, (
        "a cap at or above the provider default cannot prevent the 625s runaway")


def test_token_cap_leaves_headroom_over_a_healthy_read():
    """Too tight truncates real feature lists and silently degrades diagram grading."""
    mod = _load_extract_features()
    assert mod._FEAT_MAX_TOKENS >= LARGEST_HEALTHY_READ * 1.5


def test_a_capped_call_fits_inside_its_own_per_call_timeout():
    """The point of the cap: a maxed-out response must fail within the call's OWN budget instead of
    escaping to the stage budget. At the measured ~26 tok/s floor for this model."""
    mod = _load_extract_features()
    slowest_observed_tok_per_s = 26.0
    worst_case_s = mod._FEAT_MAX_TOKENS / slowest_observed_tok_per_s
    assert worst_case_s < mod._DIAG_TIMEOUT, (
        f"a maxed response takes ~{worst_case_s:.0f}s but the per-call timeout is {mod._DIAG_TIMEOUT}s")


def test_token_cap_is_env_tunable():
    os.environ["DIAGRAM_FEATURES_MAX_TOKENS"] = "999"
    try:
        assert _load_extract_features()._FEAT_MAX_TOKENS == 999
    finally:
        del os.environ["DIAGRAM_FEATURES_MAX_TOKENS"]


# --- defect 4: diagram GRADING had none of the above ----------------------------------------------
#
# evaluate_diagrams.py used a bare `with ThreadPoolExecutor(...)`, which joins every worker at block
# exit. One wedged call therefore held the stage until the orchestrator's 420s watchdog killed it,
# discarding every diagram already graded -- nothing is printed until the end. With
# DIAGRAM_EVAL_MAX_TOKENS=12288 (.env) a single runaway pass is ~473s on its own, and eval_single makes
# TWO sequential passes.

EVAL_DIAG = os.path.join(ROOT, "skills/diagram_evaluator/scripts/evaluate_diagrams.py")


def test_diagram_grading_has_a_future_level_budget():
    src = open(EVAL_DIAG).read()
    assert "concurrent.futures.wait(" in src and "timeout=_budget" in src
    assert "DIAGRAM_EVAL_STAGE_TIMEOUT" in src
    assert "as_completed" not in src, "as_completed still joins every worker at block exit"


def _load_evaluate_diagrams():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ed_under_test", EVAL_DIAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_diagram_grading_budget_covers_two_sequential_passes():
    """eval_single grades then AUDITS, so a budget sized for ONE pass abandons legitimate work.
    Reads the module's own _STAGE_BUDGET -- an earlier version of this test recomputed the default
    itself, which meant changing the source changed nothing and the test was decorative."""
    mod = _load_evaluate_diagrams()
    one_pass_worst_case = mod._DIAG_TIMEOUT * (mod._DIAG_RETRIES + 1)
    assert mod._STAGE_BUDGET > 2 * one_pass_worst_case


def test_diagram_grading_budget_stays_under_the_orchestrator_watchdog():
    """Otherwise the watchdog kills the stage first and the partial results are lost anyway -- the
    entire point of the budget."""
    mod = _load_evaluate_diagrams()
    assert mod._STAGE_BUDGET < fe._STAGE_TIMEOUTS["evaluate_diagrams.py"]


def test_diagram_grading_budget_is_env_tunable():
    os.environ["DIAGRAM_EVAL_STAGE_TIMEOUT"] = "123"
    try:
        assert _load_evaluate_diagrams()._STAGE_BUDGET == 123
    finally:
        del os.environ["DIAGRAM_EVAL_STAGE_TIMEOUT"]


def test_diagram_grading_actually_uses_the_module_budget():
    """Guards against the budget being computed correctly and then ignored at the call site."""
    src = open(EVAL_DIAG).read()
    assert "_budget = _STAGE_BUDGET" in src
    assert "timeout=_budget" in src


def test_completed_diagram_grades_are_kept_when_others_stall():
    src = open(EVAL_DIAG).read()
    assert "for future in done:" in src and "if pending:" in src
    assert "were abandoned" in src


def test_diagram_grading_cannot_hang_at_interpreter_exit():
    src = open(EVAL_DIAG).read()
    assert "shutdown(wait=False, cancel_futures=True)" in src
    assert "os._exit(0)" in src and "sys.stdout.flush()" in src


def test_a_diagram_worker_that_raises_does_not_abort_the_stage():
    src = open(EVAL_DIAG).read()
    assert "Diagram evaluation worker failed" in src


# --- defect 5: the vision audit was unconditional, and its failure ZEROED the question -------------
#
# MEASURED per call on the Science sheet: pass 1 (text) 12-35s / ~500 out tokens; pass 2 (vision)
# 34-110s / 1400-4300 out tokens for ~1700 chars of JSON -- the rest is hidden reasoning. Pass 2 was
# ~75% of a stage that was the pipeline's critical path.
#
# Worse, pass 2 returns an EMPTY response (in=0, out=0, ~12s) for MULTI-PAGE diagram questions -- two
# pages is ~17k input tokens and ~3.5MB of image. That hit 2 of 4 diagram questions on one sheet, and
# because the failure fell through to the generic handler, both were scored 0.0 despite pass 1 having
# already produced a valid graded draft. MAX_DIAGRAM_PAGES_PER_Q is 4, so this reaches any sheet whose
# diagram spans pages.

def _ed():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ed_audit", EVAL_DIAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_cascade_defaults_off_because_it_did_not_buy_wall_clock():
    """Measured: always 122.6/132.7s vs cascade 186.6/99.4s. All pass-2 calls already run
    CONCURRENTLY (max_workers=10), so dropping 4 to 2 removes parallel work, not critical-path work."""
    assert _ed()._AUDIT_MODE == "always"
    assert _ed().audit_reason({"confidence_score": 0.98, "marks_awarded": 2.0}, 2.0) == "always"


def test_a_confident_non_zero_draft_skips_the_vision_audit(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    assert _ed().audit_reason({"confidence_score": 0.98, "marks_awarded": 2.0}, 2.0) is None


def test_a_low_confidence_draft_is_audited(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    assert _ed().audit_reason({"confidence_score": 0.5, "marks_awarded": 2.0}, 2.0) == "low_confidence"


def test_a_zero_is_never_left_to_a_text_only_read(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    """The highest-stakes outcome. Measured worth: Q22's text pass scored 0/2 and the audit it
    triggered corrected it to 2/2."""
    assert _ed().audit_reason({"confidence_score": 0.99, "marks_awarded": 0}, 2.0) == "zero_mark"


def test_full_marks_are_not_treated_as_a_zero(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    assert _ed().audit_reason({"confidence_score": 0.99, "marks_awarded": 2.0}, 2.0) is None


@pytest.mark.parametrize("draft", [None, "not a dict", {"confidence_score": "x"},
                                   {"confidence_score": 0.9, "marks_awarded": "y"}])
def test_an_unusable_draft_is_always_audited(draft, monkeypatch):
    """Never silently accept a draft we could not read."""
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    assert _ed().audit_reason(draft, 2.0) is not None


def test_partial_credit_alone_does_not_trigger_the_audit(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "cascade")
    """Deliberate: measuring the same trigger for TEXT grading showed escalating on partial credit
    moved marks AWAY from the teacher. See cascade-escalation-policy."""
    assert _ed().audit_reason({"confidence_score": 0.95, "marks_awarded": 1.0}, 3.0) is None


def test_the_audit_can_be_disabled_entirely(monkeypatch):
    monkeypatch.setenv("DIAGRAM_EVAL_AUDIT", "never")
    assert _ed().audit_reason({"confidence_score": 0.1, "marks_awarded": 0}, 2.0) is None


def test_the_audit_threshold_matches_the_needs_review_threshold():
    """0.8 is not a new number -- anything below it is already flagged needs_review, so the audit runs
    exactly on the drafts a human would be asked to check anyway."""
    assert _ed()._AUDIT_CONF == 0.8


def test_a_failed_audit_keeps_the_first_pass_grade_instead_of_zeroing():
    src = open(EVAL_DIAG).read()
    body = src.split("def eval_single(")[1].split("\ndef ")[0]
    assert "keeping the first-pass grade" in body
    assert "final = dict(draft)" in body, "a failed audit must fall back to the draft, not to 0"
    # and the fallback must not be silently trusted
    assert '"Audited"] = "failed"' in body and 'final["needs_review"] = True' in body


def test_the_audit_decision_is_recorded_on_the_result():
    src = open(EVAL_DIAG).read()
    assert '"Audit Reason"' in src and '"Audited"' in src


def test_an_unusable_diagram_verdict_is_reported_not_silent():
    """Vinayak's Q36 had features extracted but vanished from diagram_evals.json with no trace, so an
    un-assessed diagram was indistinguishable from one deliberately marked 0."""
    src = open(EVAL_DIAG).read()
    assert "no usable verdict returned" in src


# --- fix 3: display-only cropping must not sit in front of the graders ----------------------------

@fe_only
def test_display_cropping_runs_alongside_extraction_not_before_it():
    """It was serialised ahead of extract_features, which never reads its output -- ~33s of measured
    critical path (grading blocks on this job's sentinel, so this job IS the critical path)."""
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    body = src.split("def _run_diagrams(")[1].split("\n        # Clear stale evals")[0]
    assert "threading.Thread(target=_display_crops" in body
    crop_start = body.index("_display_crops")
    extract_call = body.index("extract_script")
    assert crop_start < extract_call, "cropper must be launched before extraction, not awaited before it"
    assert "run_command([\"python3\", crop_diag_script" not in body, "still called inline/serially"


@fe_only
def test_display_crops_are_joined_before_the_sentinel_drops():
    """evaluate.py reads DIAGRAM_CROPS_JSON only after this sentinel -- that ordering is what stops the
    report seeing a half-finished crop set."""
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    body = src.split("def _run_diagrams(")[1].split("\n        # Clear stale evals")[0]
    join_at = body.index("crop_t.join(")
    sentinel_at = body.index("open(diagram_sentinel")
    assert join_at < sentinel_at, "sentinel must not drop before the crop set is complete"


@fe_only
def test_display_cropping_still_cannot_change_a_mark():
    """The graders take the full-page map via ARGV, so overlapping the cropper is safe by construction."""
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    body = src.split("def _run_diagrams(")[1].split("\n        # Clear stale evals")[0]
    assert "extract_script, diagram_crops_path" in body
    assert "eval_diag_script, diagram_crops_path" in body
    # The display path is touched ONLY by _display_crops; nothing in the grading chain may reference it.
    assert "diagram_display_path" not in body
