import os
import sys
import json
import concurrent.futures
from dotenv import load_dotenv

load_dotenv()

# Cost meter (single source of truth: scripts/llm_pricing.py); safe no-op fallback if import fails.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))
try:
    from llm_pricing import log_cost
except Exception:
    def log_cost(*a, **k): pass

from llm_client import generate, diagram_llm_opts, get_real_cost

# Model is env-driven so this vision stage can A/B Qwen3-VL sizes with no code change.
# Falls back to OCR_MODEL (the other vision stage).
MODEL_ID = os.environ.get("DIAGRAM_FEATURES_MODEL", os.environ.get("OCR_MODEL", "qwen/qwen3-vl-30b-a3b-instruct"))

# Tight per-call timeout + low retry budget so ONE stalled crop fast-fails instead of burning the full
# 180s x 3 = 540s global budget (the observed 527s-for-6-crops straggler). Only the stalled crop
# degrades; the rest of the diagrams are transcribed normally.
_DIAG_TIMEOUT, _DIAG_RETRIES = diagram_llm_opts()

# OUTPUT CAP -- the real bound on this stage's latency. `timeout` is NOT one.
#
# A page that tipped the model into a repetition loop generated 16384 tokens (the provider's default
# cap) in 625s, against 591-816 tokens / 20-30s for a healthy read of the same kind of page. The
# generation RATE was normal throughout (26 vs 31 tok/s) -- nothing was slow, the model just would not
# stop. Two runs lost their whole diagram stage to this: it is what the 210s stage budget below was
# firing on, and on a 1-diagram sheet it burned 64% of the run to produce nothing.
#
# The per-call `timeout` cannot bound a runaway: it is an httpx READ timeout, i.e. the longest allowed
# SILENCE between bytes, and a steady 26 tok/s stream never goes silent. Measured: a probe with
# timeout=200 ran 625s to completion. Only a token cap ends it.
#
# 1536 is ~1.9x the largest healthy read observed (816), and 1536/26 tok/s ~= 59s -- inside the 90s
# per-call timeout, so a capped call now fails within its OWN budget instead of escaping to the
# stage's. Every other vision stage already sets one (OCR 32768, grading 12288, separator 512).
_FEAT_MAX_TOKENS = int(os.environ.get("DIAGRAM_FEATURES_MAX_TOKENS", "1536"))

def extract_single(qid, path, index):
    prompt = f"""You are an expert visual analysis engine.
List every label, shape, axis, arrow, and structural relationship present in this cropped student diagram for question {qid}.
Be extremely detailed. Do not interpret or evaluate, just list what is visually present."""
    try:
        # Provider-agnostic vision call (Gemini or Qwen3-VL) via llm_client.
        text, in_tok, out_tok = generate(model=MODEL_ID, prompt=prompt, images=[path],
                                         max_tokens=_FEAT_MAX_TOKENS,
                                         timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
        return qid, text, index, in_tok, out_tok
    except Exception as e:
        print(f"Error extracting features for {qid}: {e}", file=sys.stderr)
        return qid, f"[SYSTEM ERROR: Failed to extract features - {str(e)}]", index, 0, 0

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 extract_features.py <diagram_json_list_or_file>")
        sys.exit(1)

    arg = sys.argv[1]
    if arg.endswith('.json') and os.path.isfile(arg):
        with open(arg, 'r', encoding="utf-8") as f:
            diagrams = json.load(f)
    else:
        diagrams = json.loads(arg)

    # One vision call PER page-crop, all in parallel. The crop list is already bounded upstream by
    # detect_diagrams (it caps pages-per-question), so a single over-mapped question can no longer
    # inflate this stage into dozens of calls -- the fix that keeps the diagram stage cheap/fast.
    results_list = []
    total_in = total_out = 0

    # STAGE BUDGET. The per-call timeout bounds each crop, but a wedged connection that never
    # returns leaves its future pending forever -- and this stage used to be ALL-OR-NOTHING: the
    # orchestrator's watchdog killed the process, so NINE completed crops were thrown away along
    # with the one straggler, and the run silently produced a report with no diagram grades.
    #
    # So: wait at the FUTURE level too, with a budget derived from the per-call one, and keep
    # whatever finished. Partial features are worth far more than none -- every crop that came back
    # still gets graded. Deliberately independent of the SDK's own timeout so a failure inside it
    # cannot take the stage down.
    _budget = float(os.environ.get(
        "DIAGRAM_FEATURES_STAGE_TIMEOUT", str(_DIAG_TIMEOUT * (_DIAG_RETRIES + 1) + 30)))

    _stalled_any = False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
    try:
        futures = [executor.submit(extract_single, d['question_id'], d['image'], idx)
                   for idx, d in enumerate(diagrams)]
        done, pending = concurrent.futures.wait(futures, timeout=_budget)
        for future in done:
            try:
                qid, feats, index, in_tok, out_tok = future.result()
            except Exception as e:                       # a worker that raised despite its own guard
                print(f"Feature extraction worker failed: {e}", file=sys.stderr)
                continue
            total_in += in_tok
            total_out += out_tok
            results_list.append({"index": index, "qid": qid, "feats": feats})
        if pending:
            _stalled_any = True
            # Name the abandoned crops so the missing diagram grades are explainable afterwards.
            stalled = sorted({diagrams[i]['question_id'] for i, f in enumerate(futures) if f in pending})
            print(f"[diagram-features] {len(pending)} of {len(futures)} crops did not return within "
                  f"{_budget:.0f}s and were abandoned (questions: {', '.join(stalled)}). "
                  f"Keeping the {len(results_list)} that completed.", file=sys.stderr)
            for f in pending:
                f.cancel()
    finally:
        # Never block shutdown on a wedged worker -- that is precisely the hang being fixed.
        executor.shutdown(wait=False, cancel_futures=True)

    # One ledger entry for the whole feature-extraction stage (no stdout output -- this script's
    # stdout is the features JSON consumed downstream).
    _best, _nreal, _n = get_real_cost()
    log_cost("diagram_features", MODEL_ID, total_in, total_out, cost_usd=(_best if _nreal > 0 else None))

    # Sort results by original index to maintain order for downstream pipeline
    results_list.sort(key=lambda x: x["index"])

    # Reconstruct final ordered dictionary, combining features if diagram spans multiple pages
    results = {}
    for item in results_list:
        qid = item["qid"]
        feats = item["feats"]
        if qid in results:
            results[qid] += "\n\n--- Diagram continued on next page ---\n\n" + feats
        else:
            results[qid] = feats

    print(json.dumps(results, indent=2))
    return bool(_stalled_any)


if __name__ == "__main__":
    _had_stalled = main()
    # A ThreadPoolExecutor's threads are NON-DAEMON, and Python's atexit handler JOINS them. So a
    # wedged worker would hang the interpreter at exit even after shutdown(wait=False) -- the stage
    # would still be killed by the watchdog and its results still lost, defeating everything above.
    # The features JSON is already written to stdout, so once it is flushed there is nothing left to
    # do: leave immediately without waiting on the straggler.
    sys.stdout.flush()
    sys.stderr.flush()
    if _had_stalled:
        os._exit(0)
