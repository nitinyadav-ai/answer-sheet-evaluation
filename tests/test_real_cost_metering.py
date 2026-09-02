"""Offline tests for provider-accurate cost metering. No network: the OpenAI client is stubbed.

Pin the contract: `generate()` asks OpenRouter for usage accounting and records the REAL billed cost
(`usage.cost`) into the per-process accumulator; `log_cost` prefers that real cost over the per-model
estimate (and always keeps the estimate alongside for comparison); and everything falls back safely to
the estimate when the provider doesn't price a call or accounting is disabled."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import llm_client as C      # noqa: E402
import llm_pricing as P     # noqa: E402

_THINK = "qwen/qwen3-vl-235b-a22b-thinking"
_INSTR = "qwen/qwen3-vl-235b-a22b-instruct"


class _Usage:
    """Attribute-style stand-in for the SDK's CompletionUsage (which allows extra fields like `cost`)."""
    def __init__(self, **k):
        self.__dict__.update(k)


# --------------------------------------------------------------------------- _usage_cost extraction
def test_usage_cost_from_attribute():
    assert C._usage_cost(_Usage(cost=0.0123)) == pytest.approx(0.0123)


def test_usage_cost_from_model_extra():
    assert C._usage_cost(_Usage(model_extra={"cost": 0.05})) == pytest.approx(0.05)


def test_usage_cost_from_dict():
    assert C._usage_cost({"cost": 0.07}) == pytest.approx(0.07)


def test_usage_cost_missing_is_none():
    assert C._usage_cost(_Usage(prompt_tokens=10, completion_tokens=5)) is None
    assert C._usage_cost(None) is None


def test_usage_cost_rejects_bad_values():
    assert C._usage_cost(_Usage(cost=-1.0)) is None            # negative -> "not reported"
    assert C._usage_cost(_Usage(cost=float("nan"))) is None
    assert C._usage_cost(_Usage(cost=float("inf"))) is None
    assert C._usage_cost(_Usage(cost="not-a-number")) is None


def test_usage_cost_coerces_numeric_string():
    assert C._usage_cost(_Usage(cost="0.02")) == pytest.approx(0.02)


# --------------------------------------------------------------------------- accumulator
def test_accumulator_mixes_real_and_estimate():
    C.reset_cost_accum()
    C._accumulate_cost(_THINK, 1000, 1000, 0.10)   # provider-billed
    C._accumulate_cost(_THINK, 1000, 1000, None)   # provider didn't price -> estimate fill
    best, nreal, n = C.get_real_cost()
    assert best == pytest.approx(0.10 + P.estimate_cost(_THINK, 1000, 1000))
    assert (nreal, n) == (1, 2)


def test_accumulator_all_estimate_has_no_real():
    C.reset_cost_accum()
    C._accumulate_cost(_INSTR, 500, 500, None)
    best, nreal, n = C.get_real_cost()
    assert (nreal, n) == (0, 1)
    assert best == pytest.approx(P.estimate_cost(_INSTR, 500, 500))


def test_reset_zeroes_the_accumulator():
    C._accumulate_cost(_THINK, 1, 1, 0.5)
    C.reset_cost_accum()
    assert C.get_real_cost() == (0.0, 0, 0)


# --------------------------------------------------------------------------- log_cost real vs estimate
def test_log_cost_prefers_real(tmp_path, monkeypatch):
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv("API_COST_LOG", str(ledger))
    P.log_cost("grading", _THINK, 1000, 2000, cost_usd=0.99)
    rec = json.loads(ledger.read_text().strip())
    assert rec["cost_usd"] == 0.99
    assert rec["cost_source"] == "openrouter"
    assert rec["cost_estimate_usd"] == round(P.estimate_cost(_THINK, 1000, 2000), 6)   # estimate kept alongside


def test_log_cost_estimate_fallback(tmp_path, monkeypatch):
    ledger = tmp_path / "costs.jsonl"
    monkeypatch.setenv("API_COST_LOG", str(ledger))
    P.log_cost("ocr", _INSTR, 1000, 2000)          # no cost_usd -> legacy estimate
    rec = json.loads(ledger.read_text().strip())
    est = round(P.estimate_cost(_INSTR, 1000, 2000), 6)
    assert rec["cost_usd"] == est
    assert rec["cost_source"] == "estimate"
    assert rec["cost_estimate_usd"] == est


def test_log_cost_noop_without_env(monkeypatch):
    monkeypatch.delenv("API_COST_LOG", raising=False)
    P.log_cost("grading", _THINK, 1, 1, cost_usd=0.5)   # must not raise / must not write


# --------------------------------------------------------------------------- generate() end-to-end (stubbed client)
class _FakeCompletions:
    def __init__(self, captured, usage):
        self._captured, self._usage = captured, usage

    def create(self, **kwargs):
        self._captured.clear()
        self._captured.update(kwargs)
        resp = _Usage(choices=[_Usage(message=_Usage(content='{"ok": true}'))],
                      usage=self._usage, provider="fake-provider")
        return resp


class _FakeClient:
    def __init__(self, captured, usage):
        self.chat = _Usage(completions=_FakeCompletions(captured, usage))

    def with_options(self, **k):
        return self


def _isolate_routing(monkeypatch):
    for v in ("LLM_PROVIDER_SORT", "LLM_PROVIDER_ORDER", "LLM_PROVIDER_ONLY", "LLM_PROVIDER_QUANTIZATIONS"):
        monkeypatch.delenv(v, raising=False)


def test_generate_requests_usage_and_records_real_cost(monkeypatch):
    captured = {}
    usage = _Usage(prompt_tokens=11, completion_tokens=22, cost=0.0333)
    monkeypatch.setattr(C, "_client", lambda: _FakeClient(captured, usage))
    monkeypatch.setenv("LLM_USAGE_ACCOUNTING", "1")
    _isolate_routing(monkeypatch)
    C.reset_cost_accum()

    text, i, o = C.generate(model=_THINK, prompt="hi")

    assert captured.get("extra_body", {}).get("usage") == {"include": True}   # asked for real cost
    assert (i, o) == (11, 22)
    best, nreal, n = C.get_real_cost()
    assert nreal == 1 and best == pytest.approx(0.0333)                        # recorded the billed amount


def test_generate_omits_usage_when_disabled(monkeypatch):
    captured = {}
    usage = _Usage(prompt_tokens=5, completion_tokens=5)   # server returns no cost when not asked
    monkeypatch.setattr(C, "_client", lambda: _FakeClient(captured, usage))
    monkeypatch.setenv("LLM_USAGE_ACCOUNTING", "0")
    _isolate_routing(monkeypatch)
    C.reset_cost_accum()

    C.generate(model=_INSTR, prompt="hi")

    assert "usage" not in (captured.get("extra_body") or {})   # no accounting requested
    _best, nreal, _n = C.get_real_cost()
    assert nreal == 0                                          # no usage.cost -> estimate path
