"""Per-page orientation AUTOFIX, fully offline. The gibberish-proof readability judge is exercised
directly; the orchestrator `_orient_autofix_pages` is driven with a stubbed process_page (no network) to
pin the never-degrade contract + edge cases:
  * a clearly-upright page is never re-OCR'd (skipped) and never rotated,
  * a 180 / 90 / 270 mis-orientation is corrected, INCLUDING a sheet that mixes 90 and 270 per page,
  * a blank page and a small-margin ambiguous page are KEPT (never rotate without a clear reliable win),
  * an MCQ page is decided by in-set question numbers; a no-question-set run falls back to dict-words,
  * an OCR error at one candidate rotation is skipped, not fatal.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/vision-ocr/scripts"))

try:
    import run_ocr
except (ImportError, SystemExit) as e:  # pragma: no cover
    run_ocr = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr unavailable in this env")


@pytest.fixture(autouse=True)
def _enable_autofix(monkeypatch):
    # Autofix is DEFAULT-OFF (the OCR-readability judge proved unreliable for Qwen-VL on real sheets);
    # these tests validate the decision/guard LOGIC on synthetic data, so enable the flag here.
    if run_ocr is not None:
        monkeypatch.setattr(run_ocr, "_ORIENT_AUTOFIX", True)

# 22 real dictionary/exam words (all in the embedded set) -> high dict_words at the correct rotation.
UPRIGHT = ("the given function value area limit solution equation answer proof section number total "
           "vector matrix angle triangle probability derivative integral maximum minimum")
GIBBERISH = "xkq zpfl wvbn jhtr qzx mmbn vvcx zzq ~~ || \\ //"      # ~0 real words (wrong rotation)


def _page(idx, text, path=None, rotation=0):
    return {"index": idx, "image_path": path or f"p{idx}.png", "text": text,
            "tokens": {"prompt": 1, "completion": 1}, "rotation": rotation, "error": None}


def _fake_pp(text_by_rotation):
    def pp(path, idx, prompt_text="", prev=None, rotation=0):
        return {"index": idx, "image_path": path, "text": text_by_rotation(rotation),
                "tokens": {"prompt": 2, "completion": 2}, "rotation": rotation, "error": None}
    return pp


# ---- readability judge -----------------------------------------------------------------------------

def test_readability_upright_beats_gibberish():
    assert run_ocr._readability(UPRIGHT)["dict_words"] >= 12
    assert run_ocr._readability(GIBBERISH)["dict_words"] <= 1


def test_readability_counts_in_and_out_of_set():
    r = run_ocr._readability("[START_Q: 1] a [START_Q: 87] b", {1, 2, 3})
    assert r["in_set"] == 1 and r["oos"] == 1


# ---- never-degrade ---------------------------------------------------------------------------------

def test_autofix_skips_clearly_upright_page(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a clearly-upright page must not be re-OCR'd")
    monkeypatch.setattr(run_ocr, "process_page", boom)
    results = [_page(0, UPRIGHT, rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert out is results and ep == 0 and ec == 0 and flags == []


def test_autofix_keeps_blank_page(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: ""))     # blank at all rotations
    results = [_page(0, "", rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert out[0]["text"] == "" and not any(f["action"] == "rotated" for f in flags)


def test_autofix_keeps_on_small_margin(monkeypatch):
    # Correct-ish page below the skip threshold; the best rotation gains only 1 real word -> NOT enough.
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")
    small, small2 = "the given value area", "the given value area limit"      # 4 vs 5 dict words
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: small2 if rot == 180 else small))
    results = [_page(0, small, rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert not any(f["action"] == "rotated" for f in flags)                    # gain 1 < margin -> kept


# ---- corrections -----------------------------------------------------------------------------------

def test_autofix_corrects_180(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: UPRIGHT if rot == 180 else GIBBERISH))
    results = [_page(0, GIBBERISH, rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert flags and flags[0]["action"] == "rotated" and flags[0]["to"] == 180 and ep > 0


def test_autofix_mixed_90_and_270_per_page(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")

    def pp(path, idx, prompt_text="", prev=None, rotation=0):
        want = 270 if idx == 0 else 90                                          # page 0 -> 270, page 1 -> 90
        return {"index": idx, "image_path": path, "text": UPRIGHT if rotation == want else GIBBERISH,
                "tokens": {"prompt": 2, "completion": 2}, "rotation": rotation, "error": None}
    monkeypatch.setattr(run_ocr, "process_page", pp)
    results = [_page(0, GIBBERISH), _page(1, GIBBERISH)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert {f["index"]: f["to"] for f in flags} == {0: 270, 1: 90}             # each page decided independently


def test_autofix_mcq_decided_by_in_set(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(
        lambda rot: "[START_Q: 1] a [START_Q: 2] b [START_Q: 3] c" if rot == 90
        else "[START_Q: 87] z [START_Q: 88] y"))
    results = [_page(0, "[START_Q: 87] z [START_Q: 88] y", rotation=0)]        # out-of-set as-scanned
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert flags and flags[0]["action"] == "rotated" and flags[0]["to"] == 90


def test_autofix_no_set_uses_dict_words(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")
    monkeypatch.setattr(run_ocr, "process_page", _fake_pp(lambda rot: UPRIGHT if rot == 270 else GIBBERISH))
    results = [_page(0, GIBBERISH, rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", None, 4)  # no question set
    assert flags and flags[0]["to"] == 270


def test_autofix_handles_ocr_error_candidate(monkeypatch):
    monkeypatch.setenv("OCR_ORIENT_AUTOFIX_REWRITE", "0")

    def pp(path, idx, prompt_text="", prev=None, rotation=0):
        if rotation == 90:
            return {"index": idx, "image_path": path, "text": "",
                    "tokens": {"prompt": 0, "completion": 0}, "rotation": rotation, "error": "boom"}
        return {"index": idx, "image_path": path, "text": UPRIGHT if rotation == 180 else GIBBERISH,
                "tokens": {"prompt": 2, "completion": 2}, "rotation": rotation, "error": None}
    monkeypatch.setattr(run_ocr, "process_page", pp)
    results = [_page(0, GIBBERISH, rotation=0)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert flags and flags[0]["to"] == 180                                     # errored 90 skipped, 180 wins


def test_autofix_disabled_is_hard_noop(monkeypatch):
    monkeypatch.setattr(run_ocr, "_ORIENT_AUTOFIX", False)

    def boom(*a, **k):
        raise AssertionError("nothing must run when autofix is disabled")
    monkeypatch.setattr(run_ocr, "process_page", boom)
    results = [_page(0, GIBBERISH)]
    out, ep, ec, flags = run_ocr._orient_autofix_pages(results, "P", [1, 2, 3], 4)
    assert out is results and ep == 0 and ec == 0 and flags == []
