"""Orientation auto-correction in run_ocr (detection parsing + the upright rotation), fully offline --
the vision call is stubbed. Pins the non-degradation contract: a '0' / unparseable / errored detection
leaves the page BYTE-IDENTICAL; auto-orient off returns the input unchanged; 90/270 swap the image
dimensions (proof the rotation actually happened)."""
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


def _stub_gen(reply):
    def g(*a, **k):
        return reply, 1, 1
    return g


# ---- detection parsing (stubbed vision call) ----------------------------------------------------

def test_detect_parses_clean_number(monkeypatch):
    monkeypatch.setattr(run_ocr, "generate", _stub_gen("180"))
    assert run_ocr._detect_rotation_cw(_png(80, 100)) == 180


def test_detect_parses_number_in_sentence(monkeypatch):
    monkeypatch.setattr(run_ocr, "generate", _stub_gen("Rotate 90 degrees clockwise."))
    assert run_ocr._detect_rotation_cw(_png(80, 100)) == 90


def test_detect_garbage_falls_back_to_zero(monkeypatch):
    monkeypatch.setattr(run_ocr, "generate", _stub_gen("I cannot tell"))
    assert run_ocr._detect_rotation_cw(_png(80, 100)) == 0


def test_detect_error_falls_back_to_zero(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(run_ocr, "generate", boom)
    assert run_ocr._detect_rotation_cw(_png(80, 100)) == 0


# ---- the upright rotation -----------------------------------------------------------------------

def test_orient_off_returns_input_unchanged(monkeypatch):
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", False)
    p = _png(80, 100)
    assert run_ocr._orient_upright(p) is p          # byte-identical, no work done


def test_orient_zero_returns_input_unchanged(monkeypatch):
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", True)
    monkeypatch.setattr(run_ocr, "_detect_rotation_cw", lambda img: 0)
    p = _png(80, 100)
    assert run_ocr._orient_upright(p) is p          # upright page never re-encoded


def test_orient_180_preserves_dims(monkeypatch):
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", True)
    monkeypatch.setattr(run_ocr, "_detect_rotation_cw", lambda img: 180)
    out = run_ocr._orient_upright(_png(80, 100))
    assert isinstance(out, (bytes, bytearray)) and _dims(out) == (80, 100)


def test_orient_90_swaps_dims(monkeypatch):
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", True)
    monkeypatch.setattr(run_ocr, "_detect_rotation_cw", lambda img: 90)
    assert _dims(run_ocr._orient_upright(_png(80, 100))) == (100, 80)


def test_orient_270_swaps_dims(monkeypatch):
    monkeypatch.setattr(run_ocr, "_AUTO_ORIENT", True)
    monkeypatch.setattr(run_ocr, "_detect_rotation_cw", lambda img: 270)
    assert _dims(run_ocr._orient_upright(_png(80, 100))) == (100, 80)
