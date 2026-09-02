"""Tight diagram crops for the report (crop_diagram_regions.py). Offline -- the vision call is mocked.

This stage shipped dormant and silently produced NOTHING (0 of 2 pages cropped on real data) because of
two independent bugs, both pinned here:

  1. it asked for {"xmin","ymin","xmax","ymax"} while Qwen-VL answers in its native
     {"bbox_2d": [x0,y0,x1,y1]} form, so every box hit a KeyError guard and was discarded;
  2. it read the numbers as PIXELS when they are NORMALIZED 0-1000 -- the same units bug that caused
     the answer-region mis-cropping.

The rest of the gates exist because the model boxes TEXT when a page has no figure on it, which happens
routinely: detect_diagrams.py assigns every page of a question to that question. Thresholds here are the
ones measured on real sheets, so the boundary cases are the real numbers.
"""
import json
import os
import sys

import numpy as np
import pytest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills", "feature-extracter", "scripts"))

import crop_diagram_regions as c  # noqa: E402

PAGE_W, PAGE_H = 3509, 2480


# ---- reply parsing -------------------------------------------------------------------------------
def test_parses_the_native_bbox_2d_shape():
    """THE bug that made this stage yield nothing: the model never returns xmin/ymin."""
    got = c._parse_boxes('[{"bbox_2d": [558, 785, 811, 940], "label": "hand-drawn diagram"}]')
    assert got == [[558.0, 785.0, 811.0, 940.0]]


# --- the SECOND shape bug: json_mode forces an OBJECT, not the array the prompt asks for -----------
#
# Observed live on the Science sheet -- the model located the diagrams correctly and every single one
# was thrown away. This call sets json_mode=True, so the provider forces a top-level JSON OBJECT and
# the reply is a bare {"bbox_2d": [...]}. The old parser sliced between the outermost [ and ], which on
# that text yields the COORDINATE array, then iterated its four NUMBERS looking for boxes -- so it
# returned []. Measured: 9 of 9 pages reported "no diagram found"; after the fix, 4 of 4 real diagrams
# cropped and the 5 text-only continuation pages correctly dropped.

def test_parses_the_bare_object_json_mode_actually_returns():
    """The exact string observed from the live call."""
    assert c._parse_boxes('{"bbox_2d": [198, 507, 843, 763]}') == [[198.0, 507.0, 843.0, 763.0]]


def test_an_empty_object_still_means_no_diagram():
    """`{}` is the json_mode spelling of "no diagram on this page" and must not invent a box."""
    assert c._parse_boxes("{}") == []


def test_a_bare_coordinate_array_is_one_box_not_four_candidates():
    """The precise failure mode: [x0,y0,x1,y1] iterated as four numbers yielded nothing."""
    assert c._parse_boxes("[198, 507, 843, 763]") == [[198.0, 507.0, 843.0, 763.0]]


def test_parses_an_array_wrapped_under_a_key():
    assert c._parse_boxes('{"diagrams": [{"bbox_2d": [1, 2, 3, 4]}]}') == [[1.0, 2.0, 3.0, 4.0]]


def test_parses_a_bare_object_in_the_legacy_xmin_shape():
    assert c._parse_boxes('{"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40}') == [[10.0, 20.0, 30.0, 40.0]]


def test_the_requested_array_shape_still_works():
    """The fix must not regress the shape the prompt actually asks for."""
    assert c._parse_boxes('[{"bbox_2d": [1, 2, 3, 4]}, {"bbox_2d": [5, 6, 7, 8]}]') == \
        [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]


def test_a_fenced_array_still_parses():
    assert c._parse_boxes('```json\n[{"bbox_2d": [1, 2, 3, 4]}]\n```') == [[1.0, 2.0, 3.0, 4.0]]


@pytest.mark.parametrize("raw", ["", "sorry, no diagram here", "[]", "{", "null", "true"])
def test_unusable_replies_yield_no_boxes(raw):
    assert c._parse_boxes(raw) == []


def test_booleans_are_not_mistaken_for_coordinates():
    """bool is a subclass of int -- [True, False, True, False] must not become a box."""
    assert c._parse_boxes("[true, false, true, false]") == []


@pytest.mark.parametrize("raw,expected", [
    ('[{"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40}]', [[10.0, 20.0, 30.0, 40.0]]),   # legacy shape
    ('[[10, 20, 30, 40]]', [[10.0, 20.0, 30.0, 40.0]]),                                   # bare list
    ('```json\n[{"bbox_2d": [1, 2, 3, 4]}]\n```', [[1.0, 2.0, 3.0, 4.0]]),                # fenced
    ('[{"bbox_2d": [30, 40, 10, 20]}]', [[10.0, 20.0, 30.0, 40.0]]),                      # reversed
    ('[]', []),
    ('no json here', []),
    ('[{"bbox_2d": [1, 2]}]', []),                                                        # wrong arity
    ('[{"label": "x"}]', []),
])
def test_reply_parsing_is_robust(raw, expected):
    assert c._parse_boxes(raw) == expected


# ---- coordinates ---------------------------------------------------------------------------------
def test_boxes_are_normalized_not_pixels():
    """0-1000 scaled by page_dim/1000. Read as raw pixels, every box collapses into the top-left
    corner of a 3509x2480 page -- which is what the original code did."""
    box, reason = c._pick_box([[250.0, 250.0, 500.0, 500.0]], PAGE_W, PAGE_H)
    assert box is not None, reason
    # 25%..50% of each side, then padded by PAD_FRAC of the page's LONGEST side (0.012 * 3509 = 42px).
    pad = int(c.PAD_FRAC * PAGE_W)
    assert box[0] == pytest.approx(0.25 * PAGE_W - pad, abs=2)     # 877 - 42
    assert box[1] == pytest.approx(0.25 * PAGE_H - pad, abs=2)     # 620 - 42
    assert box[2] == pytest.approx(0.50 * PAGE_W + pad, abs=2)
    assert box[3] == pytest.approx(0.50 * PAGE_H + pad, abs=2)
    # and the whole box sits well away from the top-left corner it collapsed into when read as pixels
    assert box[0] > 500 and box[1] > 400


# ---- the gates, at the measured boundaries -------------------------------------------------------
def test_a_text_block_covering_a_third_of_the_page_is_rejected():
    """Real case: a continuation page of pure algebra came back boxed at 27-31% of the page."""
    box, reason = c._pick_box([[128.0, 131.0, 737.0, 599.0]], PAGE_W, PAGE_H)   # ~28.5%
    assert box is None and "of the page" in reason


def test_a_real_diagram_sized_box_is_kept():
    """Genuine diagrams measured 3.4-5.0% of the page."""
    box, _ = c._pick_box([[558.0, 785.0, 811.0, 940.0]], PAGE_W, PAGE_H)        # ~3.9%
    assert box is not None


def test_a_stray_mark_is_rejected():
    """A crop of the pen-mark '30.' came out at 0.96% of the page; real figures were >= 3.6%."""
    box, reason = c._pick_box([[400.0, 400.0, 540.0, 468.0]], PAGE_W, PAGE_H)   # ~0.95%
    assert box is None and "stray mark" in reason


def test_a_line_of_text_is_rejected_by_shape():
    """Chemical equations came back at aspect 8.4 and a boxed answer at 6.4; the area gate cannot
    catch these because a single line is small."""
    # 70% of width x 6% of height -> ~8.3:1 once scaled to the page
    box, reason = c._pick_box([[100.0, 500.0, 800.0, 560.0]], PAGE_W, PAGE_H)
    assert box is None and "wide" in reason


def test_a_wide_but_genuine_diagram_survives():
    """A row of three labelled test tubes measured 4.8:1 -- it must stay on the keep side of 6.0."""
    # ~67% of width x 20% of height -> ~4.8:1 on this page
    box, reason = c._pick_box([[150.0, 400.0, 820.0, 600.0]], PAGE_W, PAGE_H)
    assert box is not None, reason


def test_largest_qualifying_box_wins():
    boxes = [[100.0, 100.0, 250.0, 250.0], [400.0, 400.0, 700.0, 700.0]]
    box, _ = c._pick_box(boxes, PAGE_W, PAGE_H)
    assert box[0] > 1300           # the second, larger box


# ---- ink check -----------------------------------------------------------------------------------
def _page_with_ink(h=600, w=800, band=(200, 300)):
    g = np.full((h, w), 250, dtype=np.uint8)
    for y in range(band[0], band[1], 20):
        for x in range(60, w - 60, 26):
            g[y:y + 11, x:x + 11] = 20
    return g


def test_ink_check_rejects_a_box_over_blank_paper():
    gray = _page_with_ink()
    assert c._has_ink(gray, (0, 200, 800, 300)) is True      # over the handwriting
    assert c._has_ink(gray, (0, 420, 800, 560)) is False     # over blank paper


# ---- end-to-end page handling (vision mocked) ----------------------------------------------------
def _mock_generate(payload, calls=None):
    def _g(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return (payload if isinstance(payload, str) else json.dumps(payload)), 10, 5
    return _g


def _page(tmp_path, name="p.png"):
    p = str(tmp_path / name)
    Image.fromarray(_page_with_ink(PAGE_H // 4, PAGE_W // 4, (200, 380)), mode="L").save(p)
    return p


def test_a_diagram_page_yields_a_crop(tmp_path, monkeypatch):
    p = _page(tmp_path)
    monkeypatch.setattr(c, "generate", _mock_generate([{"bbox_2d": [200, 300, 700, 640]}]))
    _, res, _, _ = c._crop_one("Q24", p, 0, str(tmp_path / "crops"), use_api=True)
    assert res["crop"] and os.path.exists(res["crop"])
    assert res["image"] == p                                  # full page kept as the fallback


def test_no_diagram_reply_leaves_the_crop_null(tmp_path, monkeypatch):
    p = _page(tmp_path)
    monkeypatch.setattr(c, "generate", _mock_generate([]))
    _, res, _, _ = c._crop_one("Q24", p, 0, str(tmp_path / "crops"), use_api=True)
    assert res["crop"] is None and res["reason"].startswith("no diagram")


def test_a_malformed_reply_never_raises(tmp_path, monkeypatch):
    p = _page(tmp_path)
    monkeypatch.setattr(c, "generate", _mock_generate("total nonsense, no json"))
    _, res, _, _ = c._crop_one("Q24", p, 0, str(tmp_path / "crops"), use_api=True)
    assert res["crop"] is None                                # -> report shows the full page


def test_without_an_api_key_nothing_is_cropped(tmp_path):
    p = _page(tmp_path)
    _, res, i_tok, _ = c._crop_one("Q24", p, 0, str(tmp_path / "crops"), use_api=False)
    assert res["crop"] is None and i_tok == 0


def test_a_rejected_box_is_retried(tmp_path, monkeypatch):
    p = _page(tmp_path)
    replies = [[{"bbox_2d": [10, 10, 990, 990]}],          # whole page -> rejected
               [{"bbox_2d": [200, 300, 700, 640]}]]        # then a real one
    calls = []

    def _g(**kw):
        calls.append(kw)
        return json.dumps(replies[min(len(calls) - 1, len(replies) - 1)]), 10, 5

    monkeypatch.setattr(c, "generate", _g)
    _, res, _, _ = c._crop_one("Q24", p, 0, str(tmp_path / "crops"), use_api=True)
    assert len(calls) == 2 and res["crop"]


# ---- which entries survive ----------------------------------------------------------------------
def _run_main(tmp_path, entries, monkeypatch, payload):
    src = tmp_path / "diagram_crops.json"
    src.write_text(json.dumps(entries))
    out = tmp_path / "diagram_display_crops.json"
    monkeypatch.setattr(c, "generate", _mock_generate(payload))
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setattr(sys, "argv", ["crop_diagram_regions.py", str(src), str(out)])
    c.main()
    return json.loads(out.read_text())


def test_a_no_diagram_page_is_dropped_when_the_question_has_a_real_crop(tmp_path, monkeypatch):
    """The reported case: Q24 spans two pages, only one holds the parallelogram. Keeping the other as a
    full page would show a redundant whole page beside the figure."""
    good, blank = _page(tmp_path, "a.png"), _page(tmp_path, "b.png")
    entries = [{"question_id": "Q24", "image": good}, {"question_id": "Q24", "image": blank}]

    calls = {"n": 0}

    def _g(**kw):
        calls["n"] += 1
        # first page (and its retry) find a diagram; the second finds nothing
        return (json.dumps([{"bbox_2d": [200, 300, 700, 640]}]) if calls["n"] <= 1
                else json.dumps([])), 10, 5

    src = tmp_path / "in.json"
    src.write_text(json.dumps(entries))
    out = tmp_path / "out.json"
    monkeypatch.setattr(c, "generate", _g)
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setattr(c, "MAX_WORKERS", 1)
    monkeypatch.setattr(sys, "argv", ["crop_diagram_regions.py", str(src), str(out)])
    c.main()
    res = json.loads(out.read_text())
    assert len(res) == 1 and res[0]["crop"]


def test_a_question_with_no_crop_at_all_keeps_its_full_page(tmp_path, monkeypatch):
    """A figure we simply failed to bound must still show SOMETHING -- never lose the question."""
    p = _page(tmp_path)
    res = _run_main(tmp_path, [{"question_id": "Q31", "image": p}], monkeypatch, [])
    assert len(res) == 1 and res[0]["crop"] is None and res[0]["image"] == p


def test_empty_input_writes_an_empty_manifest(tmp_path, monkeypatch):
    src = tmp_path / "in.json"
    src.write_text("[]")
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["crop_diagram_regions.py", str(src), str(out)])
    c.main()
    assert json.loads(out.read_text()) == []


def test_the_manifest_is_written_atomically(tmp_path):
    """The orchestrator seeds this file before grading and the report reads it from another process, so
    a half-written file must never be observable."""
    out = tmp_path / "m.json"
    c._write_atomic(str(out), [{"question_id": "Q1"}])
    assert json.loads(out.read_text()) == [{"question_id": "Q1"}]
    assert not (tmp_path / "m.json.tmp").exists()


# ---- wiring guards -------------------------------------------------------------------------------
def test_grading_still_receives_the_full_pages():
    """DISPLAY-ONLY is structural: the diagram graders take diagram_crops_path as ARGV, so repointing
    the report's DIAGRAM_CROPS_JSON cannot reach them. If this ever changes, a bad crop could move a
    student's marks."""
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    assert 'run_command([PYTHON_EXE, extract_script, diagram_crops_path]' in src
    assert "eval_diag_script, diagram_crops_path," in src


def test_the_display_file_is_seeded_before_grading_starts():
    """env is snapshotted when the grading subprocess launches, so the value must be set on the main
    thread -- setting it from the background thread would never reach evaluate.py."""
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    seed = src.index('"crop": None, "reason": "not cropped yet"')
    thread = src.index("def _run_diagrams(")
    assert seed < thread, "the display manifest must be seeded before the diagram thread is defined/run"
