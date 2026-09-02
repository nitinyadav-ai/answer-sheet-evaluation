#!/usr/bin/env python3
"""Verify every configured model still resolves under the ACTIVE provider-privacy policy.

Why this exists
---------------
`zdr: true` and `data_collection: "deny"` both RESTRICT which endpoints OpenRouter may route to. A
model served by many providers shrugs that off; a model served by two may lose both. The grading
model `qwen3-vl-235b-a22b-thinking` has exactly two (Alibaba, Novita), so the privacy defaults could
in principle take grading offline.

OpenRouter's failure mode here is the RIGHT one -- it errors rather than quietly routing to a
non-compliant provider -- but an error at 2am mid-exam-cycle is still an outage. Run this after any
change to the LLM_PROVIDER_* settings, and in CI before a deploy.

Each check is a real API call capped at a couple of tokens, so a full run costs a fraction of a cent.

Usage:
    python3 scripts/check_provider_privacy.py            # check the models named in the environment
    python3 scripts/check_provider_privacy.py --compare  # also show what the policy excludes
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import llm_client  # noqa: E402

# EVERY model the pipeline can call, with the env var that overrides each.
#
# This list MUST stay exhaustive. The first version checked only five of these and reported "all
# configured models resolve" -- while DIAGRAM_FEATURES_MODEL, which it never tested, could not be
# served under the policy at all. That stage then burned 420s retrying and billed nothing, turning a
# ~4.5 min evaluation into ~9 min. A partial check is worse than no check, because it produces a
# green tick that is not true. tests/test_provider_privacy_coverage.py fails if a *_MODEL env var
# exists in the codebase but is missing here.
MODELS = [
    ("grading", "EVAL_MODEL", "qwen/qwen3-vl-235b-a22b-thinking"),
    ("grading (cascade fast pass)", "EVAL_CASCADE_FAST_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("OCR", "OCR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("answer-key parser", "KEY_PARSER_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("question separator", "SEPARATOR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("diagram features", "DIAGRAM_FEATURES_MODEL", "qwen/qwen3-vl-30b-a3b-instruct"),
    ("diagram grading", "DIAGRAM_EVAL_MODEL", "qwen/qwen3-vl-30b-a3b-thinking"),
    ("diagram crop", "DIAGRAM_CROP_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("orientation", "ORIENT_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("segment repair", "SEGMENT_REPAIR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
    ("glue matcher", "GLUE_MATCHER_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"),
]


def _probe(model):
    """One minimal completion. Returns (ok, detail) -- never raises."""
    try:
        text, _in, _out = llm_client.generate(
            model=model, prompt="Reply with the single word: ok",
            temperature=0.0, max_tokens=4, max_retries=0, timeout=60)
        return True, (text or "").strip()[:40] or "(empty reply)"
    except Exception as e:                                   # noqa: BLE001 - report, never crash
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def main():
    directive = llm_client._provider_directive()
    print("Active provider directive:")
    for k, v in sorted((directive or {}).items()):
        print(f"    {k:22} = {v}")
    if not directive or not directive.get("zdr"):
        print("    !! ZDR is OFF -- prompts may be retained at rest by the serving provider")
    if (directive or {}).get("data_collection") != "deny":
        print("    !! data_collection is not 'deny' -- a provider may TRAIN on student answers")
    print()

    seen, failures = set(), []
    for role, env_var, default in MODELS:
        model = os.environ.get(env_var, default)
        if model in seen:
            continue
        seen.add(model)
        ok, detail = _probe(model)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {role:28} {model}")
        if not ok:
            print(f"         {detail}")
            failures.append((role, model, detail))

    if "--compare" in sys.argv:
        print("\nWithout the privacy policy (what it is excluding):")
        saved = {k: os.environ.get(k) for k in ("LLM_PROVIDER_ZDR", "LLM_PROVIDER_DATA_COLLECTION")}
        os.environ["LLM_PROVIDER_ZDR"] = "0"
        os.environ["LLM_PROVIDER_DATA_COLLECTION"] = "allow"
        try:
            for role, env_var, default in MODELS:
                model = os.environ.get(env_var, default)
                if any(model == f[1] for f in failures):
                    ok, detail = _probe(model)
                    verdict = "works WITHOUT privacy -> the policy is what blocks it" if ok else "fails either way"
                    print(f"  {model}: {verdict}")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    print()
    if failures:
        print(f"{len(failures)} model(s) cannot be served under the current privacy policy.")
        print("Options: relax the policy for that model, switch to a model with more compliant")
        print("providers, or accept the reduced availability. Do NOT silently disable privacy.")
        return 1
    print("All configured models resolve under the active privacy policy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
