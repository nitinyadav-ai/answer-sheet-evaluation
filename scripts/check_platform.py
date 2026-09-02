#!/usr/bin/env python3
"""check_platform.py -- one-command preflight for running this app on a given machine.

Answers, in a single paste-able report: is the interpreter usable, are the dependencies really
installed (and from wheels, not a failed source build), do the OS-specific pieces work (stage spawn,
process-tree kill, utf-8 pipes), and can the pipeline's own stages actually run.

Written to survive a BROKEN install: stdlib only at import time, every check isolated, and a missing
dependency is reported rather than raised. Run it before anything else when a machine misbehaves:

    python scripts/check_platform.py            # full report
    python scripts/check_platform.py --quick    # skip the stage probes (no subprocesses)

Exit code 0 if nothing FAILED, 1 otherwise (warnings do not fail the run).
"""
import argparse
import os
import platform
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WINDOWS = (os.name == "nt")

_RESULTS = []          # (status, section, message)
PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


def record(status, section, message):
    _RESULTS.append((status, section, message))
    icon = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL ", INFO: "      "}[status]
    print(f"[{icon}] {message}")


def section(title):
    print(f"\n--- {title} " + "-" * max(0, 74 - len(title)))


# ---------------------------------------------------------------------------------------------------
# 1. Interpreter & platform
# ---------------------------------------------------------------------------------------------------
def check_interpreter():
    section("interpreter & platform")
    v = sys.version_info
    record(INFO, "python", f"Python {platform.python_version()} ({platform.python_implementation()})")
    record(INFO, "python", f"executable: {sys.executable}")
    record(INFO, "os", f"{platform.system()} {platform.release()} / {platform.machine()}")
    record(INFO, "os", f"filesystem encoding: {sys.getfilesystemencoding()} | "
                       f"stdio: {sys.stdout.encoding}")

    if (v.major, v.minor) < (3, 9):
        record(FAIL, "python", f"Python {v.major}.{v.minor} is too old; this app targets 3.12.")
    elif (v.major, v.minor) > (3, 13):
        record(WARN, "python", f"Python {v.major}.{v.minor} is NEWER than the pinned wheels support. "
                               f"numpy/opencv/PyMuPDF publish no cp{v.major}{v.minor} wheels, so pip "
                               f"falls back to building from source and needs a C/C++ toolchain "
                               f"(MSVC Build Tools on Windows). Install 3.12 or 3.13 instead -- this is "
                               f"the most common cause of 'multiple libraries fail to install'.")
    else:
        record(PASS, "python", f"Python {v.major}.{v.minor} has prebuilt wheels for every pinned dep.")

    if IS_WINDOWS:
        # The App Execution Alias: a zero-byte stub named python3.exe that opens the Microsoft Store.
        try:
            p = subprocess.run(["python3", "-c", "import sys; print(sys.executable)"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15)
            if p.returncode != 0 or not (p.stdout or "").strip():
                record(INFO, "python", "`python3` on PATH is the Microsoft Store stub (does nothing). "
                                       "Harmless -- the app uses sys.executable, not the literal name.")
        except (OSError, subprocess.SubprocessError):
            record(INFO, "python", "no `python3` on PATH (normal on Windows; the app does not need it).")


# ---------------------------------------------------------------------------------------------------
# 2. Dependencies
# ---------------------------------------------------------------------------------------------------
# (import name, distribution name, required?, what breaks without it)
# "required" means the PIPELINE cannot grade without it. Several pins are imported lazily behind a
# fallback, so their absence degrades one feature rather than stopping a run -- calling those FAIL
# would raise a false alarm on a machine that actually works.
_DEPS = [
    ("flask", "Flask", True, ""),
    ("flask_cors", "Flask-Cors", True, ""),
    ("werkzeug", "Werkzeug", True, ""),
    ("fitz", "PyMuPDF", True, ""),
    ("cv2", "opencv-python-headless", True, ""),
    ("numpy", "numpy", True, ""),
    ("PIL", "pillow", True, ""),
    ("PyPDF2", "PyPDF2", True, ""),
    ("docx", "python-docx", True, ""),
    ("dotenv", "python-dotenv", True, ""),
    ("fpdf", "fpdf2", True, ""),
    ("openai", "openai", True, ""),
    ("markdown", "Markdown", False, "the /upload-guidelines page renders as plain text "
                                    "instead of formatted HTML (app.py falls back automatically)"),
    ("pytesseract", "pytesseract", False, "nothing -- orientation is fully manual, so the only "
                                          "module that uses it is never reached"),
    ("psycopg2", "psycopg2-binary", False, "nothing -- reports are archived to a local folder, "
                                           "not Postgres"),
    ("gunicorn", "gunicorn", False, "nothing on Windows (POSIX-only; the app serves via Flask)"),
]


def check_dependencies():
    section("dependencies")
    try:
        import importlib.metadata as md
    except ImportError:                                    # pragma: no cover
        md = None

    pins = {}
    req = os.path.join(ROOT, "requirements.txt")
    if os.path.exists(req):
        with open(req, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                spec = line.split(";", 1)[0].strip()       # drop the environment marker
                for sep in ("==", ">="):
                    if sep in spec:
                        name, ver = spec.split(sep, 1)
                        pins[name.strip().lower()] = (sep, ver.strip())
                        break

    missing = []
    for mod, dist, required, impact in _DEPS:
        try:
            __import__(mod)
        except Exception as e:
            missing.append(dist)
            if required:
                record(FAIL, "deps", f"{dist}: NOT IMPORTABLE ({type(e).__name__}: {e})")
            elif dist == "gunicorn" and IS_WINDOWS:
                missing.pop()
                record(PASS, "deps", "gunicorn: absent, as intended on Windows (POSIX-only; the app "
                                     "serves via Flask instead).")
            else:
                record(WARN, "deps", f"{dist}: not installed -- impact: {impact}")
            continue

        installed = None
        if md is not None:
            try:
                installed = md.version(dist)
            except Exception:
                pass
        pin = pins.get(dist.lower())
        if pin and installed and pin[0] == "==" and installed != pin[1]:
            record(WARN, "deps", f"{dist}: {installed} installed, requirements pin {pin[1]}")
        else:
            record(PASS, "deps", f"{dist}: {installed or 'ok'}")

    if IS_WINDOWS:
        try:
            import gunicorn  # noqa: F401
            record(WARN, "deps", "gunicorn IS installed on Windows -- it imports fcntl and will fail at "
                                 "startup. Run `python evaluation_app/app.py` instead of gunicorn.")
        except Exception:
            pass

    if missing:
        record(INFO, "deps", f"install with:  pip install -r requirements.txt   (missing: "
                             f"{', '.join(missing)})")


# ---------------------------------------------------------------------------------------------------
# 3. OS-specific pipeline mechanics
# ---------------------------------------------------------------------------------------------------
def check_process_mechanics():
    section("stage spawn / kill / pipes")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import full_evaluator as fe
    except Exception as e:
        record(FAIL, "spawn", f"cannot import full_evaluator ({type(e).__name__}: {e}) -- "
                              f"fix the dependency errors above first.")
        return

    if fe.PYTHON_EXE == sys.executable:
        record(PASS, "spawn", f"stages will run under this interpreter ({fe.PYTHON_EXE})")
    else:
        record(WARN, "spawn", f"stages resolve to {fe.PYTHON_EXE}, not {sys.executable}")

    expected = "creationflags" if IS_WINDOWS else "start_new_session"
    kwargs = fe._new_group_kwargs()
    if expected in kwargs:
        record(PASS, "spawn", f"process-group isolation uses {expected} (correct for this OS)")
    else:
        record(FAIL, "spawn", f"process-group isolation returned {kwargs}, expected {expected}")

    # A real stage that prints characters cp1252 cannot represent.
    glyphs = "H₂O → 2H⁺ ≤ 40°C ✓"
    ok, out = fe.run_command([fe.PYTHON_EXE, "-c",
                              "print('" + glyphs + "')"])
    if ok and glyphs in out:
        record(PASS, "pipes", f"utf-8 round-trip through a stage: {glyphs}")
    else:
        record(FAIL, "pipes", f"utf-8 round-trip FAILED (ok={ok}): {out.strip()[:160]!r}")

    # Tree kill, including a grandchild -- the guarantee cancel and the watchdog rest on.
    child = ("import subprocess,sys,time;"
             "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
             "print(g.pid,flush=True);time.sleep(120)")
    try:
        proc = subprocess.Popen([fe.PYTHON_EXE, "-c", child], stdout=subprocess.PIPE,
                                text=True, encoding="utf-8", **fe._new_group_kwargs())
        gpid = int((proc.stdout.readline() or "0").strip())
        killed = fe.kill_process_tree(proc)
        proc.wait(timeout=15)
        time.sleep(0.5)
        alive = _pid_alive(gpid)
        if killed and not alive:
            record(PASS, "kill", "whole process tree dies, grandchild included "
                                 "(cancel + stage watchdog will work)")
        elif killed:
            record(FAIL, "kill", f"direct child died but the GRANDCHILD (pid {gpid}) survived -- a "
                                 f"cancelled run would leave workers burning API credit.")
        else:
            record(FAIL, "kill", "kill_process_tree reported failure")
    except Exception as e:
        record(FAIL, "kill", f"tree-kill probe errored: {type(e).__name__}: {e}")

    # Watchdog: a hung stage must be terminated, not hang the pipeline.
    with tempfile.TemporaryDirectory() as td:
        hang = os.path.join(td, "hang_stage.py")
        with open(hang, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(60)\n")
        os.environ["STAGE_TIMEOUT"] = "3"
        try:
            t0 = time.perf_counter()
            ok, _ = fe.run_command([fe.PYTHON_EXE, hang])
            dt = time.perf_counter() - t0
            if not ok and dt < 15:
                record(PASS, "watchdog", f"a hung stage is terminated ({dt:.1f}s)")
            else:
                record(FAIL, "watchdog", f"hung stage not terminated (ok={ok}, {dt:.1f}s)")
        finally:
            os.environ.pop("STAGE_TIMEOUT", None)


def _pid_alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
        # tasklist emits the console OEM code page, not utf-8, so decode loosely -- this is a
        # diagnostic and a stray byte must not take the preflight down.
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------------
# 4. Text encoding (the rubrics are the load-bearing case)
# ---------------------------------------------------------------------------------------------------
def check_encoding():
    section("text encoding")
    rub_dir = os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "references")
    names = sorted(n for n in os.listdir(rub_dir)) if os.path.isdir(rub_dir) else []
    rubrics = [n for n in names if n.endswith("rubric.md")]
    if not rubrics:
        record(WARN, "encoding", f"no rubric files found under {rub_dir}")
        return
    bad = []
    for n in rubrics:
        try:
            with open(os.path.join(rub_dir, n), encoding="utf-8") as f:
                f.read()
        except Exception as e:
            bad.append(f"{n} ({type(e).__name__})")
    if bad:
        record(FAIL, "encoding", f"rubric(s) unreadable as utf-8: {', '.join(bad)}")
    else:
        record(PASS, "encoding", f"all {len(rubrics)} grading rubrics decode as utf-8 "
                                 f"(these contain arrows/subscripts that cp1252 cannot read)")

    # Prove the locale default would NOT have worked, so the explicit encoding is load-bearing.
    import locale
    pref = locale.getpreferredencoding(False)
    record(INFO, "encoding", f"locale preferred encoding: {pref}")
    if pref.lower().replace("-", "") not in ("utf8", "cp65001"):
        record(INFO, "encoding", "not utf-8 -- explicit encoding= is doing real work on this machine.")


# ---------------------------------------------------------------------------------------------------
# 5. Real stage probe: ingestion + preprocessing (ProcessPoolExecutor -- spawn on Windows)
# ---------------------------------------------------------------------------------------------------
def check_stages():
    section("real stage probe (ingestion + preprocessing)")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import full_evaluator as fe
        import fitz                                          # noqa: F401
        from PIL import Image
    except Exception as e:
        record(FAIL, "stages", f"cannot run stage probe ({type(e).__name__}: {e})")
        return

    with tempfile.TemporaryDirectory() as td:
        # A tiny 2-page PDF, then the real ingestion + preprocessing stages over it.
        pdf = os.path.join(td, "probe_sheet.pdf")
        Image.new("RGB", (1240, 1754), "white").save(
            pdf, "PDF", resolution=150.0,
            save_all=True, append_images=[Image.new("RGB", (1240, 1754), "white")])

        images_dir = os.path.join(td, "images")
        pre_dir = os.path.join(td, "preprocessed")
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(pre_dir, exist_ok=True)

        ingest = os.path.join(ROOT, "skills", "ingestion-handler", "scripts", "process_input.py")
        ok, out = fe.run_command([fe.PYTHON_EXE, ingest, pdf, "--output-dir", images_dir])
        pages = sorted(f for f in os.listdir(images_dir) if f.lower().endswith(".png"))
        if ok and pages:
            record(PASS, "stages", f"ingestion rendered {len(pages)} page image(s)")
        else:
            record(FAIL, "stages", f"ingestion FAILED (ok={ok}): {out.strip()[-300:]}")
            return

        pre = os.path.join(ROOT, "skills", "img-preprocessing", "scripts", "preprocess.py")
        cmd = [fe.PYTHON_EXE, pre] + [os.path.join(images_dir, p) for p in pages] \
            + ["--output-dir", pre_dir]
        ok, out = fe.run_command(cmd)
        made = [f for f in os.listdir(pre_dir) if f.lower().endswith(".png")]
        if ok and len(made) >= len(pages):
            record(PASS, "stages", f"preprocessing produced {len(made)} image(s) "
                                   f"(ProcessPoolExecutor works on this OS)")
        else:
            record(FAIL, "stages", f"PREPROCESSING FAILED (ok={ok}, {len(made)}/{len(pages)} images)")
            tail = (out or "").strip()[-800:]
            if tail:
                record(INFO, "stages", "stage output below -- this is the text to report:")
                print("      " + tail.replace("\n", "\n      "))


# ---------------------------------------------------------------------------------------------------
# 6. Config
# ---------------------------------------------------------------------------------------------------
def check_config():
    section("configuration")
    env_path = os.path.join(ROOT, ".env")
    if not os.path.exists(env_path):
        record(WARN, "config", ".env not found -- copy .env.example to .env and set LLM_API_KEY "
                               "(the pipeline cannot reach the model without it).")
        return
    key_set = False
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "LLM_API_KEY" and v.strip().strip('"').strip("'"):
                    key_set = True
    except Exception as e:
        record(FAIL, "config", f".env unreadable: {type(e).__name__}: {e}")
        return
    record(PASS, "config", ".env present and readable as utf-8")
    record(PASS if key_set else WARN, "config",
           "LLM_API_KEY is set" if key_set else "LLM_API_KEY is empty or missing in .env")

    out = os.path.join(ROOT, "output")
    try:
        os.makedirs(out, exist_ok=True)
        probe = os.path.join(out, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        record(PASS, "config", f"output/ is writable ({out})")
    except Exception as e:
        record(FAIL, "config", f"output/ is NOT writable: {type(e).__name__}: {e}")

    longest = len(os.path.join(out, "Computer_Science_Class_12", "preprocessed",
                               "preprocessed_Computer_Science_Class_12_page_13.png"))
    if IS_WINDOWS and longest > 240:
        record(WARN, "config", f"artifact paths reach ~{longest} chars; Windows MAX_PATH is 260. "
                               f"Move the repo nearer the drive root or enable long paths.")
    else:
        record(PASS, "config", f"longest artifact path ~{longest} chars (limit 260 on Windows)")


# ---------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Preflight check for the AI Answer Evaluator.")
    ap.add_argument("--quick", action="store_true",
                    help="skip the subprocess/stage probes (imports and config only)")
    args = ap.parse_args()

    print("=" * 84)
    print("AI Answer Evaluator -- platform preflight")
    print("=" * 84)

    check_interpreter()
    check_dependencies()
    check_encoding()
    check_config()
    if not args.quick:
        check_process_mechanics()
        check_stages()
    else:
        print("\n(--quick: skipped the spawn/kill/stage probes)")

    fails = [m for s, _, m in _RESULTS if s == FAIL]
    warns = [m for s, _, m in _RESULTS if s == WARN]
    print("\n" + "=" * 84)
    print(f"SUMMARY: {len(fails)} failed, {len(warns)} warning(s), "
          f"{sum(1 for s, _, _ in _RESULTS if s == PASS)} passed")
    for m in fails:
        print(f"  FAIL: {m}")
    for m in warns:
        print(f"  warn: {m}")
    if not fails:
        print("\nNo failures -- this machine can run the pipeline.")
    print("=" * 84)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
