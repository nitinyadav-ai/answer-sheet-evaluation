"""Orientation gate (assisted, human-in-the-loop) unit tests. Offline / no network.

Covers the deterministic mechanics behind the "improve-only, never-degrade" guarantee:
  - _apply_page_rotation: 0deg (and 360) is a byte no-op; 90/270 swap dims; 180 keeps dims.
  - orient_pages.build_review: stable index->file order (natural sort); every page presented
    as-uploaded (fully manual -- suggested_rot=0, confidence=ok, method=manual; no OSD detector).
  - orientation.suggest_rotation: honours the landscape aspect prior (wide -> {0,180}, tall -> {90,270}).
  - resume_after_orientation: applies the teacher's confirmed rotations to the preprocessed images
    and, at ZERO rotation, leaves every image byte-identical (so OCR input == today's) -- verified
    with the OCR->report tail stubbed out (no network, no repo output/ writes).
"""
import os
import sys
import json
import hashlib

import numpy as np
import cv2
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/orientation-correction/scripts"))

try:
    import full_evaluator as fe
    import orient_pages
    import orientation
except (ImportError, SystemExit) as e:  # pragma: no cover
    fe = orient_pages = orientation = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None or orient_pages is None,
                                reason="orientation stage modules unavailable")


def _write_png(path, w, h):
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(img, (w // 8, h // 8), (w // 2, h // 3), (0, 0, 0), -1)  # real content
    cv2.imwrite(path, img)
    return path


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---- _apply_page_rotation ---------------------------------------------------

def test_apply_rotation_zero_is_byte_noop(tmp_path):
    p = _write_png(str(tmp_path / "p.png"), 200, 120)
    before = _sha(p)
    fe._apply_page_rotation(p, 0)
    assert _sha(p) == before  # zero rotation must not rewrite the file at all


def test_apply_rotation_360_is_byte_noop(tmp_path):
    p = _write_png(str(tmp_path / "p.png"), 200, 120)
    before = _sha(p)
    fe._apply_page_rotation(p, 360)
    assert _sha(p) == before


def test_apply_rotation_90_swaps_dims(tmp_path):
    p = _write_png(str(tmp_path / "p.png"), 200, 120)
    fe._apply_page_rotation(p, 90)
    im = cv2.imread(p)
    assert (im.shape[1], im.shape[0]) == (120, 200)


def test_apply_rotation_270_swaps_dims(tmp_path):
    p = _write_png(str(tmp_path / "p.png"), 200, 120)
    fe._apply_page_rotation(p, 270)
    im = cv2.imread(p)
    assert (im.shape[1], im.shape[0]) == (120, 200)


def test_apply_rotation_180_keeps_dims(tmp_path):
    p = _write_png(str(tmp_path / "p.png"), 200, 120)
    fe._apply_page_rotation(p, 180)
    im = cv2.imread(p)
    assert (im.shape[1], im.shape[0]) == (200, 120)


# ---- orient_pages.build_review ---------------------------------------------

def test_build_review_natural_order_and_fields(tmp_path):
    for n in (1, 2, 10):  # page_2 must sort before page_10 (filenames not zero-padded)
        _write_png(str(tmp_path / f"preprocessed_x_page_{n}.png"), 300, 200)
    pages = orient_pages.build_review(str(tmp_path))["pages"]
    assert [p["index"] for p in pages] == [1, 2, 3]
    assert [p["file"] for p in pages] == [
        "preprocessed_x_page_1.png", "preprocessed_x_page_2.png", "preprocessed_x_page_10.png"]
    # Fully MANUAL: every page as-uploaded (0deg), neutral non-flagging state, no OSD detector.
    for p in pages:
        assert p["suggested_rot"] == 0
        assert p["confidence"] == "ok"
        assert p["method"] == "manual"
    assert not hasattr(orient_pages, "suggest_rotation")   # detector no longer wired into the gate


def test_build_review_empty_dir(tmp_path):
    assert orient_pages.build_review(str(tmp_path)) == {"pages": []}


# ---- orientation.suggest_rotation aspect prior -----------------------------

def test_suggest_rotation_landscape_prior(tmp_path):
    wide = cv2.imread(_write_png(str(tmp_path / "wide.png"), 400, 200))
    tall = cv2.imread(_write_png(str(tmp_path / "tall.png"), 200, 400))
    assert orientation.suggest_rotation(wide, "landscape")["suggested_rot"] in (0, 180)
    assert orientation.suggest_rotation(tall, "landscape")["suggested_rot"] in (90, 270)


# ---- resume_after_orientation (OCR->report tail stubbed) -------------------

def _seed_run(tmp_path, monkeypatch, n=3):
    """Fake ctx + preprocessed dir + orientation_review.json; stub _setup_run and the OCR tail so no
    network runs and nothing is written under the repo's output/."""
    pre = tmp_path / "preprocessed"
    pre.mkdir()
    files = [_write_png(str(pre / f"preprocessed_x_page_{i}.png"), 200 + i, 120)
             for i in range(1, n + 1)]
    review = {"pages": [{"index": i, "file": os.path.basename(files[i - 1]),
                         "suggested_rot": 0, "confidence": "low", "method": "ocr_fallback"}
                        for i in range(1, n + 1)]}
    (tmp_path / "orientation_review.json").write_text(json.dumps(review))

    ctx = {"run_id": "x", "skills_dir": "", "output_base": str(tmp_path),
           "images_dir": "", "preprocessed_dir": str(pre), "ocr_dir": str(tmp_path / "ocr_output"),
           "ocr_answers_path": "", "db_answers_path": "", "page_mapping_path": "",
           "env": {}, "cost_ledger_path": "", "wall_start": 0.0, "reconcile_mode": "raise"}
    monkeypatch.setattr(fe, "_setup_run", lambda run_id, marks_source=None, env_overrides=None: ctx)

    captured = {}
    def _stub_tail(c, *a, **k):
        captured["hashes"] = {os.path.basename(p): _sha(p) for p in sorted(pre.glob("*.png"))}
        return {"status": "success", "review_id": "x"}
    monkeypatch.setattr(fe, "_resume_tail", _stub_tail)
    return pre, files, captured


def test_resume_zero_rotation_is_byte_identical(tmp_path, monkeypatch):
    pre, files, captured = _seed_run(tmp_path, monkeypatch)
    before = {os.path.basename(p): _sha(p) for p in files}
    res = fe.resume_after_orientation("x", rotations={})
    assert res["status"] == "success"
    after = {os.path.basename(p): _sha(p) for p in files}
    assert after == before            # nothing rotated -> preprocessed bytes == today's
    assert captured["hashes"] == before  # the grader sees exactly the pristine images


def test_resume_missing_review_errors(tmp_path, monkeypatch):
    pre, files, captured = _seed_run(tmp_path, monkeypatch)
    os.remove(str(tmp_path / "orientation_review.json"))
    res = fe.resume_after_orientation("x", rotations={"1": 90})
    assert res.get("error") == "Orientation review missing"


def test_resume_applies_confirmed_rotation(tmp_path, monkeypatch):
    pre, files, captured = _seed_run(tmp_path, monkeypatch)
    fe.resume_after_orientation("x", rotations={"1": 90})
    im1 = cv2.imread(files[0])                     # page 1 was 201x120
    assert (im1.shape[1], im1.shape[0]) == (120, 201)  # -> 90deg -> 120x201
    im2 = cv2.imread(files[1])                     # page 2 (202x120) untouched
    assert (im2.shape[1], im2.shape[0]) == (202, 120)
