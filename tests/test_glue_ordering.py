"""Layer-3 ordering fix: the GLUED-HOST flag that recover_gaps_by_position produces (e.g. Q35, after it
lifts a literal '37.' fragment out of it) must reach repair_glued_answers the SAME run, so the LLM
matcher can reassign any OTHER question still welded inside that host (e.g. Q36's optics). Pre-fix, the
host flag was written to disk only AFTER glue-repair ran, so glue-repair short-circuited on an empty set.
These tests reproduce the wiring with the REAL functions + a stub matcher (no network)."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
except (ImportError, SystemExit) as e:
    fe = None
    _IMPORT_ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None, reason="full_evaluator import unavailable in this env")


def _stub_matcher(block, s_key, s_expected, candidates):
    """Stand-in for the LLM: the optics block welded in Q35 actually answers the blank Q36."""
    if s_key == "Q35" and any(c[0] == "Q36" for c in candidates) and "optics" in block:
        return "Q36", "(b) refraction optics block"
    return None, ""


def test_recover_gaps_host_flag_reaches_glue_repair():
    # Q35 holds: its own answer, then Q36's optics (labelled only "(b)"), then a literal "37." fragment.
    q35 = "reproduction answer\n(b) optics block\n37.\nelectrolysis answer"
    ocr = {"Q35": {"answer": q35, "is_bad_handwriting": False}}
    valid = [34, 35, 36, 37]

    # Stage 1: recover_gaps lifts the literal "37." fragment and FLAGS the host it came from (35).
    gaps = fe._recompute_gaps(ocr, valid)
    ocr, recovered, flagged, _still = fe.recover_gaps_by_position(ocr, gaps, {}, {})
    assert 37 in recovered and 35 in flagged
    assert "optics block" in ocr["Q35"]["answer"]      # optics is still stranded in the host

    # Stage 2: the ordering fix -- union the on-disk flags (empty here) with recover_gaps' flags.
    glue_flags = sorted(set([]) | set(flagged))
    assert 35 in glue_flags

    # Stage 3: glue-repair now actually inspects Q35 and reassigns the optics to the blank Q36.
    manual = {"Q36": {"answer": "refraction; snell; lens"}}
    ocr, g_recovered, _gf = fe.repair_glued_answers(ocr, manual, valid, glue_flags, None,
                                                    matcher=_stub_matcher)
    assert 36 in g_recovered
    assert ocr["Q36"]["answer"].strip() != ""


def test_pre_fix_empty_flags_no_op():
    # Without the union, the on-disk flag set is empty -> glue-repair must short-circuit and NOT call
    # the matcher (pinning that the bug was the missing flag, not the matcher).
    ocr = {"Q35": {"answer": "reproduction (b) optics block", "is_bad_handwriting": False},
           "Q36": {"answer": "", "is_bad_handwriting": False}}

    def _boom(*a, **k):
        raise AssertionError("matcher must not be called when the flag set is empty")

    ocr2, recovered, _gf = fe.repair_glued_answers(ocr, {"Q36": {"answer": "x"}}, [34, 35, 36],
                                                   [], None, matcher=_boom)
    assert recovered == []
