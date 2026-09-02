"""Re-uploading a corrected answer sheet must not inherit the previous upload's data.

`run_id` is the uploaded file's STEM, so a teacher who re-uploads a fixed sheet under the same name
lands in the SAME `output/<run_id>/` folder. Nothing downstream truncates that folder --
`process_input.py` and `preprocess.py` both `makedirs(exist_ok=True)` and only overwrite the pages they
emit -- while `_ingest_and_preprocess` and the OCR stage **glob** it. Replacing a 5-page sheet with a
2-page one therefore left pages 3-5 of the WRONG sheet on disk for OCR to read.

These pin the fix (`_reset_run_dir`) and, just as importantly, that it does NOT fire on the orientation
gate's resume path, which consumes the very files phase 1 left behind.

Offline / no network: only directory mechanics are exercised.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
except (ImportError, SystemExit) as e:  # pragma: no cover
    pytest.skip(f"full_evaluator unavailable: {e}", allow_module_level=True)


def _populate_run(d, pages=5):
    """A run folder as a completed evaluation leaves it."""
    imgs = d / "images"
    pre = d / "preprocessed"
    ocr = d / "ocr_output"
    crops = d / "answer_crops"
    for sub in (imgs, pre, ocr, crops):
        sub.mkdir(parents=True, exist_ok=True)
    for i in range(1, pages + 1):
        (imgs / f"Sheet_page_{i}.png").write_bytes(b"old-image")
        (pre / f"preprocessed_Sheet_page_{i}.png").write_bytes(b"old-preprocessed")
    (ocr / "ocr_answers.json").write_text('{"Q1": {"answer": "old"}}')
    (ocr / "page_mapping.json").write_text("{}")
    (crops / "Q1_p1.jpg").write_bytes(b"old-crop")
    (d / "review_state.json").write_text('{"marks": 42}')
    (d / "answer_crops.json").write_text("[]")
    (d / "api_costs.jsonl").write_text('{"stage":"ocr"}\n')
    (d / "stage_timings.json").write_text("{}")
    return d


# ---- _reset_run_dir ------------------------------------------------------------------------------
def test_reset_clears_every_derived_artifact(tmp_path):
    run = _populate_run(tmp_path / "Sheet")
    assert fe._reset_run_dir(str(run)) > 0
    assert os.listdir(run) == []


def test_reset_removes_the_stale_pages_that_caused_the_bug(tmp_path):
    """THE regression guard: 5-page sheet replaced by a 2-page one.

    Reproduced before the fix: after re-ingesting the 2-page PDF the folder still held 5 images, and
    both the preprocess glob and the OCR glob picked up all five.
    """
    run = _populate_run(tmp_path / "Sheet", pages=5)
    fe._reset_run_dir(str(run))
    # Re-ingest of the corrected 2-page sheet.
    imgs = run / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    for i in (1, 2):
        (imgs / f"Sheet_page_{i}.png").write_bytes(b"new-image")

    from pathlib import Path as _P
    globbed = sorted(p.name for p in _P(str(imgs)).glob("*.png"))
    assert globbed == ["Sheet_page_1.png", "Sheet_page_2.png"]
    assert all((imgs / n).read_bytes() == b"new-image" for n in globbed)


def test_reset_preserves_the_batch_ipc_input(tmp_path):
    """`batch_sheet_args.json` is written by the batch PARENT before the subprocess starts; wiping it
    would delete the child's own arguments out from under it."""
    run = _populate_run(tmp_path / "Sheet")
    (run / "batch_sheet_args.json").write_text('{"student_name": "A"}')
    fe._reset_run_dir(str(run))
    assert os.listdir(run) == ["batch_sheet_args.json"]
    assert (run / "batch_sheet_args.json").read_text() == '{"student_name": "A"}'


def test_reset_drops_the_previous_grading(tmp_path):
    """A stale review_state.json meant a run that died mid-pipeline still served the PREVIOUS grading
    as though it were the new sheet's."""
    run = _populate_run(tmp_path / "Sheet")
    assert (run / "review_state.json").exists()
    fe._reset_run_dir(str(run))
    assert not (run / "review_state.json").exists()


def test_reset_is_a_noop_on_a_first_upload(tmp_path):
    assert fe._reset_run_dir(str(tmp_path / "never-existed")) == 0


def test_reset_survives_an_undeletable_entry(tmp_path, monkeypatch):
    """A locked leftover must not abort the evaluation -- best-effort, keep going."""
    run = _populate_run(tmp_path / "Sheet")
    real_remove = os.remove

    def flaky(path):
        if path.endswith("review_state.json"):
            raise OSError("locked")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky)
    fe._reset_run_dir(str(run))                     # must not raise
    assert os.listdir(run) == ["review_state.json"]  # everything else still cleared


# ---- wiring: fresh run resets, orientation resume does NOT ---------------------------------------
def _ctx_for(tmp_path, monkeypatch, run_id, **kw):
    """Point full_evaluator's project root at tmp_path so _setup_run builds output/<run_id> there."""
    monkeypatch.setattr(fe.os.path, "abspath", fe.os.path.abspath)
    real_join = os.path.join
    monkeypatch.setattr(
        fe.os.path, "join",
        lambda *a: (real_join(str(tmp_path), *a[1:]) if len(a) > 1 and a[1] == "output" else real_join(*a)),
    )
    return fe._setup_run(run_id, **kw)


def test_setup_run_resets_when_asked(tmp_path, monkeypatch):
    run = _populate_run(tmp_path / "output" / "Sheet")
    _ctx_for(tmp_path, monkeypatch, "Sheet", reset=True)
    assert os.listdir(run) == []


def test_setup_run_does_not_reset_by_default(tmp_path, monkeypatch):
    """resume_after_orientation calls _setup_run WITHOUT reset -- phase 2 of the orientation gate reads
    the preprocessed pages and orientation_review.json that phase 1 wrote. Resetting here would delete
    the teacher's confirmed input and break the gate."""
    run = _populate_run(tmp_path / "output" / "Sheet")
    (run / "orientation_review.json").write_text('{"pages": [{"index": 1, "file": "p1.png"}]}')
    _ctx_for(tmp_path, monkeypatch, "Sheet")        # default reset=False
    assert (run / "orientation_review.json").exists()
    assert (run / "preprocessed").exists()
    assert len(os.listdir(run / "preprocessed")) == 5


def test_resume_after_orientation_never_resets():
    """Static guard on the call site: resume must not pass reset=True, whatever else changes."""
    import inspect
    src = inspect.getsource(fe.resume_after_orientation)
    setup_call = [ln for ln in src.splitlines() if "_setup_run(" in ln]
    assert setup_call, "resume_after_orientation no longer calls _setup_run"
    assert "reset=True" not in "".join(setup_call)


def test_prepare_orientation_resets():
    """The other half of the same contract: phase 1 IS a fresh upload and must reset."""
    import inspect
    src = inspect.getsource(fe.prepare_orientation)
    setup_call = [ln for ln in src.splitlines() if "_setup_run(" in ln]
    assert setup_call, "prepare_orientation no longer calls _setup_run"
    assert "reset=True" in "".join(setup_call)


def test_full_evaluate_resets():
    """full_evaluate has its own inline setup rather than _setup_run, so it needs its own guard."""
    import inspect
    src = inspect.getsource(fe.full_evaluate)
    assert "_reset_run_dir(output_base)" in src
