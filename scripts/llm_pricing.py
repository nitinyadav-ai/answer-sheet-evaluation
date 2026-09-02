"""Single source of truth for LLM API pricing + per-run cost accounting.

Every pipeline stage that calls an LLM -- Gemini or an OpenAI-compatible Qwen3 endpoint via
llm_client -- imports this so the cost meter is CORRECT (priced per the model each stage actually
uses) and COMPLETE (each stage logs its own spend to one ledger). When a provider changes prices or
a model is added/removed, update MODEL_PRICING here -- and nowhere else.

Prices are USD per 1,000,000 tokens, paid tier (text in / text out). Gemini verified against
https://ai.google.dev/gemini-api/docs/pricing; Qwen3 against OpenRouter (June 2026). Locally
self-hosted models (vLLM/SGLang) have no per-token charge -> price them at (0.0, 0.0).
"""
import os
import json

# model_id -> (input_per_1M, output_per_1M)
MODEL_PRICING = {
    "gemini-3.5-flash":        (1.50, 9.00),
    "gemini-3-flash-preview":  (0.50, 3.00),
    "gemini-3.1-flash-lite":   (0.25, 1.50),
    "gemini-2.5-flash":        (0.30, 2.50),
    "gemini-2.5-flash-lite":   (0.10, 0.40),
    "gemini-2.0-flash":        (0.10, 0.40),    # deprecated (shutdown 2026-06-01); kept for legacy math
    "gemini-2.0-flash-lite":   (0.075, 0.30),   # deprecated (shutdown 2026-06-01)

    # --- Qwen3-VL via OpenRouter (paid API testing). Refine to the live OpenRouter rate as needed. ---
    "qwen/qwen3-vl-30b-a3b-instruct": (0.13, 0.52),    # live OpenRouter (Jun 2026); diagram-features + separator tier
    "qwen/qwen3-vl-235b-a22b-instruct": (0.20, 0.88),  # flagship (22B active) -- OCR + key-parser tier
    "qwen/qwen3-vl-235b-a22b-thinking": (0.26, 2.60),  # flagship thinking -- grading tier (subjective partial-credit)
    "qwen/qwen3.5-397b-a17b":         (0.385, 2.45),   # Qwen3.5 (Feb 2026) hybrid VL, 17B active -- grading tier (thinking via reasoning=low)
    "qwen/qwen3-vl-32b-instruct":     (0.104, 0.416),
    "qwen/qwen3-vl-30b-a3b-thinking": (0.13, 1.56),    # diagram-evaluation tier
    "qwen/qwen3-vl-8b-instruct":      (0.08, 0.50),
    # Locally self-hosted (DGX Spark / server vLLM/SGLang) -- no per-token charge.
    "qwen-local":                     (0.0, 0.0),
}

# Unknown / unrecognised model id -> price as the MOST EXPENSIVE Flash, so the meter can
# never silently UNDER-report (an over-estimate is far safer than an under-estimate, which
# is exactly the bug this module replaces).
_DEFAULT_PRICE = (1.50, 9.00)


def price_for(model_id):
    """(input_per_1M, output_per_1M) for a model id; safe default when unknown."""
    return MODEL_PRICING.get((model_id or "").strip(), _DEFAULT_PRICE)


def estimate_cost(model_id, input_tokens, output_tokens):
    """USD cost for a call/stage, given its model and token counts."""
    p_in, p_out = price_for(model_id)
    return ((int(input_tokens or 0) / 1_000_000.0) * p_in
            + (int(output_tokens or 0) / 1_000_000.0) * p_out)


def log_cost(stage, model_id, input_tokens, output_tokens, cost_usd=None):
    """Append one cost record to the per-run ledger at $API_COST_LOG (JSON-lines).

    `cost_usd` -- when provided (not None), the REAL provider-billed cost for the stage (OpenRouter's
    usage accounting, via llm_client.get_real_cost): the record uses it verbatim and marks
    cost_source="openrouter". When None, the record falls back to the per-model ESTIMATE (legacy
    behaviour), cost_source="estimate". The table estimate is ALWAYS also written as cost_estimate_usd so
    the real-vs-estimate gap is visible in the ledger even when the real cost is used.

    No-op when API_COST_LOG is unset (e.g. a standalone script run, or the answer-key /
    question-paper parse at upload time, which is per-exam not per-paper). NEVER raises --
    cost accounting must not be able to break the pipeline.
    """
    path = os.environ.get("API_COST_LOG")
    if not path:
        return
    try:
        est = estimate_cost(model_id, input_tokens, output_tokens)
        real = cost_usd if cost_usd is not None else est
        rec = {
            "stage": stage,
            "model": (model_id or "").strip(),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cost_usd": round(real, 6),
            "cost_source": "openrouter" if cost_usd is not None else "estimate",
            "cost_estimate_usd": round(est, 6),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
