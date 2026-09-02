"""Offline tests for report_sync (the LOCAL report archive). No network by nature -- everything is local
disk + SQLite. Pins the contract: only completed runs are archived; each bundle carries the lossless report
data + a manifest whose sha256s verify; the SQLite index round-trips; archiving is idempotent and versions a
teacher edit; the include_evidence toggle includes/excludes the original scans."""
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import report_sync as rs  # noqa: E402


def _make_run(root, run_id, awarded=(1.0, 1.0), flags=None, render=False, with_image=True):
    """Create output/<run_id>/ with a minimal review_state.json (+ optional review_render / scan / pdf)."""
    d = os.path.join(root, run_id)
    os.makedirs(os.path.join(d, "ocr_output"), exist_ok=True)
    flags = flags or {}
    q = {"Marks Awarded": awarded[0], "Maximum Marks": awarded[1],
         "Needs Review (Yes/No)": flags.get("nr", "No"),
         "Off-Topic (Yes/No)": flags.get("ot", "No"),
         "Prompt Injection Detected": flags.get("inj", "No")}
    rstate = {"review_id": run_id, "student_name": "Test Student",
              "student_details": {"name": "Test Student", "roll_no": "42", "tester_id": "MrX"},
              "evaluations": [["Q1", q]], "exam_class": "Class X", "exam_subject": "Mathematics",
              "tester_id": "MrX", "report_path": os.path.join(d, "report.pdf")}
    with open(os.path.join(d, "review_state.json"), "w") as f:
        json.dump(rstate, f)
    with open(os.path.join(d, "ocr_output", "ocr_answers.json"), "w") as f:
        json.dump({"Q1": "x"}, f)
    with open(os.path.join(d, "report.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 fake")
    if with_image:
        os.makedirs(os.path.join(d, "images"), exist_ok=True)
        with open(os.path.join(d, "images", "page_1.png"), "wb") as f:
            f.write(b"\x89PNG" + b"0" * 2000)
    if render:
        with open(os.path.join(d, "review_render.json"), "w") as f:
            json.dump({"edited": True}, f)
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    arch = tmp_path / "archive"
    monkeypatch.setattr(rs, "LEDGER_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(rs, "CONFIG_DIR", str(tmp_path / "cfgdir"))
    cfg = {"output_base": str(out), "archive_dir": str(arch), "tester_default": "central",
           "include_evidence": False, "include_preprocessed": False, "index_full_review_state": True}
    return cfg, str(out), str(arch)


# ---- scan / summarize ----------------------------------------------------------------------------
def test_scan_ready_only_completed(env):
    cfg, out, _ = env
    _make_run(out, "done1")
    os.makedirs(os.path.join(out, "in_progress"))          # no review_state.json -> not ready
    ready = dict(rs.scan_ready(out))
    assert "done1" in ready and "in_progress" not in ready


def test_summarize_marks_and_flags():
    rstate = {"evaluations": [
        ["Q1", {"Marks Awarded": 2, "Maximum Marks": 3, "Needs Review (Yes/No)": "Yes"}],
        ["Q2", {"Marks Awarded": 1, "Maximum Marks": 1, "Off-Topic (Yes/No)": "Yes",
                "Prompt Injection Detected": "Yes"}]]}
    assert rs.summarize(rstate) == {"num_questions": 2, "total_awarded": 3.0, "total_max": 4.0,
                                    "needs_review_count": 1, "off_topic_count": 1, "injection_count": 1}


# ---- bundle + manifest ---------------------------------------------------------------------------
def test_bundle_members_and_manifest_checksums(env):
    cfg, out, _ = env
    d = _make_run(out, "run1")
    data, manifest, _ = rs.build_bundle("run1", d, cfg, version=1)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert {"review_state.json", "manifest.json", "report.pdf", "ocr_output/ocr_answers.json"} <= set(names)
    assert "images/page_1.png" not in names                # evidence off by default (no scan duplication)
    for n, meta in manifest["files"].items():              # every manifest checksum verifies
        assert hashlib.sha256(z.read(n)).hexdigest() == meta["sha256"]
    assert manifest["tester_id"] == "MrX"
    assert manifest["evidence_included"] is False and manifest["pdf_included"] is True
    assert os.path.abspath(d) == manifest["run_dir"]       # scans stay findable via run_dir


def test_include_evidence_adds_scans(env):
    cfg, out, _ = env
    d = _make_run(out, "run2")
    data, manifest, _ = rs.build_bundle("run2", d, dict(cfg, include_evidence=True), version=1)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert "images/page_1.png" in z.namelist() and manifest["evidence_included"] is True


# ---- LocalSink + process_run ---------------------------------------------------------------------
def test_localsink_roundtrip_and_process(env):
    cfg, out, arch = env
    d = _make_run(out, "run3")
    assert rs.process_run("run3", d, cfg, {}, rs.make_sink(cfg), False, lambda *_: None) == "archived"
    con = sqlite3.connect(os.path.join(arch, "index.sqlite3"))
    row = con.execute("select tester_id, student_name, total_awarded, total_max, version, bundle_path "
                      "from report_submissions where run_id='run3'").fetchone()
    con.close()
    assert row[0] == "MrX" and row[2] == 1.0 and row[4] == 1
    assert os.path.exists(os.path.join(arch, "bundles", row[5]))   # the bundle .zip is on disk


def test_idempotent_skip_and_version_bump(env):
    cfg, out, arch = env
    d = _make_run(out, "run4")
    ledger, sink = {}, rs.make_sink(cfg)
    assert rs.process_run("run4", d, cfg, ledger, sink, False, lambda *_: None) == "archived"
    assert rs.process_run("run4", d, cfg, ledger, sink, False, lambda *_: None) == "skip"      # unchanged
    with open(os.path.join(d, "review_render.json"), "w") as f:   # teacher edit -> new content hash
        json.dump({"edited": 1}, f)
    assert rs.process_run("run4", d, cfg, ledger, sink, False, lambda *_: None) == "archived"  # -> v2
    con = sqlite3.connect(os.path.join(arch, "index.sqlite3"))
    versions = [r[0] for r in con.execute(
        "select version from report_submissions where run_id='run4' order by version").fetchall()]
    con.close()
    assert versions == [1, 2]                              # full history preserved


def test_dry_run_writes_nothing(env):
    cfg, out, arch = env
    d = _make_run(out, "run5")
    assert rs.process_run("run5", d, cfg, {}, None, dry_run=True, log=lambda *_: None) == "would-archive"
    assert not os.path.exists(os.path.join(arch, "index.sqlite3"))
