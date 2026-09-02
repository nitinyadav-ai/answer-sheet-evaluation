"""Tests for per-answer region crops (crop_answer_regions.py).

Pins the accuracy contract: cuts land in whitespace (never through handwriting), bands cover the page
with shared edges (nothing lost between crops), the last answer on a page is trimmed to its ink instead
of trailing into blank paper, multi-page answers yield one crop per page, and EVERY validation failure
degrades to a full-page image rather than a wrong crop. No API and no network -- the vision call is
monkeypatched.
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

import crop_answer_regions as c  # noqa: E402

INK_BANDS = [(50, 120), (220, 300), (400, 470)]


def _ink_array(h=600, w=400, bands=INK_BANDS):
    """A page whose ink looks like HANDWRITING: short strokes with gaps, laid out in text lines.

    It must not be solid bars -- the profiler strips long horizontal/vertical runs to remove ruled
    lines and margins, so solid bars would be erased exactly like a printed rule."""
    g = np.full((h, w), 250, dtype=np.uint8)
    for a, b in bands:
        for y in range(a, b - 4, 20):                 # text lines inside the answer
            for x in range(40, w - 40, 26):           # short strokes, each well under the rule kernel
                g[y:min(y + 11, b), x:x + 11] = 20
    return g


def _synthetic_page(path, h=600, w=400, bands=INK_BANDS):
    g = _ink_array(h, w, bands)
    Image.fromarray(g, mode="L").save(path)
    return g


# ---- ink detection -------------------------------------------------------------------------------
def test_ink_is_detected_at_the_otsu_level():
    """Regression: Otsu's class 0 is [0..thr], so the profile must use `<=`. With `<` a cleanly
    bimodal page detected ZERO ink and the whole page read as blank."""
    profile, eps = c._row_ink_profile(_ink_array())
    assert profile[50] > eps and profile[220] > eps     # rows inside the answers
    assert profile[150] <= eps                          # row in the gap between answers
    assert profile.sum() > 0


def test_rules_and_margins_are_stripped_from_the_profile():
    """Ruled paper put ink in EVERY row, which flattened the profile and silently disabled both the
    snap and the trim. Long horizontal rules and vertical margins must not register as content."""
    g = _ink_array()
    g[::14, :] = 20              # full-width ruled lines every 14 rows
    g[:, 12:16] = 20             # vertical margin rule
    profile, eps = c._row_ink_profile(g)
    assert profile[150] <= eps   # a genuinely blank row stays blank despite the rules crossing it
    assert len(c._whitespace_runs(profile, eps)) >= 3


def test_whitespace_runs_finds_the_gaps_between_answers():
    profile, eps = c._row_ink_profile(_ink_array())
    runs = c._whitespace_runs(profile, eps)
    # the three answers are separated by blank stretches around rows 120-220 and 300-400
    assert any(a <= 150 < b for a, b in runs)
    assert any(a <= 350 < b for a, b in runs)


# ---- snapping ------------------------------------------------------------------------------------
def test_snap_pulls_anchor_out_of_ink_into_a_gap():
    runs = [(0, 50), (120, 220), (300, 400), (470, 600)]
    assert c._snap_to_gap(230, runs) == 170     # 230 sits in ink -> centre of the gap above
    assert c._snap_to_gap(405, runs) == 350


def test_snap_leaves_anchor_when_no_gap_is_near():
    assert c._snap_to_gap(1000, [(0, 5)], window=10) == 1000


# ---- bands ---------------------------------------------------------------------------------------
def test_bands_share_edges_and_last_runs_to_page_bottom():
    bands = c._build_bands([("Q1", 10), ("Q2", 170), ("Q3", 350)], 600)
    assert bands == [("Q1", 10, 170), ("Q2", 170, 350), ("Q3", 350, 600)]
    for i in range(len(bands) - 1):
        assert bands[i][2] == bands[i + 1][1]           # no gap between consecutive crops


def test_continuation_band_starts_at_page_top():
    bands = c._build_bands([("Q5", 300)], 600, continuation_qid="Q4", continuation_top=0)
    assert bands == [("Q4", 0, 300), ("Q5", 300, 600)]


def test_single_answer_page_covers_whole_page():
    assert c._build_bands([("Q9", 20)], 600) == [("Q9", 20, 600)]


# ---- ink trim ------------------------------------------------------------------------------------
def test_ink_trim_stops_at_last_ink_not_page_bottom():
    profile, eps = c._row_ink_profile(_ink_array())
    top, bottom = c._ink_trim(profile, 350, 600, eps, pad=8, page_h=600)
    assert top >= 380 and bottom <= 495          # hugs the 400-470 answer, nowhere near row 600
    assert bottom - top < 120


def test_ink_trim_none_on_blank_band():
    profile = np.zeros(600, dtype=np.int64)
    assert c._ink_trim(profile, 0, 600, eps=1, pad=8, page_h=600) is None


# ---- validation ----------------------------------------------------------------------------------
def test_validate_accepts_well_formed():
    ok, ys = c._validate_ys([10, 99], 2, 600)
    assert ok and ys == [10, 99]


@pytest.mark.parametrize("ys,n,reason_bit", [
    ([99, 10], 2, "increasing"),
    ([10], 2, "expected 2"),
    ([10, 20, 30], 2, "expected 2"),
    # Renamed deliberately: "y outside 0-1000" read like a coordinate-UNITS fault, which is a different
    # bug we already fixed once. The measured cause is token degeneracy (a value of 8 followed by ~200
    # zeros), so the reason now says so.
    ([9999], 1, "degenerate"),
    (["x"], 1, "integer"),
    ("not-a-list", 1, "list"),
    (None, 1, "list"),
])
def test_validate_rejects(ys, n, reason_bit):
    ok, reason = c._validate_ys(ys, n, 600)
    assert not ok and reason_bit in reason


@pytest.mark.parametrize("raw,expected", [
    ('{"ys": [1, 2]}', {"ys": [1, 2]}),
    ('Sure! Here you go:\n```json\n{"ys": [3]}\n```', {"ys": [3]}),
    ('{"ys": [4]} then the model kept rambling forever...', {"ys": [4]}),
    ('no json at all', {}),
    ('{"ys": [broken', {}),
    # truncated runaway array (token cap cut the closing bracket) -> salvaged, so the length check
    # can reject it explicitly as a degenerate run
    ('{"ys": [190, 458, 605, 667', {"ys": [190, 458, 605, 667]}),
])
def test_extract_json_obj_is_robust(raw, expected):
    """The model wraps JSON in prose/fences and sometimes rambles past it; extraction must survive."""
    assert c._extract_json_obj(raw) == expected


# ---- page roles ----------------------------------------------------------------------------------
def test_page_roles_marks_first_page_as_start_and_later_as_continuation():
    pm = {
        "/p/page_1.png": [{"question_id": "Q1"}, {"question_id": "Q2"}],
        "/p/page_2.png": [{"question_id": "Q2"}, {"question_id": "Q3"}],   # Q2 spans pages
    }
    roles = c._page_roles(pm)
    assert roles[0]["starts"] == ["Q1", "Q2"] and roles[0]["continuations"] == []
    assert roles[1]["starts"] == ["Q3"] and roles[1]["continuations"] == ["Q2"]
    assert [r["page_index"] for r in roles] == [1, 2]


def test_safe_qid_and_page_number():
    assert c._safe_qid("Q37.iii") == "Q37.iii"
    assert c._safe_qid("Q1/2 x") == "Q1_2_x"
    assert c._page_number("preprocessed_foo_page_12.png", 99) == 12
    assert c._page_number("noNumberHere.png", 7) == 7


# ---- end-to-end page processing (vision mocked) --------------------------------------------------
def _mock_generate(payload, calls=None):
    def _g(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return json.dumps(payload), 10, 5
    return _g


def test_process_page_produces_tight_bands(tmp_path, monkeypatch):
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p)
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [45, 215]}))
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)

    assert [e["question_id"] for e in entries] == ["Q1", "Q2"]
    assert all(e["method"] == "band" for e in entries)
    for e in entries:
        f = tmp_path / "crops" / e["crop_file"]
        assert f.exists()
        with Image.open(f) as im:
            assert im.height < 600          # a real crop, not the whole page
        assert e["page"] == 1


def test_process_page_falls_back_to_full_page_on_bad_vision_output(tmp_path, monkeypatch):
    p = str(tmp_path / "page_3.png")
    _synthetic_page(p)
    # ys running bottom-to-top -> must be rejected by validation
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [215, 45]}))
    role = {"page": p, "page_index": 3, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)

    assert {e["question_id"] for e in entries} == {"Q1", "Q2"}
    assert all(e["method"] == "page" for e in entries)      # full page, never a wrong crop
    assert all(e["reason"] for e in entries)


def test_coordinates_are_normalized_not_pixels(tmp_path, monkeypatch):
    """THE critical unit contract. Qwen-VL grounding is natively 0-1000 and returns that scale even when
    asked for pixels, so ys MUST be scaled by page_height/1000. Read as raw pixels, every anchor
    collapses into the top ~17% of the page (the printed header) -- the real cause of the mis-crops.

    Page is 600 tall; y=500 is the MIDDLE (300px), not 500px down."""
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p)                                    # ink at rows 50-120, 220-300, 400-470
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [80, 700]}))   # -> px 48 and 420
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)

    assert [e["method"] for e in entries] == ["band", "band"]
    heights = []
    for e in entries:
        im = Image.open(os.path.join(str(tmp_path / "crops"), e["crop_file"]))
        heights.append(im.height)
        im.close()
    # y=700 maps to px 420 (inside the third answer). Read as a raw pixel it would sit at 700 -- past
    # the 600px page bottom entirely -- so the second band could not contain content at all.
    assert heights[1] > 20, "second band is empty -> ys were treated as pixels, not normalized"
    assert sum(heights) < 600 * 2


def test_dense_page_safety_valve_still_works_when_configured(tmp_path, monkeypatch):
    """The >=N-answers gate is dormant by default (it was a workaround for the unit bug) but must still
    fire when a deployment lowers it."""
    p = str(tmp_path / "page_2.png")
    _synthetic_page(p)
    calls = []
    monkeypatch.setattr(c, "MAX_STARTS_PER_PAGE", 5)
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [45, 60, 75, 90, 215, 410]}, calls))
    role = {"page": p, "page_index": 2, "starts": [f"Q{i}" for i in range(1, 7)], "continuations": []}
    entries, i_tok, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)

    assert calls == [] and i_tok == 0                    # no API call at all
    assert len(entries) == 6 and all(e["method"] == "page" for e in entries)
    assert "dense objective page" in entries[0]["reason"]


def test_process_page_without_api_key_falls_back(tmp_path):
    p = str(tmp_path / "page_2.png")
    _synthetic_page(p)
    role = {"page": p, "page_index": 2, "starts": ["Q1"], "continuations": []}
    entries, i_tok, o_tok = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=False)
    assert len(entries) == 1 and entries[0]["method"] == "page"
    assert (i_tok, o_tok) == (0, 0)                          # no API call was made


def test_multipage_answer_yields_one_crop_per_page(tmp_path, monkeypatch):
    """A question spanning two pages must produce two entries, in page order."""
    p1, p2 = str(tmp_path / "page_1.png"), str(tmp_path / "page_2.png")
    _synthetic_page(p1)
    _synthetic_page(p2)
    pm = {p1: [{"question_id": "Q1"}, {"question_id": "Q2"}], p2: [{"question_id": "Q2"}]}
    roles = c._page_roles(pm)

    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [45, 215]}))
    e1, _, _ = c._process_page(roles[0], str(tmp_path), str(tmp_path / "crops"), use_api=True)

    # Page 2 opens with Q2 continuing and starts nothing new. It IS now called: with nothing located,
    # the page could only be handed whole to one answer, which is what dropped the second continuing
    # answer on pages shared by two of them (7 such pages on one real 30-page sheet). The model is asked
    # where the continuation ENDS so the band can be tightened instead.
    calls = []
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [520]}, calls))
    e2, i_tok, _ = c._process_page(roles[1], str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert len(calls) == 1
    assert "continue onto this page" in calls[0]["parts"][0]["text"]   # the continuation prompt, not starts

    q2 = [e for e in (e1 + e2) if e["question_id"] == "Q2"]
    assert len(q2) == 2
    assert [e["page"] for e in q2] == [1, 2]
    assert len({e["crop_file"] for e in q2}) == 2            # distinct files, not one reused
    assert e2[0]["method"] == "band"                          # and it IS a real crop, not a fallback


# ---- sentinel wait bound -------------------------------------------------------------------------
# The crop pass runs in a BACKGROUND thread that overlaps grading, so it normally costs zero
# wall-clock (measured: 18.5s of cropping hidden inside a 157s grading window). The one way it can
# land on the critical path is evaluate.py blocking on the sentinel after grading has already
# finished -- these pin that bound.

def _ev():
    sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))
    import evaluate
    return evaluate


def test_wait_returns_immediately_when_crops_already_done(tmp_path):
    """The normal case: the background pass finished long ago, so the wait must not cost anything."""
    import time
    sent = tmp_path / "answer_crops.json.done"
    sent.write_text("")
    t0 = time.time()
    assert _ev().wait_for_crops_sentinel(str(sent), 90) is True
    assert time.time() - t0 < 0.2


def test_wait_gives_up_at_the_bound_instead_of_hanging(tmp_path):
    """A stalled crop provider must degrade to 'report without screenshots', never a long hang."""
    import time
    t0 = time.time()
    assert _ev().wait_for_crops_sentinel(str(tmp_path / "never.done"), 0.3) is False
    assert 0.25 < time.time() - t0 < 2.0        # honoured the bound, and did not overshoot it


def test_wait_is_a_noop_without_a_sentinel(tmp_path):
    """Crops disabled upstream -> nothing to wait for."""
    assert _ev().wait_for_crops_sentinel("", 90) is True
    assert _ev().wait_for_crops_sentinel(None, 90) is True


def test_wait_notices_a_sentinel_that_appears_mid_wait(tmp_path):
    """Polling must actually observe a late arrival, not decide once and sleep through it."""
    import threading
    sent = tmp_path / "late.done"
    threading.Timer(0.3, lambda: sent.write_text("")).start()
    assert _ev().wait_for_crops_sentinel(str(sent), 10) is True


def test_default_wait_bound_is_well_under_the_crop_stage_timeout():
    """The default must sit clear of the ~65s worst case measured under BATCH_SHEET_CONCURRENCY, yet
    far below the crop stage's own 300s budget -- waiting the full stage timeout was the regression
    that could add minutes of dead wall-clock per sheet."""
    import re
    src = open(os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation",
                            "scripts", "evaluate.py")).read()
    m = re.search(r'ANSWER_CROPS_WAIT_TIMEOUT["\']\s*,\s*["\'](\d+)["\']', src)
    assert m, "ANSWER_CROPS_WAIT_TIMEOUT default not found"
    assert 70 <= int(m.group(1)) <= 120


# ---- salvage, retry and cross-page ---------------------------------------------------------------
# The accuracy work. Measured before it: one sheet scored 61% tight where the same code gave 92% on
# another, because a single flaky sample discards a whole page; and every residual `not placed` traced
# to a page carrying two continuing answers, of which only the first was ever used.

@pytest.mark.parametrize("ys,n,expected", [
    ([130, 270, 460, 590, 700, 8 * 10 ** 200], 6, [130, 270, 460, 590, 700, None]),  # the real reply
    ([10, 20, 30], 3, [10, 20, 30]),                       # already valid -> untouched
    ([560, 560], 2, [None, None]),                         # tie: BOTH dropped, never split
    ([10, 20, 15, 40], 4, [10, 20, None, 40]),             # only the offender goes
    ([-5, 20], 2, [None, 20]),                             # negative is unusable
    (["x", 20], 2, [None, 20]),
    ("not-a-list", 2, [None, None]),
    (None, 3, [None, None, None]),
    ([10], 3, [10, None, None]),                           # short reply is padded
])
def test_salvage_keeps_the_good_anchors(ys, n, expected):
    assert c._salvage_ys(ys, n) == expected


def test_salvage_never_invents_a_boundary_for_a_tie():
    """[560, 560] means the model could not separate the pair. Splitting the difference would fabricate
    a cut it never saw -- the one thing the whole design forbids."""
    out = c._salvage_ys([560, 560], 2)
    assert out == [None, None]
    assert 560 not in [v for v in out if v is not None]


def test_page_is_retried_and_the_good_sample_wins(tmp_path, monkeypatch):
    """The dominant real failure was transient: two pages that each lost 6 answers passed 4/4 on
    re-sampling."""
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p)
    replies = [{"ys": [215, 45]}, {"ys": [45, 215]}]        # first rejected, second fine
    calls = []

    def _g(**kw):
        calls.append(kw)
        return json.dumps(replies[min(len(calls) - 1, len(replies) - 1)]), 10, 5

    monkeypatch.setattr(c, "generate", _g)
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert len(calls) == 2                                  # retried once, then stopped
    assert all(e["method"] == "band" for e in entries)      # and the good sample was used
    assert calls[0]["temperature"] == 0.0
    assert calls[1]["temperature"] > 0.0                    # retries warm up or they resample identically


def test_retries_are_bounded_and_tokens_accumulate(tmp_path, monkeypatch):
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p)
    calls = []
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [999, 999]}, calls))
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, i_tok, o_tok = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert len(calls) == 1 + c.MAX_RETRIES                  # bounded
    assert i_tok == 10 * len(calls) and o_tok == 5 * len(calls)   # every attempt is billed honestly
    assert all(e["method"] == "page" for e in entries)      # a tie still falls back, never guesses


def test_a_muddled_reply_is_discarded_not_half_used(tmp_path, monkeypatch):
    """Confidence gate: with 2 answers and [215, 45] one value contradicts the other, so keeping either
    is a coin flip. Salvage is for one bad apple, not for a reply the model muddled."""
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p)
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [215, 45]}))
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert all(e["method"] == "page" for e in entries)


def test_one_bad_anchor_no_longer_costs_the_whole_page(tmp_path, monkeypatch):
    """6 answers, 5 good values + 1 degenerate -> 5 tight crops, 1 fallback (was: 0 tight)."""
    p = str(tmp_path / "page_1.png")
    _synthetic_page(p, h=1200, bands=[(60 + i * 180, 150 + i * 180) for i in range(6)])
    ys = [50, 200, 350, 500, 650, 8 * 10 ** 200]
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": ys}))
    role = {"page": p, "page_index": 1, "starts": [f"Q{i}" for i in range(1, 7)], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    banded = [e["question_id"] for e in entries if e["method"] == "band"]
    assert len(banded) >= 4, f"expected most answers salvaged, got {banded}"
    assert "Q6" not in banded                                # the degenerate one still falls back
    assert next(e for e in entries if e["question_id"] == "Q6")["reason"] == "anchor rejected"


def test_two_continuations_on_one_page_both_get_crops(tmp_path, monkeypatch):
    """THE cross-page bug: `cont_qid = conts[0]` dropped every second continuing answer. Real sheets had
    1, 3 and 7 such pages."""
    p = str(tmp_path / "page_5.png")
    _synthetic_page(p, h=1200, bands=[(60, 300), (400, 700), (800, 1100)])
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [330, 1000]}))   # Q22 ends ~1/3 down
    role = {"page": p, "page_index": 5, "starts": [], "continuations": ["Q22", "Q23"]}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)

    assert {e["question_id"] for e in entries} == {"Q22", "Q23"}      # neither is dropped
    assert all(e["method"] == "band" for e in entries)
    q22 = next(e for e in entries if e["question_id"] == "Q22")
    q23 = next(e for e in entries if e["question_id"] == "Q23")
    assert q22["crop_file"] != q23["crop_file"]                       # genuinely different regions


def test_single_continuation_sharing_a_page_needs_no_call(tmp_path, monkeypatch):
    """Bounded already by the first start anchor, so a second call would buy nothing."""
    p = str(tmp_path / "page_10.png")
    _synthetic_page(p)
    calls = []
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [215]}, calls))
    role = {"page": p, "page_index": 10, "starts": ["Q24"], "continuations": ["Q33"]}
    c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert len(calls) == 1                                    # the starts call only
    assert "begin on this page" in calls[0]["parts"][0]["text"]


def test_a_failed_starts_call_no_longer_kills_the_continuation(tmp_path, monkeypatch):
    """A rejected starts reply used to send the WHOLE page to fallback, taking a continuation whose band
    (page-top -> first start) never depended on those coordinates."""
    p = str(tmp_path / "page_10.png")
    _synthetic_page(p)
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [999, 999]}))
    role = {"page": p, "page_index": 10, "starts": ["Q24", "Q34"], "continuations": ["Q33"]}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert {e["question_id"] for e in entries} == {"Q33", "Q24", "Q34"}
    q33 = next(e for e in entries if e["question_id"] == "Q33")
    assert q33["method"] == "band", "the continuation should still be cropped"


def test_a_blank_ruled_sliver_is_rejected_as_a_band(tmp_path, monkeypatch):
    """Row COUNT alone let two real crops through as 38px and 22px slivers of blank ruled paper: a
    curved rule survives the morphological strip and inks nearly every row of a thin band. The gate now
    also needs one row DENSE enough to be handwriting (measured: rules peak at 6-9% of page width,
    handwriting at 19-43%)."""
    h, w = 600, 400
    g = np.full((h, w), 250, dtype=np.uint8)
    for a, b in ((50, 120), (400, 470)):                    # two real answers
        for y in range(a, b - 4, 20):
            for x in range(40, w - 40, 26):
                g[y:min(y + 11, b), x:x + 11] = 20
    # A faint, slightly-curved rule through the middle gap -- ink on many rows, dense on none.
    for i, y in enumerate(range(250, 262)):
        g[y, 20 + i: w - 20 + i] = 90
    p = str(tmp_path / "page_1.png")
    Image.fromarray(g, mode="L").save(p)

    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [60, 420]}))
    role = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    entries, _, _ = c._process_page(role, str(tmp_path), str(tmp_path / "crops"), use_api=True)
    assert all(e["method"] == "band" for e in entries)       # the two real answers still crop tightly

    # Now aim a band squarely at the rule-only strip: it must NOT be published as a tight crop.
    monkeypatch.setattr(c, "generate", _mock_generate({"ys": [410, 425]}))
    role2 = {"page": p, "page_index": 1, "starts": ["Q1", "Q2"], "continuations": []}
    e2, _, _ = c._process_page(role2, str(tmp_path), str(tmp_path / "crops2"), use_api=True)
    thin = [e for e in e2 if e["question_id"] == "Q1"]
    assert thin and thin[0]["method"] == "page", "a rule-only sliver must fall back, not be shown"
