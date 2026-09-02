import os
import sys
import json
import re
import subprocess
import threading
import concurrent.futures
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Missing dependency. Run: pip install PyMuPDF")
    sys.exit(1)

# Reuse the existing single-student pipeline verbatim (one report per student).
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from full_evaluator import (full_evaluate, prepare_orientation, resume_after_orientation,
                            kill_process_tree, _new_group_kwargs)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _safe_stem(name, idx):
    """Filesystem-safe, unique stem for a separated sheet PDF (drives full_evaluate's run_id)."""
    base = re.sub(r'[^A-Za-z0-9_]', '', (name or "").replace(' ', '_')) or "Student"
    return f"sheet_{idx}_{base}"


def slice_pdf(source_pdf, start_page, end_page, out_path):
    """Write pages [start_page, end_page] (1-indexed, inclusive) of source_pdf to out_path."""
    src = fitz.open(source_pdf)
    dst = fitz.open()
    dst.insert_pdf(src, from_page=start_page - 1, to_page=end_page - 1)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    dst.save(out_path)
    dst.close()
    src.close()
    return out_path


def _score(evaluations):
    """Sum awarded / max across the per-question result tuples [qid, result_dict]."""
    awarded = 0.0
    maximum = 0.0
    for item in evaluations or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        res = item[1] or {}
        try:
            awarded += float(res.get("Marks Awarded", 0) or 0)
        except (TypeError, ValueError):
            pass
        try:
            maximum += float(res.get("Maximum Marks", 0) or 0)
        except (TypeError, ValueError):
            pass
    return awarded, maximum


def _parse_cost(cost_str):
    try:
        return float(str(cost_str).replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Sheet-level concurrency (opt-in via BATCH_SHEET_CONCURRENCY, default 1).
#
#   1  -> the original SERIAL, IN-PROCESS path: each sheet is graded one at a time by calling
#         full_evaluate() / resume_after_orientation() directly -- byte-identical to before.
#   >1 -> sheets are graded in PARALLEL, each in its OWN python subprocess (the --run-sheet entry
#         below). A subprocess gets a private os.environ, cost ledger and stage-timings, so every
#         sheet runs EXACTLY as a standalone evaluation -- no shared-state cross-talk (full_evaluate
#         deliberately seeds per-run config into the global os.environ for its in-process glue-matcher,
#         which is only safe when each run owns its interpreter).
#
# The speedup is pure latency-hiding: grading is the long, throughput-bound stage and a single sheet
# already saturates the grading endpoint, so we do NOT try to grade faster -- we overlap one sheet's
# grading with another's OCR / diagram / ingest stages (separate endpoints / CPU). To keep the
# AGGREGATE load on each OpenRouter endpoint ~a single sheet's worth (no new 429s, no throughput loss),
# the per-sheet concurrency caps are SPLIT across the in-flight sheets (_scaled_caps).
# ---------------------------------------------------------------------------

_SHEET_ARGS_NAME = "batch_sheet_args.json"
_SHEET_RESULT_NAME = "batch_sheet_result.json"


def _dotenv_raw(key):
    """Read `key` from the project .env file (last match wins, mirroring full_evaluator/app.py's
    loaders). Returns the raw string, or None if absent. Needed because the Flask app does NOT load
    .env into os.environ -- it only overlays .env into per-SUBPROCESS envs -- so the batch PARENT must
    consult .env itself to honor a deployment default (e.g. BATCH_SHEET_CONCURRENCY) set there."""
    env_file = os.path.join(PROJECT_ROOT, ".env")
    val = None
    try:
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() != key:
                        continue
                    v = v.strip()
                    if v[:1] not in ('"', "'"):
                        v = re.sub(r'\s+#.*$', '', v).strip()
                    val = v.strip('"').strip("'")
    except OSError:
        pass
    return val


def _sheet_concurrency():
    """How many sheets to grade in parallel. Precedence: os.environ (an explicit runtime/shell/test
    override wins) -> project .env (deployment default) -> 1 (serial, in-process). Clamped to [1, 8]."""
    raw = os.environ.get("BATCH_SHEET_CONCURRENCY")
    if raw is None:
        raw = _dotenv_raw("BATCH_SHEET_CONCURRENCY")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, 8))


def _sheet_timeout():
    """Outer wall-clock ceiling (s) for ONE sheet subprocess, so a single hung sheet can never stall
    the whole batch (the per-stage watchdogs inside full_evaluate fire first on a healthy run).
    os.environ -> .env -> 2400. Override via BATCH_SHEET_TIMEOUT."""
    raw = os.environ.get("BATCH_SHEET_TIMEOUT")
    if raw is None:
        raw = _dotenv_raw("BATCH_SHEET_TIMEOUT")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 2400.0


def _effective_int(key, default):
    """The value full_evaluate will actually run `key` with: the project .env WINS (full_evaluate
    overlays it LAST, over os.environ), else the current environment, else `default`. Mirrored here so
    the concurrency split is computed against the SAME base each sheet will see."""
    raw = _dotenv_raw(key)
    if raw is None:
        raw = os.environ.get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _split_cap(key, default, n, floor):
    """`key` split n ways, held at `floor` -- but NEVER above what the operator configured.

    That last clamp is the subtle one: a bare max(floor, cap // n) turns the floor into a BOOST when
    someone has deliberately configured a cap below it (e.g. ANSWER_CROP_MAX_WORKERS=2 to be gentle on
    the provider), so a concurrent batch would run MORE workers per sheet than a serial one -- the exact
    inverse of this function's job. No effect at the shipped defaults, where every cap is above its
    floor; it only binds on a hand-lowered config."""
    cap = _effective_int(key, default)
    return str(min(cap, max(floor, cap // n)))


def _scaled_caps(n):
    """When grading n sheets at once, SPLIT the per-endpoint concurrency caps across them so the
    aggregate provider load stays ~one sheet's worth. Returns env overrides for full_evaluate, or {}
    for n <= 1 (subprocesses then see the unmodified .env caps -- exactly today's behavior)."""
    if n <= 1:
        return {}
    return {
        "EVAL_MAX_CONCURRENCY": _split_cap("EVAL_MAX_CONCURRENCY", 24, n, 4),
        "OCR_MAX_WORKERS": _split_cap("OCR_MAX_WORKERS", 20, n, 6),
        # Display-only answer crops also hit a vision endpoint, so split them too -- but on a HIGHER
        # floor than the others. Crops run on the INSTRUCT model while grading runs on the THINKING
        # one, so throttling them does not protect the grading throughput ceiling that actually bounds
        # a batch; the split here only matters against a staggered sheet's OCR, which shares the
        # instruct pool. Measured: a 20-page sheet needs 14 calls, so floor 2 => 7 waves (~65s) vs
        # floor 4 => 4 waves (~35s). Crops must finish inside the grading window or evaluate.py blocks
        # on the sentinel, so the wider floor is what keeps that margin comfortable at n=3.
        "ANSWER_CROP_MAX_WORKERS": _split_cap("ANSWER_CROP_MAX_WORKERS", 8, n, 4),
    }


def _student_record(unique_name, subject, result, pages_range):
    """Build the per-student result dict (+ its cost contribution) from a full_evaluate / resume
    result. Shared by the serial and concurrent paths so their success/error shapes never diverge."""
    if result.get("status") == "success":
        awarded, maximum = _score(result.get("evaluations"))
        return {
            "name": unique_name,
            "subject": subject,
            "status": "success",
            "report_path": result.get("report_path"),
            "marks_awarded": round(awarded, 2),
            "marks_max": round(maximum, 2),
            "cost": result.get("cost"),
            "evaluations": result.get("evaluations", []),
            "student_details": result.get("student_details", {}),
            "review_id": result.get("review_id"),
            "pages": pages_range,
        }, _parse_cost(result.get("cost"))
    return {
        "name": unique_name,
        "subject": subject,
        "status": "error",
        "error": result.get("error") or result.get("details") or "Evaluation failed",
        "pages": pages_range,
    }, 0.0


def _run_sheet_subprocess(run_id, sheet_kwargs, timeout):
    """Grade ONE sheet in a private python subprocess (isolated env / cost ledger / timings), returning
    full_evaluate's / resume_after_orientation's result dict verbatim. A per-sheet outer timeout means
    one stuck sheet can't stall the batch; on timeout/crash a normal error dict is returned so the
    batch still finishes (matching the serial path's per-sheet try/except)."""
    output_base = os.path.join(PROJECT_ROOT, "output", run_id)
    os.makedirs(output_base, exist_ok=True)
    args_path = os.path.join(output_base, _SHEET_ARGS_NAME)
    result_path = os.path.join(output_base, _SHEET_RESULT_NAME)
    try:
        if os.path.exists(result_path):
            os.remove(result_path)          # never read a stale result from a prior run
    except OSError:
        pass
    with open(args_path, "w") as f:
        json.dump(dict(sheet_kwargs, result_path=result_path), f)

    # Same interpreter as this (parent) process, so the child has the identical import environment the
    # in-process call would; full_evaluate spawns its stage subprocesses with that same interpreter.
    cmd = [sys.executable, os.path.abspath(__file__), "--run-sheet", args_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **_new_group_kwargs())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)                              # kill the whole tree (stages included)
        try:
            proc.communicate(timeout=15)
        except Exception:
            pass
        return {"status": "error",
                "error": f"Sheet exceeded its {timeout:.0f}s batch timeout and was terminated."}

    if not os.path.exists(result_path):
        tail = (err or out or "").strip()[-1500:]
        return {"status": "error",
                "error": (f"Sheet process produced no result (exit {proc.returncode}). {tail}").strip()}
    try:
        with open(result_path) as f:
            return json.load(f)
    except Exception as e:
        return {"status": "error", "error": f"Could not read sheet result: {e}"}


def _run_sheet_entry(args_path):
    """Subprocess entrypoint (`python batch_evaluator.py --run-sheet <args.json>`): grade a single
    sheet and write the result JSON to args['result_path']. Runs full_evaluate / resume_after_orientation
    exactly as the in-process call would -- this is the isolation boundary for concurrent batches."""
    with open(args_path) as f:
        a = json.load(f)
    result_path = a.get("result_path")
    try:
        mode = a.get("mode")
        if mode == "evaluate":
            res = full_evaluate(
                a["input_file"], student_name=a.get("student_name", "Student"),
                answer_key_path=a.get("answer_key_path"), report_dir=a.get("report_dir"),
                exam_class=a.get("exam_class"), exam_subject=a.get("exam_subject"),
                question_paper_path=a.get("question_paper_path"), marks_source=a.get("marks_source"),
                tester_id=a.get("tester_id"), env_overrides=a.get("env_overrides"))
        elif mode == "resume":
            res = resume_after_orientation(
                a["run_id"], rotations=a.get("rotations") or {},
                student_name=a.get("student_name", "Student"), answer_key_path=a.get("answer_key_path"),
                report_dir=a.get("report_dir"), exam_class=a.get("exam_class"),
                exam_subject=a.get("exam_subject"), question_paper_path=a.get("question_paper_path"),
                marks_source=a.get("marks_source"), tester_id=a.get("tester_id"),
                env_overrides=a.get("env_overrides"))
        else:
            res = {"status": "error", "error": f"Unknown sheet mode: {mode!r}"}
    except Exception as e:
        import traceback
        res = {"status": "error", "error": str(e), "details": traceback.format_exc()[-1500:]}
    try:
        with open(result_path, "w") as f:
            json.dump(res, f)
    except Exception:
        try:
            with open(result_path, "w") as f:
                json.dump({"status": "error", "error": "Could not serialize sheet result"}, f)
        except Exception:
            pass


def _grade_sheets_concurrent(items, worker, status_cb, total, concurrency):
    """Run worker(item) -> (record, cost) for each prepared sheet across a bounded thread pool (each
    worker just supervises one sheet subprocess, so threads are ideal -- the CPU/API work is in the
    child). Results are returned IN INPUT ORDER; progress is reported on completion with a MONOTONIC
    'done' count (out-of-order safe)."""
    records = [None] * total
    costs = [0.0] * total
    done = 0
    lock = threading.Lock()

    def _one(i, item):
        return i, worker(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        fut_map = {ex.submit(_one, i, item): i for i, item in enumerate(items)}
        for fut in concurrent.futures.as_completed(fut_map):
            i = fut_map[fut]
            item = items[i]
            try:
                _i, (rec, cost) = fut.result()
            except Exception as e:      # a worker should never raise, but never let one kill the batch
                rec, cost = _student_record(
                    item["name"], item["subject"],
                    {"status": "error", "error": f"Sheet worker crashed: {e}"}, item["pages_range"])
            with lock:
                records[i] = rec
                costs[i] = cost
                done += 1
                if status_cb:
                    status_cb(done, total, item["name"])
    # Sum in INPUT order (not completion order) so the batch total is float-identical to the serial path.
    total_cost = 0.0
    for c in costs:
        total_cost += c
    return records, total_cost


def batch_evaluate(batch_id, manifest, answer_key_path, status_cb=None, report_dir=None,
                   exam_class=None, exam_subject=None, question_paper_path=None, marks_source=None,
                   tester_id=None):
    """Slice the combined PDF per sheet and run the existing full_evaluate() on each student.

    manifest: dict with "source_pdf" and "sheets" (each: name, subject, start_page, end_page).
    status_cb(done, total, current_name): optional progress callback.
    report_dir: optional confirmed folder; each report saved as "{student_name}.pdf" there.
    Returns {status, batch_id, students:[...], total_cost}.

    Sheets are graded one at a time by default; set BATCH_SHEET_CONCURRENCY>1 to grade several in
    parallel (each in its own isolated subprocess -- see the module header)."""
    source_pdf = manifest["source_pdf"]
    sheets = manifest.get("sheets", [])
    separated_dir = os.path.join(PROJECT_ROOT, "output", batch_id, "separated")
    os.makedirs(separated_dir, exist_ok=True)

    total = len(sheets)
    concurrency = min(_sheet_concurrency(), total or 1)

    # Pre-pass: resolve each sheet's unique display name + sliced-PDF path + run_id IN ORDER (the name
    # de-dup is order-dependent, so this reproduces the serial numbering exactly for both paths).
    used_names = {}
    prepared = []
    for idx, sheet in enumerate(sheets, start=1):
        display_name = (sheet.get("name") or f"Student {idx}").strip() or f"Student {idx}"
        unique_name = _dedup_name(display_name, idx, used_names)
        stem = _safe_stem(display_name, idx)
        prepared.append({
            "idx": idx, "sheet": sheet, "name": unique_name, "subject": sheet.get("subject", ""),
            "run_id": stem, "sheet_pdf": os.path.join(separated_dir, f"{stem}.pdf"),
            "pages_range": f"{sheet['start_page']}-{sheet['end_page']}",
        })

    if concurrency <= 1:
        # ---- Serial, in-process path: behaviourally identical to the original loop. ----
        students = []
        total_cost = 0.0
        for i, item in enumerate(prepared):
            if status_cb:
                status_cb(i, total, item["name"])
            sheet = item["sheet"]
            try:
                slice_pdf(source_pdf, sheet["start_page"], sheet["end_page"], item["sheet_pdf"])
                result = full_evaluate(item["sheet_pdf"], student_name=item["name"],
                                       answer_key_path=answer_key_path, report_dir=report_dir,
                                       exam_class=exam_class, exam_subject=exam_subject,
                                       question_paper_path=question_paper_path, marks_source=marks_source,
                                       tester_id=tester_id)
            except Exception as e:
                result = {"status": "error", "error": str(e)}
            rec, cost = _student_record(item["name"], item["subject"], result, item["pages_range"])
            students.append(rec)
            total_cost += cost
        if status_cb:
            status_cb(total, total, None)
        return {"status": "success", "batch_id": batch_id, "students": students,
                "total_cost": f"${total_cost:.6f}"}

    # ---- Concurrent path: one isolated subprocess per sheet, bounded pool. ----
    env_overrides = _scaled_caps(concurrency)
    timeout = _sheet_timeout()
    # Slice every sheet up front, serially in-process: fast, and it keeps PyMuPDF single-threaded.
    for item in prepared:
        sheet = item["sheet"]
        try:
            slice_pdf(source_pdf, sheet["start_page"], sheet["end_page"], item["sheet_pdf"])
        except Exception as e:
            item["slice_error"] = str(e)

    def _worker(item):
        if item.get("slice_error"):
            return _student_record(item["name"], item["subject"],
                                   {"status": "error", "error": f"Could not slice sheet: {item['slice_error']}"},
                                   item["pages_range"])
        result = _run_sheet_subprocess(item["run_id"], {
            "mode": "evaluate", "input_file": item["sheet_pdf"], "student_name": item["name"],
            "answer_key_path": answer_key_path, "report_dir": report_dir, "exam_class": exam_class,
            "exam_subject": exam_subject, "question_paper_path": question_paper_path,
            "marks_source": marks_source, "tester_id": tester_id, "env_overrides": env_overrides,
        }, timeout)
        return _student_record(item["name"], item["subject"], result, item["pages_range"])

    if status_cb:
        status_cb(0, total, prepared[0]["name"] if prepared else None)
    students, total_cost = _grade_sheets_concurrent(prepared, _worker, status_cb, total, concurrency)
    if status_cb:
        status_cb(total, total, None)
    return {"status": "success", "batch_id": batch_id, "students": students,
            "total_cost": f"${total_cost:.6f}"}


# ---------------------------------------------------------------------------
# Human-in-the-loop orientation gate for the batch flow: prepare all sheets (ingest + preprocess +
# auto-orient suggestion, NO OCR) -> teacher confirms every page grouped by student -> grade all.
# The "Skip & grade as-is" button uses batch_evaluate above; the gated path uses
# batch_resume_orientation below. Both share the same per-sheet aggregation + BATCH_SHEET_CONCURRENCY
# machinery, and a confirm with no rotations grades byte-identically (resume_after_orientation(
# rotations={}) == full_evaluate). run_id per sheet is _safe_stem(display_name, idx) in every path, so
# prepare / resume / skip all address the same output/<run_id>/ directory.
# ---------------------------------------------------------------------------

def _dedup_name(display_name, idx, used_names):
    """Same per-sheet display-name de-duplication batch_evaluate uses (kept identical so prepare,
    resume and skip agree on unique_name -> report filename)."""
    count = used_names.get(display_name, 0) + 1
    used_names[display_name] = count
    return display_name if count == 1 else f"{display_name} ({count})"


def batch_prepare_orientation(batch_id, manifest, answer_key_path, status_cb=None, report_dir=None,
                              exam_class=None, exam_subject=None, question_paper_path=None, tester_id=None):
    """Phase 1 of the batch orientation gate. Slice each sheet and run prepare_orientation on it
    (ingest + preprocess + per-page cardinal SUGGESTION, stops before OCR). Offline -- spends no API
    credit. Returns {"batch_id", "sheets":[{sheet_id, run_id, name, subject, pages_range, pages|error}]}
    for the grouped-by-student review. status_cb(done, total, current) drives the progress bar."""
    source_pdf = manifest["source_pdf"]
    sheets = manifest.get("sheets", [])
    separated_dir = os.path.join(PROJECT_ROOT, "output", batch_id, "separated")
    os.makedirs(separated_dir, exist_ok=True)

    total = len(sheets)
    out_sheets = []
    used_names = {}
    for idx, sheet in enumerate(sheets, start=1):
        display_name = (sheet.get("name") or f"Student {idx}").strip() or f"Student {idx}"
        unique_name = _dedup_name(display_name, idx, used_names)
        run_id = _safe_stem(display_name, idx)
        if status_cb:
            status_cb(idx - 1, total, unique_name)

        sheet_pdf = os.path.join(separated_dir, f"{run_id}.pdf")
        entry = {"sheet_id": sheet.get("id") or f"sheet_{idx}", "run_id": run_id,
                 "name": unique_name, "subject": sheet.get("subject", ""),
                 "pages_range": f"{sheet['start_page']}-{sheet['end_page']}"}
        try:
            slice_pdf(source_pdf, sheet["start_page"], sheet["end_page"], sheet_pdf)
            result = prepare_orientation(sheet_pdf, student_name=unique_name,
                                         answer_key_path=answer_key_path, report_dir=report_dir,
                                         exam_class=exam_class, exam_subject=exam_subject,
                                         question_paper_path=question_paper_path, tester_id=tester_id)
        except Exception as e:
            result = {"error": str(e)}
        if result.get("status") == "orient_review":
            entry["pages"] = result.get("pages", [])
        else:
            entry["pages"] = []
            entry["error"] = result.get("error") or result.get("details") or "Preparation failed"
        out_sheets.append(entry)

    if status_cb:
        status_cb(total, total, None)
    return {"batch_id": batch_id, "sheets": out_sheets}


def batch_resume_orientation(batch_id, manifest, rotations_by_run, answer_key_path, status_cb=None,
                             report_dir=None, exam_class=None, exam_subject=None,
                             question_paper_path=None, marks_source=None, tester_id=None):
    """Phase 2 of the batch orientation gate. Apply each sheet's teacher-confirmed rotations and grade
    it via resume_after_orientation. Mirrors batch_evaluate's loop + aggregation (duplicated on purpose
    so the untouched batch_evaluate skip-path stays byte-identical). rotations_by_run maps run_id ->
    {page_index(str): deg}. Returns the same {status, batch_id, students, total_cost} shape. Honors
    BATCH_SHEET_CONCURRENCY exactly like batch_evaluate (serial in-process by default)."""
    sheets = manifest.get("sheets", [])
    rotations_by_run = rotations_by_run or {}
    total = len(sheets)
    concurrency = min(_sheet_concurrency(), total or 1)

    used_names = {}
    prepared = []
    for idx, sheet in enumerate(sheets, start=1):
        display_name = (sheet.get("name") or f"Student {idx}").strip() or f"Student {idx}"
        unique_name = _dedup_name(display_name, idx, used_names)
        prepared.append({
            "idx": idx, "name": unique_name, "subject": sheet.get("subject", ""),
            "run_id": _safe_stem(display_name, idx),
            "pages_range": f"{sheet['start_page']}-{sheet['end_page']}",
        })

    if concurrency <= 1:
        # ---- Serial, in-process path: behaviourally identical to the original loop. ----
        students = []
        total_cost = 0.0
        for i, item in enumerate(prepared):
            if status_cb:
                status_cb(i, total, item["name"])
            try:
                result = resume_after_orientation(
                    item["run_id"], rotations=rotations_by_run.get(item["run_id"], {}),
                    student_name=item["name"], answer_key_path=answer_key_path, report_dir=report_dir,
                    exam_class=exam_class, exam_subject=exam_subject,
                    question_paper_path=question_paper_path, marks_source=marks_source, tester_id=tester_id)
            except Exception as e:
                result = {"status": "error", "error": str(e)}
            rec, cost = _student_record(item["name"], item["subject"], result, item["pages_range"])
            students.append(rec)
            total_cost += cost
        if status_cb:
            status_cb(total, total, None)
        return {"status": "success", "batch_id": batch_id, "students": students,
                "total_cost": f"${total_cost:.6f}"}

    # ---- Concurrent path: one isolated subprocess per sheet, bounded pool. ----
    env_overrides = _scaled_caps(concurrency)
    timeout = _sheet_timeout()

    def _worker(item):
        result = _run_sheet_subprocess(item["run_id"], {
            "mode": "resume", "run_id": item["run_id"],
            "rotations": rotations_by_run.get(item["run_id"], {}), "student_name": item["name"],
            "answer_key_path": answer_key_path, "report_dir": report_dir, "exam_class": exam_class,
            "exam_subject": exam_subject, "question_paper_path": question_paper_path,
            "marks_source": marks_source, "tester_id": tester_id, "env_overrides": env_overrides,
        }, timeout)
        return _student_record(item["name"], item["subject"], result, item["pages_range"])

    if status_cb:
        status_cb(0, total, prepared[0]["name"] if prepared else None)
    students, total_cost = _grade_sheets_concurrent(prepared, _worker, status_cb, total, concurrency)
    if status_cb:
        status_cb(total, total, None)
    return {"status": "success", "batch_id": batch_id, "students": students,
            "total_cost": f"${total_cost:.6f}"}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-sheet":
        _run_sheet_entry(sys.argv[2])           # isolated single-sheet worker (concurrent batch path)
        sys.exit(0)
    if len(sys.argv) < 3:
        print("Usage: python3 batch_evaluator.py <manifest.json> <answer_key.json> [batch_id]")
        sys.exit(1)
    manifest_path = sys.argv[1]
    answer_key = sys.argv[2]
    bid = sys.argv[3] if len(sys.argv) > 3 else Path(manifest_path).parent.parent.name
    with open(manifest_path) as f:
        mf = json.load(f)
    res = batch_evaluate(bid, mf, answer_key,
                         status_cb=lambda d, t, n: print(f"[{d}/{t}] {n or 'done'}"))
    print(json.dumps(res, indent=2))
