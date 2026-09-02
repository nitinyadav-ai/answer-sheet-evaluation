"""An answer that is PRESENT but TRUNCATED -- the one capture failure no recovery layer could see.

Measured on Vinayak's Science sheet, Q37 (4 marks). The preprocessed page images are byte-identical
between two runs, yet the page carrying part (c) dropped that whole block in 1 of 3 controlled re-reads
(2 of 5 counting the real runs). Q37 went 688 -> 301 chars and lost 3.5 marks.

NOTHING reported it. Every existing recovery layer is gated on a question being BLANK -- full_evaluator's
`_recompute_gaps` counts a question as present when `str(answer).strip()` is non-empty -- so a
half-captured answer is invisible to `recover_gaps_by_position`, `repair_glued_answers`,
`_offtopic_rehome_hosts` and `reattach_leading_continuation` alike. The failing run was structurally
perfect: no gap, no orphan page, no out-of-set number, empty collision flags. It just stopped early.

The signal was already present and unused: the ANSWER KEY declares the question's sub-parts.

The subtlety that decides whether this is usable at all is the OR-choice key. "(a) ... OR (b) ..." means
the parts are ALTERNATIVES, and a student who answers one has omitted nothing. Intersecting the labels
across OR-segments keeps only what EVERY alternative demands. Measured on the real sheet that is the
difference between 5 flags and 1.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "vision-ocr", "scripts"))

import run_ocr as R  # noqa: E402

# Real key text from the sheet, trimmed. Q25 is an internal choice; Q37 repeats "(c)" either side of
# the OR. Keeping the genuine strings means these pin the real-world distinction, not a tidy invention.
KEY_Q25 = "(a) Each branch=R/3; 1/Rp=1/R1+1/R2+1/R3=9/R[1]; Rp=R/9[1] OR (b) Electric power = rate of..."
KEY_Q37 = ("(a) 2NaCl+2H2O -> 2NaOH+H2+Cl2[1]\n(b) Uses: NaOH-degreasing/soap[1]\n"
           "(c) (i) NaHCO3[1] (ii) Na2CO3[1]\nOR\n(c) (i) mild base[1] (ii) washing soda[1]")


# ---- label extraction ----------------------------------------------------------------------------

def test_reads_line_leading_top_level_labels():
    assert R._top_subparts("(a)\nfoo\n(b) bar\nc) baz") == {"a", "b", "c"}


@pytest.mark.parametrize("text", [
    "the value (a) is mid-sentence",          # not line-leading
    "(i)\n(ii)\n(iii)",                        # roman sub-enumerators, not top-level parts
    "A. Introduction\nB. Method",              # prose capitals, not CBSE part labels
    "(e)\n(f)",                                # beyond the (a)-(d) top-level range
])
def test_does_not_mistake_other_markers_for_top_level_parts(text):
    assert R._top_subparts(text) == set()


# ---- the OR-alternative distinction --------------------------------------------------------------

def test_or_alternatives_intersect_to_nothing():
    """'(a) ... OR (b) ...' -- answering one is complete. This is why 4 of 5 real flags were wrong."""
    assert R._key_subparts_by_base({"Q25": {"answer": KEY_Q25}}).get(25, set()) == set()


def test_a_part_required_by_every_alternative_survives():
    """Q37 repeats '(c)' on both sides of the OR, so '(c)' is required whichever branch is answered --
    and '(c)' is exactly the part the bad read dropped."""
    assert "c" in R._key_subparts_by_base({"Q37": {"answer": KEY_Q37}}).get(37, set())


def test_a_key_with_no_choice_requires_all_its_parts():
    assert R._key_subparts_by_base({"Q29": {"answer": "(a) x\n(b) y\n(c) z"}})[29] == {"a", "b", "c"}


def test_bare_word_or_inside_prose_is_not_a_choice_separator():
    key = {"Q7": {"answer": "(a) acids or bases turn litmus\n(b) salts are neutral"}}
    assert R._key_subparts_by_base(key)[7] == {"a", "b"}


# ---- detection -----------------------------------------------------------------------------------

def test_flags_an_answer_missing_a_required_part():
    got = R.incomplete_answers({"Q37": {"answer": "(a) foo\n(b) bar"}}, {37: {"a", "b", "c"}})
    assert got == {"Q37": {"c"}}


def test_does_not_flag_a_complete_answer():
    assert R.incomplete_answers({"Q37": {"answer": "(a)\n(b)\n(c)"}}, {37: {"a", "b", "c"}}) == {}


def test_does_not_flag_when_the_key_requires_nothing():
    """The OR case, end to end: no required labels -> the question is never considered."""
    assert R.incomplete_answers({"Q25": {"answer": "(b) only this branch"}}, {25: set()}) == {}


def test_ignores_the_instructions_pseudo_entry():
    assert R.incomplete_answers({"_instructions_": ["do X"]}, {1: {"a", "b"}}) == {}


# ---- SUBJECT-PREFIXED ids: what assemble_answers actually emits -------------------------------------
#
# These exist because the first cut used re.search(r'(\d{1,3})') for the base number. On the real
# pipeline's ids that returns the PREFIX digits -- 'AI10_Q37' -> 10 -- so every question was checked
# against the wrong key entry on every real run. Bare-'Q37' tests passed throughout; only running the
# live stage exposed it.

@pytest.mark.parametrize("qid,base", [
    ("AI10_Q37", 37), ("SCI10_Q8", 8), ("COMP12_Q22", 22), ("Q37", 37), ("Q31(a)", 31),
])
def test_base_number_survives_a_subject_prefix(qid, base):
    key = {qid: {"answer": "(a) x\n(b) y\n(c) z"}}
    assert R._key_subparts_by_base(key) == {base: {"a", "b", "c"}}


def test_detection_works_on_the_ids_the_pipeline_really_emits():
    """The end-to-end shape: prefixed key AND prefixed capture, as assemble_answers produces them."""
    key = R._key_subparts_by_base({"AI10_Q37": {"answer": "(a) x\n(b) y\n(c) z"}})
    got = R.incomplete_answers({"AI10_Q37": {"answer": "(a) one\n(b) two"}}, key)
    assert got == {"AI10_Q37": {"c"}}


def test_a_prefix_digit_cannot_be_mistaken_for_the_question():
    """'AI10_Q37' must not be read as question 10 -- Q10 is a different question with a different key."""
    key = R._key_subparts_by_base({"AI10_Q37": {"answer": "(a) x\n(b) y"}})
    assert 10 not in key and 37 in key


# ---- recovery ------------------------------------------------------------------------------------

GOOD = "[START_Q: 37]\n(a) one\n(b) two\n(c) three is here\n[END_Q: 37]"
TRUNCATED = "[START_Q: 37]\n(a) one\n(b) two\n[END_Q: 37]"


def _results(text):
    return [{"index": 0, "text": text, "image_path": "/p/page_1.png", "rotation": 0, "error": None}]


def _page_mapping():
    return {"/p/page_1.png": [{"question_id": "Q37", "image": "page_1.png"}]}


def _run(monkeypatch, reads):
    """Drive recovery with `reads` served in order by a stubbed process_page (no network)."""
    seq = list(reads)
    monkeypatch.setattr(R, "process_page",
                        lambda *a, **k: {"index": 0, "text": seq.pop(0), "image_path": "/p/page_1.png",
                                         "rotation": 0, "error": None})
    return R.recover_incomplete_answers(_results(TRUNCATED), {"Q37": {"answer": "(a) one\n(b) two"}},
                                        _page_mapping(), {37: {"a", "b", "c"}}, "PROMPT")


def test_a_better_read_replaces_the_truncated_page(monkeypatch):
    results, improved, targeted = _run(monkeypatch, [GOOD])
    assert improved == [0]
    assert "three is here" in results[0]["text"]
    assert targeted == {"Q37": {"c"}}


def test_a_re_read_that_still_lacks_the_part_is_rejected(monkeypatch):
    results, improved, _ = _run(monkeypatch, [TRUNCATED + "\nx", TRUNCATED + "\ny"])
    assert improved == []
    assert results[0]["text"] == TRUNCATED, "the original capture must stand"


def test_a_shorter_re_read_is_rejected_even_if_it_has_the_part(monkeypatch):
    """A re-read is a fresh sample of a non-deterministic model: finding (c) while losing more
    elsewhere is not an improvement."""
    _results_, improved, _ = _run(monkeypatch, ["(c)"])
    assert improved == []


def test_it_stops_re_reading_once_the_part_is_recovered(monkeypatch):
    calls = []

    def _fake(*a, **k):
        calls.append(1)
        return {"index": 0, "text": GOOD, "image_path": "/p/page_1.png", "rotation": 0, "error": None}

    monkeypatch.setattr(R, "process_page", _fake)
    R.recover_incomplete_answers(_results(TRUNCATED), {"Q37": {"answer": "(a) one\n(b) two"}},
                                 _page_mapping(), {37: {"a", "b", "c"}}, "PROMPT")
    assert len(calls) == 1, "must not keep paying for re-reads after the gap is closed"


def test_nothing_is_re_read_when_every_answer_is_complete(monkeypatch):
    monkeypatch.setattr(R, "process_page",
                        lambda *a, **k: pytest.fail("a healthy run must not re-read any page"))
    results, improved, targeted = R.recover_incomplete_answers(
        _results(GOOD), {"Q37": {"answer": "(a)\n(b)\n(c)"}}, _page_mapping(),
        {37: {"a", "b", "c"}}, "PROMPT")
    assert improved == [] and targeted == {}


def test_a_failing_re_read_never_takes_the_stage_down(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(R, "process_page", _boom)
    results, improved, targeted = R.recover_incomplete_answers(
        _results(TRUNCATED), {"Q37": {"answer": "(a) one\n(b) two"}}, _page_mapping(),
        {37: {"a", "b", "c"}}, "PROMPT")
    assert improved == [] and results[0]["text"] == TRUNCATED
    assert targeted == {"Q37": {"c"}}, "the gap is still reported even though repair failed"


def test_retries_are_bounded(monkeypatch):
    calls = []

    def _fake(*a, **k):
        calls.append(1)
        return {"index": 0, "text": TRUNCATED, "image_path": "/p/page_1.png", "rotation": 0,
                "error": None}

    monkeypatch.setattr(R, "process_page", _fake)
    R.recover_incomplete_answers(_results(TRUNCATED), {"Q37": {"answer": "(a) one\n(b) two"}},
                                 _page_mapping(), {37: {"a", "b", "c"}}, "PROMPT", max_retries=2)
    assert len(calls) == 2


# ---- wiring --------------------------------------------------------------------------------------

def test_the_layer_is_off_unless_a_key_is_supplied():
    """No key -> byte-for-byte the OCR behaviour that shipped before this layer existed."""
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "if _answer_key and str(os.environ.get(\"OCR_COMPLETENESS\"" in src


# ---- the non-degradation veto ---------------------------------------------------------------------
#
# The safety-critical piece: a re-read is a fresh sample of a non-deterministic model, so it can be
# better in one place and worse in another. For a grading tool, a repair that quietly shortens another
# answer costs a student marks. Tested behaviourally -- an earlier version of these tests only grepped
# the source, and a mutation that disabled the gate outright sailed through.

def test_a_repair_that_shortens_another_answer_is_vetoed():
    before = {"Q37": {"answer": "(a)(b)"}, "Q12": {"answer": "a full answer here"}}
    after = {"Q37": {"answer": "(a)(b)(c) restored"}, "Q12": {"answer": "short"}}
    assert R.answers_shortened_by(before, after) == ["Q12"]


def test_a_purely_additive_repair_passes_the_veto():
    before = {"Q37": {"answer": "(a)(b)"}, "Q12": {"answer": "unchanged"}}
    after = {"Q37": {"answer": "(a)(b)(c) restored"}, "Q12": {"answer": "unchanged"}}
    assert R.answers_shortened_by(before, after) == []


def test_an_answer_vanishing_entirely_counts_as_shortened():
    assert R.answers_shortened_by({"Q5": {"answer": "text"}}, {}) == ["Q5"]


def test_the_veto_ignores_the_instructions_pseudo_entry():
    assert R.answers_shortened_by({"_instructions_": ["x", "y"]}, {}) == []


def test_the_commit_decision_rejects_a_degrading_repair():
    before = {"Q37": {"answer": "(a)(b)"}, "Q12": {"answer": "a full answer here"}}
    after = {"Q37": {"answer": "(a)(b)(c) restored"}, "Q12": {"answer": "short"}}
    accept, fixed, shrunk = R.commit_completeness_repair(before, after, {"Q37": {"c"}})
    assert accept is False and fixed == [] and shrunk == ["Q12"]


def test_the_commit_decision_accepts_and_names_what_it_fixed():
    before = {"Q37": {"answer": "(a)(b)"}, "Q12": {"answer": "unchanged"}}
    after = {"Q37": {"answer": "(a)(b)(c) restored"}, "Q12": {"answer": "unchanged"}}
    accept, fixed, shrunk = R.commit_completeness_repair(before, after, {"Q37": {"c"}})
    assert accept is True and fixed == ["Q37"] and shrunk == []


def test_a_targeted_answer_that_did_not_grow_is_not_reported_as_fixed():
    """It was flagged and re-read, but nothing came back -- the student likely skipped that part."""
    same = {"Q37": {"answer": "(a)(b)"}}
    accept, fixed, _ = R.commit_completeness_repair(same, same, {"Q37": {"c"}})
    assert accept is True and fixed == []


def test_the_repair_is_wired_to_the_commit_decision():
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "_accept, _fixed, _shrunk = commit_completeness_repair(ocr_answers_json, _a2, _targeted)" in src
    assert "Keeping the original capture." in src


def test_a_restored_answer_is_raised_for_review():
    src = open(os.path.join(ROOT, "skills/vision-ocr/scripts/run_ocr.py")).read()
    assert "recovery_flags.json" in src and "stopped early" in src


def test_the_orchestrator_passes_the_key_to_ocr():
    src = open(os.path.join(ROOT, "scripts", "full_evaluator.py")).read()
    assert 'ocr_cmd += ["--answer-key-file", answer_key_path]' in src


def test_the_prompt_forbids_numbering_a_sub_part():
    """The model tagged Q37's '(c)' as '[START_Q: 8]'; it only landed correctly because the collision
    handler re-homed it. Working by accident is not working."""
    assert "is sub-part c of the question already in progress, not question 8" in R.MAIN_PROMPT


def test_the_prompt_demands_every_block_on_the_page():
    assert "TRANSCRIBE EVERY BLOCK OF HANDWRITING ON THE PAGE" in R.MAIN_PROMPT
