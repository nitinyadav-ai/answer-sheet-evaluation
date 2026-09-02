"""Regression lock for run_ocr.assemble_answers. This file pins the existing weld behaviour that the
Phase 3 recovery deliberately works AROUND -- so a future change to assemble_answers that reintroduces
the 'continuation orphan pollutes a completed earlier question' bug fails here. Offline / no network."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr  # imports llm_client + PIL/docx/fpdf; assemble_answers itself needs no network
except (ImportError, SystemExit) as e:  # run_ocr sys.exit(1)s if a vision dep is absent
    run_ocr = None
    _IMPORT_ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr import unavailable in this env")


def _r(i, text):
    return {"index": i, "image_path": f"/tmp/p{i}.png", "text": text,
            "tokens": {"prompt": 0, "completion": 0}, "error": None}


def test_weld_does_not_resurrect_a_completed_question():
    # Q11 is answered, then a later page's continuation is MISREAD as a fresh [START_Q: 11].
    # It must weld onto the ACTIVE question (Q12), never pollute the already-finished Q11.
    results = [
        _r(0, "[START_Q: 11]\n(a) eleven\n[END_Q: 11]"),
        _r(1, "[START_Q: 12]\n(a) twelve\n[END_Q: 12]"),
        _r(2, "[START_Q: 11]\nORPHAN_CONTINUATION\n[END_Q: 11]"),
    ]
    ocr, _pm, _q2i, _ft, _coll = run_ocr.assemble_answers(results, "", [11, 12, 13])
    assert ocr["_Q11"]["answer"] == "(a) eleven"            # Q11 stays clean
    assert "ORPHAN_CONTINUATION" in ocr["_Q12"]["answer"]   # orphan welded to active Q12


def test_failure_c_backward_suffix_misread_welds_to_active():
    # Pins the Failure-C behaviour: a backward, suffixed, IN-SET misread ('8.c' for Q37's '(c)') is
    # treated as spurious and welded to the active question -> Q37 captures nothing here. The Phase 3
    # recovery (tested separately) is what later re-homes it; assembly itself must stay as-is.
    results = [
        _r(0, "[START_Q: 36]\nq36 answer\n[END_Q: 36]"),
        _r(1, "[START_Q: 8.c]\nQ37C_CONTENT\n[END_Q: 8.c]\n[START_Q: 38]\nq38 answer\n[END_Q: 38]"),
    ]
    ocr, _pm, _q2i, _ft, _coll = run_ocr.assemble_answers(results, "", [8, 36, 37, 38])
    assert "_Q8.c" not in ocr           # no phantom key opened
    assert "_Q37" not in ocr            # Q37 captured nothing at assembly time
    assert "Q37C_CONTENT" in ocr["_Q36"]["answer"]   # welded to the active question
