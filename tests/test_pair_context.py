"""Pair-context OCR (cross-page continuation capture), fully offline -- the vision call is stubbed.

Pins the contract that makes the fix work WITHOUT serialising OCR:
  * process_page sends the previous page's bottom strip as a SECOND, read-only context image (context
    first, page second) and prepends the PAIR_CONTEXT_PREAMBLE -- only when enabled AND a predecessor
    exists; otherwise it is byte-identical single-image OCR (page 0, or OCR_PAIR_CONTEXT=0).
  * _bottom_strip_png_bytes returns the bottom fraction of a page (path OR bytes), None on bad input.
  * assemble_answers welds LEADING (pre-[START_Q]) text on page N onto the question active from page
    N-1 -- the existing machinery that carries the emitted continuation home.
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
    return Image.open(io.BytesIO(b)).size  # (w, h)


def _capture(calls, reply="[START_Q: 1]\nx\n[END_Q: 1]"):
    """Stub for run_ocr.generate: record kwargs, return a NON-blank reply (so the blank-retry, which
    would add a second call, never fires)."""
    def g(*a, **k):
        calls.append(k)
        return reply, 1, 1
    return g


# ---- bottom-strip crop --------------------------------------------------------------------------

def test_bottom_strip_is_bottom_region():
    # width preserved, height == frac of original (top = int(100*(1-0.28))=72 -> 100-72 = 28)
    assert _dims(run_ocr._bottom_strip_png_bytes(_png(80, 100), frac=0.28)) == (80, 28)


def test_bottom_strip_custom_frac():
    assert _dims(run_ocr._bottom_strip_png_bytes(_png(80, 100), frac=0.5)) == (80, 50)


def test_bottom_strip_accepts_path(tmp_path):
    p = tmp_path / "prev.png"
    Image.new("RGB", (60, 90), "white").save(p)
    w, h = _dims(run_ocr._bottom_strip_png_bytes(str(p), frac=0.3))
    assert w == 60 and abs(h - 27) <= 1   # ~30% of 90 (±1 for float rounding of the crop line)


def test_bottom_strip_bad_input_returns_none():
    assert run_ocr._bottom_strip_png_bytes(b"not a png") is None


# ---- process_page image plumbing (stubbed vision call) ------------------------------------------

def test_pair_context_sends_two_images_context_first(monkeypatch):
    monkeypatch.setattr(run_ocr, "_PAIR_CONTEXT", True)
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", False)   # so ocr_input is the path, unchanged
    calls = []
    monkeypatch.setattr(run_ocr, "generate", _capture(calls))
    res = run_ocr.process_page("page.png", 1, run_ocr.MAIN_PROMPT, prev_image_path=_png(80, 100))
    assert res["error"] is None
    assert len(calls) == 1                                # no blank-retry
    imgs = calls[0]["images"]
    assert len(imgs) == 2
    assert isinstance(imgs[0], (bytes, bytearray))        # context strip FIRST
    assert imgs[1] == "page.png"                          # page to transcribe SECOND
    assert calls[0]["prompt"].startswith("TWO IMAGES ARE PROVIDED")


def test_no_predecessor_sends_one_image(monkeypatch):
    monkeypatch.setattr(run_ocr, "_PAIR_CONTEXT", True)
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", False)
    calls = []
    monkeypatch.setattr(run_ocr, "generate", _capture(calls))
    run_ocr.process_page("page.png", 0, run_ocr.MAIN_PROMPT, prev_image_path=None)
    assert calls[0]["images"] == ["page.png"]
    assert not calls[0]["prompt"].startswith("TWO IMAGES")   # base prompt, no preamble


def test_pair_context_off_sends_one_image(monkeypatch):
    monkeypatch.setattr(run_ocr, "_PAIR_CONTEXT", False)     # kill switch
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", False)
    calls = []
    monkeypatch.setattr(run_ocr, "generate", _capture(calls))
    run_ocr.process_page("page.png", 3, run_ocr.MAIN_PROMPT, prev_image_path=_png(80, 100))
    assert calls[0]["images"] == ["page.png"]               # predecessor ignored when disabled


# ---- the weld the feature relies on -------------------------------------------------------------

def test_assembler_welds_cross_page_continuation():
    """Page 2 begins with un-numbered continuation text (no [START_Q]) then opens Q6; that leading
    text must weld onto Q5 (active from page 1), NOT land in Q6."""
    results = [
        {"error": None, "image_path": "p1.png",
         "text": "[START_Q: 5]\nStart of Q5 answer\n[END_Q: 5]"},
        {"error": None, "image_path": "p2.png",
         "text": "continued tail of Q5 here\n[START_Q: 6]\nQ6 answer\n[END_Q: 6]"},
    ]
    ocr, _pm, _q2i, _ft, _cb = run_ocr.assemble_answers(results, "AI10", valid_base_numbers=[5, 6])
    assert "Start of Q5 answer" in ocr["AI10_Q5"]["answer"]
    assert "continued tail of Q5 here" in ocr["AI10_Q5"]["answer"]   # welded onto the prior question
    assert "continued tail of Q5 here" not in ocr["AI10_Q6"]["answer"]
    assert "Q6 answer" in ocr["AI10_Q6"]["answer"]
