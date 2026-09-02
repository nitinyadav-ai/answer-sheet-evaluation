import os
import docx
import sys
import io
import re
import json
import uuid
import shutil
import threading
import subprocess
import time
import hmac
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file, abort, session, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename

try:
    import fitz  # PyMuPDF — used to slice per-student PDFs on the fly for the review screen
except ImportError:
    fitz = None

# Add the scripts directory to sys.path so we can import the orchestrators
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from full_evaluator import (full_evaluate, prepare_orientation, resume_after_orientation,
                            request_cancel, is_cancelled, clear_cancel, PYTHON_EXE)
from batch_evaluator import batch_evaluate, batch_prepare_orientation, batch_resume_orientation
from review_corrections import (
    apply_corrections, store_rejected_answers,
    load_working_state, ensure_working_copy, apply_decisions, compute_review_progress,
)
from upload_validation import (validate_raw_file, validate_parsed_questions, cross_check,
                               validate_for_evaluation, has_blocking, WARNING, compute_marks_mismatch,
                               build_marks_breakdown, apply_marks_corrections,
                               validate_question_paper_structure)


def _load_json(path):
    """Load a JSON file, or None if missing/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

app = Flask(__name__)
app.secret_key = "secret_key_for_session"
CORS(app)


# ---------------------------------------------------------------------------------------------------
# Optional password gate for PUBLIC exposure (e.g. behind a Cloudflare tunnel). Activates ONLY when
# APP_AUTH_PASSWORD is set in the environment; when it is unset (normal local use) this is a no-op,
# so local behaviour is unchanged. Username defaults to "teacher" (override with APP_AUTH_USERNAME).
# ---------------------------------------------------------------------------------------------------
@app.before_request
def _require_password_when_public():
    expected_pw = os.environ.get("APP_AUTH_PASSWORD")
    if not expected_pw:
        return  # gate disabled -> local/dev behaviour unchanged
    expected_user = os.environ.get("APP_AUTH_USERNAME", "teacher")
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", expected_user) \
            and hmac.compare_digest(auth.password or "", expected_pw):
        return  # credentials OK -> continue to the route
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="AI Answer Evaluator"'},
    )

if os.environ.get("VERCEL"):
    UPLOAD_FOLDER = "/tmp/uploads"
    REPORTS_FOLDER = "/tmp/Evaluation Reports"
else:
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
    REPORTS_FOLDER = os.path.expanduser("~/Evaluation Reports")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORTS_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['REPORTS_FOLDER'] = REPORTS_FOLDER

# ---------------------------------------------------------------------------
# Answer Sheet Separator — batch state (filesystem-backed, polled by the UI)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_BASE = os.path.join(PROJECT_ROOT, "output")
SEPARATOR_SCRIPT = os.path.join(PROJECT_ROOT, "skills", "answer-sheet-separator", "scripts", "separate_sheets.py")
ANSWER_KEY_PATH = os.path.join(UPLOAD_FOLDER, "current_answer_key.json")
# Step 1 of the sequential flow: the parsed question paper. Supplies richer question TEXT only;
# the answer key remains the sole source of expected answers + marks.
QUESTION_PAPER_PATH = os.path.join(UPLOAD_FOLDER, "current_question_paper.json")
# Exam-level metadata (name, subject, class, section, date, time, duration) collected alongside the
# question-paper upload in Step 1. Independent of parsing success -- these are teacher-typed fields,
# not AI-extracted -- so they are saved unconditionally and later read by the report generator to
# print a header on every student's PDF.
EXAM_METADATA_PATH = os.path.join(UPLOAD_FOLDER, "current_exam_metadata.json")
# Teacher's "which document is authoritative for MARKS" choice, written when the answer key is parsed
# (only matters when the key's marks disagree with the paper's). Read at evaluation time.
MARKS_SOURCE_PATH = os.path.join(UPLOAD_FOLDER, "marks_source_state.json")
_BATCH_ID_RE = re.compile(r'^batch_[A-Za-z0-9]+$')
_SHEET_ID_RE = re.compile(r'^sheet_\d+$')

# ---------------------------------------------------------------------------
# Report folder: auto-generate ~/Desktop/{Class}/{Subject}, teacher-confirmed.
# ---------------------------------------------------------------------------
DESKTOP_DIR = os.path.expanduser("~/Desktop")
REPORT_STATE_PATH = os.path.join(UPLOAD_FOLDER, "report_path_state.json")


def _sanitize_segment(s):
    """Make a folder-safe path segment (keeps spaces, e.g. 'Class X')."""
    s = re.sub(r'[\\/:*?"<>|]', '', (s or "")).strip().strip('.')
    return re.sub(r'\s+', ' ', s).strip()


def _first_subject(questions):
    """Subject is uniform across a key; fall back to any question's subject."""
    if isinstance(questions, dict):
        for v in questions.values():
            if isinstance(v, dict) and v.get("subject"):
                return str(v["subject"])
    return ""


def _suggested_report_path(cls, subject):
    c = _sanitize_segment(cls) or "Unknown Class"
    s = _sanitize_segment(subject) or "Unknown Subject"
    return os.path.join(DESKTOP_DIR, c, s)


def _read_report_state():
    try:
        with open(REPORT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_report_state(state):
    with open(REPORT_STATE_PATH, "w") as f:
        json.dump(state, f)


def _build_env():
    """Copy os.environ and overlay project .env (mirrors full_evaluator's env loader)."""
    env = os.environ.copy()
    env_file = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip()
                    if v[:1] not in ('"', "'"):
                        v = re.sub(r'\s+#.*$', '', v).strip()  # drop an inline comment from unquoted values
                    env[k.strip()] = v.strip('"').strip("'")
    return env


def _utf8_env(base=None):
    """Child env with utf-8 IO pinned. These children hand their result back over STDOUT, and on Windows
    a child would otherwise encode that stream as cp1252 -- mangling, or hard-failing on, the maths and
    chemistry glyphs the parsers emit. No-op on POSIX, whose locale encoding is already utf-8."""
    return {**(os.environ if base is None else base), "PYTHONIOENCODING": "utf-8"}


def _batch_dir(batch_id):
    return os.path.join(OUTPUT_BASE, batch_id)


def _sep_dir(batch_id):
    return os.path.join(_batch_dir(batch_id), "separation")


def _status_path(batch_id):
    return os.path.join(_batch_dir(batch_id), "status.json")


def _manifest_path(batch_id):
    return os.path.join(_sep_dir(batch_id), "manifest.json")


def _valid_batch(batch_id):
    return bool(_BATCH_ID_RE.match(batch_id or "")) and os.path.isdir(_batch_dir(batch_id))


def _write_status(batch_id, status):
    os.makedirs(_batch_dir(batch_id), exist_ok=True)
    tmp = _status_path(batch_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f)
    os.replace(tmp, _status_path(batch_id))


def _read_status(batch_id):
    try:
        with open(_status_path(batch_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_manifest(batch_id):
    with open(_manifest_path(batch_id), encoding="utf-8") as f:
        return json.load(f)


def _write_manifest(batch_id, manifest):
    os.makedirs(_sep_dir(batch_id), exist_ok=True)
    with open(_manifest_path(batch_id), "w") as f:
        json.dump(manifest, f, indent=2)


def _normalize_manifest(existing, posted_sheets):
    """Rebuild a well-formed manifest from teacher-edited sheets (clamp ranges, re-id, re-sort)."""
    num_pages = int(existing.get("num_pages", 0))
    src = existing.get("source_pdf")
    cleaned = []
    for s in sorted(posted_sheets, key=lambda x: int(x.get("start_page", 1))):
        try:
            sp = int(s.get("start_page"))
            ep = int(s.get("end_page"))
        except (TypeError, ValueError):
            continue
        sp = max(1, min(num_pages, sp))
        ep = max(sp, min(num_pages, ep))
        cleaned.append({
            "id": None,
            "name": (s.get("name") or "").strip(),
            "subject": (s.get("subject") or "").strip(),
            "start_page": sp,
            "end_page": ep,
            "page_count": ep - sp + 1,
            "is_omr": bool(s.get("is_omr", False)),
            "needs_review": bool(s.get("needs_review", False)),
            "confidence": s.get("confidence"),
        })
    for i, s in enumerate(cleaned, 1):
        s["id"] = f"sheet_{i}"
    return {"source_pdf": src, "num_pages": num_pages, "sheets": cleaned}


def _run_separation(batch_id, pdf_path):
    """Background worker: run the separator skill, then publish the manifest for review."""
    try:
        _write_status(batch_id, {"batch_id": batch_id, "phase": "separating", "source_pdf": pdf_path})
        result = subprocess.run(
            [PYTHON_EXE, SEPARATOR_SCRIPT, pdf_path, "--output-dir", _sep_dir(batch_id)],
            capture_output=True, text=True, encoding="utf-8", cwd=PROJECT_ROOT,
            env=_utf8_env(_build_env()))
        if result.returncode != 0 or not os.path.exists(_manifest_path(batch_id)):
            _write_status(batch_id, {"batch_id": batch_id, "phase": "error",
                                     "error": "Separation failed",
                                     "details": (result.stderr or result.stdout)[-2000:]})
            return
        manifest = _read_manifest(batch_id)
        _write_status(batch_id, {"batch_id": batch_id, "phase": "review",
                                 "source_pdf": pdf_path, "manifest": manifest})
    except Exception as e:
        _write_status(batch_id, {"batch_id": batch_id, "phase": "error", "error": str(e)})


def _run_batch_eval(batch_id, manifest, report_dir=None, exam_class=None, exam_subject=None,
                    question_paper_path=None, marks_source=None, tester_id=None):
    """Background worker: grade every separated sheet, streaming progress into status.json."""
    def status_cb(done, total, current):
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "evaluating"
        st["progress"] = {"done": done, "total": total, "current": current}
        _write_status(batch_id, st)

    try:
        result = batch_evaluate(batch_id, manifest, ANSWER_KEY_PATH, status_cb=status_cb,
                                report_dir=report_dir, exam_class=exam_class, exam_subject=exam_subject,
                                question_paper_path=question_paper_path, marks_source=marks_source,
                                tester_id=tester_id)
        _write_status(batch_id, {"batch_id": batch_id, "phase": "done",
                                 "manifest": manifest, "results": result})
    except Exception as e:
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "error"
        st["error"] = str(e)
        _write_status(batch_id, st)


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/reports/<path:filename>')
def serve_report(filename):
    # Reports now save into the teacher-confirmed folder; serve from there when the file exists,
    # else fall back to the legacy ~/Evaluation Reports. send_from_directory blocks path traversal.
    state = _read_report_state()
    if state and state.get("confirmed") and state.get("path"):
        cdir = os.path.expanduser(state["path"])
        if os.path.exists(os.path.join(cdir, filename)):
            return send_from_directory(cdir, filename)
    return send_from_directory(app.config['REPORTS_FOLDER'], filename)

@app.route('/upload-guidelines')
def upload_guidelines():
    """Serve the teacher upload handout (docs/UPLOAD_GUIDELINES.md) as a readable page, so the guidance
    is one click from the upload screen. Renders markdown when the `markdown` lib is present; otherwise
    falls back to a clean monospaced view of the same text."""
    doc_path = os.path.join(PROJECT_ROOT, "docs", "UPLOAD_GUIDELINES.md")
    try:
        with open(doc_path, encoding="utf-8") as f:
            md = f.read()
    except Exception:
        return ("Upload guidelines document not found.", 404)
    try:
        import markdown as _md
        body = _md.markdown(md, extensions=["tables", "fenced_code"])
    except Exception:
        from markupsafe import escape as _esc
        body = f"<pre style='white-space:pre-wrap'>{_esc(md)}</pre>"
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Upload Guidelines</title><style>"
            "body{max-width:820px;margin:2rem auto;padding:0 1rem;font-family:-apple-system,Segoe UI,"
            "Roboto,Helvetica,Arial,sans-serif;line-height:1.55;color:#1f2937}"
            "h1,h2{border-bottom:1px solid #e5e7eb;padding-bottom:.3rem}code{background:#f3f4f6;"
            "padding:.1rem .3rem;border-radius:4px}table{border-collapse:collapse}"
            "th,td{border:1px solid #d1d5db;padding:.4rem .6rem;text-align:left}"
            "blockquote{border-left:4px solid #f59e0b;margin:1rem 0;padding:.4rem 1rem;background:#fffbeb}"
            "</style></head><body>" + body + "</body></html>")
    return html


def _finalize_question_paper(parsed_json, raw_issues=None):
    """Persist a parsed OR pasted question paper and run the post-parse structural check. Shared by
    /parse-question-paper (from a PDF/DOCX parse) and /paste-question-paper (teacher-pasted JSON).
    Accepts the nested {questions:{...}} shape OR a flat {qid:{...}} map; returns a jsonify Response."""
    raw_issues = raw_issues or []
    # The parser returns {questions:{...}} for PDF/DOCX, or a flat dict for raw/pasted JSON.
    # Unwrap so current_question_paper.json is ALWAYS a flat question dict.
    if isinstance(parsed_json, dict) and isinstance(parsed_json.get("questions"), dict):
        questions = parsed_json["questions"]
    else:
        questions = parsed_json

    with open(QUESTION_PAPER_PATH, "w") as f:
        json.dump(questions, f)

    # Post-parse structural check (0 questions, missing marks). Block on ERROR-severity.
    parsed_issues = validate_parsed_questions(questions, "question paper")
    # Completeness heuristic: warn (never block) if the numbering doesn't start at Q1 or has gaps --
    # the fingerprint of a dropped section (e.g. the parser skipping the objective 'Section A').
    parsed_issues += validate_question_paper_structure(questions)
    issues = raw_issues + parsed_issues
    if has_blocking(parsed_issues):
        return jsonify({"status": "error", "error": "Upload check failed",
                        "details": parsed_issues[0]["message"], "issues": issues})

    # Deliberately do NOT touch report_path_state.json here: the report folder is derived
    # from the ANSWER KEY's class/subject (Step 2), not the question paper.
    return jsonify({"status": "success", "data": questions,
                    "count": len(questions) if isinstance(questions, dict) else 0,
                    "issues": issues})


def _finalize_answer_key(parsed_json, raw_issues=None):
    """Persist a parsed OR pasted answer key, write the choices sidecar, cross-check vs the question
    paper, and derive the report folder + marks-source decision. Shared by /parse-answer-key (from a
    PDF/DOCX parse) and /paste-answer-key (teacher-pasted JSON). Accepts the nested {metadata,questions}
    shape OR a flat {qid:{...}} map (empty choices/metadata then); returns a jsonify Response."""
    raw_issues = raw_issues or []
    # The parser returns a wrapped object {metadata, questions} for PDF/DOCX keys, or a flat question
    # dict for raw/pasted JSON. Unwrap so current_answer_key.json ALWAYS stays a flat question dict ->
    # downstream alignment/grading is byte-for-byte unchanged.
    if isinstance(parsed_json, dict) and isinstance(parsed_json.get("questions"), dict):
        questions = parsed_json["questions"]
        meta = parsed_json.get("metadata") or {}
    else:
        questions = parsed_json
        meta = {}

    # Write to a persistent temp file instead of session to avoid 4KB cookie limit
    temp_key_path = os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key.json")
    with open(temp_key_path, "w") as f:
        json.dump(questions, f)

    # Persist internal-choice metadata (Part 11) to a sidecar beside the flat key, so
    # full_evaluate can merge OR-pairs / flag inline-OR without changing the key's shape.
    # A fresh parse always rewrites it (empty lists when the document has no choices).
    choices_path = os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key_choices.json")
    choices_dict = {"choice_groups": meta.get("choice_groups") or [],
                    "inline_choice_ids": meta.get("inline_choice_ids") or []}
    with open(choices_path, "w") as f:
        json.dump(choices_dict, f)

    # Pristine snapshot of the freshly-parsed key + choices, so the marks editor's "Reset to parsed"
    # can restore the original parse after the teacher edits current_answer_key.json in place.
    with open(os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key_parsed.json"), "w") as f:
        json.dump(questions, f)
    with open(os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key_choices_parsed.json"), "w") as f:
        json.dump(choices_dict, f)

    # Post-parse checks: structure (0 questions / missing marks) + cross-check the key's marks
    # against the question paper (Step 1) so a dropped/duplicated part is caught NOW, before
    # evaluation. Block on ERROR; warnings are surfaced but allowed.
    qp_json = _load_json(QUESTION_PAPER_PATH)
    parsed_issues = validate_parsed_questions(questions, "answer key")
    if qp_json:
        cross_issues = cross_check(questions, choices_dict, qp_json)
    else:
        cross_issues = [{"severity": WARNING, "code": "no_question_paper",
                         "message": "No question paper has been uploaded yet, so the answer key "
                                    "could not be cross-checked. Upload the question paper (Step 1) "
                                    "to enable the automatic marks check."}]
    issues = raw_issues + parsed_issues + cross_issues
    if has_blocking(parsed_issues):
        return jsonify({"status": "error", "error": "Upload check failed",
                        "details": parsed_issues[0]["message"], "issues": issues})

    # Derive Class + Subject for the auto report-folder path. Subject falls back to any
    # question's subject (uniform across a key); Class is blank if the document lacked it.
    cls = (meta.get("class") or "").strip()
    subject = (meta.get("subject") or _first_subject(questions)).strip()
    suggested_path = _suggested_report_path(cls, subject)
    # A fresh parse invalidates any prior confirmation (teacher must confirm the new path).
    _write_report_state({"class": cls, "subject": subject,
                         "suggested_path": suggested_path, "confirmed": False, "path": ""})

    # Marks-source decision: when the key's effective marks disagree with the paper, ask the
    # teacher which document is authoritative (default = recommended). Auto-confirmed when they
    # already agree, so the chooser only appears on a real mismatch. Reset on every fresh parse.
    if qp_json:
        mm = compute_marks_mismatch(questions, choices_dict, qp_json)
    else:
        mm = {"mismatch": False, "key_total": 0, "qp_total": 0,
              "recommended": "answer_key", "per_question": []}
    with open(MARKS_SOURCE_PATH, "w") as f:
        json.dump({**mm, "source": mm["recommended"],
                   "confirmed": (not mm["mismatch"])}, f)

    return jsonify({"status": "success", "data": questions,
                    "class": cls, "subject": subject, "suggested_path": suggested_path,
                    "issues": issues, "marks_mismatch": mm})


@app.route('/parse-question-paper', methods=['POST'])
def parse_question_paper_route():
    """Step 1 of the sequential flow. Parse the uploaded question paper into a flat question dict
    and persist it as current_question_paper.json. full_evaluate later overlays its richer question
    TEXT onto the answer key; the answer key stays the sole source of answers + marks."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "No file part"})

    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "error": "No selected file"})

    if file:
        filename = secure_filename("question_paper_" + file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Save the teacher-typed exam metadata unconditionally -- it doesn't depend on whether the
        # AI parse below succeeds, and a re-upload always overwrites it with the latest values.
        exam_metadata = {
            "qp_name": (request.form.get("qp_name") or "").strip(),
            "subject": (request.form.get("qp_subject") or "").strip(),
            "class": (request.form.get("qp_class") or "").strip(),
            "section": (request.form.get("qp_section") or "").strip(),
            "date": (request.form.get("qp_date") or "").strip(),
            "time": (request.form.get("qp_time") or "").strip(),
            "duration": (request.form.get("qp_duration") or "").strip(),
        }
        try:
            with open(EXAM_METADATA_PATH, "w") as _mf:
                json.dump(exam_metadata, _mf)
        except OSError as _e:
            print(f"Warning: could not save exam metadata: {_e}")

        # Pre-parse gate: reject a scanned/image-only or empty file BEFORE spending an LLM parse call.
        raw_issues = validate_raw_file(filepath, "question paper")
        if has_blocking(raw_issues):
            return jsonify({"status": "error", "error": "Upload check failed",
                            "details": raw_issues[0]["message"], "issues": raw_issues})

        try:
            cmd = [PYTHON_EXE, os.path.join("..", "scripts", "extract_json_from_question_paper.py"), filepath]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                    cwd=os.path.dirname(__file__), env=_utf8_env())

            if result.returncode != 0:
                return jsonify({"status": "error", "error": "Parsing failed", "details": result.stderr})

            output = result.stdout.strip()
            if not output:
                return jsonify({"status": "error", "error": "Parsing failed", "details": "No output from script"})

            try:
                parsed_json = json.loads(output)
            except json.JSONDecodeError:
                return jsonify({"status": "error", "error": "JSON Decode Error", "details": f"Invalid JSON output: {output[:200]}..."})

            return _finalize_question_paper(parsed_json, raw_issues)
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})


@app.route('/parse-answer-key', methods=['POST'])
def parse_answer_key_route():
    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "No file part"})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "error": "No selected file"})
    
    if file:
        filename = secure_filename("answer_key_" + file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Pre-parse gate: reject a scanned/image-only or empty file BEFORE spending an LLM parse call.
        raw_issues = validate_raw_file(filepath, "answer key")
        if has_blocking(raw_issues):
            return jsonify({"status": "error", "error": "Upload check failed",
                            "details": raw_issues[0]["message"], "issues": raw_issues})

        # We need to use Gemini to parse this.
        # In a real OpenClaw tool, I would call an agent.
        # Here I will call a shell command that triggers a sub-agent turn or use a script.
        try:
            # For now, we'll create a JSON file and return it
            # I will use the 'exec' capability of the agent to actually run the parsing logic later.
            # But for the Flask app, I'll assume a script exists: scripts/extract_json_from_key.py
            cmd = [PYTHON_EXE, os.path.join("..", "scripts", "extract_json_from_key.py"), filepath]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                    cwd=os.path.dirname(__file__), env=_utf8_env())
            
            if result.returncode != 0:
                return jsonify({"status": "error", "error": "Parsing failed", "details": result.stderr})
            
            output = result.stdout.strip()
            if not output:
                return jsonify({"status": "error", "error": "Parsing failed", "details": "No output from script"})
                
            try:
                parsed_json = json.loads(output)
            except json.JSONDecodeError as je:
                return jsonify({"status": "error", "error": "JSON Decode Error", "details": f"Invalid JSON output: {output[:200]}..."})

            return _finalize_answer_key(parsed_json, raw_issues)
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)})


@app.route('/paste-question-paper', methods=['POST'])
def paste_question_paper_route():
    """Alternative to /parse-question-paper: accept the question paper as JSON pasted directly by the
    teacher (the app's OWN schema), skipping the PDF upload + LLM parse. Same downstream + validation."""
    raw = request.get_data(as_text=True) or ""
    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError as je:
        return jsonify({"status": "error", "error": "Invalid JSON",
                        "details": f"That isn't valid JSON ({je.msg} at line {je.lineno}). "
                                   f"Paste the question paper in the documented format (see the "
                                   f"upload guidelines)."}), 400
    try:
        return _finalize_question_paper(parsed_json, [])
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route('/paste-answer-key', methods=['POST'])
def paste_answer_key_route():
    """Alternative to /parse-answer-key: accept the answer key as JSON pasted directly by the teacher
    (the app's OWN {metadata, questions} schema), skipping the PDF upload + LLM parse."""
    raw = request.get_data(as_text=True) or ""
    try:
        parsed_json = json.loads(raw)
    except json.JSONDecodeError as je:
        return jsonify({"status": "error", "error": "Invalid JSON",
                        "details": f"That isn't valid JSON ({je.msg} at line {je.lineno}). "
                                   f"Paste the answer key in the documented format (see the upload "
                                   f"guidelines)."}), 400
    try:
        return _finalize_answer_key(parsed_json, [])
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route('/report-path')
def report_path():
    """Current report-folder state, so the UI can restore the confirmed banner on reload."""
    state = _read_report_state() or {}
    return jsonify({
        "confirmed": bool(state.get("confirmed")),
        "path": state.get("path", ""),
        "suggested_path": state.get("suggested_path", ""),
        "class": state.get("class", ""),
        "subject": state.get("subject", ""),
        # Step-completion flags so the wizard can restore the right step on reload.
        "question_paper_ready": os.path.exists(QUESTION_PAPER_PATH),
        "answer_key_ready": os.path.exists(ANSWER_KEY_PATH),
    })


@app.route('/confirm-report-path', methods=['POST'])
def confirm_report_path():
    """Persist the teacher-confirmed (possibly edited) report folder. Unblocks evaluation."""
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"status": "error", "error": "Path is required"}), 400
    path = os.path.abspath(os.path.expanduser(path))
    state = _read_report_state() or {}
    state["path"] = path
    state["confirmed"] = True
    _write_report_state(state)
    return jsonify({"status": "ok", "path": path})


@app.route('/marks-source')
def marks_source():
    """Current marks-source state, so the UI can show the chooser (only on a real mismatch) and restore
    it on reload."""
    st = _load_json(MARKS_SOURCE_PATH) or {}
    return jsonify({
        "mismatch": bool(st.get("mismatch")),
        "confirmed": bool(st.get("confirmed")),
        "source": st.get("source", "question_paper"),
        "recommended": st.get("recommended", "question_paper"),
        "key_total": st.get("key_total", 0),
        "qp_total": st.get("qp_total", 0),
        "per_question": st.get("per_question", []),
    })


@app.route('/confirm-marks-source', methods=['POST'])
def confirm_marks_source():
    """Persist the teacher's choice of which document governs MARKS (question_paper | answer_key)."""
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    if source not in ("question_paper", "answer_key"):
        return jsonify({"status": "error", "error": "source must be 'question_paper' or 'answer_key'"}), 400
    st = _load_json(MARKS_SOURCE_PATH) or {}
    st["source"] = source
    st["confirmed"] = True
    with open(MARKS_SOURCE_PATH, "w") as f:
        json.dump(st, f)
    return jsonify({"status": "ok", "source": source})


# ---- Marks-breakdown editor: correct misread marks, group choices, add/remove questions ----------
KEY_PARSED_PATH = os.path.join(UPLOAD_FOLDER, "current_answer_key_parsed.json")
CHOICES_PATH = os.path.join(UPLOAD_FOLDER, "current_answer_key_choices.json")
CHOICES_PARSED_PATH = os.path.join(UPLOAD_FOLDER, "current_answer_key_choices_parsed.json")


@app.route('/marks-breakdown')
def marks_breakdown():
    """Per-entry rows for the editable marks breakdown (each key entry's marks + the paper's marks for
    its base question), current choice groups, totals, and the mismatch/confirmed flags. Openable any
    time after the key is parsed; the grading gate only FORCES it on a real mismatch."""
    key = _load_json(ANSWER_KEY_PATH)
    if not key:
        return jsonify({"available": False})
    choices = _load_json(CHOICES_PATH) or {}
    qp = _load_json(QUESTION_PAPER_PATH) or {}
    bd = build_marks_breakdown(key, choices, qp)
    st = _load_json(MARKS_SOURCE_PATH) or {}
    bd["confirmed"] = bool(st.get("confirmed"))
    bd["has_question_paper"] = bool(qp)
    bd["available"] = True
    return jsonify(bd)


@app.route('/confirm-marks-breakdown', methods=['POST'])
def confirm_marks_breakdown():
    """Apply the teacher's corrected breakdown to the answer key IN PLACE (marks edits, added/removed
    questions, choice groups), then mark the marks decision confirmed with source=answer_key. Because
    grading -- single AND batch -- reads current_answer_key.json, the corrections apply everywhere, and
    trust_key mode then leaves the teacher's numbers untouched."""
    corr = request.get_json(silent=True) or {}
    key = _load_json(ANSWER_KEY_PATH)
    if not key:
        return jsonify({"status": "error", "error": "No answer key is loaded to correct."}), 400
    choices = _load_json(CHOICES_PATH) or {}
    try:
        new_key, new_choices = apply_marks_corrections(key, choices, corr)
    except Exception as e:
        return jsonify({"status": "error", "error": f"Could not apply corrections: {e}"}), 400
    if not new_key:
        return jsonify({"status": "error", "error": "The corrected key would have no questions."}), 400
    with open(ANSWER_KEY_PATH, "w") as f:
        json.dump(new_key, f)
    with open(CHOICES_PATH, "w") as f:
        json.dump(new_choices, f)
    qp = _load_json(QUESTION_PAPER_PATH) or {}
    bd = build_marks_breakdown(new_key, new_choices, qp)
    st = _load_json(MARKS_SOURCE_PATH) or {}
    st.update({"confirmed": True, "source": "answer_key", "edited": True,
               "key_total": bd["key_total"], "qp_total": bd["qp_total"], "mismatch": bd["mismatch"]})
    with open(MARKS_SOURCE_PATH, "w") as f:
        json.dump(st, f)
    return jsonify({"status": "ok", "key_total": bd["key_total"], "qp_total": bd["qp_total"],
                    "questions": len(new_key)})


@app.route('/reset-marks-breakdown', methods=['POST'])
def reset_marks_breakdown():
    """Restore the answer key + choices to the original parse (undo all teacher edits)."""
    parsed = _load_json(KEY_PARSED_PATH)
    if parsed is None:
        return jsonify({"status": "error", "error": "No original parse is available to reset to."}), 400
    with open(ANSWER_KEY_PATH, "w") as f:
        json.dump(parsed, f)
    parsed_choices = _load_json(CHOICES_PARSED_PATH) or {"choice_groups": [], "inline_choice_ids": []}
    with open(CHOICES_PATH, "w") as f:
        json.dump(parsed_choices, f)
    qp = _load_json(QUESTION_PAPER_PATH) or {}
    mm = (compute_marks_mismatch(parsed, parsed_choices, qp) if qp
          else {"mismatch": False, "key_total": 0, "qp_total": 0, "recommended": "answer_key", "per_question": []})
    with open(MARKS_SOURCE_PATH, "w") as f:
        json.dump({**mm, "source": mm["recommended"], "confirmed": (not mm["mismatch"])}, f)
    return jsonify({"status": "ok", "mismatch": mm.get("mismatch", False)})


@app.route('/evaluate', methods=['POST'])
def evaluate():
    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "No file part"})
    
    file = request.files['file']
    student_name = request.form.get('student_name', 'Student')
    
    if file.filename == '':
        return jsonify({"status": "error", "error": "No selected file"})
    
    # Save the uploaded student paper
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    # Step 1 (sequential flow): REQUIRE the question paper. This guard is web-only; full_evaluate
    # stays QP-optional so the CLI/legacy path is completely unaffected.
    if not os.path.exists(QUESTION_PAPER_PATH):
        return jsonify({
            "status": "error",
            "error": "No Question Paper Found",
            "details": "Upload and parse the Question Paper (Step 1) before evaluating a student paper."
        })

    # REQUIRE the manual answer key (Database fallback disabled)
    answer_key_path = os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key.json")
    if not os.path.exists(answer_key_path):
        return jsonify({
            "status": "error",
            "error": "No Answer Key Found",
            "details": "The database fallback has been removed. You MUST upload and parse a Teacher's Answer Key in the 'Manage Answer Key' tab before evaluating a student paper."
        })

    # REQUIRE a confirmed report folder (blocks evaluation until the teacher confirms the path)
    state = _read_report_state()
    if not (state and state.get("confirmed") and state.get("path")):
        return jsonify({
            "status": "error",
            "error": "Report folder not confirmed",
            "details": "Confirm the report save folder (shown after parsing the answer key in the 'Manage Answer Key' tab) before evaluating."
        })

    # Final structural gate before the (long) evaluation: block on any ERROR-severity issue in the
    # key/question-paper (a scan is already rejected at upload; this catches e.g. no questions parsed).
    # Warnings (total mismatch, dropped/extra questions) are NOT blocked -- the grading-time reconciler
    # corrects the total against the paper and flags the affected answers -- but are returned so the UI
    # can show them.
    _key_json = _load_json(answer_key_path)
    _choices_json = _load_json(os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key_choices.json")) or {}
    _qp_json = _load_json(QUESTION_PAPER_PATH)
    eval_issues = validate_for_evaluation(_key_json, _choices_json, _qp_json)
    if has_blocking(eval_issues):
        _blk = next(i for i in eval_issues if i["severity"] == "error")
        return jsonify({"status": "error", "error": "Upload check failed",
                        "details": _blk["message"], "issues": eval_issues})

    # Marks-source gate: when the key's marks disagree with the paper, the teacher must first choose
    # which document is authoritative (the chooser shown after parsing the key). Only blocks on a real,
    # unconfirmed mismatch; when they agree it is auto-confirmed and this is a no-op.
    _ms = _load_json(MARKS_SOURCE_PATH) or {}
    if _ms.get("mismatch") and not _ms.get("confirmed"):
        return jsonify({
            "status": "error",
            "error": "Marks source not confirmed",
            "details": (f"The answer key totals {_ms.get('key_total')} marks but the question paper "
                        f"totals {_ms.get('qp_total')}. Choose which document to use for marks "
                        f"(shown after parsing the answer key) before evaluating.")
        })
    marks_source = _ms.get("source") if (_ms.get("mismatch") or _ms.get("edited")) else None

    # Optional "replace a previous evaluation": validated BEFORE grading so a bad id fails fast, but
    # acted on only AFTER success (see _supersede_run). A re-upload under the SAME filename needs
    # nothing here -- it reuses the folder and _reset_run_dir clears it in place.
    replaces, rep_err = _validated_replaces(request.form.get('replaces_run_id', ''),
                                            os.path.splitext(filename)[0])
    if rep_err:
        return jsonify(rep_err)
    clear_cancel(os.path.splitext(filename)[0])   # a cancelled attempt at this name must not block a retry

    # Trigger the full evaluation pipeline
    try:
        result = full_evaluate(filepath, student_name, answer_key_path=answer_key_path,
                               report_dir=state["path"], exam_class=state.get("class", ""),
                               exam_subject=state.get("subject", ""), tester_id=request.form.get('tester_id', ''),
                               question_paper_path=(QUESTION_PAPER_PATH
                                                    if os.path.exists(QUESTION_PAPER_PATH) else None),
                               marks_source=marks_source)
        if replaces and result.get("status") == "success":
            result["replaced"] = _supersede_run(replaces, os.path.splitext(filename)[0],
                                                result.get("report_path"))
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route('/submit-corrections', methods=['POST'])
def submit_corrections():
    """Teacher review/override: apply corrected marks, regenerate the stored report in place
    (no re-OCR/grading), and persist rejected answers to Postgres. Reloads the pristine
    server-side review state so the recorded "original marks" are the true AI marks."""
    data = request.get_json(silent=True) or {}
    review_id = str(data.get("review_id", "")).strip()
    corrections = data.get("corrections", []) or []

    # Validate the review id (no path traversal) and locate the pristine review state.
    if not review_id or "/" in review_id or "\\" in review_id or ".." in review_id:
        return jsonify({"status": "error", "error": "Invalid review id"}), 400
    review_dir = os.path.join(OUTPUT_BASE, review_id)
    state_path = os.path.join(review_dir, "review_state.json")
    if not os.path.exists(state_path):
        return jsonify({
            "status": "error",
            "error": "Review state not found",
            "details": f"No saved evaluation for review id '{review_id}'. Re-run the evaluation."
        }), 404

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "error": "Could not read review state", "details": str(e)}), 500

    # Route every teacher edit through ONE working copy (review_render.json): create it lazily from
    # the pristine snapshot on first edit, then apply decisions on top of whatever edits (prior
    # overrides, single-answer regrades) it already holds. review_state.json stays pristine, so the
    # Accept baseline and the DB's "original marks" remain the true AI marks.
    ensure_working_copy(review_dir)
    working = load_working_state(review_dir) or state
    pristine_by_qid = {}
    for _it in state.get("evaluations", []) or []:
        if isinstance(_it, (list, tuple)) and len(_it) == 2 and isinstance(_it[1], dict):
            pristine_by_qid[str(_it[0])] = _it[1].get("Marks Awarded", 0)

    # apply_decisions records BOTH accept and reject (never mutates its input); un-decided questions
    # keep their existing working-copy state verbatim.
    updated, rows, total_awarded, total_max = apply_decisions(
        working.get("evaluations", []), corrections, pristine_by_qid)

    # Persist the working copy, then rebuild the PDF from it in place.
    report_dir = working.get("report_dir") or state.get("report_dir") or os.path.dirname(
        working.get("report_path") or state.get("report_path") or "")
    render_payload = dict(working)
    render_payload.update({
        "student_name": working.get("student_name") or state.get("student_name", "Student"),
        "student_details": working.get("student_details") or state.get("student_details") or {},
        "evaluations": updated,
        "report_dir": report_dir,
        "report_path": working.get("report_path") or state.get("report_path"),
    })
    render_path = os.path.join(review_dir, "review_render.json")
    try:
        with open(render_path, "w") as f:
            json.dump(render_payload, f, indent=2)
    except Exception as e:
        return jsonify({"status": "error", "error": "Could not write render input", "details": str(e)}), 500

    evaluate_script = os.path.join(PROJECT_ROOT, "skills", "answer-evaluator-and-report-generation",
                                   "scripts", "evaluate.py")
    env = _build_env()
    if report_dir:
        env["REPORT_OUTPUT_DIR"] = os.path.expanduser(report_dir)
    try:
        proc = subprocess.run([PYTHON_EXE, evaluate_script, "--regenerate", render_path],
                              capture_output=True, text=True, encoding="utf-8", env=_utf8_env(env))
    except Exception as e:
        return jsonify({"status": "error", "error": "Report regeneration failed", "details": str(e)}), 500
    if proc.returncode != 0:
        return jsonify({"status": "error", "error": "Report regeneration failed",
                        "details": (proc.stderr or proc.stdout)}), 500

    # Pull the regenerator's JSON (report path + recomputed totals) out of stdout.
    report_path = state.get("report_path")
    for block in re.findall(r'\{.*\}', proc.stdout, re.DOTALL):
        try:
            cand = json.loads(block)
        except Exception:
            continue
        if "report_path" in cand:
            report_path = cand.get("report_path", report_path)
            total_awarded = cand.get("total_awarded", total_awarded)
            total_max = cand.get("total_max", total_max)
            break

    # Persist rejected answers to Postgres (best-effort — the regenerated report is never lost).
    details = state.get("student_details") or {}
    context = {
        "student_name": details.get("name", ""),
        "roll_no": details.get("roll_no", ""),
        "class": details.get("class", ""),
        "subject": details.get("subject", ""),
        "report_path": report_path,
    }
    rejected_stored = 0
    db_error = None
    try:
        rejected_stored = store_rejected_answers(review_id, context, rows)
    except Exception as e:
        db_error = str(e)

    resp = {
        "status": "success",
        "report_path": report_path,
        "total_awarded": total_awarded,
        "total_max": total_max,
        "rejected_count": len(rows),
        "rejected_stored": rejected_stored,
        "evaluations": updated,
        "review_progress": compute_review_progress(updated),
    }
    if db_error:
        resp["db_error"] = db_error
    return jsonify(resp)


# ---------------------------------------------------------------------------
# Single-question re-grade job store. The grader can take 25-80s, so it runs in a BACKGROUND thread
# and the browser polls for the result -- no minute-long blocking request (which a hosting proxy would
# time out) and the teacher can keep reviewing meanwhile. In-memory is fine: the app is single-process
# (app.run threaded=True). A per-review lock serialises concurrent re-grades on the SAME sheet so two
# background writes to review_render.json can't clobber each other.
# ---------------------------------------------------------------------------
_REGRADE_JOBS = {}
_REGRADE_JOBS_LOCK = threading.Lock()
_REGRADE_REVIEW_LOCKS = {}


def _regrade_review_lock(review_id):
    with _REGRADE_JOBS_LOCK:
        lk = _REGRADE_REVIEW_LOCKS.get(review_id)
        if lk is None:
            lk = _REGRADE_REVIEW_LOCKS[review_id] = threading.Lock()
        return lk


def _set_regrade_job(job_id, patch):
    with _REGRADE_JOBS_LOCK:
        job = _REGRADE_JOBS.get(job_id, {})
        job.update(patch)
        job["ts"] = time.time()
        _REGRADE_JOBS[job_id] = job
        cutoff = time.time() - 600      # drop finished jobs older than 10 min so the store can't grow
        for k in [k for k, v in _REGRADE_JOBS.items()
                  if v.get("status") in ("done", "error") and v.get("ts", 0) < cutoff]:
            _REGRADE_JOBS.pop(k, None)


def _run_regrade_job(job_id, review_id, review_dir, cmd, env):
    """Background worker: run evaluate.py --regrade-one, then stash the parsed result for polling."""
    try:
        with _regrade_review_lock(review_id):
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  env=_utf8_env(env))
        if proc.returncode != 0:
            _set_regrade_job(job_id, {"status": "error", "error": "Re-grade failed",
                                      "details": (proc.stderr or proc.stdout)})
            return
        for block in re.findall(r'\{.*\}', proc.stdout, re.DOTALL):
            try:
                cand = json.loads(block)
            except Exception:
                continue
            if cand.get("status") == "success" and "report_path" in cand:
                _w = load_working_state(review_dir)
                if _w is not None:
                    cand["review_progress"] = compute_review_progress(_w.get("evaluations", []))
                _set_regrade_job(job_id, {"status": "done", "result": cand})
                return
        _set_regrade_job(job_id, {"status": "error", "error": "Re-grade produced no result",
                                  "details": (proc.stdout or proc.stderr)[:500]})
    except Exception as e:
        _set_regrade_job(job_id, {"status": "error", "error": "Re-grade failed", "details": str(e)})


@app.route('/re-evaluate-status/<job_id>')
def re_evaluate_status(job_id):
    """Poll a background re-grade started by /re-evaluate-question. Returns {status:'running'} until
    done, then the same success payload the old synchronous route returned (or an error)."""
    with _REGRADE_JOBS_LOCK:
        job = _REGRADE_JOBS.get(job_id)
        job = dict(job) if job else None
    if job is None:
        return jsonify({"status": "error", "error": "Unknown or expired re-grade job"}), 404
    st = job.get("status")
    if st == "done":
        return jsonify(job.get("result") or {"status": "error", "error": "Empty result"})
    if st == "error":
        return jsonify({"status": "error", "error": job.get("error"), "details": job.get("details")})
    return jsonify({"status": "running"})


@app.route('/re-evaluate-question', methods=['POST'])
def re_evaluate_question():
    """Teacher backstop for the segmentation flags: edit ONE question's OCR text and re-grade just
    that question (via the same grader), then regenerate the report in place. Every other mark is
    preserved verbatim and the question's original AI mark is stashed for audit, so a teacher action
    on one question can never alter another -- improve-only by construction. Runs in the BACKGROUND
    (returns a job_id to poll) so a minute-long grade never blocks the request."""
    data = request.get_json(silent=True) or {}
    review_id = str(data.get("review_id", "")).strip()
    question_id = str(data.get("question_id", "")).strip()
    edited_text = data.get("answer_text", "")
    if not review_id or "/" in review_id or "\\" in review_id or ".." in review_id:
        return jsonify({"status": "error", "error": "Invalid review id"}), 400
    if not question_id:
        return jsonify({"status": "error", "error": "Missing question_id"}), 400
    review_dir = os.path.join(OUTPUT_BASE, review_id)
    state_path = os.path.join(review_dir, "review_state.json")
    db_path = os.path.join(review_dir, "db_answers.json")
    if not (os.path.exists(state_path) and os.path.exists(db_path)):
        return jsonify({"status": "error", "error": "Review state not found",
                        "details": f"No saved evaluation for review id '{review_id}'."}), 404

    # Stage the edited answer in a file (avoids arg-length / quoting issues) and find the report dir.
    try:
        with open(state_path, encoding="utf-8") as f:
            _state = json.load(f)
        report_dir = _state.get("report_dir") or os.path.dirname(_state.get("report_path") or "")
        # Regrade AGAINST the working copy so prior corrections/regrades are preserved; create it
        # from the pristine snapshot on first edit. Falls back to pristine if it can't be made.
        working_path = ensure_working_copy(review_dir) or state_path
        text_path = os.path.join(review_dir, "regrade_input.txt")
        # Explicit utf-8: this is the teacher's corrected answer text -- maths, chemistry, superscripts --
        # and it is the one file in the pipeline carrying raw non-ASCII rather than ASCII-escaped JSON.
        # Windows' cp1252 default would raise UnicodeEncodeError here. evaluate.py reads it back the same.
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(edited_text if isinstance(edited_text, str) else str(edited_text))
    except Exception as e:
        return jsonify({"status": "error", "error": "Could not stage re-grade", "details": str(e)}), 500

    evaluate_script = os.path.join(PROJECT_ROOT, "skills", "answer-evaluator-and-report-generation",
                                   "scripts", "evaluate.py")
    env = _build_env()
    if report_dir:
        env["REPORT_OUTPUT_DIR"] = os.path.expanduser(report_dir)
    # Kick off the grader in the background and return a job id immediately; the browser polls
    # /re-evaluate-status. evaluate.py's skip-if-unchanged fast path makes a no-op confirm near-instant.
    cmd = [PYTHON_EXE, evaluate_script, "--regrade-one", working_path, db_path, question_id, text_path]
    job_id = uuid.uuid4().hex[:12]
    _set_regrade_job(job_id, {"status": "running", "review_id": review_id})
    threading.Thread(target=_run_regrade_job, args=(job_id, review_id, review_dir, cmd, env),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ---------------------------------------------------------------------------
# Answer Sheet Separator — batch routes (upload combined PDF -> review -> grade)
# ---------------------------------------------------------------------------
@app.route('/separate', methods=['POST'])
def separate_route():
    """Accept one combined PDF, kick off separation in the background, return a batch id."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "No file part"})
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "error": "No selected file"})
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"status": "error", "error": "Please upload a single combined PDF of the answer sheets."})

    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    os.makedirs(_batch_dir(batch_id), exist_ok=True)
    filename = secure_filename(file.filename) or "combined.pdf"
    pdf_path = os.path.join(_batch_dir(batch_id), filename)
    file.save(pdf_path)

    _write_status(batch_id, {"batch_id": batch_id, "phase": "separating", "source_pdf": pdf_path})
    threading.Thread(target=_run_separation, args=(batch_id, pdf_path), daemon=True).start()
    return jsonify({"status": "started", "batch_id": batch_id})


@app.route('/batch/<batch_id>/status')
def batch_status(batch_id):
    if not _BATCH_ID_RE.match(batch_id):
        return jsonify({"status": "error", "error": "Invalid batch id"}), 400
    st = _read_status(batch_id)
    if st is None:
        return jsonify({"status": "error", "error": "Unknown batch"}), 404
    return jsonify(st)


@app.route('/thumbs/<batch_id>/<int:page>')
def batch_thumb(batch_id, page):
    if not _valid_batch(batch_id):
        abort(404)
    return send_from_directory(os.path.join(_sep_dir(batch_id), "thumbs"), f"page_{page}.png")


@app.route('/sheets/<batch_id>/<sheet_id>.pdf')
def batch_sheet_pdf(batch_id, sheet_id):
    """Slice the source PDF to this sheet's CURRENT page range so previews reflect teacher edits."""
    if fitz is None:
        abort(500)
    if not _valid_batch(batch_id) or not _SHEET_ID_RE.match(sheet_id):
        abort(404)
    try:
        manifest = _read_manifest(batch_id)
    except Exception:
        abort(404)
    sheet = next((s for s in manifest.get("sheets", []) if s["id"] == sheet_id), None)
    if not sheet:
        abort(404)
    src = fitz.open(manifest["source_pdf"])
    dst = fitz.open()
    dst.insert_pdf(src, from_page=sheet["start_page"] - 1, to_page=sheet["end_page"] - 1)
    try:
        data = dst.tobytes()
    except AttributeError:
        data = dst.write()
    dst.close()
    src.close()
    return send_file(io.BytesIO(data), mimetype="application/pdf", download_name=f"{sheet_id}.pdf")


@app.route('/batch/<batch_id>/manifest', methods=['POST'])
def batch_save_manifest(batch_id):
    """Autosave teacher edits (names, subjects, merge/split boundaries)."""
    if not _valid_batch(batch_id):
        return jsonify({"status": "error", "error": "Unknown batch"}), 404
    try:
        existing = _read_manifest(batch_id)
    except Exception:
        return jsonify({"status": "error", "error": "No manifest yet"}), 400
    posted = request.get_json(silent=True) or {}
    manifest = _normalize_manifest(existing, posted.get("sheets", []))
    _write_manifest(batch_id, manifest)
    st = _read_status(batch_id) or {"batch_id": batch_id, "phase": "review"}
    st["manifest"] = manifest
    _write_status(batch_id, st)
    return jsonify({"status": "ok", "manifest": manifest})


@app.route('/batch/<batch_id>/rescan', methods=['POST'])
def batch_rescan(batch_id):
    """Scan Again: discard edits and re-run separation on the original PDF."""
    if not _valid_batch(batch_id):
        return jsonify({"status": "error", "error": "Unknown batch"}), 404
    src = None
    try:
        src = _read_manifest(batch_id).get("source_pdf")
    except Exception:
        st = _read_status(batch_id) or {}
        src = st.get("source_pdf")
    if not src or not os.path.exists(src):
        return jsonify({"status": "error", "error": "Source PDF missing"}), 400
    _write_status(batch_id, {"batch_id": batch_id, "phase": "separating", "source_pdf": src})
    threading.Thread(target=_run_separation, args=(batch_id, src), daemon=True).start()
    return jsonify({"status": "started", "batch_id": batch_id})


@app.route('/batch/<batch_id>/approve', methods=['POST'])
def batch_approve(batch_id):
    """Persist the final manifest and start batch grading in the background."""
    if not _valid_batch(batch_id):
        return jsonify({"status": "error", "error": "Unknown batch"}), 404
    # Step 1 (sequential flow): REQUIRE the question paper. Web-only; the pipeline stays QP-optional.
    if not os.path.exists(QUESTION_PAPER_PATH):
        return jsonify({"status": "error", "error": "No Question Paper Found",
                        "details": "Upload and parse the Question Paper (Step 1) before evaluating."})
    if not os.path.exists(ANSWER_KEY_PATH):
        return jsonify({"status": "error", "error": "No Answer Key Found",
                        "details": "Upload and parse a Teacher's Answer Key in the 'Manage Answer Key' tab first."})
    # REQUIRE a confirmed report folder (blocks batch grading until the teacher confirms the path)
    state = _read_report_state()
    if not (state and state.get("confirmed") and state.get("path")):
        return jsonify({"status": "error", "error": "Report folder not confirmed",
                        "details": "Confirm the report save folder (shown after parsing the answer key in the 'Manage Answer Key' tab) before evaluating."})
    # Marks-source gate (same as single-student): block batch grading on a real, unconfirmed marks
    # mismatch. Confirming the "Review & fix marks" editor sets confirmed=True and unblocks.
    _ms = _load_json(MARKS_SOURCE_PATH) or {}
    if _ms.get("mismatch") and not _ms.get("confirmed"):
        return jsonify({"status": "error", "error": "Marks not confirmed",
                        "details": (f"The answer key totals {_ms.get('key_total')} but the question paper "
                                    f"totals {_ms.get('qp_total')}. Review & fix the marks (in the 'Manage "
                                    f"Answer Key' tab) before evaluating.")})
    _marks_source = _ms.get("source") if (_ms.get("mismatch") or _ms.get("edited")) else None
    try:
        existing = _read_manifest(batch_id)
    except Exception:
        return jsonify({"status": "error", "error": "No manifest to approve"}), 400
    posted = request.get_json(silent=True) or {}
    if posted.get("sheets"):
        manifest = _normalize_manifest(existing, posted["sheets"])
        _write_manifest(batch_id, manifest)
    else:
        manifest = existing
    if not manifest.get("sheets"):
        return jsonify({"status": "error", "error": "No sheets to evaluate"}), 400

    params = {"report_dir": state["path"], "exam_class": state.get("class", ""),
              "exam_subject": state.get("subject", ""), "marks_source": _marks_source,
              "question_paper_path": QUESTION_PAPER_PATH if os.path.exists(QUESTION_PAPER_PATH) else None,
              "tester_id": posted.get("tester_id", "")}
    if bool(posted.get("skip_orientation")):
        # Skip the orientation gate -> today's exact grading path (byte-for-byte unchanged).
        _write_status(batch_id, {"batch_id": batch_id, "phase": "evaluating", "manifest": manifest,
                                 "progress": {"done": 0, "total": len(manifest["sheets"]), "current": None}})
        threading.Thread(target=_run_batch_eval,
                         args=(batch_id, manifest, state["path"], state.get("class", ""), state.get("subject", ""),
                               QUESTION_PAPER_PATH if os.path.exists(QUESTION_PAPER_PATH) else None, _marks_source,
                               posted.get("tester_id", "")),
                         daemon=True).start()
    else:
        # Orientation gate: prepare (ingest+preprocess+auto-orient) every sheet, then wait for the
        # teacher to confirm orientation grouped by student before grading.
        _write_status(batch_id, {"batch_id": batch_id, "phase": "orienting", "manifest": manifest,
                                 "progress": {"done": 0, "total": len(manifest["sheets"]), "current": None}})
        threading.Thread(target=_run_batch_prepare_orientation, args=(batch_id, manifest, params),
                         daemon=True).start()
    return jsonify({"status": "started", "batch_id": batch_id})


def _run_batch_prepare_orientation(batch_id, manifest, params):
    """Background worker: prepare orientation for every sheet (offline), then publish the grouped
    per-student review for confirmation."""
    def status_cb(done, total, current):
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "orienting"
        st["progress"] = {"done": done, "total": total, "current": current}
        _write_status(batch_id, st)

    try:
        orient = batch_prepare_orientation(
            batch_id, manifest, ANSWER_KEY_PATH, status_cb=status_cb,
            report_dir=params.get("report_dir"), exam_class=params.get("exam_class"),
            exam_subject=params.get("exam_subject"), question_paper_path=params.get("question_paper_path"),
            tester_id=params.get("tester_id"))
        _write_status(batch_id, {"batch_id": batch_id, "phase": "orient_review",
                                 "manifest": manifest, "orient": orient, "params": params})
    except Exception as e:
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "error"
        st["error"] = str(e)
        _write_status(batch_id, st)


def _run_batch_resume_orientation(batch_id, manifest, rotations, params):
    """Background worker: apply the teacher's confirmed rotations and grade every sheet. Writes the
    same 'done'/'results' status shape _run_batch_eval does, so the results UI is unchanged."""
    def status_cb(done, total, current):
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "evaluating"
        st["progress"] = {"done": done, "total": total, "current": current}
        _write_status(batch_id, st)

    try:
        result = batch_resume_orientation(
            batch_id, manifest, rotations, ANSWER_KEY_PATH, status_cb=status_cb,
            report_dir=params.get("report_dir"), exam_class=params.get("exam_class"),
            exam_subject=params.get("exam_subject"), question_paper_path=params.get("question_paper_path"),
            marks_source=params.get("marks_source"), tester_id=params.get("tester_id"))
        _write_status(batch_id, {"batch_id": batch_id, "phase": "done",
                                 "manifest": manifest, "results": result})
    except Exception as e:
        st = _read_status(batch_id) or {"batch_id": batch_id}
        st["phase"] = "error"
        st["error"] = str(e)
        _write_status(batch_id, st)


@app.route('/batch/<batch_id>/confirm-orientation', methods=['POST'])
def batch_confirm_orientation(batch_id):
    """Apply the teacher's per-sheet, per-page confirmed rotations and grade the whole batch."""
    if not _valid_batch(batch_id):
        return jsonify({"status": "error", "error": "Unknown batch"}), 404
    st = _read_status(batch_id)
    if not st or st.get("phase") not in ("orient_review", "error"):
        return jsonify({"status": "error", "error": "Batch is not awaiting orientation confirmation"}), 400
    manifest = st.get("manifest") or {}
    params = st.get("params") or {}
    raw = (request.get_json(silent=True) or {}).get("rotations", {}) or {}
    # Sanitize into {run_id: {page_index(str): deg in {0,90,180,270}}}.
    rotations = {}
    for run_id, pages in raw.items():
        if not _valid_run_id(run_id) or not isinstance(pages, dict):
            continue
        clean = {}
        for k, v in pages.items():
            try:
                ki, vi = int(k), int(v) % 360
            except (TypeError, ValueError):
                continue
            if vi in (0, 90, 180, 270):
                clean[str(ki)] = vi
        if clean:
            rotations[run_id] = clean

    _write_status(batch_id, {"batch_id": batch_id, "phase": "evaluating", "manifest": manifest,
                             "params": params,
                             "progress": {"done": 0, "total": len(manifest.get("sheets", [])), "current": None}})
    threading.Thread(target=_run_batch_resume_orientation, args=(batch_id, manifest, rotations, params),
                     daemon=True).start()
    return jsonify({"status": "started", "batch_id": batch_id})


# ---------------------------------------------------------------------------
# Orientation gate (single-student): upload -> auto-orient first pass -> teacher confirms each page
# LARGE -> resume into OCR+grading on the confirmed-upright images. Async + filesystem-status polling,
# mirroring the separator flow. The synchronous /evaluate route is left intact as the legacy path.
# ---------------------------------------------------------------------------
_RUN_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _valid_run_id(run_id):
    return bool(_RUN_ID_RE.match(run_id or "")) and ".." not in (run_id or "")


def _orient_status_path(run_id):
    return os.path.join(OUTPUT_BASE, run_id, "orient_status.json")


def _read_orient_status(run_id):
    try:
        with open(_orient_status_path(run_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_orient_status(run_id, status):
    d = os.path.join(OUTPUT_BASE, run_id)
    os.makedirs(d, exist_ok=True)
    tmp = _orient_status_path(run_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f)
    os.replace(tmp, _orient_status_path(run_id))


def _saved_evaluations():
    """Every graded single-sheet run, newest first, for the "replace a previous evaluation" picker.

    Reads the working copy when the teacher has edited it (review_render.json), else the pristine
    review_state.json -- the same precedence /student-report uses. Batch runs are included: they are
    graded the same way and a teacher may equally want to supersede one.
    """
    out = []
    try:
        names = os.listdir(OUTPUT_BASE)
    except OSError:
        return out
    for run_id in names:
        if not _valid_run_id(run_id):
            continue
        run_dir = os.path.join(OUTPUT_BASE, run_id)
        if not os.path.isdir(run_dir):
            continue
        state_file = next((p for p in ("review_render.json", "review_state.json")
                           if os.path.exists(os.path.join(run_dir, p))), None)
        if not state_file:
            continue                      # not graded (or mid-run) -> nothing to supersede
        st = _load_json(os.path.join(run_dir, state_file)) or {}
        details = st.get("student_details") or {}
        evals = st.get("evaluations") or []
        try:
            mtime = os.path.getmtime(os.path.join(run_dir, state_file))
        except OSError:
            mtime = 0
        out.append({
            "run_id": run_id,
            "student_name": details.get("name") or details.get("student_name") or run_id,
            "roll_no": details.get("roll_no") or details.get("rollNo") or "",
            "questions": len(evals),
            "report_path": st.get("report_path") or "",
            "graded_at": mtime,
        })
    out.sort(key=lambda r: r["graded_at"], reverse=True)
    return out


@app.route('/previous-evaluations')
def previous_evaluations():
    """Populates the replace picker in Step 3. Read-only."""
    return jsonify({"status": "success", "evaluations": _saved_evaluations()})


def _validated_replaces(raw, new_run_id):
    """Validate a `replaces_run_id` supplied with an upload. Returns (run_id_or_None, error_or_None).

    Absent/blank is the normal case and is NOT an error -- replacing is opt-in.
    """
    old = (raw or "").strip()
    if not old:
        return None, None
    if not _valid_run_id(old):
        return None, {"status": "error", "error": "Invalid run id",
                      "details": "The evaluation to replace has an unusable id."}
    if old == new_run_id:
        # Same filename already reuses the folder and is reset in-place by _reset_run_dir; deleting it
        # here would destroy the run we are about to produce.
        return None, None
    if not os.path.isdir(os.path.join(OUTPUT_BASE, old)):
        return None, {"status": "error", "error": "Nothing to replace",
                      "details": f"No saved evaluation found for '{old}'."}
    return old, None


def _supersede_run(old_run_id, new_run_id, new_report_path=None):
    """Remove a superseded evaluation. Call ONLY after the replacement has succeeded.

    Ordering is the whole safety story: deleting up front would leave a teacher whose re-upload then
    failed with neither evaluation. Best-effort -- a failure here must never turn a successful grading
    into a reported error.

    The report PDF is deleted only when the new run wrote to a DIFFERENT path. Reports are named after
    the student, so an unchanged name means both runs share one file and the new grading has already
    overwritten it -- removing it then would delete the report we just produced.
    """
    if not old_run_id or old_run_id == new_run_id or not _valid_run_id(old_run_id):
        return {"removed": False}
    removed = {"run_id": old_run_id, "removed": False, "report_removed": False}
    old_dir = os.path.join(OUTPUT_BASE, old_run_id)
    old_report = ""
    try:
        st = (_load_json(os.path.join(old_dir, "review_render.json"))
              or _load_json(os.path.join(old_dir, "review_state.json")) or {})
        old_report = st.get("report_path") or ""
    except Exception:
        old_report = ""
    try:
        shutil.rmtree(old_dir)
        removed["removed"] = True
    except OSError as e:
        print(f"Warning: could not remove superseded run {old_run_id}: {e}")
    if old_report and new_report_path and os.path.abspath(old_report) != os.path.abspath(new_report_path):
        try:
            if os.path.isfile(old_report):
                os.remove(old_report)
                removed["report_removed"] = True
        except OSError as e:
            print(f"Warning: could not remove superseded report {old_report}: {e}")
    return removed


def _eval_prereqs():
    """Shared gate for the student-sheet flows (same checks /evaluate enforces): question paper,
    answer key, confirmed report folder, key structural validity, and the marks-source decision.
    Returns (error_dict_or_None, ctx). Kept separate so /evaluate stays byte-for-byte unchanged."""
    if not os.path.exists(QUESTION_PAPER_PATH):
        return {"status": "error", "error": "No Question Paper Found",
                "details": "Upload and parse the Question Paper (Step 1) before evaluating a student paper."}, None
    answer_key_path = os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key.json")
    if not os.path.exists(answer_key_path):
        return {"status": "error", "error": "No Answer Key Found",
                "details": "You MUST upload and parse a Teacher's Answer Key in the 'Manage Answer Key' tab before evaluating a student paper."}, None
    state = _read_report_state()
    if not (state and state.get("confirmed") and state.get("path")):
        return {"status": "error", "error": "Report folder not confirmed",
                "details": "Confirm the report save folder (shown after parsing the answer key in the 'Manage Answer Key' tab) before evaluating."}, None
    _key_json = _load_json(answer_key_path)
    _choices_json = _load_json(os.path.join(app.config['UPLOAD_FOLDER'], "current_answer_key_choices.json")) or {}
    _qp_json = _load_json(QUESTION_PAPER_PATH)
    eval_issues = validate_for_evaluation(_key_json, _choices_json, _qp_json)
    if has_blocking(eval_issues):
        _blk = next(i for i in eval_issues if i["severity"] == "error")
        return {"status": "error", "error": "Upload check failed",
                "details": _blk["message"], "issues": eval_issues}, None
    _ms = _load_json(MARKS_SOURCE_PATH) or {}
    if _ms.get("mismatch") and not _ms.get("confirmed"):
        return {"status": "error", "error": "Marks source not confirmed",
                "details": (f"The answer key totals {_ms.get('key_total')} marks but the question paper "
                            f"totals {_ms.get('qp_total')}. Choose which document to use for marks "
                            f"(shown after parsing the answer key) before evaluating.")}, None
    marks_source = _ms.get("source") if (_ms.get("mismatch") or _ms.get("edited")) else None
    return None, {"answer_key_path": answer_key_path, "state": state, "marks_source": marks_source}


def _cancelled_now(run_id, result=None):
    """True when this run was cancelled, so the worker must stay silent.

    Checked BOTH on the in-memory flag and on the returned status: the flag is cleared once
    /cancel-evaluation has finished tearing the run down, and a worker unwinding slowly can reach here
    afterwards. Writing a status then would resurrect the folder that was just deleted and leave the UI
    reporting a failure for work the teacher deliberately stopped.
    """
    return bool(is_cancelled(run_id) or (result or {}).get("status") == "cancelled")


@app.route('/cancel-evaluation/<run_id>', methods=['POST'])
def cancel_evaluation(run_id):
    """Abort an in-flight evaluation and hand the teacher back a clean slate.

    Reachable at every stage: while pages are being prepared, while the orientation review is open, and
    mid-grading. Grading is the expensive stage, so this SIGKILLs the running stage's process group
    rather than just setting a flag and letting it bill out.

    The run folder is then removed so the wrong sheet leaves nothing behind -- a cancelled run has no
    review_state.json, so it never appears in the "replace a previous evaluation" picker either.
    """
    if not _valid_run_id(run_id):
        return jsonify({"status": "error", "error": "Invalid run id"}), 400
    killed = request_cancel(run_id)
    st = _read_orient_status(run_id) or {"run_id": run_id}
    st.update({"phase": "cancelled", "error": None,
               "details": "Evaluation cancelled. Upload the correct answer sheet to start again."})
    _write_orient_status(run_id, st)          # so a poll in flight sees `cancelled`, not a hang

    # Give a stage that is mid-write a moment to die before the folder goes, so rmtree does not race it.
    time.sleep(0.4)
    removed = False
    try:
        shutil.rmtree(os.path.join(OUTPUT_BASE, run_id))
        removed = True
    except FileNotFoundError:
        removed = True
    except OSError as e:
        print(f"Warning: could not remove cancelled run {run_id}: {e}")
    # The flag is deliberately NOT cleared here. A worker can still be unwinding, and _write_orient_status
    # recreates the run folder -- clearing now would let a late write resurrect the directory we just
    # deleted and report a failure for work the teacher chose to stop. It is cleared instead when a new
    # run claims the same run_id (see /evaluate and /prepare-orientation), which is the real lifecycle.
    return jsonify({"status": "cancelled", "run_id": run_id,
                    "stages_killed": killed, "removed": removed})


def _run_prepare_orientation(run_id, filepath, params):
    """Background worker: ingest + preprocess + auto-orient first pass, then publish the review."""
    try:
        result = prepare_orientation(
            filepath, student_name=params.get("student_name", "Student"),
            answer_key_path=params.get("answer_key_path"), report_dir=params.get("report_dir"),
            exam_class=params.get("exam_class"), exam_subject=params.get("exam_subject"),
            question_paper_path=params.get("question_paper_path"),
            marks_source=params.get("marks_source"), tester_id=params.get("tester_id"))
        if _cancelled_now(run_id, result):
            return            # /cancel-evaluation owns the status and has removed the folder
        if result.get("status") == "orient_review":
            _write_orient_status(run_id, {"run_id": run_id, "phase": "orient_review",
                                          "filename": os.path.basename(filepath),
                                          "pages": result.get("pages", []), "params": params})
        else:
            _write_orient_status(run_id, {"run_id": run_id, "phase": "error",
                                          "error": result.get("error", "Preparation failed"),
                                          "details": result.get("details", "")})
    except Exception as e:
        if _cancelled_now(run_id):
            return
        _write_orient_status(run_id, {"run_id": run_id, "phase": "error", "error": str(e)})


def _run_resume_orientation(run_id, rotations, params):
    """Background worker: apply the teacher's rotations, then OCR -> grade -> report."""
    try:
        result = resume_after_orientation(
            run_id, rotations=rotations, student_name=params.get("student_name", "Student"),
            answer_key_path=params.get("answer_key_path"), report_dir=params.get("report_dir"),
            exam_class=params.get("exam_class"), exam_subject=params.get("exam_subject"),
            question_paper_path=params.get("question_paper_path"),
            marks_source=params.get("marks_source"), tester_id=params.get("tester_id"))
        if _cancelled_now(run_id, result):
            return            # cancelled mid-grading: never supersede, never publish a result
        st = _read_orient_status(run_id) or {"run_id": run_id}
        if result.get("status") == "success":
            # Only now is it safe to drop the evaluation this one replaces -- had grading failed, the
            # teacher would still have the original.
            if params.get("replaces_run_id"):
                result["replaced"] = _supersede_run(params["replaces_run_id"], run_id,
                                                    result.get("report_path"))
            st.update({"phase": "done", "result": result})
        else:
            st.update({"phase": "error", "error": result.get("error", "Evaluation failed"),
                       "details": result.get("details", "")})
        _write_orient_status(run_id, st)
    except Exception as e:
        if _cancelled_now(run_id):
            return
        st = _read_orient_status(run_id) or {"run_id": run_id}
        st.update({"phase": "error", "error": str(e)})
        _write_orient_status(run_id, st)


@app.route('/prepare-orientation', methods=['POST'])
def prepare_orientation_route():
    """Phase 1: accept the student sheet, run the same gates as /evaluate, then kick off ingest +
    preprocess + auto-orient in the background. Returns a run_id the UI polls."""
    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "No file part"})
    file = request.files['file']
    student_name = request.form.get('student_name', 'Student')
    if file.filename == '':
        return jsonify({"status": "error", "error": "No selected file"})

    err, ctx = _eval_prereqs()
    if err:
        return jsonify(err)

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    run_id = os.path.splitext(filename)[0]
    if not _valid_run_id(run_id):
        return jsonify({"status": "error", "error": "Invalid file name",
                        "details": "Use only letters, numbers, '.', '-' or '_' in the sheet's file name."})

    replaces, rep_err = _validated_replaces(request.form.get('replaces_run_id', ''), run_id)
    if rep_err:
        return jsonify(rep_err)

    # A previous attempt at this same file name may have been cancelled; the flag is left set on purpose
    # so late workers stay quiet, so clearing it is the new run's job.
    clear_cancel(run_id)

    params = {"student_name": student_name, "answer_key_path": ctx["answer_key_path"],
              "report_dir": ctx["state"]["path"], "exam_class": ctx["state"].get("class", ""),
              "exam_subject": ctx["state"].get("subject", ""),
              "question_paper_path": QUESTION_PAPER_PATH if os.path.exists(QUESTION_PAPER_PATH) else None,
              "marks_source": ctx["marks_source"], "tester_id": request.form.get('tester_id', ''),
              # Carried through orient_status so phase 2 can supersede once grading actually succeeds.
              "replaces_run_id": replaces}
    _write_orient_status(run_id, {"run_id": run_id, "phase": "preparing", "filename": filename})
    threading.Thread(target=_run_prepare_orientation, args=(run_id, filepath, params), daemon=True).start()
    return jsonify({"status": "started", "run_id": run_id})


@app.route('/orient-status/<run_id>')
def orient_status(run_id):
    if not _valid_run_id(run_id):
        return jsonify({"status": "error", "error": "Invalid run id"}), 400
    st = _read_orient_status(run_id)
    if st is None:
        return jsonify({"status": "error", "error": "Unknown run"}), 404
    # Live pipeline step for the "Processing Paper" checklist (written by full_evaluator._write_progress
    # as each stage begins). Only meaningful while grading; ignored once done/error.
    if st.get("phase") == "evaluating":
        prog = _load_json(os.path.join(OUTPUT_BASE, run_id, "progress.json"))
        if prog:
            st = {**st, "progress": prog}
    return jsonify(st)


@app.route('/orient-preview/<run_id>/<int:page>')
def orient_preview(run_id, page):
    """Serve the pristine (un-rotated) preprocessed page, downscaled. The browser rotates it via CSS
    to show the current angle, so what the teacher sees is exactly what OCR will grade on confirm."""
    if not _valid_run_id(run_id):
        abort(404)
    # Read the page->file map from the pipeline-written review manifest (exists for BOTH the
    # single-student and batch flows: prepare_orientation writes output/<run_id>/orientation_review.json).
    review = _load_json(os.path.join(OUTPUT_BASE, run_id, "orientation_review.json")) or {}
    match = next((p for p in review.get("pages", []) if p.get("index") == page), None)
    if not match:
        abort(404)
    img_path = os.path.join(OUTPUT_BASE, run_id, "preprocessed", match["file"])
    if not os.path.exists(img_path):
        abort(404)
    import cv2  # local import: keep app startup free of a hard cv2 dependency
    im = cv2.imread(img_path)
    if im is None:
        abort(404)
    h, w = im.shape[:2]
    max_w = 1400
    if w > max_w:
        s = max_w / w
        im = cv2.resize(im, (max_w, int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        abort(500)
    return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")


@app.route('/answer-crop/<run_id>/<path:filename>')
def answer_crop(run_id, filename):
    """Serve one per-answer screenshot from output/<run_id>/answer_crops/.

    The crops are referenced by FILENAME in the report data (never inlined as base64), which keeps
    review_state.json small enough to persist and archive. send_from_directory confines the read to
    the crops folder, so a traversal in `filename` cannot escape it."""
    if not _valid_run_id(run_id):
        abort(404)
    crops_dir = os.path.join(OUTPUT_BASE, run_id, "answer_crops")
    if not os.path.isdir(crops_dir):
        abort(404)
    return send_from_directory(crops_dir, filename)


@app.route('/confirm-orientation/<run_id>', methods=['POST'])
def confirm_orientation(run_id):
    """Phase 2: apply the teacher's confirmed per-page rotations and resume into OCR + grading."""
    if not _valid_run_id(run_id):
        return jsonify({"status": "error", "error": "Invalid run id"}), 400
    st = _read_orient_status(run_id)
    if not st:
        return jsonify({"status": "error", "error": "Unknown run"}), 404

    raw = (request.get_json(silent=True) or {}).get("rotations", {}) or {}
    rotations = {}
    for k, v in raw.items():
        try:
            ki, vi = int(k), int(v) % 360
        except (TypeError, ValueError):
            continue
        if vi in (0, 90, 180, 270):
            rotations[str(ki)] = vi

    params = st.get("params", {})
    st.update({"phase": "evaluating", "progress": None})
    _write_orient_status(run_id, st)
    threading.Thread(target=_run_resume_orientation, args=(run_id, rotations, params), daemon=True).start()
    return jsonify({"status": "started", "run_id": run_id})


@app.route('/student-report/<run_id>')
def student_report(run_id):
    """Authoritative per-question report for a graded run (single OR batch student), read from the
    working copy (review_render.json) if the teacher has edited it, else the pristine
    review_state.json. Powers the batch card -> full report open and single-mode reload recovery.
    Read-only."""
    if not _valid_run_id(run_id):
        return jsonify({"status": "error", "error": "Invalid run id"}), 400
    run_dir = os.path.join(OUTPUT_BASE, run_id)
    st = load_working_state(run_dir)
    if st is None:
        return jsonify({"status": "error", "error": "No saved evaluation for this run"}), 404
    evals = st.get("evaluations", []) or []
    return jsonify({
        "status": "success",
        "review_id": run_id,
        "student_details": st.get("student_details") or {},
        "evaluations": evals,
        "report_path": st.get("report_path"),
        "review_progress": compute_review_progress(evals),
    })


if __name__ == '__main__':
    # host/port/debug are env-overridable. debug defaults ON for local dev, but MUST be OFF when the
    # app is exposed publicly (e.g. via a Cloudflare tunnel) — Flask's debugger allows remote code
    # execution. run-public.sh sets FLASK_DEBUG=0 + APP_AUTH_PASSWORD. threaded=True lets the browser's
    # page load + status polling run concurrently (the app already coordinates jobs via files/threads).
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "5005"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    gated = " (password gate ENABLED)" if os.environ.get("APP_AUTH_PASSWORD") else ""
    print("AI Evaluation Web App starting%s on http://localhost:%d" % (gated, port))
    app.run(host=host, port=port, debug=debug, threaded=True)
