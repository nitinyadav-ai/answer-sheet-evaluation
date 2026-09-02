"""Content-fallback orientation probe (cheap-signal gated), fully offline. The legibility scorer is
exercised directly; the orchestrator `_orient_probe_pages` is driven with real portrait/landscape PNGs +
out-of-set tags and a stubbed process_page (no network) to pin the decision + safety + COST contract:
  * a clean in-set PORTRAIT page is never re-OCR'd (the cost win),
  * a LANDSCAPE (sideways) page is re-OCR'd and rotated to the upright perpendicular,
  * an OUT-OF-SET page is re-OCR'd and rotated to the angle that recovers in-set numbers,
  * never-worse guard: a suspect no rotation improves is kept; only an unresolved out-of-set page is
    flagged 'uncertain' (a legit wide/landscape page is kept silently, no false alarm),
  * the 30B detector is OPT-IN (off by default) so no per-page model call is made.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr
    from PIL import Image
except (ImportError, SystemExit) as e:  # pragma: no cover
    run_ocr = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr/PIL unavailable in this env")


@pytest.fixture(autouse=True)
def _enable_probe(monkeypatch):
    # The content-probe is now OPT-IN (autofix superseded it as the default); enable it so these tests
    # exercise the probe's behaviour. Individual tests may override this back to False.
    if run_ocr is not None:
        monkeypatch.setattr(run_ocr, "_ORIENT_PROBE", True)

UPRIGHT = ("The process of photosynthesis converts carbon dioxide and water into glucose using "
           "sunlight captured by chlorophyll in the green leaves of the plant.")
GARBLED = "l/ x~ ]v 3 |. ,, ^^ // \\ ` : ; ~ _ + = < >"


def _png(path, w, h):
    Image.new("RGB", (w, h), "white").save(path, format="PNG")
    return path


def _page(idx, text, path, rotation=0):
    return {"index": idx, "image_path": path, "text": text,
            "tokens": {"prompt": 1, "completion": 1}, "rotation": rotation, "error": None}


def _fake_pp(text_by_rotation):
    def pp(path, idx, prompt_text="", prev=None, rotation=0):
        return {"index": idx, "image_path": path, "text": text_by_rotation(rotation),
                "tokens": {"prompt": 2, "completion": 2}, "rotation": rotation, "error": None}
    return pp


# ---- legibility scorer ---------------------------------------------------------------------------

def test_legibility_upright_beats_garbled():
    assert run_ocr._legibility_score(UPRIGHT) > run_ocr._legibility_score(GARBLED)


def test_legibility_empty_and_markup_only_are_zero():
    assert run_ocr._legibility_score("") == 0.0
    assert run_ocr._legibility_score("[START_Q: 5]") == 0.0


def test_legibility_strips_markup_tags():
    with_tags = run_ocr._legibility_score("[START_Q: 5] hello world foo [END_Q: 5]")
    plain = run_ocr._legibility_score("hello world foo")
    assert abs(with_tags - plain) < 1e-9


# ---- orchestrator: cost gate / landscape / out-of-set / never-worse / detector-opt-in --------------

def test_probe_skips_clean_portrait_in_set_page(tmp_path, monkeypatch):
    # Portrait + in-set + no out-of-set -> matches NO cheap signal -> never re-OCR'd (the cost win).
    def boom(*a, **k):
        raise AssertionError("a clean in-set portrait page must not be re-OCR'd")
    monkeypatch.setattr(run_ocr, "process_page", boom)
    p = _png(str(tmp_path / "p0.png"), 50, 100)                  # portrait
    results = [_page(0, "[START_Q: 1] " + UPRIGHT, p)]
    out, ep, ec, flags = run_ocr._orient_probe_pages(results, "P", [1, 2, 3], 4)
    assert out is results and ep == 0 and ec == 0 and flags == []


def test_probe_rotates_landscape_page_to_upright_perpendicular(tmp_path, monkeypatch):
    p = _png(str(tmp_path / "p0.png"), 100, 50)                  # landscape (sideways scan)
    # 90 -> clean+in-set; 270 -> garbled. The probe must pick 90 (higher legibility).
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(
        lambda rot: ("[START_Q: 2] " + UPRIGHT) if rot == 90 else ("[START_Q: 2] " + GARBLED)))
    results = [_page(0, "[START_Q: 2] " + GARBLED, p)]
    out, ep, ec, flags = run_ocr._orient_probe_pages(results, "P", [1, 2, 3], 4)
    assert flags and flags[0]["action"] == "rotated" and flags[0]["reason"] == "landscape"
    assert flags[0]["to"] == 90 and ep > 0 and ec > 0


def test_probe_rotates_out_of_set_portrait_page(tmp_path, monkeypatch):
    p = _png(str(tmp_path / "p0.png"), 50, 100)                  # portrait but upside-down (87 = misread 3)
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(
        lambda rot: ("[START_Q: 3] " + UPRIGHT) if rot == 180 else ("[START_Q: 87] " + GARBLED)))
    results = [_page(0, "[START_Q: 87] " + GARBLED, p)]
    out, ep, ec, flags = run_ocr._orient_probe_pages(results, "P", [1, 2, 3], 4)
    assert run_ocr._startq_bases(out[0]["text"]) == [3]
    assert flags and flags[0]["action"] == "rotated" and flags[0]["reason"] == "out-of-set" and flags[0]["to"] == 180


def test_probe_flags_uncertain_when_out_of_set_unresolved(tmp_path, monkeypatch):
    p = _png(str(tmp_path / "p0.png"), 50, 100)                  # portrait
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: "[START_Q: 88] " + GARBLED))  # never resolves
    results = [_page(0, "[START_Q: 87] " + GARBLED, p)]          # out-of-set, unfixable
    out, ep, ec, flags = run_ocr._orient_probe_pages(results, "P", [1, 2, 3], 4)
    assert out[0]["text"].startswith("[START_Q: 87]")           # original kept
    assert flags and flags[0]["action"] == "uncertain"


def test_probe_keeps_legit_landscape_silently_no_false_flag(tmp_path, monkeypatch):
    # A legitimate wide page: landscape but already in-set + readable; no rotation beats it -> keep, NO flag.
    p = _png(str(tmp_path / "p0.png"), 100, 50)                  # landscape (a wide table/diagram)
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: "[START_Q: 99] " + GARBLED))  # worse
    good = "[START_Q: 2] " + UPRIGHT
    out, ep, ec, flags = run_ocr._orient_probe_pages([_page(0, good, p)], "P", [1, 2, 3], 4)
    assert out[0]["text"] == good and flags == []               # kept silently, no false-alarm review


def test_probe_detector_is_opt_in_off_by_default(tmp_path, monkeypatch):
    # Clean portrait page + detector OFF (default) -> the detector must never be called.
    def boom(*a, **k):
        raise AssertionError("the 30B detector must not run unless OCR_ORIENT_USE_DETECTOR=1")
    monkeypatch.setattr(run_ocr, "_detect_rotation_cw", boom)
    monkeypatch.setattr(run_ocr, "process_page", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no re-OCR")))
    p = _png(str(tmp_path / "p0.png"), 50, 100)
    out, ep, ec, flags = run_ocr._orient_probe_pages([_page(0, "[START_Q: 1] " + UPRIGHT, p)], "P", [1], 4)
    assert out is not None and flags == []


def test_probe_disabled_is_hard_noop(monkeypatch):
    monkeypatch.setattr(run_ocr, "_ORIENT_PROBE", False)

    def boom(*a, **k):
        raise AssertionError("nothing must run when the probe is disabled")
    monkeypatch.setattr(run_ocr, "process_page", boom)
    results = [_page(0, "[START_Q: 1] " + UPRIGHT, "p0.png")]
    out, ep, ec, flags = run_ocr._orient_probe_pages(results, "P", [1], 4)
    assert out is results and ep == 0 and ec == 0 and flags == []
