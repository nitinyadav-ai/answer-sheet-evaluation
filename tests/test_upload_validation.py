"""Pre-upload structural validation (scripts/upload_validation.py): the gate that catches input-side
causes of parse errors the moment a teacher uploads a question paper / answer key -- BEFORE the LLM
parse and BEFORE evaluation. Covers scan / no-text-layer detection, parsed-question checks (no
questions, missing marks), the marks-by-base model, and the full key<->paper cross-check (total
mismatch, shortfall, inflation, dropped, unknown). Offline: builds tiny real PDFs/DOCX locally."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import upload_validation as uv
except (ImportError, SystemExit) as e:  # pragma: no cover
    uv = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(uv is None, reason="upload_validation.py unavailable in this env")


def _codes(issues):
    return {i["code"] for i in issues}


def _sev(issues, code):
    return next(i["severity"] for i in issues if i["code"] == code)


# ---- file builders --------------------------------------------------------------------------------
def _image_pdf(path):
    """A 1-page image-only PDF (simulated scan) -- has NO text layer."""
    from PIL import Image
    Image.new("RGB", (800, 1000), "white").save(path, "PDF", resolution=100.0)
    return path


def _blank_pdf(path):
    import PyPDF2
    w = PyPDF2.PdfWriter()
    w.add_blank_page(width=595, height=842)
    with open(path, "wb") as f:
        w.write(f)
    return path


def _text_docx(path, n=25):
    import docx
    d = docx.Document()
    for i in range(n):
        d.add_paragraph(f"Question {i}. This paragraph carries real, selectable text content.")
    d.save(path)
    return path


# ---- RAW file (pre-parse) : scan / no-text detection ---------------------------------------------
def test_scanned_image_pdf_blocks(tmp_path):
    p = _image_pdf(os.path.join(str(tmp_path), "scan.pdf"))
    issues = uv.validate_raw_file(p, "answer key")
    assert _codes(issues) == {"no_text_layer"}
    assert uv.has_blocking(issues)


def test_blank_pdf_blocks(tmp_path):
    p = _blank_pdf(os.path.join(str(tmp_path), "blank.pdf"))
    assert "no_text_layer" in _codes(uv.validate_raw_file(p, "question paper"))


def test_text_docx_passes(tmp_path):
    p = _text_docx(os.path.join(str(tmp_path), "key.docx"))
    assert uv.validate_raw_file(p, "answer key") == []


def test_missing_file_blocks(tmp_path):
    issues = uv.validate_raw_file(os.path.join(str(tmp_path), "nope.pdf"), "answer key")
    assert _codes(issues) == {"missing_file"} and uv.has_blocking(issues)


def test_unsupported_type_blocks(tmp_path):
    p = os.path.join(str(tmp_path), "key.txt")
    open(p, "w").write("hello")
    assert _codes(uv.validate_raw_file(p, "answer key")) == {"unsupported_type"}


def test_json_upload_skips_raw_check(tmp_path):
    p = os.path.join(str(tmp_path), "key.json")
    open(p, "w").write("{}")
    assert uv.validate_raw_file(p, "answer key") == []


def test_raw_length_thresholds(tmp_path, monkeypatch):
    p = os.path.join(str(tmp_path), "x.pdf")
    open(p, "wb").write(b"%PDF-1.4")   # existence only; extraction is stubbed
    monkeypatch.setattr(uv, "_extract_raw_text", lambda _p: ("", 1))
    assert _codes(uv.validate_raw_file(p)) == {"no_text_layer"}
    monkeypatch.setattr(uv, "_extract_raw_text", lambda _p: ("only a little text", 1))
    assert _codes(uv.validate_raw_file(p)) == {"very_little_text"}
    assert _sev(uv.validate_raw_file(p), "very_little_text") == uv.WARNING
    monkeypatch.setattr(uv, "_extract_raw_text", lambda _p: ("x " * 300, 2))
    assert uv.validate_raw_file(p) == []


# ---- PARSED questions : no questions / missing marks ---------------------------------------------
def test_no_questions_blocks():
    issues = uv.validate_parsed_questions({}, "answer key")
    assert _codes(issues) == {"no_questions"} and uv.has_blocking(issues)


def test_missing_marks_minority_warns():
    qs = {"Q1": {"marks": 1}, "Q2": {"marks": 2}, "Q3": {"marks": 3}, "Q4": {"answer": "x"}}
    issues = uv.validate_parsed_questions(qs, "answer key")
    assert _codes(issues) == {"missing_marks"} and _sev(issues, "missing_marks") == uv.WARNING


def test_missing_marks_majority_blocks():
    qs = {"Q1": {"answer": "x"}, "Q2": {"answer": "y"}, "Q3": {"marks": 3}}
    issues = uv.validate_parsed_questions(qs, "answer key")
    assert _sev(issues, "missing_marks") == uv.ERROR and uv.has_blocking(issues)


def test_parsed_clean():
    assert uv.validate_parsed_questions({"Q1": {"marks": 1}, "Q2": {"marks": 2}}, "answer key") == []


# ---- marks-by-base model -------------------------------------------------------------------------
def test_qp_marks_by_base_uses_max():
    qp = {"Q8(a)": {"marks": 2}, "Q8(b)": {"marks": 2}, "Q1": {"marks": 1}}
    assert uv.qp_marks_by_base(qp) == {"8": 2.0, "1": 1.0}


def test_key_effective_choice_max_additive_sum():
    key = {"Q1": {"marks": 1},
           "Q34(a)": {"marks": 5}, "Q34(b)": {"marks": 5},          # choice -> 5
           "Q37(c)(i)": {"marks": 2}, "Q37(c)(ii)": {"marks": 2},   # choice -> 2
           "Q5(a)": {"marks": 2}, "Q5(b)": {"marks": 3}}            # additive -> 5
    choices = {"choice_groups": [
        {"parent": "Q34", "members": ["Q34(a)", "Q34(b)"], "required": 1},
        {"parent": "Q37", "members": ["Q37(c)(i)", "Q37(c)(ii)"], "required": 1}]}
    eff = uv.key_effective_marks_by_base(key, choices)
    assert eff == {"1": 1.0, "34": 5.0, "37": 2.0, "5": 5.0}


# ---- cross-check : key vs paper ------------------------------------------------------------------
def test_cross_shortfall_and_total():
    key = {"Q37": {"marks": 2}, "Q1": {"marks": 1}}
    qp = {"Q37": {"marks": 4}, "Q1": {"marks": 1}}
    issues = uv.cross_check(key, {"choice_groups": []}, qp)
    assert {"total_mismatch", "under_marked"} <= _codes(issues)
    assert all(i["severity"] == uv.WARNING for i in issues)      # warnings, not blocks


def test_cross_inflation():
    key = {"Q5": {"marks": 8}}
    qp = {"Q5": {"marks": 5}}
    assert {"total_mismatch", "over_marked"} <= _codes(uv.cross_check(key, {}, qp))


def test_cross_missing_question():
    key = {"Q1": {"marks": 1}}
    qp = {"Q1": {"marks": 1}, "Q40": {"marks": 4}}
    codes = _codes(uv.cross_check(key, {}, qp))
    assert "missing_questions" in codes and "total_mismatch" in codes


def test_cross_unknown_question():
    key = {"Q1": {"marks": 1}, "Q99": {"marks": 2}}
    qp = {"Q1": {"marks": 1}}
    assert "unknown_questions" in _codes(uv.cross_check(key, {}, qp))


def test_cross_clean_when_totals_and_questions_match():
    key = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    qp = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    assert uv.cross_check(key, {"choice_groups": []}, qp) == []


# ---- question-paper completeness (leading / internal numbering gaps) ------------------------------
def test_paper_structure_leading_gap_warns():
    # The exact live bug: the parser dropped the objective 'Section A', so numbering starts at Q9.
    qp = {f"Q{n}": {"marks": 1} for n in range(9, 38)}
    issues = uv.validate_question_paper_structure(qp)
    assert "paper_leading_gap" in _codes(issues)
    assert _sev(issues, "paper_leading_gap") == uv.WARNING       # warns, never blocks


def test_paper_structure_starts_at_one_clean():
    qp = {"Q1": {"marks": 1}, "Q2": {"marks": 2}, "Q3": {"marks": 3}}
    assert uv.validate_question_paper_structure(qp) == []


def test_paper_structure_internal_gap_warns():
    qp = {"Q1": {"marks": 1}, "Q2": {"marks": 1}, "Q5": {"marks": 1}}   # Q3, Q4 missing
    codes = _codes(uv.validate_question_paper_structure(qp))
    assert "paper_internal_gap" in codes and "paper_leading_gap" not in codes


def test_paper_structure_ignores_subparts_and_empty():
    # sub-parts collapse to their base (no false internal gap); no questions -> no warning
    qp = {"Q1": {"marks": 1}, "Q2(a)": {"marks": 2}, "Q2(b)": {"marks": 2}}
    assert uv.validate_question_paper_structure(qp) == []
    assert uv.validate_question_paper_structure({}) == []


def test_validate_question_paper_orchestrator_flags_leading_gap(tmp_path):
    import json
    qp = {f"Q{n}": {"marks": 1} for n in range(9, 12)}
    p = tmp_path / "qp.json"
    p.write_text(json.dumps(qp))                          # .json raw path -> raw check is skipped
    assert "paper_leading_gap" in _codes(uv.validate_question_paper(str(p), qp))


# ---- orchestrators -------------------------------------------------------------------------------
def test_validate_answer_key_without_paper_warns(tmp_path):
    p = _text_docx(os.path.join(str(tmp_path), "key.docx"))
    issues = uv.validate_answer_key(p, {"Q1": {"marks": 1}}, {"choice_groups": []}, qp_json=None)
    assert "no_question_paper" in _codes(issues) and not uv.has_blocking(issues)


def test_validate_for_evaluation_requires_paper():
    issues = uv.validate_for_evaluation({"Q1": {"marks": 1}}, {}, None)
    assert "no_question_paper" in _codes(issues) and uv.has_blocking(issues)


def test_validate_for_evaluation_clean_passes():
    key = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    qp = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    issues = uv.validate_for_evaluation(key, {"choice_groups": []}, qp)
    assert not uv.has_blocking(issues) and issues == []


# ---- compute_marks_mismatch (feeds the teacher's marks-source chooser) ---------------------------
def test_mismatch_detected_and_listed():
    key = {"Q34": {"marks": 10}, "Q1": {"marks": 1}}     # Q34 inflated (choice counted twice)
    qp = {"Q34": {"marks": 5}, "Q1": {"marks": 1}}
    mm = uv.compute_marks_mismatch(key, {"choice_groups": []}, qp)
    assert mm["mismatch"] is True
    assert mm["key_total"] == 11 and mm["qp_total"] == 6
    assert mm["recommended"] == "question_paper"
    assert {d["q"] for d in mm["per_question"]} == {"Q34"}
    assert mm["per_question"][0] == {"q": "Q34", "key": 10.0, "qp": 5.0}


def test_mismatch_clean_when_equal():
    key = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    qp = {"Q1": {"marks": 1}, "Q2": {"marks": 2}}
    mm = uv.compute_marks_mismatch(key, {"choice_groups": []}, qp)
    assert mm["mismatch"] is False and mm["per_question"] == []


def test_mismatch_recommends_key_when_paper_has_no_marks():
    key = {"Q1": {"marks": 1}}
    qp = {"Q1": {"answer": "no marks here"}}
    mm = uv.compute_marks_mismatch(key, {}, qp)
    assert mm["recommended"] == "answer_key"


# ---- lost choice sidecar : the silently-inflated key total ----------------------------------------
# A key whose parse dropped its `metadata` comes back with choice_groups == [], so every OR-alternative
# is counted ADDITIVELY and the key's total is overstated (measured: 106 instead of 80 on a real CBSE
# Science X key) -- with no signal at all when no question paper is present to cross-check against.
_LOST_KEY = {
    "Q1": {"marks": 1},                                              # no sub-parts -> never a candidate
    "Q22(a)": {"marks": 2}, "Q22(b)": {"marks": 2},                  # equal-marks OR pair
    "Q36(i)": {"marks": 1}, "Q36(ii)": {"marks": 1},                 # case study: OR sits in (iii) only,
    "Q36(iii)(a)": {"marks": 4}, "Q36(iii)(b)": {"marks": 4},        # so alternatives are UNEQUAL
}
_LOST_GROUPS = {"choice_groups": [
    {"parent": "Q22", "members": ["Q22(a)", "Q22(b)"], "required": 1},
    {"parent": "Q36", "members": ["Q36(iii)(a)", "Q36(iii)(b)"], "required": 1}]}


def test_ungrouped_choice_bases_silent_when_choices_parsed():
    """A key whose choices parsed is trusted as-is -- no second-guessing, so working keys are untouched."""
    assert uv.ungrouped_choice_bases(_LOST_KEY, _LOST_GROUPS) == []
    assert uv.choices_lost_issues(_LOST_KEY, _LOST_GROUPS) == []


def test_ungrouped_choice_bases_flags_a_lost_sidecar():
    assert uv.ungrouped_choice_bases(_LOST_KEY, {"choice_groups": []}) == ["22", "36"]


def test_ungrouped_choice_bases_detects_unequal_alternatives():
    """Regression: alternatives worth the SAME was measured and rejected as a filter -- a case study whose
    OR sits only in its last part scores [1, 1, 4] and would be missed (3 of 9 real choices on live data)."""
    case_study = {k: v for k, v in _LOST_KEY.items() if k.startswith("Q36")}
    assert uv.ungrouped_choice_bases(case_study, {}) == ["36"]


def test_ungrouped_choice_bases_treats_missing_and_empty_alike():
    """'file absent' and 'file empty' both reach here as a falsy dict -- neither may be read as 'no choices'."""
    got = [uv.ungrouped_choice_bases(_LOST_KEY, c)
           for c in (None, {}, {"choice_groups": []}, {"choice_groups": [], "inline_choice_ids": []})]
    assert got == [["22", "36"]] * 4


def test_ungrouped_choice_bases_ignores_questions_without_alternatives():
    """A key with no sub-part structure genuinely has no choices to lose -> silence, not noise."""
    assert uv.ungrouped_choice_bases({"Q1": {"marks": 1}, "Q2": {"marks": 2}}, {}) == []


def test_choices_lost_is_a_warning_and_never_blocks():
    issues = uv.choices_lost_issues(_LOST_KEY, {})
    assert _codes(issues) == {"choices_unavailable"}
    assert _sev(issues, "choices_unavailable") == uv.WARNING
    assert uv.has_blocking(issues) is False


def test_choices_lost_names_the_affected_questions():
    msg = uv.choices_lost_issues(_LOST_KEY, {})[0]["message"]
    assert "Q22" in msg and "Q36" in msg and "overstated" in msg


def test_lost_choices_does_not_alter_any_computed_total():
    """The zero-degradation guarantee: detection reports the risk, it never rewrites the arithmetic.
    With the choice data gone there is no way to know which alternative the paper offers."""
    before = uv.key_effective_marks_by_base(_LOST_KEY, {})
    uv.choices_lost_issues(_LOST_KEY, {})
    uv.ungrouped_choice_bases(_LOST_KEY, {})
    assert uv.key_effective_marks_by_base(_LOST_KEY, {}) == before
    assert sum(before.values()) == 15.0                      # the inflated additive sum, left intact
    assert sum(uv.key_effective_marks_by_base(_LOST_KEY, _LOST_GROUPS).values()) == 9.0   # true total


def test_validate_answer_key_flags_lost_choices_with_no_paper(tmp_path):
    """The blind spot: with no question paper, cross_check never runs and nothing else looks at this."""
    p = _text_docx(os.path.join(str(tmp_path), "key.docx"))
    issues = uv.validate_answer_key(p, _LOST_KEY, {"choice_groups": []}, qp_json=None)
    assert "choices_unavailable" in _codes(issues)


def test_validate_for_evaluation_flags_lost_choices():
    qp = {"Q22": {"marks": 2}, "Q36": {"marks": 6}, "Q1": {"marks": 1}}
    assert "choices_unavailable" in _codes(uv.validate_for_evaluation(_LOST_KEY, {}, qp))


def test_validate_answer_key_stays_quiet_when_choices_parsed(tmp_path):
    p = _text_docx(os.path.join(str(tmp_path), "key.docx"))
    issues = uv.validate_answer_key(p, _LOST_KEY, _LOST_GROUPS, qp_json=None)
    assert "choices_unavailable" not in _codes(issues)


def test_breakdown_exposes_choices_missing_for_the_ui():
    """The UI must be able to stop announcing an unverified additive sum as 'Marks verified'."""
    qp = {"Q22": {"marks": 2}, "Q36": {"marks": 6}, "Q1": {"marks": 1}}
    assert uv.build_marks_breakdown(_LOST_KEY, {}, qp)["choices_missing"] == ["22", "36"]
    assert uv.build_marks_breakdown(_LOST_KEY, _LOST_GROUPS, qp)["choices_missing"] == []
