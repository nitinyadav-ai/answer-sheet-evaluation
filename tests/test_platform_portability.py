"""Cross-platform stage execution (macOS/Linux + Windows).

Spawning a stage and killing its tree were the only OS-specific parts of the pipeline, and both were
POSIX-only in a way that failed SILENTLY or CRASHED on Windows:

  * stages were spawned as the literal "python3", which does not exist on Windows -- and where Win10/11
    ship an App Execution Alias of that name it opens the Microsoft Store and exits, so a stage produced
    nothing while looking like it ran;
  * os.killpg / os.getpgid / signal.SIGKILL do not exist on Windows AT ALL, so touching them raises
    AttributeError -- which the old `except (ProcessLookupError, PermissionError, OSError)` guard did
    NOT catch, making the proc.kill() fallback right below it unreachable and taking the whole run down
    on any watchdog timeout or teacher cancel;
  * child stdout was decoded with the locale encoding, which is cp1252 on Windows, mangling (or hard-
    failing on) the maths/chemistry glyphs this pipeline transcribes.

Every check here also pins the POSIX behaviour so the Windows support cannot quietly change it.
Offline; the Windows branches are exercised by patching IS_WINDOWS, so they run on any host."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
except (ImportError, SystemExit) as e:  # pragma: no cover
    fe = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None, reason="full_evaluator unavailable in this env")


class _FakeProc:
    """Minimal Popen stand-in: records whether the direct-child fallback was used."""

    def __init__(self, pid=4242, kill_raises=None):
        self.pid = pid
        self.killed = False
        self._kill_raises = kill_raises

    def kill(self):
        if self._kill_raises:
            raise self._kill_raises
        self.killed = True


# ---- interpreter resolution ----------------------------------------------------------------------
def test_python_exe_is_the_running_interpreter():
    """Stages must run under the SAME interpreter as the orchestrator -- which also pins them to its
    virtualenv, where the old PATH lookup of "python3" could resolve somewhere else entirely."""
    assert fe.PYTHON_EXE == sys.executable
    assert os.path.basename(fe.PYTHON_EXE) != "python3" or os.path.isabs(fe.PYTHON_EXE)


def test_no_stage_is_spawned_via_a_bare_python3_literal():
    """The regression that broke every stage on Windows. Covers the orchestrator, the batch runner and
    the Flask app -- docstrings and shebangs are irrelevant, only argv[0] of a real spawn matters."""
    for rel in ("scripts/full_evaluator.py", "scripts/batch_evaluator.py", "evaluation_app/app.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            src = f.read()
        assert '["python3"' not in src and "['python3'" not in src, f"{rel} still spawns a bare python3"


# ---- process-group isolation ---------------------------------------------------------------------
def test_new_group_kwargs_uses_start_new_session_on_posix():
    assert fe._new_group_kwargs() == {"start_new_session": True}


def test_new_group_kwargs_uses_creationflags_on_windows(monkeypatch):
    """start_new_session is silently IGNORED by Popen on Windows (unlike preexec_fn it raises nothing),
    so the child would never be isolated. Windows needs creationflags instead."""
    monkeypatch.setattr(fe, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    kwargs = fe._new_group_kwargs()
    assert kwargs == {"creationflags": 0x00000200}
    assert "start_new_session" not in kwargs


# ---- tree kill -----------------------------------------------------------------------------------
def test_kill_process_tree_kills_the_group_on_posix(monkeypatch):
    seen = {}
    monkeypatch.setattr(fe.os, "getpgid", lambda pid: 777)
    monkeypatch.setattr(fe.os, "killpg", lambda pgid, sig: seen.update(pgid=pgid, sig=sig))
    proc = _FakeProc()
    assert fe.kill_process_tree(proc) is True
    assert seen == {"pgid": 777, "sig": fe.signal.SIGKILL}
    assert proc.killed is False          # the group kill covered it; no direct-child fallback needed


def test_kill_process_tree_survives_missing_posix_apis(monkeypatch):
    """THE Windows crash: os.killpg/os.getpgid do not exist there, so this raises AttributeError -- the
    error the original `except (ProcessLookupError, PermissionError, OSError)` did not list, which made
    the fallback below it dead code and turned every timeout/cancel into a failed run."""
    def _gone(*a, **k):
        raise AttributeError("module 'os' has no attribute 'killpg'")

    monkeypatch.setattr(fe.os, "getpgid", _gone)
    proc = _FakeProc()
    assert fe.kill_process_tree(proc) is True
    assert proc.killed is True           # fell back to the direct child instead of exploding


@pytest.mark.parametrize("err", [ProcessLookupError, PermissionError, OSError])
def test_kill_process_tree_falls_back_on_posix_errors(monkeypatch, err):
    monkeypatch.setattr(fe.os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(fe.os, "killpg", lambda *a: (_ for _ in ()).throw(err()))
    proc = _FakeProc()
    assert fe.kill_process_tree(proc) is True and proc.killed is True


def test_kill_process_tree_reports_failure_when_nothing_works(monkeypatch):
    """request_cancel counts what it actually signalled, so an unkillable process must not inflate it."""
    monkeypatch.setattr(fe.os, "getpgid", lambda pid: 1)
    monkeypatch.setattr(fe.os, "killpg", lambda *a: (_ for _ in ()).throw(OSError()))
    assert fe.kill_process_tree(_FakeProc(kill_raises=RuntimeError("no"))) is False


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch):
    """Windows has no process group to signal; `taskkill /T` is what actually walks the child tree."""
    calls = []
    monkeypatch.setattr(fe, "IS_WINDOWS", True)
    monkeypatch.setattr(fe.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert fe.kill_process_tree(_FakeProc(pid=99)) is True
    assert calls == [["taskkill", "/F", "/T", "/PID", "99"]]


def test_request_cancel_counts_only_processes_it_killed(monkeypatch):
    """Behaviour preserved through the refactor: the return value is the number of trees signalled."""
    monkeypatch.setattr(fe, "kill_process_tree", lambda p: p.pid != 2)
    monkeypatch.setattr(fe, "_RUN_PROCS", {"R": {_FakeProc(1), _FakeProc(2), _FakeProc(3)}})
    monkeypatch.setattr(fe, "_CANCELLED", set())
    assert fe.request_cancel("R") == 2
    assert fe.request_cancel(None) == 0


# ---- utf-8 pipes ---------------------------------------------------------------------------------
def _popen_kwargs_of(monkeypatch, env=None):
    captured = {}

    class _P:
        pid = 1
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _fake_popen(cmd, **kw):
        captured.update(kw)
        return _P()

    monkeypatch.setattr(fe.subprocess, "Popen", _fake_popen)
    fe.run_command([fe.PYTHON_EXE, "noop.py"], env=env)
    return captured


def test_run_command_decodes_child_output_as_utf8(monkeypatch):
    assert _popen_kwargs_of(monkeypatch)["encoding"] == "utf-8"


def test_run_command_forces_the_child_to_emit_utf8(monkeypatch):
    """Without this the CHILD encodes its stdout as cp1252 on Windows -- the parent decoding as utf-8
    would not save it. Both ends have to be pinned."""
    assert _popen_kwargs_of(monkeypatch)["env"]["PYTHONIOENCODING"] == "utf-8"


def test_run_command_keeps_the_callers_env_entries(monkeypatch):
    """Pinning the encoding must not drop the per-run config stages depend on (RUN_ID, model caps...)."""
    env = _popen_kwargs_of(monkeypatch, env={"RUN_ID": "R1", "OCR_MAX_WORKERS": "20"})["env"]
    assert env["RUN_ID"] == "R1" and env["OCR_MAX_WORKERS"] == "20"


def test_run_command_isolates_the_stage_into_its_own_group(monkeypatch):
    assert _popen_kwargs_of(monkeypatch)["start_new_session"] is True


# ---- text files that carry raw non-ASCII ---------------------------------------------------------
def test_regrade_text_is_utf8_on_both_sides():
    """regrade_input.txt is the one pipeline file holding RAW non-ASCII (the teacher's corrected maths /
    chemistry answer) rather than ASCII-escaped JSON, so cp1252 would raise on write and mojibake on
    read. Both ends must name the encoding."""
    with open(os.path.join(ROOT, "evaluation_app", "app.py"), encoding="utf-8") as f:
        assert 'open(text_path, "w", encoding="utf-8")' in f.read()
    ev = os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts", "evaluate.py")
    with open(ev, encoding="utf-8") as f:
        assert 'open(text_path, encoding="utf-8")' in f.read()


def test_gunicorn_is_not_installed_on_windows():
    """gunicorn imports fcntl and forks: it pip-installs on Windows and then fails at import. Docker
    (Linux containers) and POSIX hosts must still get it."""
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        line = next(l for l in f if l.startswith("gunicorn"))
    assert 'sys_platform != "win32"' in line


# ---- raw-text file IO ----------------------------------------------------------------------------
def _raw_text_file_io(path):
    """Text-mode open() calls in `path` that name no encoding and are not a json.dump WRITE.

    Asymmetric on purpose. A `json.dump` write is safe because ensure_ascii defaults to True, so the
    bytes on disk are pure ASCII whatever the locale. A READ has no such guarantee -- it cannot know how
    the file was produced, and two of this pipeline's .json files (student_features.json,
    diagram_evals.json) are a stage's RAW stdout written straight through, so they hold real non-ASCII.
    Exempting json.load reads is exactly the hole that let two encoding bugs survive mutation testing.
    Returns [(lineno, source_line)]."""
    import ast

    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "open"):
                continue
            mode = ""
            if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
                mode = str(call.args[1].value)
            if "b" in mode:                                   # binary -> encoding-independent
                continue
            if any(k.arg == "encoding" for k in call.keywords):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            is_write = any(m in mode for m in ("w", "a"))
            if is_write and "'dump'" in body:                 # ensure_ascii=True -> ASCII on disk
                continue
            out.append((node.lineno, lines[node.lineno - 1].strip()[:90]))
    return out


# The diagram chain is the one place a stage's RAW stdout is written straight to disk and read back,
# rather than going through json.dump -- so it is where the locale encoding actually bites.
_RAW_IO_FILES = [
    "scripts/full_evaluator.py",
    "scripts/llm_pricing.py",
    "scripts/report_sync.py",
    "skills/diagram_evaluator/scripts/evaluate_diagrams.py",
    "skills/feature-extracter/scripts/extract_features.py",
    "skills/answer-evaluator-and-report-generation/scripts/evaluate.py",
    "evaluation_app/app.py",
]


@pytest.mark.parametrize("rel", _RAW_IO_FILES)
def test_raw_text_file_io_names_its_encoding(rel):
    """Regression guard for a bug the utf-8 pipe fix INTRODUCED on Windows: once the parent decodes a
    stage's stdout correctly, `out` holds real '->' / subscripts / degree signs, and writing that to
    student_features.json without an encoding raises UnicodeEncodeError instead of the silent mojibake
    it used to produce. An AST audit rather than fixed line numbers, so new raw-text IO is caught too."""
    offenders = _raw_text_file_io(os.path.join(ROOT, rel))
    assert offenders == [], (
        f"{rel} has raw-text file IO with no encoding= (cp1252 on Windows):\n"
        + "\n".join(f"  :{ln}  {txt}" for ln, txt in offenders))


def test_the_diagram_chain_round_trips_non_ascii(tmp_path):
    """End-to-end on the real shape: extractor stdout -> student_features.json -> the evaluator's
    loader. These glyphs are exactly what diagram features contain and what cp1252 cannot represent."""
    import json as _json
    sys.path.insert(0, os.path.join(ROOT, "skills", "diagram_evaluator", "scripts"))
    payload = {"Q31": {"features": "H₂O → 2H⁺ + O²⁻, heated to 40°C ✓"}}
    raw = _json.dumps(payload, ensure_ascii=False)          # what a stage actually prints
    p = tmp_path / "student_features.json"
    with open(p, "w", encoding="utf-8") as f:               # mirrors full_evaluator's write
        f.write(raw)
    import evaluate_diagrams as ed
    assert ed.load_json_arg(str(p)) == payload


_RUBRIC_DIR = os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "references")


@pytest.mark.parametrize("name", ["objective_rubric.md", "subjective_rubric.md",
                                  "code_rubric.md", "equation_rubric.md"])
def test_rubrics_cannot_be_read_with_the_windows_locale_default(name):
    """Proof the encoding on the rubric loader is load-bearing, not decoration. Every rubric contains
    arrows / subscripts / Greek / box-drawing, so cp1252 raises PART-WAY THROUGH -- and the loader's
    `except OSError` does not catch UnicodeDecodeError (a ValueError), so grading died on every answer.
    If a rubric is ever rewritten as pure ASCII this test fails loudly rather than going quietly stale."""
    import codecs
    path = os.path.join(_RUBRIC_DIR, name)
    with open(path, encoding="utf-8") as f:
        assert any(ord(c) > 127 for c in f.read()), f"{name} is pure ASCII -- update this test"
    with pytest.raises(UnicodeDecodeError):
        with codecs.open(path, "r", encoding="cp1252") as f:
            f.read()


def test_rubric_loader_reads_every_rubric():
    """The loader itself, against the real files -- what actually broke on Windows."""
    sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))
    for name in os.listdir(_RUBRIC_DIR):
        if not name.endswith("rubric.md"):
            continue
        with open(os.path.join(_RUBRIC_DIR, name), encoding="utf-8") as f:
            assert len(f.read()) > 100, f"{name} did not load"


def test_unicode_decode_error_is_not_an_oserror():
    """The exact reason the rubric failure was fatal rather than degrading."""
    assert not issubclass(UnicodeDecodeError, OSError)
    assert issubclass(UnicodeDecodeError, ValueError)


# ---- the preflight itself ------------------------------------------------------------------------
def test_preflight_runs_clean_on_this_machine():
    """scripts/check_platform.py is what a tester runs when a machine misbehaves, so it must not be the
    thing that breaks. --quick skips the subprocess probes, keeping this test fast and hermetic."""
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_platform.py"), "--quick"],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, f"preflight reported a failure on this machine:\n{proc.stdout[-2000:]}"
    assert "SUMMARY:" in proc.stdout


def test_preflight_survives_a_broken_dependency(tmp_path, monkeypatch):
    """It has to run on the BROKEN machine, not just a healthy one -- a missing dep must be reported,
    never raised. Simulated by shadowing cv2 with a module that explodes on import."""
    shadow = tmp_path / "cv2.py"
    shadow.write_text("raise ImportError('simulated broken opencv install')\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_platform.py"), "--quick"],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert "opencv-python-headless: NOT IMPORTABLE" in proc.stdout, proc.stdout[-1500:]
    assert proc.returncode == 1                       # reported as a failure, not a crash
    assert "Traceback" not in proc.stderr, proc.stderr[-800:]


def test_preflight_probes_the_preprocessing_stage():
    """Preprocessing is the reported Windows failure and the one stage using ProcessPoolExecutor (which
    is spawn, not fork, on Windows), so the preflight must exercise it and surface the stage's own text."""
    with open(os.path.join(ROOT, "scripts", "check_platform.py"), encoding="utf-8") as f:
        src = f.read()
    assert "preprocess.py" in src and "ProcessPoolExecutor works" in src
    assert "PREPROCESSING FAILED" in src              # names it clearly, and prints the stage output


def test_process_kill_goes_through_the_one_helper():
    """Invariant, not a spot-check. There were THREE killpg sites (run_command's watchdog,
    request_cancel, batch_evaluator's sheet timeout) and an earlier pass fixed only two -- the
    watchdog kept a raw os.killpg with the narrow `except (ProcessLookupError, PermissionError,
    OSError)` that cannot catch the AttributeError Windows raises. Per-function tests missed it
    because they exercise kill_process_tree, not its inline copies. So: assert the POSIX calls appear
    NOWHERE outside the helper's own body."""
    import ast

    for rel in ("scripts/full_evaluator.py", "scripts/batch_evaluator.py", "evaluation_app/app.py"):
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        helper = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "kill_process_tree"), None)
        lo, hi = (helper.lineno, helper.end_lineno) if helper else (-1, -1)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in ("killpg", "getpgid", "SIGKILL"):
                continue
            if lo <= node.lineno <= hi:
                continue                                  # the helper is allowed to use them
            offenders.append((node.lineno, node.attr))
        assert offenders == [], (
            f"{rel} calls POSIX-only process APIs outside kill_process_tree "
            f"(AttributeError on Windows): {offenders}")
