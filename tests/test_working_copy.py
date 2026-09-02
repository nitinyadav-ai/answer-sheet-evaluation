"""Working-copy model (review_corrections) — offline / no network / no LLM.

These lock the persistence contract that makes teacher edits durable and non-degrading:
  - load_working_state prefers review_render.json (the teacher working copy) over the pristine
    review_state.json; ensure_working_copy seeds the working copy lazily + stamps Machine Marks and
    is idempotent (never clobbers later edits);
  - apply_decisions records BOTH accept and reject, clamps, reverts an override to the machine
    baseline on accept, never mutates its input, and preserves un-decided questions verbatim;
  - COMPOSITION: a correction and a single-answer regrade (each writing review_render.json) never
    overwrite each other, in either order — the latent data-loss bug this change fixes.
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import review_corrections as rc  # noqa: E402


def _ev(qid, awarded, mx, **extra):
    d = {"Marks Awarded": awarded, "Maximum Marks": mx,
         "Student Wrote": f"ans {qid}", "Justification": f"just {qid}"}
    d.update(extra)
    return [qid, d]


def _state(evals):
    return {"review_id": "run1", "student_name": "Asha",
            "student_details": {"name": "Asha", "roll_no": "7"},
            "evaluations": evals, "report_dir": "/tmp/reports",
            "report_path": "/tmp/reports/Asha_7.pdf",
            "exam_class": "X", "exam_subject": "Math"}


def _write_state(run_dir, evals):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "review_state.json"), "w") as f:
        json.dump(_state(evals), f)


# ---- load / ensure -----------------------------------------------------------

def test_load_returns_none_when_no_state(tmp_path):
    assert rc.load_working_state(str(tmp_path)) is None


def test_load_prefers_working_copy_over_state(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 1, 2)])
    assert rc.load_working_state(run)["evaluations"][0][1]["Marks Awarded"] == 1  # only state yet
    with open(os.path.join(run, "review_render.json"), "w") as f:
        json.dump({"evaluations": [_ev("Q1", 2, 2)]}, f)
    assert rc.load_working_state(run)["evaluations"][0][1]["Marks Awarded"] == 2  # render wins


def test_ensure_working_copy_seeds_machine_marks_and_identity(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 1.5, 3), _ev("Q2", 0, 2)])
    path = rc.ensure_working_copy(run)
    assert path and os.path.exists(path)
    with open(path) as f:
        wc = json.load(f)
    assert wc["evaluations"][0][1]["Machine Marks"] == 1.5   # stamped from current AI marks
    assert wc["evaluations"][1][1]["Machine Marks"] == 0
    assert wc["student_name"] == "Asha"                      # identity carried for PDF naming
    assert wc["student_details"]["roll_no"] == "7"
    assert wc["report_path"].endswith("Asha_7.pdf")


def test_ensure_working_copy_is_idempotent(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 1, 3)])
    path = rc.ensure_working_copy(run)
    with open(path) as f:
        wc = json.load(f)
    wc["evaluations"][0][1]["Marks Awarded"] = 99            # a later teacher edit
    with open(path, "w") as f:
        json.dump(wc, f)
    rc.ensure_working_copy(run)                              # must NOT reseed / clobber
    with open(path) as f:
        assert json.load(f)["evaluations"][0][1]["Marks Awarded"] == 99


def test_ensure_working_copy_none_without_state(tmp_path):
    assert rc.ensure_working_copy(str(tmp_path)) is None


# ---- backfill Question into a stale working copy -----------------------------
# A working copy written before the Question field existed strips objective/MCQ cards; load restores
# the display-only Question from the pristine snapshot without touching any teacher edit.

def _write_render(run, evals):
    os.makedirs(run, exist_ok=True)
    with open(os.path.join(run, "review_render.json"), "w") as f:
        json.dump({"evaluations": evals}, f)


def test_backfill_restores_question_from_stale_working_copy(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 1, 1, Question="Define a stack.",
                            Formatted={"Question": [{"type": "text", "content": "Define a stack."}]})])
    _write_render(run, [_ev("Q1", 1, 1)])                    # working copy has NO Question key
    r = rc.load_working_state(run)["evaluations"][0][1]
    assert r["Question"] == "Define a stack."
    assert r["Formatted"]["Question"] == [{"type": "text", "content": "Define a stack."}]


def test_backfill_preserves_teacher_edits(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 3, 3, Question="Explain X.")])
    _write_render(run, [_ev("Q1", 1, 3, **{"Student Wrote": "edited answer",
                                            "Teacher Corrected": True})])
    r = rc.load_working_state(run)["evaluations"][0][1]
    assert r["Question"] == "Explain X."                     # question restored
    assert r["Marks Awarded"] == 1                           # override untouched
    assert r["Student Wrote"] == "edited answer"             # OCR edit untouched
    assert r["Teacher Corrected"] is True


def test_backfill_keeps_existing_working_question(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q1", 1, 1, Question="pristine Q")])
    _write_render(run, [_ev("Q1", 1, 1, Question="working Q")])
    assert rc.load_working_state(run)["evaluations"][0][1]["Question"] == "working Q"


def test_backfill_no_crash_without_pristine_or_on_junk(tmp_path):
    run = str(tmp_path)
    _write_render(run, [_ev("Q1", 1, 1), ["oops"], "junk", None])   # no review_state.json
    r = rc.load_working_state(run)
    assert r["evaluations"][0][1].get("Question", "") == ""          # nothing to backfill, no crash


# ---- apply_decisions ---------------------------------------------------------

def test_reject_override_records_baseline_and_row():
    evals = [_ev("Q1", 3, 3, **{"Machine Marks": 3})]
    updated, rows, ta, tm = rc.apply_decisions(
        evals, [{"question_id": "Q1", "decision": "reject", "corrected_marks": 1,
                 "remark_evaluation": "wrong step", "remark_justification": "bad"}], {"Q1": 3})
    r = updated[0][1]
    assert r["Marks Awarded"] == 1
    assert r["Teacher Corrected"] is True and r["Teacher Reviewed"] is True
    assert r["Teacher Decision"] == "reject" and r["Needs Review (Yes/No)"] == "No"
    assert r["Teacher Original Marks"] == 3 and r["Teacher Corrected Marks"] == 1
    assert rows == [{"question_id": "Q1", "original_marks": 3, "corrected_marks": 1,
                     "max_marks": 3, "remark_evaluation": "wrong step",
                     "remark_justification": "bad", "ai_justification": "just Q1"}]
    assert ta == 1 and tm == 3


def test_reject_clamps_to_max():
    evals = [_ev("Q1", 3, 3, **{"Machine Marks": 3})]
    updated, _, _, _ = rc.apply_decisions(
        evals, [{"question_id": "Q1", "decision": "reject", "corrected_marks": 99}], {"Q1": 3})
    assert updated[0][1]["Marks Awarded"] == 3


def test_accept_reverts_override_to_machine_baseline():
    evals = [_ev("Q1", 1, 3, **{"Machine Marks": 3, "Teacher Corrected": True,
                                 "Teacher Corrected Marks": 1, "Teacher Original Marks": 3,
                                 "Teacher Remark Evaluation": "x"})]
    updated, rows, ta, _ = rc.apply_decisions(
        evals, [{"question_id": "Q1", "decision": "accept"}], {"Q1": 3})
    r = updated[0][1]
    assert r["Marks Awarded"] == 3                       # reverted to machine baseline
    assert r["Teacher Corrected"] is False
    assert "Teacher Corrected Marks" not in r and "Teacher Remark Evaluation" not in r
    assert r["Teacher Reviewed"] is True and r["Teacher Decision"] == "accept"
    assert rows == [] and ta == 3                        # accept emits no DB row


def test_input_not_mutated_and_untouched_preserved():
    evals = [_ev("Q1", 1, 2, **{"Machine Marks": 1}),
             _ev("Q2", 2, 2, **{"Machine Marks": 2, "Teacher Corrected": True,
                                 "Teacher Corrected Marks": 2})]
    updated, _, ta, tm = rc.apply_decisions(
        evals, [{"question_id": "Q1", "decision": "accept"}], {"Q1": 1, "Q2": 2})
    assert updated[1][1]["Teacher Corrected"] is True    # Q2 (no decision) preserved verbatim
    assert "Teacher Reviewed" not in evals[0][1]         # input list not mutated
    assert ta == 3 and tm == 4


def test_malformed_items_ignored():
    evals = [_ev("Q1", 1, 2, **{"Machine Marks": 1}), ["oops"], "junk", None]
    updated, _, ta, tm = rc.apply_decisions(
        evals, [{"question_id": "Q1", "decision": "accept"}], {"Q1": 1})
    assert updated[0][1]["Teacher Reviewed"] is True
    assert ta == 1 and tm == 2                           # only the valid item counts


# ---- composition: correction + single-answer regrade never clobber -----------

def _render_path(run):
    return os.path.join(run, "review_render.json")


def _apply_correction(run, qid, marks, pristine):
    """Mirror /submit-corrections: read the working copy, apply a reject/override, write it back."""
    wc = rc.load_working_state(run)
    updated, _, _, _ = rc.apply_decisions(
        wc["evaluations"], [{"question_id": qid, "decision": "reject", "corrected_marks": marks}],
        pristine)
    wc["evaluations"] = updated
    with open(_render_path(run), "w") as f:
        json.dump(wc, f)


def _simulate_regrade(run, qid, new_marks):
    """Mirror evaluate.py --regrade-one operating ON THE WORKING COPY: replace ONLY qid (stamping
    the new Machine Marks + reviewed flags) and preserve every other question."""
    wc = rc.load_working_state(run)
    for i, it in enumerate(wc["evaluations"]):
        if isinstance(it, list) and str(it[0]) == qid:
            res = dict(it[1])
            res.update({"Marks Awarded": new_marks, "Machine Marks": new_marks,
                        "Teacher Reviewed": True, "Teacher Re-evaluated": "Yes",
                        "Needs Review (Yes/No)": "No"})
            wc["evaluations"][i] = [qid, res]
    with open(_render_path(run), "w") as f:
        json.dump(wc, f)


def test_compose_correct_then_regrade_preserves_both(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q3", 3, 3), _ev("Q5", 0, 4)])
    rc.ensure_working_copy(run)
    _apply_correction(run, "Q3", 1, {"Q3": 3, "Q5": 0})     # override Q3 -> 1
    _simulate_regrade(run, "Q5", 4)                          # then regrade Q5 -> 4
    by = {it[0]: it[1] for it in rc.load_working_state(run)["evaluations"]}
    assert by["Q3"]["Marks Awarded"] == 1                    # correction survived the regrade
    assert by["Q3"]["Teacher Corrected"] is True
    assert by["Q5"]["Marks Awarded"] == 4


def test_compose_regrade_correct_regrade_preserves_all(tmp_path):
    run = str(tmp_path)
    _write_state(run, [_ev("Q3", 3, 3), _ev("Q5", 0, 4), _ev("Q7", 2, 5)])
    rc.ensure_working_copy(run)
    _simulate_regrade(run, "Q5", 4)                          # regrade Q5 -> 4
    _apply_correction(run, "Q3", 1, {"Q3": 3, "Q5": 0, "Q7": 2})  # override Q3 -> 1
    _simulate_regrade(run, "Q7", 5)                          # regrade Q7 -> 5
    by = {it[0]: it[1] for it in rc.load_working_state(run)["evaluations"]}
    assert by["Q3"]["Marks Awarded"] == 1                    # override not reverted by later regrade
    assert by["Q5"]["Marks Awarded"] == 4                    # first regrade preserved
    assert by["Q7"]["Marks Awarded"] == 5                    # last regrade applied
