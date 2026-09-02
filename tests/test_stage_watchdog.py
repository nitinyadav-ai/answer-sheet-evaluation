"""Stage watchdog (kills a hung subprocess so the pipeline can never hang forever) + the tightened
diagram per-call timeout/retry budget. All offline -- the watchdog test uses a real short-lived
subprocess, no network."""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import full_evaluator as fe
    from llm_client import diagram_llm_opts
except (ImportError, SystemExit) as e:
    fe = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(fe is None, reason="full_evaluator/llm_client unavailable in this env")


# ---- diagram per-call budget (llm_client.diagram_llm_opts) --------------------------------------

def test_diagram_opts_defaults(monkeypatch):
    monkeypatch.delenv("DIAGRAM_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("DIAGRAM_LLM_MAX_RETRIES", raising=False)
    assert diagram_llm_opts() == (90.0, 1)


def test_diagram_opts_env_override(monkeypatch):
    monkeypatch.setenv("DIAGRAM_LLM_TIMEOUT", "45")
    monkeypatch.setenv("DIAGRAM_LLM_MAX_RETRIES", "0")
    assert diagram_llm_opts() == (45.0, 0)


def test_diagram_opts_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("DIAGRAM_LLM_TIMEOUT", "nope")
    monkeypatch.setenv("DIAGRAM_LLM_MAX_RETRIES", "-3")   # negative clamps to 0
    t, r = diagram_llm_opts()
    assert t == 90.0 and r == 0


# ---- per-stage watchdog ceiling resolution (full_evaluator._stage_timeout) ----------------------

def test_stage_timeout_defaults(monkeypatch):
    for k in ("STAGE_TIMEOUT", "STAGE_TIMEOUT_EVALUATE", "STAGE_TIMEOUT_DETECT_DIAGRAMS"):
        monkeypatch.delenv(k, raising=False)
    assert fe._stage_timeout("evaluate.py") == 1200
    assert fe._stage_timeout("detect_diagrams.py") == 180
    assert fe._stage_timeout("unknown.py") == fe._DEFAULT_STAGE_TIMEOUT_S


def test_stage_timeout_global_override(monkeypatch):
    monkeypatch.delenv("STAGE_TIMEOUT_RUN_OCR", raising=False)
    monkeypatch.setenv("STAGE_TIMEOUT", "50")
    assert fe._stage_timeout("run_ocr.py") == 50.0


def test_stage_timeout_per_stage_beats_global(monkeypatch):
    monkeypatch.setenv("STAGE_TIMEOUT", "50")
    monkeypatch.setenv("STAGE_TIMEOUT_EVALUATE", "999")
    assert fe._stage_timeout("evaluate.py") == 999.0   # per-stage override wins
    assert fe._stage_timeout("run_ocr.py") == 50.0     # others still take the global


# ---- run_command: normal, error, and the hang-killing watchdog ----------------------------------

def test_run_command_normal_success():
    ok, out = fe.run_command([sys.executable, "-c", "print('hello-watchdog')"])
    assert ok and "hello-watchdog" in out


def test_run_command_nonzero_exit_returns_false():
    ok, out = fe.run_command([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert not ok and "boom" in out


def test_run_command_watchdog_kills_a_hang(monkeypatch):
    monkeypatch.setenv("STAGE_TIMEOUT", "1")            # force a 1s ceiling on every stage
    t0 = time.time()
    ok, out = fe.run_command([sys.executable, "-c", "import time; time.sleep(30)"])
    dt = time.time() - t0
    assert not ok                                       # degraded, not hung
    assert "watchdog" in out.lower()
    assert dt < 10                                       # proves the kill fired (did NOT wait 30s)
