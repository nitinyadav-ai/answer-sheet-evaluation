"""Orientation-by-boundary-vote, fully offline. The pure helpers (rotation, tag parsing, in/out-of-set
scoring) are exercised directly; the orchestrator `_reorient_by_boundary_vote` is driven with a stubbed
process_page (no network) to pin the decision + safety contract:
  * trigger only when out-of-set tags >= threshold,
  * pick the rotation that maximises in-set question numbers,
  * accept the re-OCR only if it reduces sheet-wide out-of-set tags.
"""
import io
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr
    from PIL import Image
except (ImportError, SystemExit) as e:
    run_ocr = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr/PIL unavailable in this env")


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _dims(b):
    return Image.open(io.BytesIO(b)).size


def _page(idx, base, path=None):
    return {"index": idx, "image_path": path or f"p{idx}.png",
            "text": f"[START_Q: {base}]\nx\n[END_Q: {base}]",
            "tokens": {"prompt": 1, "completion": 1}, "rotation": 0, "error": None}


# ---- pure helpers -------------------------------------------------------------------------------

def test_rotate_90_swaps_dims():
    assert _dims(run_ocr._rotate_png_bytes(_png(80, 100), 90)) == (100, 80)


def test_rotate_180_preserves_dims():
    assert _dims(run_ocr._rotate_png_bytes(_png(80, 100), 180)) == (80, 100)


def test_startq_bases_prefix_tolerant():
    assert run_ocr._startq_bases("[START_Q: 7] a [START_Q: Q8] b [START_Q: 12.a] c") == [7, 8, 12]


def test_orient_score_counts_in_and_out_of_set():
    valid = {7, 8, 9}
    text = "[START_Q: 7] a [START_Q: 87] b [START_Q: 8] c"   # 7,8 in set; 87 out
    assert run_ocr._orient_score(text, valid) == (2, 1)


# ---- orchestrator -------------------------------------------------------------------------------

def test_reorient_noop_when_clean(monkeypatch):
    # In-set tags only, below threshold -> must not re-OCR at all.
    def boom(*a, **k):
        raise AssertionError("process_page must not be called on a clean sheet")
    monkeypatch.setattr(run_ocr, "process_page", boom)
    results = [_page(0, 1), _page(1, 2)]
    out, ep, ec = run_ocr._reorient_by_boundary_vote(results, ["p0.png", "p1.png"], "P", [1, 2], 4)
    assert out is results and ep == 0 and ec == 0


def test_reorient_picks_rotation_maximising_in_set(monkeypatch):
    # Fake OCR: at 180 deg the numbers read in-set (7,8,9); otherwise out-of-set (87,88,89).
    def fake_pp(path, idx, prompt_text="", prev=None, rotation=0):
        base = (7 + idx) if rotation == 180 else (87 + idx)
        return {"index": idx, "image_path": path,
                "text": f"[START_Q: {base}]\nx\n[END_Q: {base}]",
                "tokens": {"prompt": 2, "completion": 2}, "rotation": rotation, "error": None}
    monkeypatch.setattr(run_ocr, "process_page", fake_pp)

    valid = [7, 8, 9]
    inputs = ["p0.png", "p1.png", "p2.png"]
    primary = [fake_pp(inputs[i], i, "P", None, 0) for i in range(3)]   # 87,88,89 -> all out-of-set
    out, ep, ec = run_ocr._reorient_by_boundary_vote(primary, inputs, "P", valid, 4)

    got = sorted(run_ocr._startq_bases(r["text"])[0] for r in out)
    assert got == [7, 8, 9]                 # re-OCR'd at 180 deg -> now all in-set
    assert ep > 0 and ec > 0                # vote spent tokens and reported them


def test_reorient_reverts_when_no_rotation_helps(monkeypatch):
    # Fake OCR: every rotation still yields out-of-set numbers -> keep the original pass unchanged.
    def fake_pp(path, idx, prompt_text="", prev=None, rotation=0):
        return {"index": idx, "image_path": path,
                "text": f"[START_Q: {87 + idx}]\nx\n[END_Q: {87 + idx}]",
                "tokens": {"prompt": 1, "completion": 1}, "rotation": rotation, "error": None}
    monkeypatch.setattr(run_ocr, "process_page", fake_pp)

    inputs = ["p0.png", "p1.png"]
    primary = [fake_pp(inputs[i], i, "P", None, 0) for i in range(2)]
    out, ep, ec = run_ocr._reorient_by_boundary_vote(primary, inputs, "P", [7, 8], 4)
    assert out is primary                   # no improvement -> original kept


def test_orient_vote_off_by_default(monkeypatch):
    # Orientation is now fully MANUAL (the teacher confirms every page in the review gate). The OCR-stage
    # boundary-vote must DEFAULT off. Re-import the module gate with OCR_ORIENT_VOTE removed so we test the
    # CODE default, not the ambient/.env value. The orchestrator itself (tested above) still works when a
    # caller explicitly sets OCR_ORIENT_VOTE=1.
    import importlib
    monkeypatch.delenv("OCR_ORIENT_VOTE", raising=False)
    importlib.reload(run_ocr)
    assert run_ocr._ORIENT_VOTE is False
