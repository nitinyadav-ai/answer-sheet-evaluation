#!/usr/bin/env python3
"""report_sync.py -- collect every completed evaluation report into a LOCAL archive on this Mac.

Runs OFF the grading hot path (a launchd agent / cron on the central host). It scans the evaluator's
``output/<run_id>/`` directories for COMPLETED reports (a run is complete exactly when
``review_state.json`` exists -- which uniformly identifies every single- AND batch-student report),
bundles each into a single ``.zip`` (the lossless report JSON + OCR result + PDF + a manifest with a
sha256 per file), and writes it to a LOCAL archive -- no cloud, no account, no credentials, nothing
leaves the Mac:

  * a bundle .zip  -- the durable, immutable per-report archive under <archive>/bundles/
  * a SQLite index -- queryable verification at <archive>/index.sqlite3 (+ a human-readable index.csv)

It is IDEMPOTENT: a report is archived once (keyed by the sha256 of its report JSON); a teacher edit
changes that hash so it re-archives as a new *version* (history preserved); a failure simply leaves the
run un-marked so the next tick retries. The first pass backfills every existing run. The archive sits on
the same disk as ``output/`` (which already holds the original scans), so by default the bundle carries
only the small lossless data (JSON + OCR + PDF + manifest) and does NOT duplicate the ~59 MB of scans --
set include_evidence=true for fully self-contained bundles.

Zero third-party deps (stdlib only: sqlite3 + zipfile + hashlib + json). Config is a gitignored
``~/.report_sync/config`` (JSON) or ``REPORT_SYNC_*`` env -- paths + toggles only, no secrets.

Usage:
    python3 scripts/report_sync.py --dry-run     # list what WOULD be archived (no writes)
    python3 scripts/report_sync.py --once        # one pass (used by the launchd agent); backfills too
    python3 scripts/report_sync.py --loop --interval 300
"""
import argparse
import csv
import datetime
import glob
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.report_sync")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config")
LEDGER_PATH = os.path.join(CONFIG_DIR, "state.json")
DEFAULT_ARCHIVE = os.path.expanduser("~/Evaluation Report Archive")
BUNDLE_SCHEMA = "1"

# Report files copied verbatim into the bundle when present (the run dir's own artifacts).
_RUN_FILES = ("review_state.json", "review_render.json", "run_meta.json", "db_answers.json",
              "key_integrity.json", "student_details.json", "orientation_review.json",
              "stage_timings.json", "progress.json", "api_costs.jsonl", "diagram_crops.json")
# Source-evidence subdirs. ocr_output/ (the OCR result -- small, NOT regenerable) is ALWAYS bundled;
# images/ (original scans) is opt-in (they already live in output/ on this same Mac, so off by default to
# avoid a ~59 MB duplicate per report); preprocessed/ is DERIVED from images/ and off by default too.
_MODEL_ENV_KEYS = ("OCR_MODEL", "EVAL_MODEL", "KEY_PARSER_MODEL", "QP_PARSER_MODEL",
                   "DIAGRAM_EVAL_MODEL", "DIAGRAM_FEATURES_MODEL", "SEPARATOR_MODEL")
_FLAG_ENV_KEYS = ("EVAL_CASCADE", "EVAL_POINTWISE", "EVAL_VOTES", "EVAL_MAX_TOKENS",
                  "EVAL_REASONING_EFFORT", "OCR_ORIENT_VOTE", "OCR_ARBITRATE", "OCR_VERIFY_MATH",
                  "LLM_PROVIDER_SORT", "LLM_PROVIDER_ORDER", "RECONCILE_KEY_MARKS_WITH_QP",
                  "LLM_USAGE_ACCOUNTING")
_INDEX_COLS = ("run_id", "content_hash", "version", "tester_id", "student_name", "exam_class",
               "exam_subject", "total_awarded", "total_max", "num_questions", "needs_review_count",
               "off_topic_count", "injection_count", "bundle_path", "bundle_bytes", "app_git_sha",
               "model_ids", "env_flags", "review_state", "graded_at", "uploaded_at")


# ---------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------
def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _slug(s):
    """Filesystem-safe token for archive paths (never carries raw spaces/slashes)."""
    s = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(s or "").strip())
    return s.strip("-.") or "unknown"


def _parse_env_file(path):
    """Parse a .env file into a dict (same rules full_evaluator uses). Best-effort."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if v[:1] not in ('"', "'") and "#" in v:
                        v = v.split("#", 1)[0].strip()
                    out[k] = v.strip('"').strip("'")
    except Exception:
        pass
    return out


def _git_sha():
    try:
        return subprocess.check_output(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True,
                                       encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------------------------------
# Config + ledger
# ---------------------------------------------------------------------------------------------------
def load_config():
    """Config from ~/.report_sync/config (JSON) overlaid with REPORT_SYNC_* env. Paths/toggles only."""
    cfg = {
        "enabled": True,                 # local + harmless; set false to pause the launchd agent
        "archive_dir": DEFAULT_ARCHIVE,  # where bundles + index.sqlite3 live (on this Mac)
        "output_base": os.path.join(REPO_ROOT, "output"),
        "tester_default": "central",
        "include_evidence": False,       # copy the original scans (images/) into each bundle? off = no dup
        "include_preprocessed": False,   # derived PNGs -- off (regenerable)
        "index_full_review_state": True, # store the full graded JSON in the SQLite index (queryable)
    }
    file_cfg = _load_json(CONFIG_PATH, {}) or {}
    cfg.update({k: v for k, v in file_cfg.items() if v is not None})
    for k in list(cfg.keys()):           # env overlay: REPORT_SYNC_<UPPER>
        env_v = os.environ.get("REPORT_SYNC_" + k.upper())
        if env_v is not None:
            cfg[k] = (env_v.strip().lower() not in ("0", "false", "no", "off", "")) \
                if isinstance(cfg[k], bool) else env_v
    return cfg


def load_ledger():
    return _load_json(LEDGER_PATH, {}) or {}


def save_ledger(ledger):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    os.replace(tmp, LEDGER_PATH)


# ---------------------------------------------------------------------------------------------------
# Report inspection: which runs are ready, their content hash, and a summary for the index
# ---------------------------------------------------------------------------------------------------
def scan_ready(output_base):
    """Yield (run_id, run_dir) for every dir with a review_state.json (a completed report)."""
    if not os.path.isdir(output_base):
        return
    for name in sorted(os.listdir(output_base)):
        run_dir = os.path.join(output_base, name)
        if os.path.isdir(run_dir) and os.path.exists(os.path.join(run_dir, "review_state.json")):
            yield name, run_dir


def content_hash(run_dir):
    """Identity of a report's CONTENT: sha256 over review_state.json (+ review_render.json if the teacher
    edited it). Changes iff the graded/edited result changes -> drives version + skip-unchanged."""
    h = hashlib.sha256()
    for fn in ("review_state.json", "review_render.json"):
        p = os.path.join(run_dir, fn)
        if os.path.exists(p):
            h.update(fn.encode())
            h.update(_read_bytes(p))
    return h.hexdigest()


def _q_dicts(evaluations):
    """Yield each question's result dict, tolerating both [qid, dict] pairs and bare dicts."""
    for item in evaluations or []:
        if isinstance(item, dict):
            yield item
        elif isinstance(item, (list, tuple)):
            yield next((x for x in item if isinstance(x, dict)), {})


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def summarize(review_state):
    """Marks totals + flag counts for the index row (defensive against structure drift)."""
    evals = review_state.get("evaluations") or []
    awarded = maximum = 0.0
    needs_review = off_topic = injection = 0
    for q in _q_dicts(evals):
        awarded += _num(q.get("Marks Awarded"))
        maximum += _num(q.get("Maximum Marks"))
        if str(q.get("Needs Review (Yes/No)", "")).strip().lower() == "yes":
            needs_review += 1
        if str(q.get("Off-Topic (Yes/No)", "")).strip().lower() == "yes":
            off_topic += 1
        inj = q.get("Prompt Injection Detected")
        if inj is True or str(inj).strip().lower() == "yes":
            injection += 1
    return {
        "num_questions": len(evals),
        "total_awarded": round(awarded, 4),
        "total_max": round(maximum, 4),
        "needs_review_count": needs_review,
        "off_topic_count": off_topic,
        "injection_count": injection,
    }


def grade_config(run_dir):
    """The models + flags a report was graded with. Prefer the per-run run_meta.json (exact, grade-time);
    fall back to the current .env (approximate, for legacy runs graded before run_meta existed)."""
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    if isinstance(meta, dict) and (meta.get("models") or meta.get("flags")):
        return meta.get("models", {}), meta.get("flags", {}), meta.get("app_git_sha") or _git_sha(), "run_meta"
    env = _parse_env_file(os.path.join(REPO_ROOT, ".env"))
    models = {k: env[k] for k in _MODEL_ENV_KEYS if k in env}
    flags = {k: env[k] for k in _FLAG_ENV_KEYS if k in env}
    return models, flags, _git_sha(), "env"


# ---------------------------------------------------------------------------------------------------
# Bundle: zip the report data (+ optional scans) + PDF + manifest (with a sha256 per member)
# ---------------------------------------------------------------------------------------------------
def build_bundle(run_id, run_dir, cfg, version):
    """Return (zip_bytes, manifest_dict, summary_dict). Adds each report file, ocr_output/ (always),
    images/preprocessed (opt-in), the PDF (from review_state.report_path), and a manifest hashing
    every member. When scans are excluded, the manifest records run_dir so they stay findable."""
    review_state = _load_json(os.path.join(run_dir, "review_state.json"), {}) or {}
    student = review_state.get("student_details") or {}
    tester_id = (review_state.get("tester_id") or student.get("tester_id")
                 or cfg.get("tester_default") or "central")
    files_meta = {}
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def _add(arcname, data):
            files_meta[arcname] = {"sha256": _sha256_bytes(data), "bytes": len(data)}
            zf.writestr(arcname, data)

        def _add_dir(sub):
            base = os.path.join(run_dir, sub)
            for p in sorted(glob.glob(os.path.join(base, "**", "*"), recursive=True)):
                if os.path.isfile(p):
                    _add(os.path.join(sub, os.path.relpath(p, base)), _read_bytes(p))

        for fn in _RUN_FILES:
            p = os.path.join(run_dir, fn)
            if os.path.exists(p):
                _add(fn, _read_bytes(p))
        _add_dir("ocr_output")                        # always: small + the OCR result (not regenerable)
        if cfg.get("include_evidence", False):
            _add_dir("images")                        # original scans -> opt-in (already in output/)
        if cfg.get("include_preprocessed", False):
            _add_dir("preprocessed")                  # derived from images/ -> opt-in
        # The rendered PDF lives OUTSIDE the run dir (report_dir); pull it in when resolvable.
        pdf_path = os.path.expanduser(review_state.get("report_path") or "")
        if pdf_path and os.path.isfile(pdf_path):
            _add("report.pdf", _read_bytes(pdf_path))

        models, flags, git_sha, cfg_source = grade_config(run_dir)
        summary = summarize(review_state)
        graded_at = datetime.datetime.fromtimestamp(
            os.path.getmtime(os.path.join(run_dir, "review_state.json")),
            datetime.timezone.utc).isoformat()
        manifest = {
            "bundle_schema": BUNDLE_SCHEMA,
            "run_id": run_id,
            "run_dir": os.path.abspath(run_dir),      # where the (excluded) original scans still live
            "content_hash": content_hash(run_dir),
            "version": version,
            "tester_id": tester_id,
            "student": {"name": student.get("name") or review_state.get("student_name"),
                        "roll_no": student.get("roll_no"),
                        "class": student.get("class") or review_state.get("exam_class"),
                        "subject": student.get("subject") or review_state.get("exam_subject")},
            "exam_class": review_state.get("exam_class"),
            "exam_subject": review_state.get("exam_subject"),
            "summary": summary,
            "app": {"name": "ai-answer-evaluator", "git_sha": git_sha},
            "grading": {"model_ids": models, "env_flags": flags, "config_source": cfg_source},
            "graded_at": graded_at,
            "archived_at": _now_iso(),
            "evidence_included": bool(cfg.get("include_evidence", False)),
            "pdf_included": "report.pdf" in files_meta,
            "files": files_meta,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return buf.getvalue(), manifest, summarize(review_state)


def index_row(manifest, review_state, bundle_key, bundle_bytes, cfg):
    """Flatten a bundle's manifest into the index row (idempotent on run_id+content_hash)."""
    s = manifest["summary"]
    row = {
        "run_id": manifest["run_id"], "content_hash": manifest["content_hash"],
        "version": manifest["version"], "tester_id": manifest["tester_id"],
        "student_name": (manifest["student"] or {}).get("name"),
        "exam_class": manifest.get("exam_class"), "exam_subject": manifest.get("exam_subject"),
        "total_awarded": s["total_awarded"], "total_max": s["total_max"],
        "num_questions": s["num_questions"], "needs_review_count": s["needs_review_count"],
        "off_topic_count": s["off_topic_count"], "injection_count": s["injection_count"],
        "bundle_path": bundle_key, "bundle_bytes": bundle_bytes,
        "app_git_sha": manifest["app"]["git_sha"],
        "model_ids": manifest["grading"]["model_ids"], "env_flags": manifest["grading"]["env_flags"],
        "graded_at": manifest["graded_at"], "uploaded_at": manifest["archived_at"],
    }
    if cfg.get("index_full_review_state", True):
        row["review_state"] = review_state
    return row


# ---------------------------------------------------------------------------------------------------
# LocalSink: bundle .zip on disk + a SQLite index row (+ a human-readable CSV line)
# ---------------------------------------------------------------------------------------------------
class LocalSink:
    """Write each report's bundle + index row to a LOCAL archive dir on this Mac. No network, no creds."""
    def __init__(self, cfg):
        self.archive_dir = os.path.expanduser(cfg.get("archive_dir") or DEFAULT_ARCHIVE)
        self.bundles_dir = os.path.join(self.archive_dir, "bundles")
        self.db_path = os.path.join(self.archive_dir, "index.sqlite3")
        self.csv_path = os.path.join(self.archive_dir, "index.csv")
        os.makedirs(self.bundles_dir, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS report_submissions ("
                "run_id TEXT, content_hash TEXT, version INTEGER, tester_id TEXT, student_name TEXT, "
                "exam_class TEXT, exam_subject TEXT, total_awarded REAL, total_max REAL, "
                "num_questions INTEGER, needs_review_count INTEGER, off_topic_count INTEGER, "
                "injection_count INTEGER, bundle_path TEXT, bundle_bytes INTEGER, app_git_sha TEXT, "
                "model_ids TEXT, env_flags TEXT, review_state TEXT, graded_at TEXT, uploaded_at TEXT, "
                "PRIMARY KEY (run_id, content_hash))")
            con.commit()
        finally:
            con.close()

    def put_bundle(self, key, data):
        path = os.path.join(self.bundles_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def upsert_index(self, row):
        vals = [json.dumps(row[c]) if isinstance(row.get(c), (dict, list)) else row.get(c)
                for c in _INDEX_COLS]
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(f"INSERT OR REPLACE INTO report_submissions ({','.join(_INDEX_COLS)}) "
                        f"VALUES ({','.join('?' * len(_INDEX_COLS))})", vals)
            con.commit()
        finally:
            con.close()
        new = not os.path.exists(self.csv_path)
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:   # holds raw student names
            w = csv.writer(f)
            if new:
                w.writerow(["archived_at", "tester_id", "exam_subject", "student_name", "run_id",
                            "version", "total_awarded", "total_max", "needs_review", "off_topic",
                            "injection", "bundle_path"])
            w.writerow([row.get("uploaded_at"), row.get("tester_id"), row.get("exam_subject"),
                        row.get("student_name"), row.get("run_id"), row.get("version"),
                        row.get("total_awarded"), row.get("total_max"), row.get("needs_review_count"),
                        row.get("off_topic_count"), row.get("injection_count"), row.get("bundle_path")])


def make_sink(cfg):
    return LocalSink(cfg)


# ---------------------------------------------------------------------------------------------------
# One run, one pass, entrypoint
# ---------------------------------------------------------------------------------------------------
def process_run(run_id, run_dir, cfg, ledger, sink, dry_run, log):
    ch = content_hash(run_dir)
    prev = ledger.get(run_id)
    if prev and prev.get("hash") == ch:
        return "skip"
    version = (int(prev.get("version", 0)) + 1) if prev else 1
    data, manifest, _ = build_bundle(run_id, run_dir, cfg, version)
    key = f"{_slug(manifest['tester_id'])}/{_slug(manifest.get('exam_subject') or manifest.get('exam_class'))}/{_slug(run_id)}/v{version}-{ch[:12]}.zip"
    if dry_run:
        s = manifest["summary"]
        log(f"  WOULD ARCHIVE {run_id}  v{version}  {len(data)//1024} KB  "
            f"marks={s['total_awarded']}/{s['total_max']} q={s['num_questions']} -> {key}")
        return "would-archive"
    review_state = _load_json(os.path.join(run_dir, "review_state.json"), {}) or {}
    sink.put_bundle(key, data)
    sink.upsert_index(index_row(manifest, review_state, key, len(data), cfg))
    ledger[run_id] = {"hash": ch, "version": version, "archived_at": manifest["archived_at"],
                      "bundle_path": key}
    save_ledger(ledger)
    log(f"  ARCHIVED {run_id}  v{version}  {len(data)//1024} KB -> {key}")
    return "archived"


def run_once(cfg, dry_run, log):
    ledger = load_ledger()
    sink = None if dry_run else make_sink(cfg)
    counts = {"archived": 0, "would-archive": 0, "skip": 0, "failed": 0}
    for run_id, run_dir in scan_ready(cfg["output_base"]):
        try:
            counts[process_run(run_id, run_dir, cfg, ledger, sink, dry_run, log)] += 1
        except Exception as e:              # noqa: BLE001 -- one bad run must not stop the sweep
            counts["failed"] += 1
            log(f"  FAILED {run_id}: {type(e).__name__}: {e}")
    where = os.path.expanduser(cfg.get("archive_dir") or DEFAULT_ARCHIVE)
    log(f"report_sync: {counts} (archive={where})")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Archive evaluation reports to a local folder on this Mac.")
    ap.add_argument("--once", action="store_true", help="one pass then exit (launchd/cron)")
    ap.add_argument("--loop", action="store_true", help="run forever, sleeping --interval between passes")
    ap.add_argument("--interval", type=int, default=300, help="seconds between passes in --loop mode")
    ap.add_argument("--dry-run", action="store_true", help="list what would be archived; write nothing")
    args = ap.parse_args(argv)

    cfg = load_config()
    if not args.dry_run and not cfg.get("enabled", True):
        print("report_sync: disabled (set enabled=true in ~/.report_sync/config). Use --dry-run to preview.")
        return 0
    if args.loop:
        while True:
            run_once(cfg, args.dry_run, print)
            time.sleep(max(30, args.interval))
    run_once(cfg, args.dry_run, print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
