"""The preprocessing force-rotate is now OFF by default -- orientation is decided downstream by the
OCR-stage boundary-vote. Pins that contract: default leaves a portrait page UNCHANGED (no more blind
sideways/upside-down corruption); PREPROCESS_FORCE_LANDSCAPE=1 restores the legacy rotate."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/img-preprocessing/scripts"))

try:
    import numpy as np
    import preprocess
except (ImportError, SystemExit) as e:
    preprocess = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(preprocess is None, reason="preprocess/cv2/numpy unavailable in this env")


def _portrait():
    return np.zeros((100, 60, 3), dtype=np.uint8)   # h > w


def _landscape():
    return np.zeros((60, 100, 3), dtype=np.uint8)    # w > h


def test_default_leaves_portrait_untouched(monkeypatch):
    monkeypatch.delenv("PREPROCESS_FORCE_LANDSCAPE", raising=False)
    out = preprocess._maybe_force_landscape(_portrait())
    assert out.shape[:2] == (100, 60)                # NOT rotated -> orientation preserved for OCR


def test_flag_on_rotates_portrait_to_landscape(monkeypatch):
    monkeypatch.setenv("PREPROCESS_FORCE_LANDSCAPE", "1")
    out = preprocess._maybe_force_landscape(_portrait())
    assert out.shape[:2] == (60, 100)                # legacy behaviour restored


def test_flag_on_keeps_already_landscape(monkeypatch):
    monkeypatch.setenv("PREPROCESS_FORCE_LANDSCAPE", "1")
    out = preprocess._maybe_force_landscape(_landscape())
    assert out.shape[:2] == (60, 100)                # only portrait pages were ever rotated
