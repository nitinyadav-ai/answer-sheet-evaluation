"""OCR reconcile pass (offline, no network): verify_code_regions -> reconcile_answers.

A fake _ocr_generate dispatches on the prompt to serve queued re-read / arbiter outputs, so we exercise
the real agreement-gate + word/tag invariants + splice logic without any model call. The safety guarantee
under test: a correction may change symbols/digits/superscripts but NEVER a word or a tag, the arbiter
NEVER fires on a token both passes already read the same (so a genuine student bug can't be "fixed"), and
anything uncertain degrades to today's flag-only Needs-Review behaviour.
"""
import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "vision-ocr", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))

try:
    import run_ocr
except (ImportError, SystemExit):  # pragma: no cover
    run_ocr = None

pytestmark = pytest.mark.skipif(run_ocr is None, reason="run_ocr unavailable")


class FakeOCR:
    """Stands in for run_ocr._ocr_generate. Dispatches by prompt marker to a per-kind FIFO queue."""

    def __init__(self):
        self.code, self.math, self.arb = [], [], []
        self.calls = {"code": 0, "math": 0, "arb": 0}
        self._lock = threading.Lock()          # reconcile now runs its per-answer calls concurrently

    def __call__(self, prompt_text, image_path, context_image=None):
        with self._lock:                       # keep the queues + counters deterministic under threads
            if "OCR arbiter" in prompt_text:
                self.calls["arb"] += 1
                return (self.arb.pop(0) if self.arb else ""), 1, 1
            if "strict mathematics transcription" in prompt_text:
                self.calls["math"] += 1
                return (self.math.pop(0) if self.math else "[NO MATH]"), 1, 1
            if "strict code transcription" in prompt_text:
                self.calls["code"] += 1
                return (self.code.pop(0) if self.code else "[NO CODE]"), 1, 1
            return "", 1, 1


@pytest.fixture
def fake(monkeypatch):
    f = FakeOCR()
    monkeypatch.setattr(run_ocr, "_ocr_generate", f)
    monkeypatch.setenv("OCR_VERIFY_CODE", "1")
    monkeypatch.setenv("OCR_VERIFY_MATH", "1")
    monkeypatch.setenv("OCR_ARBITRATE", "1")
    return f


def _img(tmp_path, name="p1.png"):
    p = tmp_path / name
    p.write_bytes(b"x")           # only existence matters; the fake never opens it
    return str(p)


def _run(entry, images):
    j = {"Q1": entry}
    p, c = run_ocr.verify_code_regions(j, {"Q1": images})
    return j["Q1"], p, c


# ------------------------- CODE -------------------------

def test_code_agree_no_arbiter(fake, tmp_path):
    fake.code = ["def f(x):\n    return x"]
    e, _, _ = _run({"answer": "[CODE: def f(x):\n    return x]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: def f(x):\n    return x]"
    assert not e.get("is_bad_handwriting")


def test_code_hyphen_reconciled(fake, tmp_path):
    fake.code = ["emp_id = 5"]
    fake.arb = ["emp_id = 5"]
    e, _, _ = _run({"answer": "[CODE: emp-id = 5]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 1
    assert e["answer"] == "[CODE: emp_id = 5]"
    assert e.get("ocr_reconciled") is True
    assert not e.get("is_bad_handwriting")
    assert "code_symbol_warning" not in e


def test_code_agreed_bug_never_arbitrated(fake, tmp_path):
    # Both passes read the same (wrong) identifier -> the arbiter must NOT fire and "fix" it.
    fake.code = ["rang(n)"]
    fake.arb = ["range(n)"]
    e, _, _ = _run({"answer": "[CODE: rang(n)]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: rang(n)]"
    assert fake.arb == ["range(n)"]          # arbiter queue untouched


def test_code_word_change_rejected_and_flagged(fake, tmp_path):
    # Passes disagree on a WORD -> arbiter fires but word-invariance rejects it -> flag, keep primary.
    fake.code = ["range(n)"]
    fake.arb = ["range(n)"]
    e, _, _ = _run({"answer": "[CODE: rang(n)]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 1
    assert e["answer"] == "[CODE: rang(n)]"
    # Reported on the SYMBOL channel, not as illegible handwriting: the two passes read different
    # symbols, which says nothing about whether the writing was legible.
    assert e.get("code_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_code_unbalanced_arbiter_rejected(fake, tmp_path):
    fake.code = ["x = [1,2]"]
    fake.arb = ["x = [1,2"]                   # missing ] -> unbalanced when wrapped
    e, _, _ = _run({"answer": "[CODE: x = (1,2)]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "[CODE: x = (1,2)]"
    assert e.get("code_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_code_nested_brackets_extracted(fake, tmp_path):
    fake.code = ["return arr[i] - 1"]
    e, _, _ = _run({"answer": "[CODE: return arr[i] - 1]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: return arr[i] - 1]"


def test_code_multiblock_flag_only(fake, tmp_path):
    fake.code = ["a=1\nb=2"]
    e, _, _ = _run({"answer": "[CODE: a=1] and [CODE: b-2]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0             # 2 blocks -> ineligible for splice
    assert e["answer"] == "[CODE: a=1] and [CODE: b-2]"
    assert e.get("code_symbol_warning")          # b-2 hyphen-in-identifier still reported...
    assert not e.get("is_bad_handwriting")       # ...but not as illegible handwriting


def test_code_unterminated_flag_only(fake, tmp_path):
    fake.code = ["x = 1"]
    e, _, _ = _run({"answer": "[CODE: x = 1", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: x = 1"


def test_code_multipage_flag_only(fake, tmp_path):
    fake.code = ["emp_id", "emp_id"]
    e, _, _ = _run({"answer": "[CODE: emp-id]", "is_bad_handwriting": False},
                   [_img(tmp_path, "p1.png"), _img(tmp_path, "p2.png")])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: emp-id]"
    assert e.get("code_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_code_prose_hyphen_no_false_flag(fake, tmp_path):
    # The old whole-answer count flagged any code answer containing a hyphenated English word. Per-block
    # counting fixes it.
    fake.code = ["x = 1"]
    e, _, _ = _run({"answer": "This is well-known.\n[CODE: x = 1]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert not e.get("is_bad_handwriting")
    assert "code_symbol_warning" not in e


# ------------------------- MATH -------------------------

def test_math_superscript_applied(fake, tmp_path):
    fake.math = ["x^2 = 16"]
    fake.arb = ["x^2 = 16"]
    e, _, _ = _run({"answer": "x2 = 16", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "x^2 = 16"
    assert e.get("ocr_reconciled") is True


def test_math_relation_applied(fake, tmp_path):
    fake.math = ["y ≈ 3.14"]
    fake.arb = ["y ≈ 3.14"]
    e, _, _ = _run({"answer": "y = 3.14", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "y ≈ 3.14"


def test_math_prose_line_protected(fake, tmp_path):
    # Arbiter fixes the equation line AND paraphrases the prose line; only the math line is applied and
    # the prose paraphrase is ignored (not applied, not flagged).
    fake.math = ["x^2 + y^2 = r^2"]
    fake.arb = ["x^2 + y^2 = r^2\nSo the shape is round"]
    e, _, _ = _run({"answer": "x2 + y2 = r2\nSo the circle is round", "is_bad_handwriting": False}, [_img(tmp_path)])
    lines = e["answer"].split("\n")
    assert lines[0] == "x^2 + y^2 = r^2"
    assert lines[1] == "So the circle is round"
    assert e.get("ocr_reconciled") is True
    assert not e.get("is_bad_handwriting")


def test_math_function_word_change_flagged(fake, tmp_path):
    fake.math = ["cos x = 0.5"]
    fake.arb = ["cos x = 0.5"]                # would change sin -> cos (a word) -> rejected
    e, _, _ = _run({"answer": "sin x = 0.5", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "sin x = 0.5"
    assert e.get("math_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_math_preserves_diagram_tag(fake, tmp_path):
    fake.math = ["x^2 = 4"]
    fake.arb = ["x^2 = 4\n[DIAGRAM: a parabola]"]
    e, _, _ = _run({"answer": "x2 = 4\n[DIAGRAM: a parabola]", "is_bad_handwriting": False}, [_img(tmp_path)])
    lines = e["answer"].split("\n")
    assert lines[0] == "x^2 = 4"
    assert lines[1] == "[DIAGRAM: a parabola]"


def test_math_off_no_change(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_VERIFY_MATH", "0")
    fake.math = ["x^2 = 4"]
    e, _, _ = _run({"answer": "x2 = 4", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "x2 = 4"
    assert fake.calls["math"] == 0


# ------------------------- gates / plumbing -------------------------

def test_master_off_flag_only(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_ARBITRATE", "0")
    fake.code = ["emp_id = 5"]
    e, _, _ = _run({"answer": "[CODE: emp-id = 5]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0             # arbiter never fires
    assert e["answer"] == "[CODE: emp-id = 5]"   # text unchanged (flag-only, today's behaviour)
    assert e.get("code_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_instructions_and_nondict_skipped(fake, tmp_path):
    j = {"_instructions_": ["do not grade"], "Q1": {"answer": "hi there", "is_bad_handwriting": False}}
    run_ocr.verify_code_regions(j, {})
    assert j["_instructions_"] == ["do not grade"]
    assert j["Q1"]["answer"] == "hi there"


def test_tokens_summed(fake, tmp_path):
    fake.code = ["emp_id = 5"]
    fake.arb = ["emp_id = 5"]
    _, p, c = _run({"answer": "[CODE: emp-id = 5]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert p == 2 and c == 2                  # code re-read (1,1) + arbiter (1,1)


# ------------------------- tag_spans parity with the renderer's scanner -------------------------

def test_tag_spans_parity_with_split_on_code():
    from tag_utils import tag_spans
    try:
        import evaluate as ev
    except (ImportError, SystemExit):
        pytest.skip("evaluate unavailable")

    def pieces(s):
        out, i = [], 0
        for sp in tag_spans(s):
            if sp["start"] > i:
                out.append(("text", s[i:sp["start"]]))
            out.append(("code" if sp["name"] == "CODE" else "diagram", sp["inner"].strip()))
            i = sp["end"]
        if i < len(s):
            out.append(("text", s[i:]))
        return out

    for s in [
        "plain text no tags",
        "[CODE: return arr[i] - 1]",
        "before [CODE: a=1] mid [DIAGRAM: circle] after",
        "[CODE: x = [1,2]] tail",
        "unterminated [CODE: x = 1",
        "",
        "[DIAGRAM: a]\nnext line",
        "text [CODE: for i in range(n): a[i]+=1] end",
    ]:
        assert pieces(s) == ev._split_on_code(s), repr(s)


# ------------------------- parallel reconcile (per-answer concurrency + memoised re-reads) -----------

def test_reconcile_parallel_shared_page_reread_once(fake, tmp_path):
    # Two math answers on the SAME page: the page is re-read exactly ONCE (Future-memoised across the
    # parallel workers), not once per answer, yet both answers still reconcile independently.
    img = _img(tmp_path)
    fake.math = ["x^2 = 16"]                         # a SINGLE queued page re-read
    fake.arb = ["x^2 = 16", "x^2 = 16"]             # one arbiter per answer (not memoised)
    j = {"Q1": {"answer": "x2 = 16", "is_bad_handwriting": False},
         "Q2": {"answer": "x2 = 16", "is_bad_handwriting": False}}
    run_ocr.verify_code_regions(j, {"Q1": [img], "Q2": [img]})
    assert fake.calls["math"] == 1                   # page re-read ONCE despite two answers (memo dedup)
    assert fake.calls["arb"] == 2                    # arbiter still fires per answer
    assert j["Q1"]["answer"] == "x^2 = 16"
    assert j["Q2"]["answer"] == "x^2 = 16"


def test_reconcile_parallel_many_answers_all_applied(fake, tmp_path):
    # N answers each on its OWN page all reconcile under the thread pool; summing is order-independent and
    # the token total is intact (n re-reads + n arbiters), proving no work is dropped or double-counted.
    n = 6
    imgs = [_img(tmp_path, f"p{i}.png") for i in range(n)]
    fake.math = ["x^2 = 16"] * n
    fake.arb = ["x^2 = 16"] * n
    j = {f"Q{i}": {"answer": "x2 = 16", "is_bad_handwriting": False} for i in range(n)}
    qmap = {f"Q{i}": [imgs[i]] for i in range(n)}
    p, c = run_ocr.verify_code_regions(j, qmap)
    assert fake.calls["math"] == n                   # each distinct page re-read exactly once
    assert all(j[f"Q{i}"]["answer"] == "x^2 = 16" for i in range(n))
    assert p == 2 * n and c == 2 * n                 # n re-reads (1,1) + n arbiters (1,1), summed intact


# ------------------------- answer-scoped agreement + no more give-up flags -------------------------
# Regression cover for the over-flagging bug: 66% of math-bearing answers (86 of 131) were reported as
# "Illegible handwriting". The gate compared THIS answer's math lines against a re-read of the WHOLE
# PAGE and demanded string EQUALITY, so it could never pass on a page holding more than one answer --
# measured, that was every one of the 38 archived math flags. Everything then fell through to two
# give-up branches ("multi-page" and "arbiter line count differs"), which flagged without evidence:
# 37 of the 38. None of these tests may pass by making the reconciler simply do nothing -- each pins
# either a correction that still happens or a flag that still fires.

def test_page_superset_counts_as_agreement(fake, tmp_path):
    """THE root-cause test. The page re-read also contains a NEIGHBOURING answer's math; this answer's
    own lines appear verbatim inside it, so nothing is wrong and nothing may be flagged."""
    fake.math = ["y^3 = 27\nx^2 = 16\nz = 5"]        # whole page: three equations, ours in the middle
    e, _, _ = _run({"answer": "x^2 = 16", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0                    # confirmed -> no arbiter, no cost
    assert e["answer"] == "x^2 = 16"
    assert not e.get("is_bad_handwriting")
    assert not e.get("math_symbol_warning")


def test_real_difference_inside_a_superset_still_arbitrates(fake, tmp_path):
    """The containment gate must not become a rubber stamp: a line NOT present in the page re-read is
    still unconfirmed and still gets arbitrated + corrected."""
    fake.math = ["y^3 = 27\nx^2 = 16"]               # page has x^2; the answer says x2
    fake.arb = ["x^2 = 16"]
    e, _, _ = _run({"answer": "x2 = 16", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 1
    assert e["answer"] == "x^2 = 16"                 # symbol correction still applied
    assert not e.get("is_bad_handwriting")


def test_multipage_answer_is_arbitrated_not_abandoned(fake, tmp_path):
    """Was 15 of the 38 archived flags: a 2-page answer refused arbitration and was called illegible.
    Now each page is arbitrated in turn and the correction lands."""
    imgs = [_img(tmp_path, "p1.png"), _img(tmp_path, "p2.png")]
    fake.math = ["nothing here", "also nothing"]     # neither page confirms the line
    fake.arb = ["x^2 = 16", "x^2 = 16"]
    e, _, _ = _run({"answer": "x2 = 16", "is_bad_handwriting": False}, imgs)
    assert fake.calls["arb"] >= 1
    assert e["answer"] == "x^2 = 16"
    assert not e.get("is_bad_handwriting")


def test_arbiter_line_count_mismatch_is_silent(fake, tmp_path):
    """Was 23 of the 38 archived flags. A differing line count means "could not align", not "the
    student's writing is unclear" -> apply nothing, say nothing."""
    fake.math = ["something else entirely"]
    fake.arb = ["x^2 = 16\nAN EXTRA LINE"]           # 2 lines vs the answer's 1
    e, _, _ = _run({"answer": "x2 = 16", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "x2 = 16"                  # untouched
    assert not e.get("is_bad_handwriting")
    assert not e.get("math_symbol_warning")


def test_unresolvable_line_reports_on_the_symbol_channel(fake, tmp_path):
    """The ONE genuine signal: the arbiter contradicts a line and the word invariant refuses to apply
    it. Still surfaced -- but as a symbol disagreement, never as illegible handwriting."""
    fake.math = ["cos x = 0.5"]
    fake.arb = ["cos x = 0.5"]                       # sin -> cos is a WORD change -> rejected
    e, _, _ = _run({"answer": "sin x = 0.5", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "sin x = 0.5"
    assert e.get("math_symbol_warning")
    assert not e.get("is_bad_handwriting")


def test_empty_arbiter_line_can_never_blank_out_an_equation(fake, tmp_path):
    """Data-loss guard. _WORD_RE only matches runs of >=2 letters, so "x = 5" has an EMPTY word
    multiset -- and so does "". Without an explicit content guard an empty arbiter line satisfies the
    word/tag invariant and WIPES the student's working."""
    fake.math = ["something else"]
    fake.arb = [""]
    e, _, _ = _run({"answer": "x = 5", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "x = 5"
    assert not e.get("is_bad_handwriting")
    # An arbiter that returned NOTHING is a failed call, not a finding about the student's work, so it
    # must also stay silent -- otherwise every dropped arbiter response becomes a review item.
    assert not e.get("math_symbol_warning")


def test_truncated_arbiter_line_is_rejected(fake, tmp_path):
    fake.math = ["something else"]
    fake.arb = ["x ="]                               # lost more than half the line
    e, _, _ = _run({"answer": "x = 5 + 3 - 2 + 100", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert e["answer"] == "x = 5 + 3 - 2 + 100"
    assert e.get("math_symbol_warning")              # wholesale loss counts as unresolved...
    assert not e.get("is_bad_handwriting")           # ...but is still not a handwriting verdict


def test_reconcilers_never_touch_the_handwriting_flag(fake, tmp_path):
    """`is_bad_handwriting` belongs to the OCR model's [BAD_HANDWRITING] marker alone. The reconcilers
    must neither set it nor clear it -- an answer the model DID call illegible stays illegible."""
    fake.math = ["cos x = 0.5"]
    fake.arb = ["cos x = 0.5"]
    e, _, _ = _run({"answer": "sin x = 0.5", "is_bad_handwriting": True}, [_img(tmp_path)])
    assert e["is_bad_handwriting"] is True           # preserved, not cleared
    assert e.get("math_symbol_warning")


def test_code_block_confirmed_inside_a_page_superset(fake, tmp_path):
    """Same superset fix on the code path: the page re-read carries another answer's code too."""
    fake.code = ["def other():\n    pass\nemp_id = 5"]
    e, _, _ = _run({"answer": "[CODE: emp_id = 5]", "is_bad_handwriting": False}, [_img(tmp_path)])
    assert fake.calls["arb"] == 0
    assert e["answer"] == "[CODE: emp_id = 5]"
    assert not e.get("code_symbol_warning")


# ------------------------- the symbol sidecar -------------------------

def test_symbol_flags_sidecar_written_and_scoped(tmp_path):
    j = {"_instructions_": ["x"],
         "Q7": {"answer": "a", "math_symbol_warning": "math note"},
         "Q8": {"answer": "b", "code_symbol_warning": "code note"},
         "Q9": {"answer": "c"}}
    flags = run_ocr.write_symbol_flags(str(tmp_path), j)
    assert flags == {"7": "math note", "8": "code note"}      # keyed by BASE number; clean answers absent
    import json as _json
    with open(tmp_path / run_ocr.SYMBOL_FLAGS_FILE) as f:
        assert _json.load(f) == flags


def test_symbol_flags_sidecar_absent_when_nothing_to_say(tmp_path):
    assert run_ocr.write_symbol_flags(str(tmp_path), {"Q1": {"answer": "clean"}}) == {}
    assert not (tmp_path / run_ocr.SYMBOL_FLAGS_FILE).exists()


def test_question_set_warning_is_not_a_handwriting_verdict():
    """full_evaluator used to set is_bad_handwriting purely as a "route this to review" lever, so a
    misread question NUMBER was reported to the teacher as illegible handwriting."""
    import full_evaluator as fe
    ocr = {"Q1": {"answer": "a"}, "Q99": {"answer": "b"}}
    out, gaps = fe.reconcile_ocr_to_question_set(ocr, [1, 2])
    assert out["Q99"]["question_set_warning"]
    assert not out["Q99"].get("is_bad_handwriting")   # the finding travels on its own channel
    assert not out["Q1"].get("question_set_warning")  # in-set questions untouched
    assert gaps == [2]


def test_symbol_flag_reader_is_wired_into_grading():
    """_apply_symbol_flags is called from main(), which cannot be invoked in a unit test. Guard the
    call site directly so the channel can't be silently unhooked."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert "results_ordered = _apply_symbol_flags(results_ordered, ocr_path)" in src


def test_symbol_sidecar_is_written_by_the_ocr_run():
    """Same for write_symbol_flags, which is called from run_ocr.main()."""
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "write_symbol_flags(args.output_dir, ocr_answers_json)" in src
