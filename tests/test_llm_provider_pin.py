"""Provider-pinning (the batch-latency-variance fix). Offline / no network:
  1. _provider_directive() turns LLM_PROVIDER_ORDER into an OpenRouter `order` pin (fast backend first).
  2. generate()'s error-retry drops response_format/reasoning (knobs some endpoints reject) but KEEPS
     the `provider` pin, so a retried call can't silently drift onto the slow fallback backend.
"""
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import llm_client as lc
except Exception:  # pragma: no cover
    lc = None

pytestmark = pytest.mark.skipif(lc is None, reason="llm_client unavailable")


def test_provider_directive_pins_alibaba(monkeypatch):
    for k in ("LLM_PROVIDER_ONLY", "LLM_PROVIDER_IGNORE", "LLM_PROVIDER_QUANTIZATIONS",
              "LLM_PROVIDER_ALLOW_FALLBACKS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "alibaba")
    monkeypatch.setenv("LLM_PROVIDER_SORT", "throughput")
    monkeypatch.setenv("LLM_PROVIDER_REQUIRE_PARAMETERS", "1")
    prov = lc._provider_directive()
    assert prov["order"] == ["alibaba"]          # explicit pin -> Alibaba tried first every call
    assert prov["sort"] == "throughput"          # still orders any fallback provider
    assert prov["require_parameters"] is True


def _fake_resp(text):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        provider="alibaba")


def test_retry_keeps_provider_drops_optional(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "alibaba")
    monkeypatch.setenv("LLM_PROVIDER_SORT", "throughput")
    monkeypatch.setenv("LLM_JSON_MODE", "1")
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(dict(kwargs))            # snapshot each attempt's kwargs
            if len(calls) == 1:
                raise RuntimeError("this provider does not support response_format")
            return _fake_resp('{"Marks Awarded": 1}')

    class FakeClient:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

        def with_options(self, **kw):
            return self

    monkeypatch.setattr(lc, "_client", lambda: FakeClient())

    text, _in, _out = lc.generate(model="qwen/qwen3-vl-235b-a22b-thinking", prompt="grade this",
                                  json_mode=True, reasoning_effort="low")

    assert len(calls) == 2                                          # errored once, retried once
    # First attempt carried the json knob, the reasoning knob, AND the provider pin.
    assert "response_format" in calls[0]
    assert "reasoning" in calls[0]["extra_body"]
    assert calls[0]["extra_body"]["provider"]["order"] == ["alibaba"]
    # The retry DROPPED response_format + reasoning but KEPT the provider pin (no drift to a slow backend).
    assert "response_format" not in calls[1]
    assert "reasoning" not in calls[1]["extra_body"]
    assert calls[1]["extra_body"]["provider"]["order"] == ["alibaba"]
    assert text == '{"Marks Awarded": 1}'


# ---------------------------------------------------------------------------------------------
# Provider PRIVACY (student data). This pipeline sends children's exam answers to third-party
# inference providers. OpenRouter's default is `data_collection: "allow"`, which permits providers
# that store prompts non-transiently AND MAY TRAIN ON THEM -- so the safe policy has to be what you
# get with NO configuration, not something you must remember to switch on.
#
# `zdr` (no retention at rest) and `data_collection: "deny"` (no training) are DIFFERENT guarantees;
# a provider can have either without the other, so both are always sent.
# ---------------------------------------------------------------------------------------------

_PRIVACY_ENV = ("LLM_PROVIDER_ZDR", "LLM_PROVIDER_DATA_COLLECTION", "LLM_PROVIDER_SORT",
                "LLM_PROVIDER_ORDER", "LLM_PROVIDER_ONLY", "LLM_PROVIDER_IGNORE",
                "LLM_PROVIDER_QUANTIZATIONS", "LLM_PROVIDER_ALLOW_FALLBACKS")


@pytest.fixture
def clean_env(monkeypatch):
    """No provider env at all -- the state a fresh deploy starts in."""
    for k in _PRIVACY_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_privacy_is_on_with_no_configuration_at_all(clean_env):
    """The regression that matters: an unconfigured deploy must still be private."""
    prov = lc._provider_directive()
    assert prov is not None, "no directive at all -> requests run under OpenRouter's permissive default"
    assert prov["zdr"] is True
    assert prov["data_collection"] == "deny"


def test_privacy_alone_still_emits_a_directive(clean_env):
    """Guards the original bug shape: the old code returned None unless a PERFORMANCE knob was set,
    so a privacy-only configuration would have been silently dropped on the floor."""
    prov = lc._provider_directive()
    assert "zdr" in prov and "data_collection" in prov
    for perf in ("sort", "order", "only", "quantizations"):
        assert perf not in prov          # nothing but privacy configured


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_zdr_can_be_disabled_explicitly(clean_env, val):
    """Escape hatch for a model with no ZDR-compliant provider -- but it must be DELIBERATE."""
    clean_env.setenv("LLM_PROVIDER_ZDR", val)
    assert "zdr" not in lc._provider_directive()


@pytest.mark.parametrize("val", ["", "   "])
def test_blank_zdr_does_not_silently_disable_privacy(clean_env, val):
    """An empty env var (a common way to 'unset' something in a dashboard) must not read as false."""
    clean_env.setenv("LLM_PROVIDER_ZDR", val)
    assert lc._provider_directive()["zdr"] is True


def test_data_collection_allow_is_possible_but_opt_in(clean_env):
    clean_env.setenv("LLM_PROVIDER_DATA_COLLECTION", "allow")
    assert lc._provider_directive()["data_collection"] == "allow"


@pytest.mark.parametrize("typo", ["denny", "DENY ", "true", "1", "off", "none"])
def test_an_invalid_data_collection_value_falls_back_to_deny(clean_env, typo):
    """A typo must land on the SAFE value, not merely 'not allow', and must never OMIT the field --
    omission hands the decision back to OpenRouter, whose default is 'allow' (retention + training).
    Found by mutation testing: asserting `!= "allow"` passed while the field was being dropped."""
    clean_env.setenv("LLM_PROVIDER_DATA_COLLECTION", typo)
    prov = lc._provider_directive()
    assert "data_collection" in prov, "field omitted -> OpenRouter's permissive default applies"
    assert prov["data_collection"] == "deny"


@pytest.mark.parametrize("raw,default,expected", [
    ("", False, False), ("   ", False, False),      # blank must mean "unset", not "false"
    ("", True, True), ("   ", True, True),
    ("0", True, False), ("off", True, False), ("1", False, True), ("yes", False, True),
])
def test_truthy_treats_blank_as_unset(raw, default, expected, monkeypatch):
    """Contract test for the flag reader. A dashboard that 'clears' a variable leaves an EMPTY
    string, which must fall back to the default rather than read as false."""
    monkeypatch.setenv("_PRIVACY_PROBE", raw)
    assert lc._truthy("_PRIVACY_PROBE", default) is expected


def test_privacy_and_performance_compose(clean_env):
    clean_env.setenv("LLM_PROVIDER_SORT", "throughput")
    clean_env.setenv("LLM_PROVIDER_ORDER", "alibaba")
    prov = lc._provider_directive()
    assert prov["zdr"] is True and prov["data_collection"] == "deny"
    assert prov["sort"] == "throughput" and prov["order"] == ["alibaba"]


def test_privacy_survives_the_error_retry_path(monkeypatch):
    """generate() retries by dropping optional knobs. The privacy directive must NOT be one of them,
    or a retried call would send student answers to a data-collecting provider."""
    src = open(os.path.join(ROOT, "scripts", "llm_client.py")).read()
    assert "prov.pop" not in src and "del prov[" not in src
    assert '"zdr"' in src and '"data_collection"' in src


def test_verifier_script_exists():
    """Both settings shrink the endpoint pool; the grading model has only two providers, so
    availability under the policy has to be checkable rather than assumed."""
    p = os.path.join(ROOT, "scripts", "check_provider_privacy.py")
    assert os.path.exists(p)
    assert "zdr" in open(p).read()
