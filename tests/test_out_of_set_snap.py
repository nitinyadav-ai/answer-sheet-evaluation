"""Layer-1 out-of-set digit-misread snap in run_ocr.assemble_answers. Pins the new behaviour AND its
non-degradation contract: a UNIQUE, forward, still-blank in-set question exactly one digit from an
out-of-set tag is OPENED (rescuing the Qwen '36'->'86' loss); anything ambiguous / far / unanchored
welds exactly as before. Offline / no network."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr
except (ImportError, SystemExit) as e:
    run_ocr = None
    _IMPORT_ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr import unavailable in this env")


def _r(i, text):
    return {"index": i, "image_path": f"/tmp/p{i}.png", "text": text,
            "tokens": {"prompt": 0, "completion": 0}, "error": None}


# ---- pure helpers -------------------------------------------------------------------------------

def test_one_digit_off_helper():
    f = run_ocr._qnum_one_digit_off
    assert f("36", "86") is True          # the real case (3 read as 8)
    assert f("27", "21") is True
    assert f("36", "35") is True
    assert f("36", "36") is False         # identical
    assert f("6", "36") is False          # length differs -> never a snap
    assert f("36", "48") is False         # two digits differ


def test_resolve_picks_unique_forward_gap():
    g = run_ocr._resolve_out_of_set_qnum
    valid = {34, 35, 36, 37, 38, 39}
    assert g(86, valid, {34, 35}, 35) == 36          # 86 -> 36 (unique forward gap one digit away)
    assert g(86, valid, {34, 35, 36}, 35) is None    # 36 already captured -> never overwrite
    assert g(84, valid, {35}, 35) is None            # 34 is one-off but BACKWARD (<= max) -> abstain
    assert g(15, {14, 16}, set(), 10) is None        # 14 AND 16 match -> ambiguous -> abstain
    assert g(86, None, set(), 35) is None            # unanchored -> never snaps


# ---- assemble_answers integration ---------------------------------------------------------------

def test_out_of_set_misread_opens_the_gap_question():
    # Student wrote 35. then 36. then 37., but the model misread "36" as out-of-set "86".
    results = [
        _r(0, "[START_Q: 35]\nreproduction answer\n[END_Q: 35]"),
        _r(1, "[START_Q: 86]\noptics refraction answer\n[END_Q: 86]"),
        _r(2, "[START_Q: 37]\nelectrolysis answer\n[END_Q: 37]"),
    ]
    ocr, _pm, _q2i, _ft, coll = run_ocr.assemble_answers(results, "", [34, 35, 36, 37, 38, 39])
    assert "optics refraction answer" in ocr["_Q36"]["answer"]   # OPENED Q36 (was lost before)
    assert "optics" not in ocr["_Q35"]["answer"]                 # Q35 not polluted
    assert "_Q86" not in ocr                                     # no phantom out-of-set slot
    assert 36 in coll                                            # flagged for teacher review
    assert "electrolysis answer" in ocr["_Q37"]["answer"]        # max_qnum advanced -> 37 still opens


def test_ambiguous_out_of_set_welds_as_before():
    # "15" is one digit from BOTH in-set gaps 14 and 16 -> abstain -> weld into the active question.
    results = [
        _r(0, "[START_Q: 10]\nq10 answer\n[END_Q: 10]"),
        _r(1, "[START_Q: 15]\nAMBIG_CHUNK\n[END_Q: 15]"),
    ]
    ocr, _pm, _q2i, _ft, coll = run_ocr.assemble_answers(results, "", [10, 14, 16])
    assert "_Q14" not in ocr and "_Q16" not in ocr
    assert "AMBIG_CHUNK" in ocr["_Q10"]["answer"]
    assert 14 not in coll and 16 not in coll


def test_far_out_of_set_welds_as_before():
    # "99" has no same-length in-set gap one digit away -> weld as today (real garbage stays welded).
    results = [
        _r(0, "[START_Q: 20]\nq20 answer\n[END_Q: 20]"),
        _r(1, "[START_Q: 99]\nGARBAGE\n[END_Q: 99]"),
    ]
    ocr, _pm, _q2i, _ft, coll = run_ocr.assemble_answers(results, "", [20, 21, 22])
    assert "_Q99" not in ocr
    assert "GARBAGE" in ocr["_Q20"]["answer"]
    assert 99 not in coll


def test_unanchored_is_byte_identical():
    # No question-id set -> is_out_of_set never fires -> "86" opens its own slot, exactly as today.
    results = [
        _r(0, "[START_Q: 35]\nreproduction\n[END_Q: 35]"),
        _r(1, "[START_Q: 86]\noptics\n[END_Q: 86]"),
    ]
    ocr, _pm, _q2i, _ft, coll = run_ocr.assemble_answers(results, "", None)
    assert "_Q86" in ocr                       # legacy behaviour preserved
    assert coll == []
