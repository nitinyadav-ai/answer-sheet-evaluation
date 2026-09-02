"""Teacher review/override helpers for the evaluation pipeline (Part 10).

Three responsibilities, kept isolated from grading:
  1. apply_corrections() — a pure transform that overrides per-question marks with the
     teacher's corrected marks and records the change. It NEVER mutates its input (operates
     on a deepcopy), so the caller's pristine AI evaluations stay intact and the DB's
     "original marks" remain truthful across repeated reviews.
  2. store_rejected_answers() — persists one row per rejected answer to Postgres, reusing the
     same connection env vars as skills/answer-retrieval/scripts/fetch_answers.py
     (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD) plus a dedicated REJECTED_ANSWERS_TABLE.
  3. The working-copy helpers (load_working_state / ensure_working_copy / apply_decisions /
     compute_review_progress) maintain review_render.json — ONE authoritative copy of the
     evaluations carrying every teacher edit (overrides, accept/reject, single-answer regrades).
     review_state.json stays the write-once pristine AI snapshot; the working copy is created
     lazily on the first teacher action, so an untouched run is byte-identical to grade time.
"""

import os
import copy
import json

from marks_policy import quantize_mark
from review_flags import attach_flags

import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

# Project .env lives one level up from scripts/. Resolve it explicitly (not via frame-walking
# find_dotenv) so the DB connection works regardless of how/where this module is imported.
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_env():
    try:
        if os.path.exists(_ENV_PATH):
            load_dotenv(_ENV_PATH)
        else:
            load_dotenv()
    except Exception:
        pass  # fall back to whatever is already in os.environ


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_corrections(evaluations, corrections):
    """Override marks for teacher-rejected questions; leave accepted/un-reviewed ones unchanged.

    evaluations: list of [question_id, result_dict] (the pristine AI results).
    corrections: list of {question_id, decision, corrected_marks, remark_evaluation,
                 remark_justification}; only entries with decision == "reject" take effect.

    Returns (updated_evaluations, rejected_rows, total_awarded, total_max). The input is not
    mutated. Corrected marks are clamped to [0, Maximum Marks].
    """
    updated = copy.deepcopy(evaluations)

    rejects = {}
    for c in (corrections or []):
        if str(c.get("decision", "")).strip().lower() == "reject":
            rejects[str(c.get("question_id", ""))] = c

    rejected_rows = []
    for item in updated:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        qid, res = item[0], item[1]
        c = rejects.get(str(qid))
        if not c:
            continue

        max_m = _to_float(res.get("Maximum Marks", 0))
        original = _to_float(res.get("Marks Awarded", 0))
        # Snap to a multiple of 0.5 AND clamp to [0, max]. The UI input carries step="0.5", but that
        # is only a browser hint -- a pasted or typed 0.8 posts happily -- so the rule is enforced here,
        # server-side, where it cannot be bypassed.
        corrected = quantize_mark(c.get("corrected_marks", 0), max_m)

        remark_eval = (c.get("remark_evaluation") or "").strip()
        remark_just = (c.get("remark_justification") or "").strip()

        res["Marks Awarded"] = corrected
        res["Teacher Corrected"] = True
        res["Teacher Original Marks"] = original
        res["Teacher Corrected Marks"] = corrected
        res["Teacher Remark Evaluation"] = remark_eval
        res["Teacher Remark Justification"] = remark_just
        res["Needs Review (Yes/No)"] = "No"  # the teacher has now reviewed it

        rejected_rows.append({
            "question_id": qid,
            "original_marks": original,
            "corrected_marks": corrected,
            "max_marks": max_m,
            "remark_evaluation": remark_eval,
            "remark_justification": remark_just,
            "ai_justification": res.get("Justification", ""),
        })

    total_awarded = 0.0
    total_max = 0.0
    for item in updated:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            total_awarded += _to_float(item[1].get("Marks Awarded", 0))
            total_max += _to_float(item[1].get("Maximum Marks", 0))

    return updated, rejected_rows, total_awarded, total_max


# ---------------------------------------------------------------------------
# Working copy (review_render.json): the ONE authoritative teacher-edited state
# ---------------------------------------------------------------------------

def _run_state_paths(run_dir):
    """(pristine review_state.json, working review_render.json) paths under a run output dir."""
    return (os.path.join(run_dir, "review_state.json"),
            os.path.join(run_dir, "review_render.json"))


def _atomic_write_json(path, payload):
    """Write JSON via tmp + os.replace so a concurrent reader never sees a torn file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _backfill_question_fields(state, run_dir):
    """Restore the display-only Question (and its Formatted segments) into a working copy that predates
    the field, by copying from the pristine review_state.json matched by question id. The question text
    is NEVER teacher-edited -- it comes from the key / question paper, not from OCR or grading -- so this
    can only ADD a missing Question and can never clobber a marks override or an edited student answer.
    Without it, objective/MCQ cards (whose only substantial content is the question) render empty on any
    reloaded/batch report whose working copy was written before the Question field existed. In-memory."""
    state_path, _ = _run_state_paths(run_dir)
    if not isinstance(state, dict) or not os.path.exists(state_path):
        return state
    try:
        with open(state_path) as f:
            pristine = json.load(f)
    except Exception:
        return state
    by_qid = {}
    for it in (pristine.get("evaluations") or []):
        if isinstance(it, (list, tuple)) and len(it) == 2 and isinstance(it[1], dict):
            by_qid[str(it[0])] = it[1]
    for it in (state.get("evaluations") or []):
        if not (isinstance(it, (list, tuple)) and len(it) == 2 and isinstance(it[1], dict)):
            continue
        res = it[1]
        src = by_qid.get(str(it[0]))
        if not src:
            continue
        if not res.get("Question") and src.get("Question"):
            res["Question"] = src["Question"]
        src_fmt_q = (src.get("Formatted") or {}).get("Question")
        if src_fmt_q and not (res.get("Formatted") or {}).get("Question"):
            res.setdefault("Formatted", {})["Question"] = src_fmt_q
    return state


def load_working_state(run_dir):
    """Return the authoritative evaluation state for a run: the teacher working copy
    (review_render.json) if present, else the pristine AI snapshot (review_state.json), else
    None. The working copy is a superset that carries every teacher edit, so callers always see
    the latest marks. A corrupt working copy falls back to the pristine snapshot. A working copy that
    predates the Question field has its display-only Question restored from the pristine snapshot.

    'Review Flags' (WHY a question was flagged) is likewise backfilled for any run graded before that
    field existed, following the same read-time pattern -- which is what lets every archived run show
    its reasons with no re-grade. Derived, never stored: the file on disk is untouched, and a run that
    already carries flags keeps them (`overwrite=False`)."""
    state_path, render_path = _run_state_paths(run_dir)
    if os.path.exists(render_path):
        try:
            with open(render_path) as f:
                return _backfill_review_flags(_backfill_question_fields(json.load(f), run_dir))
        except Exception:
            pass
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                return _backfill_review_flags(json.load(f))
        except Exception:
            pass
    return None


def _backfill_review_flags(state):
    """Derive 'Review Flags' on read for results that lack it. Display-only and idempotent."""
    if isinstance(state, dict):
        try:
            attach_flags(state.get("evaluations") or [])
        except Exception:
            pass          # a display nicety must never break loading a graded run
    return state


def ensure_working_copy(run_dir):
    """Create the teacher working copy (review_render.json) from the pristine review_state.json on
    the first teacher action; idempotent (no-op if it already exists). Seeds each evaluation's
    'Machine Marks' with its current 'Marks Awarded' so a later Accept can revert a mistaken
    override to the machine baseline. Mirrors the render payload shape that /submit-corrections and
    evaluate.py --regenerate/--regrade-one consume (student_name/student_details/evaluations/
    report_dir/report_path). Returns the working-copy path, or None if there's no pristine state."""
    state_path, render_path = _run_state_paths(run_dir)
    if os.path.exists(render_path):
        return render_path
    if not os.path.exists(state_path):
        return None
    with open(state_path) as f:
        state = json.load(f)
    evals = state.get("evaluations", []) or []
    for item in evals:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            res = item[1]
            if "Machine Marks" not in res:
                res["Machine Marks"] = _to_float(res.get("Marks Awarded", 0))
    payload = {
        "review_id": state.get("review_id"),
        "student_name": state.get("student_name", "Student"),
        "student_details": state.get("student_details") or {},
        "evaluations": evals,
        "report_dir": state.get("report_dir") or os.path.dirname(state.get("report_path") or ""),
        "report_path": state.get("report_path"),
        "exam_class": state.get("exam_class", ""),
        "exam_subject": state.get("exam_subject", ""),
    }
    _atomic_write_json(render_path, payload)
    return render_path


def apply_decisions(evaluations, decisions, pristine_by_qid=None):
    """Apply teacher review decisions (accept AND reject/override) to a working-copy evaluations
    list. Supersedes apply_corrections for the review route: it records BOTH kinds of decision so
    review progress can be tracked and persisted, while leaving un-decided questions (including
    prior overrides and regrades already in the working copy) exactly as they are.

    evaluations:     list of [question_id, result_dict] (the working copy).
    decisions:       list of {question_id, decision('accept'|'reject'), corrected_marks,
                     remark_evaluation, remark_justification}.
    pristine_by_qid: {qid: pristine AI 'Marks Awarded'} used as the Accept baseline when a
                     question carries no stamped 'Machine Marks'.

    Returns (updated_evaluations, rejected_rows, total_awarded, total_max). Input not mutated
    (operates on a deepcopy). Idempotent and un-reject-safe: re-submitting a question as 'accept'
    reverts a prior override to the machine baseline and drops its DB row. Rejected marks are
    clamped to [0, Maximum Marks]."""
    pristine_by_qid = pristine_by_qid or {}
    updated = copy.deepcopy(evaluations)

    by_qid = {}
    for d in (decisions or []):
        by_qid[str(d.get("question_id", ""))] = d

    rejected_rows = []
    for item in updated:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        qid, res = item[0], item[1]
        d = by_qid.get(str(qid))
        if not d:
            continue

        decision = str(d.get("decision", "")).strip().lower()
        max_m = _to_float(res.get("Maximum Marks", 0))
        # Machine baseline = the stamped machine mark (pristine AI or last regrade), else the
        # pristine AI mark, else whatever is currently on the working copy.
        # Quantized so that Accept cannot restore an illegal mark from a run graded before the
        # half-mark rule existed (the baseline is read straight off an archived file).
        baseline = quantize_mark(res.get("Machine Marks",
                                         pristine_by_qid.get(str(qid), res.get("Marks Awarded", 0))),
                                 max_m)

        if decision == "reject":
            corrected = quantize_mark(d.get("corrected_marks", 0), max_m)  # snap to 0.5 + clamp [0,max]
            remark_eval = (d.get("remark_evaluation") or "").strip()
            remark_just = (d.get("remark_justification") or "").strip()
            res["Marks Awarded"] = corrected
            res["Teacher Corrected"] = True
            res["Teacher Original Marks"] = baseline
            res["Teacher Corrected Marks"] = corrected
            res["Teacher Remark Evaluation"] = remark_eval
            res["Teacher Remark Justification"] = remark_just
            res["Needs Review (Yes/No)"] = "No"           # the teacher has now reviewed it
            res["Teacher Reviewed"] = True
            res["Teacher Decision"] = "reject"
            rejected_rows.append({
                "question_id": qid,
                "original_marks": baseline,
                "corrected_marks": corrected,
                "max_marks": max_m,
                "remark_evaluation": remark_eval,
                "remark_justification": remark_just,
                "ai_justification": res.get("Justification", ""),
            })
        else:   # accept (or any non-reject decision): approve the machine mark, clear any override
            res["Marks Awarded"] = baseline
            res["Teacher Corrected"] = False
            for k in ("Teacher Original Marks", "Teacher Corrected Marks",
                      "Teacher Remark Evaluation", "Teacher Remark Justification"):
                res.pop(k, None)
            res["Teacher Reviewed"] = True
            res["Teacher Decision"] = "accept"

    total_awarded = 0.0
    total_max = 0.0
    for item in updated:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            total_awarded += _to_float(item[1].get("Marks Awarded", 0))
            total_max += _to_float(item[1].get("Maximum Marks", 0))

    return updated, rejected_rows, total_awarded, total_max


def compute_review_progress(evaluations):
    """Summarise review state for a run for the card badges: {reviewed, total, needs_review,
    injection}. `reviewed` counts questions a teacher has acted on (accept/reject/regrade);
    `needs_review` and `injection` are the machine flags (matching the single report's manual-review
    and injection banners). Guards malformed [qid, dict] items."""
    reviewed = total = needs_review = injection = 0
    for item in (evaluations or []):
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        res = item[1]
        total += 1
        is_reviewed = (bool(res.get("Teacher Reviewed"))
                       or res.get("Teacher Corrected") is True
                       or str(res.get("Teacher Re-evaluated", "")).strip().lower() == "yes")
        if is_reviewed:
            reviewed += 1
        bad_hw = (res.get("Bad Handwriting Flag") is True
                  or str(res.get("Bad Handwriting Flag", "false")).strip().lower() == "true")
        if str(res.get("Needs Review (Yes/No)", "No")).strip().upper() == "YES" and not bad_hw:
            needs_review += 1
        if str(res.get("Prompt Injection Detected", "No")).strip().upper() == "YES":
            injection += 1
    return {"reviewed": reviewed, "total": total,
            "needs_review": needs_review, "injection": injection}


def _table_name():
    return os.environ.get("REJECTED_ANSWERS_TABLE", "rejected_answers")


def _connect():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "postgres"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def ensure_table(cur, table):
    """Create the corrections table if it doesn't exist (idempotent)."""
    cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {} (
            id SERIAL PRIMARY KEY,
            review_id TEXT,
            student_name TEXT,
            roll_no TEXT,
            class TEXT,
            subject TEXT,
            question_id TEXT,
            original_marks NUMERIC,
            corrected_marks NUMERIC,
            max_marks NUMERIC,
            remark_evaluation TEXT,
            remark_justification TEXT,
            ai_justification TEXT,
            report_path TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """).format(sql.Identifier(table)))


def store_rejected_answers(review_id, context, rows):
    """Persist rejected answers for one review to Postgres; return the number stored.

    Idempotent per review: deletes any prior rows for review_id before inserting, so a
    re-submitted review never duplicates. Connects fresh each call and closes cleanly.
    Raises on connection/SQL failure (the caller treats DB storage as best-effort).
    """
    _load_env()
    table = _table_name()
    conn = None
    cur = None
    try:
        conn = _connect()
        cur = conn.cursor()
        ensure_table(cur, table)
        # Clear any prior rows for this review so a re-review can't leave stale/duplicate rows
        # (also handles the case where a previously-rejected question is now accepted).
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE review_id = %s").format(sql.Identifier(table)),
            (review_id,),
        )
        insert = sql.SQL("""
            INSERT INTO {} (review_id, student_name, roll_no, class, subject, question_id,
                            original_marks, corrected_marks, max_marks,
                            remark_evaluation, remark_justification, ai_justification, report_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """).format(sql.Identifier(table))
        for r in (rows or []):
            cur.execute(insert, (
                review_id,
                context.get("student_name", ""),
                context.get("roll_no", ""),
                context.get("class", ""),
                context.get("subject", ""),
                r.get("question_id", ""),
                r.get("original_marks"),
                r.get("corrected_marks"),
                r.get("max_marks"),
                r.get("remark_evaluation", ""),
                r.get("remark_justification", ""),
                r.get("ai_justification", ""),
                context.get("report_path", ""),
            ))
        conn.commit()
        return len(rows or [])
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
