"""Offline tests for the answer-key parse cache (extract_json_from_key). No network: the LLM parse is
stubbed. They pin the contract: a repeat of the SAME key text returns the cached parse (no re-parse),
and the cache correctly INVALIDATES on a changed key / model / prompt-version so a stale parse is never
served. The cache can never change a parse (identical input -> identical output)."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import extract_json_from_key as ek
except (ImportError, SystemExit):  # pragma: no cover
    ek = None

pytestmark = pytest.mark.skipif(ek is None, reason="extract_json_from_key unavailable in this env")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ek, "_cache_root", lambda: str(tmp_path))     # isolate from the real .parse_cache
    monkeypatch.setenv("KEY_PARSER_MODEL", "test-model-A")
    return tmp_path


def test_put_then_get_round_trips(cache):
    obj = {"metadata": {"choice_groups": []}, "questions": {"Q1": {"answer": "x", "marks": 1}}}
    assert ek._cache_get("KEY TEXT") is None          # cold -> miss
    ek._cache_put("KEY TEXT", obj)
    assert ek._cache_get("KEY TEXT") == obj           # warm -> exact hit


def test_different_text_misses(cache):
    ek._cache_put("KEY TEXT A", {"questions": {}})
    assert ek._cache_get("KEY TEXT B") is None        # a different key never collides


def test_model_change_invalidates(cache, monkeypatch):
    ek._cache_put("SAME TEXT", {"questions": {"Q1": {}}})
    monkeypatch.setenv("KEY_PARSER_MODEL", "test-model-B")
    assert ek._cache_get("SAME TEXT") is None          # a model swap must not serve the old model's parse


def test_version_bump_invalidates(cache, monkeypatch):
    ek._cache_put("SAME TEXT", {"questions": {"Q1": {}}})
    monkeypatch.setattr(ek, "_CACHE_VERSION", ek._CACHE_VERSION + "-next")
    assert ek._cache_get("SAME TEXT") is None          # a prompt/schema change must invalidate stale entries


def test_parse_cached_calls_llm_once_then_serves_cache(cache, monkeypatch):
    calls = {"n": 0}
    def fake_parse(text):
        calls["n"] += 1
        return {"metadata": {}, "questions": {"Q1": {"answer": "a", "marks": 2}}}
    monkeypatch.setattr(ek, "parse_with_gemini", fake_parse)
    monkeypatch.setattr(ek, "_load_project_env", lambda: None)

    r1 = ek._parse_cached("THE KEY")
    r2 = ek._parse_cached("THE KEY")                   # second time -> cache, NOT the LLM
    assert calls["n"] == 1 and r1 == r2                # parsed once, identical result both times


def test_corrupt_cache_file_falls_back(cache):
    p = ek._cache_path("BROKEN")
    with open(p, "w") as f:
        f.write("{ not valid json")
    assert ek._cache_get("BROKEN") is None             # unreadable entry -> miss (never crashes)
