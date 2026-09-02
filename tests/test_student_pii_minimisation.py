"""Send the provider as little of a child's identity as possible.

`HEADER_PROMPT` is the ONLY request in this pipeline whose purpose is to extract a student's
identity -- it asks the model to read Name, Class, Roll No and Date off the front page. Every other
call sends answers. And when the teacher has already supplied the name, `_resolve_student_name`
discards the OCR'd one anyway (teacher-provided wins), so that call is pure data exposure plus a
wasted request.

WHAT THIS DOES NOT FIX, and the tests say so out loud: the student's name is *written on the page*.
The answer-OCR call sends page images, and page 1's pixels contain the header. Skipping the header
call stops us ASKING for the identity; it does not remove it from the image. Only redaction of the
header band would, and that risks clipping a real answer -- so it is a deliberate, separate,
opt-in decision rather than something smuggled in here.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr
except Exception:                                                    # pragma: no cover
    run_ocr = None
try:
    import full_evaluator as fe
except Exception:                                                    # pragma: no cover
    fe = None

ocr_only = pytest.mark.skipif(run_ocr is None, reason="run_ocr unavailable")
fe_only = pytest.mark.skipif(fe is None, reason="full_evaluator unavailable")


# --- the identity call is skippable, and skipping means NO REQUEST -------------------------------

@ocr_only
def test_process_header_makes_no_call_when_pii_extraction_is_off(monkeypatch):
    """Not 'call it and discard the answer' -- the request must never be made, so nothing about the
    child leaves the machine."""
    called = []
    monkeypatch.setattr(run_ocr, "_ocr_generate",
                        lambda *a, **k: called.append(a) or ("Name: Riya Sharma", 10, 5))
    text, tok = run_ocr.process_header("/tmp/page1.png", extract_pii=False)
    assert called == [], "an identity-extraction request was still sent"
    assert text == ""
    assert tok == {"prompt": 0, "completion": 0}


@ocr_only
def test_process_header_still_works_when_extraction_is_wanted(monkeypatch):
    monkeypatch.delenv("OCR_EXTRACT_STUDENT_PII", raising=False)
    monkeypatch.setattr(run_ocr, "_ocr_generate", lambda *a, **k: ("Name: Riya Sharma", 10, 5))
    text, tok = run_ocr.process_header("/tmp/page1.png", extract_pii=True)
    assert "Riya" in text and tok["prompt"] == 10


@ocr_only
@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_env_can_disable_identity_extraction_for_every_run(monkeypatch, val):
    monkeypatch.setenv("OCR_EXTRACT_STUDENT_PII", val)
    assert run_ocr.student_pii_extraction_enabled() is False
    called = []
    monkeypatch.setattr(run_ocr, "_ocr_generate", lambda *a, **k: called.append(1) or ("x", 1, 1))
    run_ocr.process_header("/tmp/page1.png", extract_pii=True)   # caller says yes, env overrides
    assert called == [], "OCR_EXTRACT_STUDENT_PII=0 must win over the per-run flag"


@ocr_only
def test_identity_extraction_defaults_on_so_batch_flows_keep_working(monkeypatch):
    """Turning this off by default would break batch grading, where the sheet's own header is how
    200 uploaded scans get identified. The privacy win comes from skipping it when the name is
    ALREADY KNOWN, which costs nothing."""
    monkeypatch.delenv("OCR_EXTRACT_STUDENT_PII", raising=False)
    assert run_ocr.student_pii_extraction_enabled() is True


@ocr_only
def test_the_cli_exposes_the_flag():
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "--no-header-pii" in src
    assert "not args.no_header_pii" in src        # actually wired to the dispatch, not just parsed


# --- the orchestrator decides correctly ----------------------------------------------------------

@fe_only
@pytest.mark.parametrize("provided,expect_skip", [
    ("Riya Sharma", True),          # teacher named the student -> OCR'd name would be discarded
    ("  Riya  ", True),
    ("Student", False),             # generic placeholder -> we genuinely need the sheet's header
    ("Student 3", False),
    ("", False),
    (None, False),
])
def test_skip_decision_matches_whether_the_name_is_already_known(provided, expect_skip, monkeypatch):
    """The skip must trigger on exactly the cases where `_resolve_student_name` would ignore the
    OCR'd name -- otherwise we would either leak needlessly or lose the student's identity."""
    monkeypatch.delenv("OCR_EXTRACT_STUDENT_PII", raising=False)
    assert bool(fe._resolve_student_name(provided, "")) is expect_skip


@fe_only
def test_env_off_forces_the_skip_even_without_a_provided_name(monkeypatch):
    monkeypatch.setenv("OCR_EXTRACT_STUDENT_PII", "0")
    assert fe._student_pii_extraction_enabled() is False


@fe_only
def test_orchestrator_wires_the_flag_onto_the_ocr_command():
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    assert 'ocr_cmd.append("--no-header-pii")' in src
    assert "_resolve_student_name(student_name, \"\") or not _student_pii_extraction_enabled()" in src


@fe_only
def test_the_two_pii_predicates_agree(monkeypatch):
    """full_evaluator mirrors run_ocr's predicate so it can decide before spawning the subprocess.
    Two copies of a rule drift; pin them together."""
    if run_ocr is None:
        pytest.skip("run_ocr unavailable")
    for val in ("0", "1", "off", "true", "", None):
        if val is None:
            monkeypatch.delenv("OCR_EXTRACT_STUDENT_PII", raising=False)
        else:
            monkeypatch.setenv("OCR_EXTRACT_STUDENT_PII", val)
        assert fe._student_pii_extraction_enabled() == run_ocr.student_pii_extraction_enabled(), val


# --- the honest limitation ------------------------------------------------------------------------

@ocr_only
def test_answer_ocr_still_sends_page_images_including_the_header():
    """Documents what is NOT solved, so nobody later reads 'PII minimisation' as 'the provider never
    sees the name'. The name is written ON the sheet; MAIN_PROMPT sends page images, and page 1
    contains the header. Skipping the header call stops us ASKING for the identity -- it does not
    remove it from the pixels. Removing it needs header-band redaction, which can clip a real
    answer, so it stays a separate opt-in decision.

    Mitigations that DO apply to those pixels: zdr + data_collection=deny (no retention, no
    training) -- see scripts/llm_client._provider_directive.
    """
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "MAIN_PROMPT" in src
    assert "student identity extraction disabled" in src      # the skip is announced in the log
