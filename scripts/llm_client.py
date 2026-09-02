"""LLM client for the evaluation pipeline -- OpenRouter / OpenAI-compatible Qwen3 ONLY.

Every AI stage (OCR, grading, diagram features/eval, separator, key/question-paper parsing) calls
ONE uniform function here -- `generate(...)` -- which talks to an OpenAI-compatible endpoint:
OpenRouter (hosted Qwen3) by default, or a local vLLM / SGLang server. Configure via `.env`:

    LLM_BASE_URL   default https://openrouter.ai/api/v1   (local: http://localhost:8000/v1)
    LLM_API_KEY    OpenRouter API key (any non-empty value for a local server)
    LLM_JSON_MODE  request response_format=json_object (auto-falls back if unsupported); 1=on, 0=off

Per-stage model ids are env vars (OCR_MODEL, EVAL_MODEL, DIAGRAM_EVAL_MODEL, ...), so A/B-ing Qwen3
sizes (30B <-> 32B) is a pure `.env` edit with no code change.

`generate()` returns a plain `(text, input_tokens, output_tokens)` tuple, so call sites never touch a
provider-specific response object.

Gemini support was removed -- the project relies entirely on OpenRouter's Qwen3. The `media_resolution`
and `thinking_budget` keyword args are accepted-but-IGNORED (they were Gemini-only) so existing call
sites need no change. `strip_reasoning()` removes `<think>...</think>` blocks a Qwen `-Thinking`
variant emits before its JSON/answer.
"""

import os
import re
import base64
import threading

_LOCK = threading.Lock()
_OPENAI_CLIENTS = {}   # (base_url, api_key) -> openai.OpenAI


# ---------------------------------------------------------------------------
# Real-cost accounting -- capture OpenRouter's ACTUAL billed cost (usage.cost)
# ---------------------------------------------------------------------------
# The cost meter used to price every stage from a fixed per-MODEL table, blind to which provider actually
# served the request -- so with LLM_PROVIDER_SORT=throughput (route to the fastest, not cheapest, backend)
# the real bill could drift off the table and the report never showed it. OpenRouter returns the true
# per-call cost in `usage.cost` when the request carries `usage:{include:true}`; we read it in generate()
# and accumulate it PER PROCESS. Because every metered stage runs in its own subprocess, this global scopes
# cleanly to exactly that stage, which reads it via get_real_cost() at its log_cost() call. Any call the
# provider doesn't price falls back to the per-model estimate, so the running total is always complete.
_COST_LOCK = threading.Lock()
_COST_ACCUM = {"best": 0.0, "n_real": 0, "n": 0}


def _usage_accounting_on():
    """Whether to ask OpenRouter for real-cost accounting (env LLM_USAGE_ACCOUNTING, default ON). Set to
    0 to fully revert to the legacy per-model estimate (no usage.include is sent)."""
    return os.environ.get("LLM_USAGE_ACCOUNTING", "1").strip().lower() not in ("0", "false", "no", "off")


def reset_cost_accum():
    """Zero the per-process real-cost accumulator. Defensive -- a fresh subprocess already starts at 0, but
    this makes a stage safe to run more than once in a single interpreter (tests, future refactors)."""
    with _COST_LOCK:
        _COST_ACCUM["best"] = 0.0
        _COST_ACCUM["n_real"] = 0
        _COST_ACCUM["n"] = 0


def get_real_cost():
    """(best_total_usd, n_real, n_calls) accumulated since the last reset. `best_total` sums the REAL
    provider-billed cost for every call that reported one and the per-model ESTIMATE for the rest, so it is
    a complete stage total. `n_real > 0` means at least one call was priced by the provider (pass the total
    to log_cost); `n_real == 0` means nothing was provider-priced (let log_cost estimate as before)."""
    with _COST_LOCK:
        return _COST_ACCUM["best"], _COST_ACCUM["n_real"], _COST_ACCUM["n"]


def _usage_cost(u):
    """OpenRouter's real billed cost (USD) from a response `usage` object, or None when absent/invalid.
    Returned as `usage.cost` when the request carried `usage:{include:true}`; the OpenAI SDK keeps unknown
    fields (CompletionUsage allows extras), so it reads as an attribute or via model_extra. NaN/inf/negative
    are treated as 'not reported' -> None (so we fall back to the estimate)."""
    if u is None:
        return None
    c = getattr(u, "cost", None)
    if c is None:
        me = getattr(u, "model_extra", None)
        if isinstance(me, dict):
            c = me.get("cost")
    if c is None and isinstance(u, dict):
        c = u.get("cost")
    if c is None:
        return None
    try:
        c = float(c)
    except (TypeError, ValueError):
        return None
    if c != c or c in (float("inf"), float("-inf")) or c < 0:
        return None
    return c


def _accumulate_cost(model, in_tok, out_tok, real_cost):
    """Add one call's cost to the per-process accumulator: the REAL cost when the provider reported it, else
    the per-model estimate (so the running total stays complete). Never raises."""
    if real_cost is not None:
        per_call, is_real = real_cost, True
    else:
        try:
            from llm_pricing import estimate_cost   # lazy: no circular import (llm_pricing imports only os/json)
            per_call = estimate_cost(model, in_tok, out_tok)
        except Exception:
            per_call = 0.0
        is_real = False
    with _COST_LOCK:
        _COST_ACCUM["best"] += per_call
        _COST_ACCUM["n"] += 1
        if is_real:
            _COST_ACCUM["n_real"] += 1


# ---------------------------------------------------------------------------
# Reasoning-block stripper (Qwen -Thinking variants)
# ---------------------------------------------------------------------------
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text):
    """Remove `<think>...</think>` reasoning blocks a Qwen `-Thinking` model emits before its real
    output. Also handles a TRUNCATED leading `<think>` with no closing tag (reasoning ran out of
    tokens) by dropping everything up to the first `{` or `[`. No-op (idempotent) on text with no
    such tags, so it is always safe to call (e.g. on `-Instruct` output)."""
    if not text:
        return text
    s = _THINK_RE.sub("", text)
    low = s.lstrip().lower()
    if low.startswith("<think>") and "</think>" not in low:
        idxs = [i for i in (s.find("{"), s.find("[")) if i != -1]
        if idxs:
            return s[min(idxs):].strip()
    return s.strip()


# ---------------------------------------------------------------------------
# Part normalisation -- one ordered list of {"text":...} / {"image_*":...}
# ---------------------------------------------------------------------------
def _norm_parts(prompt, parts, images):
    """Flatten prompt / parts / images into ONE ordered list (text first, then images), preserving
    the order each call site intends."""
    out = []
    if prompt is not None:
        out.append({"text": prompt})
    for p in (parts or []):
        out.append(p)
    for im in (images or []):
        out.append({"image_png": im} if isinstance(im, (bytes, bytearray)) else {"image_path": im})
    return out


def _png_bytes(item):
    if "image_png" in item:
        return bytes(item["image_png"])
    with open(item["image_path"], "rb") as fh:
        return fh.read()


def _json_mode_enabled():
    return os.environ.get("LLM_JSON_MODE", "1").strip().lower() not in ("0", "false", "no", "")


def _client():
    """Lazily create + cache the OpenAI-compatible client (thread-safe: grading drives this under
    asyncio.to_thread + a Semaphore(15))."""
    from openai import OpenAI
    base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    # OpenRouter needs a real key; a local vLLM/SGLang server accepts any non-empty value.
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "EMPTY"
    ckey = (base, key)
    with _LOCK:
        c = _OPENAI_CLIENTS.get(ckey)
        if c is None:
            # Bound each request (env LLM_TIMEOUT, default 180s) so a slow / rate-limited / hanging
            # OpenRouter response FAILS FAST and the per-stage error handlers degrade gracefully,
            # instead of stalling the whole pipeline on the SDK's 600s default timeout.
            c = _OPENAI_CLIENTS[ckey] = OpenAI(
                base_url=base, api_key=key,
                timeout=float(os.environ.get("LLM_TIMEOUT", "180")),
                max_retries=int(os.environ.get("LLM_MAX_RETRIES", "2")),
            )
    return c


def _is_timeout_error(exc):
    """True for a timeout / connection failure, as opposed to the server REJECTING a parameter.

    Only the latter is worth re-issuing. Matching is by class name plus a message fallback rather
    than by importing openai's exception classes, so this keeps working against a local
    OpenAI-compatible server or a different SDK version, and can never raise on an unexpected type.
    """
    try:
        name = type(exc).__name__.lower()
    except Exception:                                    # pragma: no cover - pathological exc type
        return False
    if any(k in name for k in ("timeout", "connection", "apiconnection")):
        return True
    # str() on an exception can itself raise (a __str__ that blows up). This runs INSIDE an except
    # handler, so letting that escape would replace a recoverable API error with an unrelated crash.
    try:
        msg = str(exc).lower()
    except Exception:
        return False
    return any(k in msg for k in ("timed out", "timeout", "connection error"))


def diagram_llm_opts():
    """(timeout, max_retries) for the FAST diagram vision calls -- deliberately tighter than the global
    LLM_TIMEOUT(180)/LLM_MAX_RETRIES(2) so a single stalled diagram crop FAST-FAILS instead of dragging
    the whole feature-extraction / evaluation stage out while every other crop sits done. Only that one
    crop degrades; the rest are unaffected.

    Worst case is `timeout x (max_retries + 1)` -- 180s by default. That bound is only true because
    `generate()` refuses to re-issue on a timeout (see _is_timeout_error at the call site): the
    parameter-rejection retry there used to fire on timeouts too and silently doubled this to 360s.

    Env: DIAGRAM_LLM_TIMEOUT (default 90s), DIAGRAM_LLM_MAX_RETRIES (default 1)."""
    try:
        t = float(os.environ.get("DIAGRAM_LLM_TIMEOUT", "90"))
    except ValueError:
        t = 90.0
    try:
        r = int(os.environ.get("DIAGRAM_LLM_MAX_RETRIES", "1"))
    except ValueError:
        r = 1
    return t, max(0, r)


def _csv_env(name):
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _truthy(name, default):
    """Read a 0/1-style env flag. `default` applies when the variable is unset or empty."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _provider_directive():
    """Build an OpenRouter `provider` routing directive from env.

    PRIVACY (on by DEFAULT -- this pipeline sends children's exam answers to third-party inference
    providers, so the safe setting has to be the one you get without configuring anything):

      LLM_PROVIDER_DATA_COLLECTION "deny" (DEFAULT) | "allow" -> `data_collection`. OpenRouter's own
                                   default is "allow", which permits providers that store prompts
                                   non-transiently AND MAY TRAIN ON THEM. "deny" routes only to
                                   providers that do not collect user data for training.
      LLM_PROVIDER_ZDR             0/1 (DEFAULT 1) -> `zdr`. Zero Data Retention: route only to
                                   endpoints that do not store the prompt AT REST. A provider can be
                                   ZDR without being data_collection:deny and vice versa, so BOTH are
                                   set -- they are different guarantees.

    NOTE: both RESTRICT the endpoint pool, and a model served by few providers can lose all of them.
    `qwen3-vl-235b-a22b-thinking` has only two (Alibaba, Novita). `allow_fallbacks` is therefore left
    at OpenRouter's default (true) unless explicitly set, and scripts/check_provider_privacy.py
    verifies each configured model still resolves under the active policy. If a model has no
    compliant endpoint, OpenRouter returns an error rather than silently downgrading privacy --
    which is the correct failure direction for student data.

    PERFORMANCE (unchanged, opt-in): routes to a faster/steadier backend WITHOUT changing the model,
    prompt or sampling -- the fix for provider-stall latency variance (identical grading jobs taking
    164s vs 271s purely by which backend OpenRouter picked).

      LLM_PROVIDER_SORT            "throughput" | "latency" | "price"
      LLM_PROVIDER_ORDER           csv of provider slugs -> explicit priority
      LLM_PROVIDER_ONLY            csv -> restrict to these providers
      LLM_PROVIDER_IGNORE          csv -> exclude these providers
      LLM_PROVIDER_QUANTIZATIONS   csv (e.g. "bf16,fp16") -> full precision only
      LLM_PROVIDER_ALLOW_FALLBACKS 0/1 (default: OpenRouter's, true)
      LLM_PROVIDER_REQUIRE_PARAMETERS 0/1 (default 1) -> only providers that honour every request
                                   param (esp. `reasoning`), so thinking is never silently dropped.

    Returns a dict (never None by default, because the privacy fields are always on) so the directive
    can no longer be dropped by forgetting to set a performance knob."""
    prov = {}

    # --- privacy: always emitted unless explicitly disabled ---------------------------------------
    # An UNRECOGNISED value falls back to "deny", never to omission: leaving the field out hands the
    # decision to OpenRouter's own default, which is "allow" (retention AND training permitted). So a
    # one-character typo -- LLM_PROVIDER_DATA_COLLECTION=denny -- would otherwise silently ship student
    # answers to a training provider while the config still read as if privacy were on.
    dc = os.environ.get("LLM_PROVIDER_DATA_COLLECTION", "deny").strip().lower()
    if dc not in ("deny", "allow"):
        print(f"[provider] ignoring invalid LLM_PROVIDER_DATA_COLLECTION={dc!r}; using 'deny'")
        dc = "deny"
    prov["data_collection"] = dc
    if _truthy("LLM_PROVIDER_ZDR", True):
        prov["zdr"] = True

    # --- performance: opt-in, unchanged -----------------------------------------------------------
    sort = os.environ.get("LLM_PROVIDER_SORT", "").strip().lower()
    if sort in ("throughput", "latency", "price"):
        prov["sort"] = sort
    order, only, ignore, quants = (_csv_env("LLM_PROVIDER_ORDER"), _csv_env("LLM_PROVIDER_ONLY"),
                                   _csv_env("LLM_PROVIDER_IGNORE"), _csv_env("LLM_PROVIDER_QUANTIZATIONS"))
    if order:
        prov["order"] = order
    if only:
        prov["only"] = only
    if ignore:
        prov["ignore"] = ignore
    if quants:
        prov["quantizations"] = quants
    af = os.environ.get("LLM_PROVIDER_ALLOW_FALLBACKS", "").strip().lower()
    if af in ("0", "false", "no", "off"):
        prov["allow_fallbacks"] = False
    elif af in ("1", "true", "yes", "on"):
        prov["allow_fallbacks"] = True
    prov["require_parameters"] = os.environ.get("LLM_PROVIDER_REQUIRE_PARAMETERS", "1").strip().lower() \
        not in ("0", "false", "no", "off")
    return prov or None


def generate(model, prompt=None, parts=None, images=None, temperature=0.0, max_tokens=None,
             top_p=None, json_mode=False, system=None, media_resolution=None,
             thinking_budget=None, timeout=None, reasoning_effort=None, max_retries=None):
    """Generate a completion from `model` and return `(text, input_tokens, output_tokens)`.

    Args:
        model: OpenRouter / OpenAI-compatible model slug (e.g. "qwen/qwen3-vl-30b-a3b-instruct").
        prompt: convenience single text part.
        parts: ordered mixed list of {"text": str} / {"image_png": bytes} / {"image_path": str}.
        images: convenience list of PNG paths or raw bytes, appended AFTER prompt/parts.
        temperature, max_tokens, top_p: standard sampling controls.
        json_mode: request response_format=json_object (best-effort; downstream robust parsing is
                   authoritative -- correctness never depends on it).
        system: optional system instruction.
        media_resolution, thinking_budget: ACCEPTED BUT IGNORED (Gemini-only legacy; kept so call
                   sites need no change).
        reasoning_effort: "low"|"medium"|"high" -> enables extended thinking on a reasoning-capable
                   model via OpenRouter's `reasoning` param. The model must be a `-thinking` variant
                   (Instruct models ignore it). OpenRouter keeps the chain-of-thought in a separate
                   `reasoning` field, so JSON `content` stays clean; any inline <think> is also
                   stripped downstream. None/empty = no thinking.
        timeout: per-request timeout in seconds.
        max_retries: per-call override of the client's retry budget (client default = LLM_MAX_RETRIES).
                   Lower it (e.g. 1) for the fast diagram calls so a stalled crop fails fast instead of
                   burning the full retry budget. None = use the client default.
    """
    items = _norm_parts(prompt, parts, images)
    client = _client()
    if max_retries is not None:
        # Per-call retry budget (OpenAI SDK returns a shallow copy sharing the same connection pool).
        try:
            client = client.with_options(max_retries=int(max_retries))
        except Exception:
            pass

    # Text-only -> a plain string (most universally accepted); else the multimodal parts list with
    # base64 data-URL images (Qwen3-VL).
    text_only = all("text" in it for it in items)
    if text_only:
        user_content = "\n".join(it["text"] for it in items)
    else:
        user_content = []
        for it in items:
            if "text" in it:
                user_content.append({"type": "text", "text": it["text"]})
            else:
                b64 = base64.b64encode(_png_bytes(it)).decode("ascii")
                user_content.append({"type": "image_url",
                                     "image_url": {"url": f"data:image/png;base64,{b64}"}})

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    # JSON mode is provider-dependent on OpenRouter / local servers and is NEVER relied upon for
    # correctness. Request it only when asked + not globally disabled; auto-retry once without it if
    # the server rejects it.
    if json_mode and _json_mode_enabled():
        kwargs["response_format"] = {"type": "json_object"}
    # extra_body carries OpenRouter-specific directives:
    #   * reasoning -> extended thinking; routed to a separate `reasoning` field (JSON content stays
    #     clean). Only effective on a `-thinking` model; Instruct models ignore it.
    #   * provider  -> route this call to a faster/steadier backend (same model + params, different
    #     server) to remove provider-stall latency variance -- no effect on the output content.
    extra_body = {}
    if reasoning_effort:
        extra_body["reasoning"] = {"effort": str(reasoning_effort)}
    _prov = _provider_directive()
    if _prov:
        extra_body["provider"] = _prov
    #   * usage -> ask OpenRouter to return the ACTUAL billed cost in usage.cost, so the meter records the
    #     real bill instead of a per-model estimate. Harmless on servers that ignore it; gated by env.
    if _usage_accounting_on():
        extra_body["usage"] = {"include": True}
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as _first_err:
        # This retry exists for ONE failure mode: an endpoint rejecting an optional knob
        # (`response_format` / `reasoning`). Re-issuing the same request with the same parameters is
        # the right response to a 400; it is the WRONG response to a timeout.
        #
        # Retrying a timeout here MULTIPLIES the caller's budget by another full round:
        #   timeout x (sdk_retries + 1) x (this retry + 1)  =  90 x 2 x 2 = 360s
        # for a caller that asked for 90s and reasonably expected 180s. That is what turned one
        # wedged diagram crop into a 420s stage timeout -- the whole stage was killed by its
        # watchdog, discarding NINE already-successful crops and silently producing a report with no
        # diagram grades at all. So: never re-issue on a timeout/connection failure; surface it and
        # let the caller's own budget govern.
        if _is_timeout_error(_first_err):
            raise
        _eb = kwargs.get("extra_body")
        _pin = _eb.get("provider") if isinstance(_eb, dict) else None
        _usg = _eb.get("usage") if isinstance(_eb, dict) else None
        if "response_format" in kwargs or isinstance(_eb, dict):
            kwargs.pop("response_format", None)
            if _pin is not None:
                # keep the provider pin (fast backend) + usage accounting; drop only reasoning/json_mode.
                _retry_eb = {"provider": _pin}
                if _usg is not None:
                    _retry_eb["usage"] = _usg
                kwargs["extra_body"] = _retry_eb
            else:
                kwargs.pop("extra_body", None)   # no pin -> drop extra_body entirely (safe for strict servers)
            resp = client.chat.completions.create(**kwargs)
        else:
            raise

    text = (resp.choices[0].message.content or "") if resp.choices else ""
    # Optional diagnostics: which OpenRouter backend actually served this call (set LLM_LOG_PROVIDER=1).
    if os.environ.get("LLM_LOG_PROVIDER", "").strip() not in ("", "0", "false", "no", "off"):
        try:
            import sys as _sys
            print(f"[provider] {getattr(resp, 'provider', None)} | model={model}", file=_sys.stderr)
        except Exception:
            pass
    u = getattr(resp, "usage", None)
    in_tok = getattr(u, "prompt_tokens", 0) or 0
    out_tok = getattr(u, "completion_tokens", 0) or 0
    # Record this call's REAL provider-billed cost (or fall back to the estimate) into the per-process meter.
    _accumulate_cost(model, in_tok, out_tok, _usage_cost(u))
    return text, in_tok, out_tok
