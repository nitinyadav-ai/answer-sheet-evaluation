# import os
# import sys
# import json
# import concurrent.futures
# from dotenv import load_dotenv

# load_dotenv()

# # Cost meter (single source of truth: scripts/llm_pricing.py); safe no-op fallback if import fails.
# sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))
# try:
#     from llm_pricing import log_cost
# except Exception:
#     def log_cost(*a, **k): pass

# from llm_client import generate, strip_reasoning, diagram_llm_opts, get_real_cost

# # Marks granularity (multiples of 0.5). evaluate.py quantizes again when it merges these results, so
# # the report is safe either way; doing it here as well keeps diagram_evals.json legal on disk and keeps
# # each justification describing the mark actually recorded.
# from marks_policy import MARK_STEP, quantize_mark

# # Partial-credit calibration switch, shared with the text grader (scripts/grading_calibration.py).
# from grading_calibration import is_v2 as _calibration_is_v2

# # Env-driven so diagram grading can A/B Qwen3-VL sizes with no code change. Falls back to EVAL_MODEL.
# # NOTE: Pass 2 sends the page image, so this MUST be a VISION-capable model (Qwen3-VL, not text-only).
# MODEL_ID = os.environ.get("DIAGRAM_EVAL_MODEL", os.environ.get("EVAL_MODEL", "qwen/qwen3-vl-30b-a3b-instruct"))
# # Extended thinking for diagram grading (opt-in): set DIAGRAM_EVAL_MODEL to a -thinking slug +
# # DIAGRAM_EVAL_REASONING_EFFORT. max_tokens is raised so reasoning never truncates the JSON verdict.
# _DIAG_REASONING = os.environ.get("DIAGRAM_EVAL_REASONING_EFFORT") or None
# _DIAG_MAX_TOKENS = int(os.environ.get("DIAGRAM_EVAL_MAX_TOKENS", "8192"))
# # Tight per-call timeout + low retry budget: a stalled pass fast-fails (only that diagram degrades)
# # rather than dragging the stage through the full 540s retry budget. Env DIAGRAM_LLM_TIMEOUT / -RETRIES.
# _DIAG_TIMEOUT, _DIAG_RETRIES = diagram_llm_opts()

# # STAGE BUDGET -- see the long note at its use in main(). Module-level so the value the stage actually
# # runs on is readable (and testable) rather than recomputed by each caller. DOUBLE the per-call worst
# # case because eval_single makes two SEQUENTIAL passes, and deliberately under the orchestrator's 420s
# # watchdog for evaluate_diagrams.py so the stage self-limits and keeps partial results.
# _STAGE_BUDGET = float(os.environ.get(
#     "DIAGRAM_EVAL_STAGE_TIMEOUT", str(2 * _DIAG_TIMEOUT * (_DIAG_RETRIES + 1) + 30)))

# # --- when to spend the vision audit (pass 2) -------------------------------------------------------
# #
# # MEASURED on the Science sheet, per call: pass 1 (text-only) costs 12-35s and ~500 output tokens;
# # pass 2 (vision) costs 34-110s and ~1500-3800 output tokens for ~1700 chars of JSON -- the remainder
# # is hidden reasoning. Pass 2 is ~75% of a stage that was the pipeline's critical path at 121-303s.
# #
# # A cascade (skip the audit on a confident, non-zero draft) is implemented here and DEFAULTS OFF,
# # because measuring it showed it does NOT buy wall-clock:
# #
# #   arm        wall              vision calls   out tokens
# #   always     122.6s / 132.7s   4, 4           9657, 5625
# #   cascade    186.6s /  99.4s   2, 2           8587, 7968
# #
# # The reason is structural: max_workers is 10 and a sheet has a handful of diagram questions, so ALL
# # the pass-2 calls already run CONCURRENTLY. Dropping 4 to 2 removes parallel work, not critical-path
# # work -- the stage wall is set by the single slowest call (86-155s), which a cascade never shortens.
# # It did not reliably cut tokens either. Kept behind the flag because it is a genuine cost lever for
# # BATCH runs (where concurrency is the scarce resource, not latency), and because the zero_mark trigger
# # is independently useful -- measured, Q22's text pass scored 0/2 and the audit it forced corrected it
# # to 2/2. Triggers mirror _cascade_escalation_reason in evaluate.py; partial credit deliberately does
# # NOT escalate (measuring that for text grading showed it moved marks AWAY from the teacher).
# #
# # DIAGRAM_EVAL_AUDIT=cascade enables it.
# _AUDIT_MODE = os.environ.get("DIAGRAM_EVAL_AUDIT", "always").strip().lower()
# # 0.8 is not a new number: evaluate_diagrams already calls anything below it `needs_review`, so the
# # audit now runs exactly on the drafts the report would flag for a human anyway.
# _AUDIT_CONF = float(os.environ.get("DIAGRAM_EVAL_AUDIT_CONFIDENCE", "0.8"))


# def audit_reason(draft, max_marks):
#     """Why pass 2 is worth its 34-110s for this draft -- or None to accept the draft as final."""
#     if _AUDIT_MODE == "always":
#         return "always"
#     if _AUDIT_MODE == "never":
#         return None
#     if not isinstance(draft, dict):
#         return "unparseable_draft"
#     try:
#         conf = float(draft.get("confidence_score", 0) or 0)
#     except (TypeError, ValueError):
#         return "non_numeric_confidence"
#     if conf < _AUDIT_CONF:
#         return "low_confidence"
#     try:
#         marks = float(draft.get("marks_awarded", 0) or 0)
#     except (TypeError, ValueError):
#         return "non_numeric_mark"
#     try:
#         mx = float(max_marks or 0)
#     except (TypeError, ValueError):
#         mx = 0.0
#     if mx > 0 and marks <= 0:
#         return "zero_mark"                       # never let a 0 stand on a text-only read
#     return None

# def eval_single(qid, image_paths, student_feats, db_answers, index):
#     in_tok = out_tok = 0
#     if qid not in db_answers:
#         return qid, None, index, in_tok, out_tok
        
#     max_marks = db_answers[qid]['marks']
#     expected = db_answers[qid]['answer'] 
#     feats = student_feats.get(qid, "")
    
#     # Per-feature partial credit. Without this the prompt was just "Calculate marks awarded" with no
#     # rubric at all, and it graded all-or-nothing against a verbatim feature list: a diagram missing
#     # only its axis labels scored 0 even when every structural relationship it showed was correct.
#     _diag_rubric = "" if not _calibration_is_v2() else f"""
#     MARKING RULES (CBSE step-marking -- award marks, do not deduct them):
#     1. Split the Expected Features into individual features and give each an equal share of the
#        {max_marks} marks.
#     2. Award EACH feature independently: full share if the student's diagram shows it, HALF share if
#        it is shown but incomplete, unlabelled or imprecise, zero only if it is absent or wrong.
#     3. Sum the shares. A diagram that gets some features right MUST score above 0.
#     4. Judge the DRAWING, not its notation: a correct shape, correct relative positions, correct
#        connections and correct relationships earn their marks even when labels use different symbols
#        or are missing. Never require an exact wording, letter or symbol match.
#     5. Accept any orientation, scale, or drawing style that conveys the same structure.
#     6. RESERVE ZERO for a missing diagram, or one that depicts something entirely different.
# """

#     # Pass 1: Initial Scoring
#     pass1_prompt = f"""
#     Evaluate the student's diagram features against the expected features.

#     Student Features: {feats}
#     Expected Features: {expected}
#     Maximum Marks: {max_marks}
#     {_diag_rubric}
#     Calculate marks awarded, provide justification and feedback.

#     MARK GRANULARITY: "marks_awarded" MUST be a multiple of {MARK_STEP} (0, {MARK_STEP}, 1, 1.5, 2, ...)
#     and must not exceed Maximum Marks. Never report a value like 0.8, 0.3, 0.7 or 2.25. If your
#     assessment falls between two legal values choose the nearer one, rounding an exact halfway case UP.
#     Your justification must describe the mark you actually report.

#     Output JSON:
#     {{
#       "marks_awarded": float,
#       "maximum_marks": float,
#       "student_diagram_features": string,
#       "correct_diagram_features": string,
#       "justification": string,
#       "feedback": string,
#       "confidence_score": float
#     }}
#     """
    
#     try:
#         # Pass 1 is text-only (student features vs expected features) -> provider-agnostic call.
#         text1, p_in, p_out = generate(model=MODEL_ID, prompt=pass1_prompt,
#                                       json_mode=True, temperature=0.1,
#                                       max_tokens=_DIAG_MAX_TOKENS, reasoning_effort=_DIAG_REASONING,
#                                       timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
#         in_tok += p_in
#         out_tok += p_out

#         def robust_parse(text):
#             text = strip_reasoning(text).strip()
#             start = text.find('{')
#             end = text.rfind('}')
#             if start != -1 and end != -1 and end >= start:
#                 text = text[start:end+1]
#             try:
#                 return json.loads(text)
#             except json.JSONDecodeError:
#                 parsed, _ = json.JSONDecoder().raw_decode(text)
#                 return parsed

#         draft = robust_parse(text1)

#         _reason = audit_reason(draft, max_marks)
#         if _reason is None:
#             # Draft is confident and non-zero: accept it and skip the vision audit. See _AUDIT_MODE.
#             draft["marks_awarded"] = quantize_mark(draft.get("marks_awarded", 0), max_marks)
#             draft["needs_review"] = draft.get("confidence_score", 1.0) < 0.8
#             draft.setdefault("Audited", "no")
#             return qid, draft, index, in_tok, out_tok

#         # Pass 2: Verification Critique -- re-checks the draft against the actual page image(s),
#         # so this is a VISION call. Text first, then image(s), preserving the original Gemini order.
#         audit_prompt = f"""
#         You are an auditor. Review this student diagram (which may span multiple pages) against the proposed evaluation draft.
#         Did the initial pass miss a feature that the student actually drew? 
#         Did it award a point for a feature that is illegible or missing?
#         Correct the evaluation if necessary.
        
#         Evaluation Draft: {json.dumps(draft)}
#         {_diag_rubric}
#         Return the FINAL corrected JSON in the same format.
#         """
#         # (image bytes are read inside llm_client from the paths passed below)
                
#         # A FAILED AUDIT MUST NOT ERASE A GOOD DRAFT. This used to fall through to the except below and
#         # return marks_awarded 0.0 -- so a question the text pass had already graded was silently
#         # zeroed by a transport failure. Measured: pass 2 returns an EMPTY response (in=0, out=0, in
#         # ~12s) for MULTI-PAGE diagram questions -- 2 pages is ~17k input tokens and ~3.5MB of image --
#         # which hit 2 of the 4 diagram questions on one sheet, both scored 0. With
#         # MAX_DIAGRAM_PAGES_PER_Q=4 that reaches any sheet whose diagram spans pages.
#         final = None
#         try:
#             text2, p_in, p_out = generate(model=MODEL_ID, parts=[{"text": audit_prompt}],
#                                           images=list(image_paths), json_mode=True, temperature=0.1,
#                                           max_tokens=_DIAG_MAX_TOKENS, reasoning_effort=_DIAG_REASONING,
#                                           timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
#             in_tok += p_in
#             out_tok += p_out
#             final = robust_parse(text2)
#         except Exception as _ae:
#             print(f"Diagram audit failed for {qid} ({_ae}); keeping the first-pass grade.",
#                   file=sys.stderr)
#         if not isinstance(final, dict):
#             final = dict(draft)
#             final["Audited"] = "failed"
#             # The audit is what checks the drawing itself, so an un-audited grade wants a human eye.
#             final["needs_review"] = True
#         else:
#             final.setdefault("Audited", "yes")
#         final.setdefault("Audit Reason", _reason)
#         # Snap to the half-mark ladder and cap at the key's maximum before this is written to disk.
#         final["marks_awarded"] = quantize_mark(final.get("marks_awarded", 0), max_marks)
#         if "needs_review" not in final:
#             final["needs_review"] = final.get("confidence_score", 1.0) < 0.8
#         return qid, final, index, in_tok, out_tok
#     except Exception as e:
#         print(f"Error evaluating diagram for {qid}: {e}", file=sys.stderr)
#         return qid, {
#             "marks_awarded": 0.0,
#             "maximum_marks": max_marks if 'max_marks' in locals() else 0.0,
#             "student_diagram_features": "ERROR",
#             "correct_diagram_features": "ERROR",
#             "justification": f"API or parsing error occurred during evaluation.",
#             "feedback": f"System Error: {str(e)}",
#             "confidence_score": 0.0,
#             "needs_review": True
#         }, index, in_tok, out_tok

# def load_json_arg(arg):
#     if arg.endswith('.json') and os.path.isfile(arg):
#         # student_features.json is the extractor's RAW stdout (not ASCII-escaped json.dump output), so
#         # it genuinely contains non-ASCII -- name the encoding or Windows decodes it as cp1252.
#         with open(arg, 'r', encoding="utf-8") as f:
#             return json.load(f)
#     return json.loads(arg)

# def main():
#     if len(sys.argv) < 4:
#         print("Usage: python3 evaluate_diagrams.py <diagram_crops_json_or_file> <student_features_json_or_file> <db_answers_json_or_file>")
#         sys.exit(1)
        
#     crops = load_json_arg(sys.argv[1])
#     student_feats = load_json_arg(sys.argv[2])
#     db_answers = load_json_arg(sys.argv[3])
    
#     # (no client object -- llm_client manages provider clients internally)
#     results_list = []
#     total_in = total_out = 0

#     # Group crops by question ID so multi-page diagrams are evaluated as one unit
#     grouped_crops = {}
#     for crop in crops:
#         qid = crop['question_id']
#         if qid not in grouped_crops:
#             grouped_crops[qid] = []
#         if crop['image'] not in grouped_crops[qid]:
#             grouped_crops[qid].append(crop['image'])
            
#     # STAGE BUDGET -- same discipline as extract_features.py, and for the same reason.
#     #
#     # This stage used to be ALL-OR-NOTHING: a bare `with ThreadPoolExecutor(...)` joins every worker at
#     # block exit, so ONE wedged call held the stage until the orchestrator's 420s watchdog killed the
#     # process -- discarding every diagram that had already been graded, because nothing is printed until
#     # the end. That is not hypothetical: DIAGRAM_EVAL_MAX_TOKENS is 12288 in .env, so a single runaway
#     # generation is ~473s on its own (see the cap comment in extract_features.py -- the per-call timeout
#     # measures idle gaps and cannot stop a steady stream).
#     #
#     # So: wait at the FUTURE level and keep whatever finished. The budget is DOUBLE the per-call worst
#     # case because eval_single makes TWO SEQUENTIAL passes (grade, then audit), and stays under the
#     # orchestrator's 420s ceiling so the stage self-limits and preserves partial results instead of
#     # being killed with nothing to show.
#     _budget = _STAGE_BUDGET

#     _stalled_any = False
#     qids = list(grouped_crops.keys())
#     executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
#     try:
#         futures = [executor.submit(eval_single, qid, image_paths, student_feats, db_answers, idx)
#                    for idx, (qid, image_paths) in enumerate(grouped_crops.items())]
#         done, pending = concurrent.futures.wait(futures, timeout=_budget)
#         for future in done:
#             try:
#                 qid, res, index, in_tok, out_tok = future.result()
#             except Exception as e:                       # a worker that raised despite its own guard
#                 print(f"Diagram evaluation worker failed: {e}", file=sys.stderr)
#                 continue
#             total_in += in_tok
#             total_out += out_tok
#             if res:
#                 results_list.append({"index": index, "qid": qid, "res": res})
#             else:
#                 # An unparseable/empty verdict used to vanish here with no trace, so a diagram that was
#                 # never assessed looked identical to one deliberately marked 0. Say so out loud; the
#                 # question keeps its WRITTEN-answer mark and full_evaluator's unassessed-diagram sidecar
#                 # flags it for review.
#                 print(f"[diagram-eval] {qid}: no usable verdict returned; keeping the written-answer "
#                       f"mark and leaving the diagram unassessed.", file=sys.stderr)
#         if pending:
#             _stalled_any = True
#             stalled = sorted({qids[i] for i, f in enumerate(futures) if f in pending})
#             print(f"[diagram-eval] {len(pending)} of {len(futures)} diagram(s) did not return within "
#                   f"{_budget:.0f}s and were abandoned (questions: {', '.join(stalled)}). "
#                   f"Keeping the {len(results_list)} that completed.", file=sys.stderr)
#             for f in pending:
#                 f.cancel()
#     finally:
#         # Never block shutdown on a wedged worker -- that is precisely the hang being fixed.
#         executor.shutdown(wait=False, cancel_futures=True)

#     # One ledger entry for the whole diagram-grading stage (2 calls per diagram).
#     _best, _nreal, _n = get_real_cost()
#     log_cost("diagram_grading", MODEL_ID, total_in, total_out, cost_usd=(_best if _nreal > 0 else None))
                
#     # Sort results by original index to maintain order for downstream processing
#     results_list.sort(key=lambda x: x["index"])
    
#     # Reconstruct final ordered dictionary
#     evaluations = {item["qid"]: item["res"] for item in results_list}
            
#     print(json.dumps(evaluations, indent=2))
#     return bool(_stalled_any)


# if __name__ == "__main__":
#     _had_stalled = main()
#     # A ThreadPoolExecutor's threads are NON-DAEMON and Python's atexit handler JOINS them, so a wedged
#     # worker would hang the interpreter at exit even after shutdown(wait=False) -- the stage would still
#     # be killed by the watchdog and its results still lost, defeating everything above. The evaluations
#     # JSON is already on stdout, so once it is flushed there is nothing left to do.
#     sys.stdout.flush()
#     sys.stderr.flush()
#     if _had_stalled:
#         os._exit(0)








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

from llm_client import generate, strip_reasoning, diagram_llm_opts, get_real_cost

# Marks granularity (multiples of 0.5). evaluate.py quantizes again when it merges these results, so
# the report is safe either way; doing it here as well keeps diagram_evals.json legal on disk and keeps
# each justification describing the mark actually recorded.
from marks_policy import MARK_STEP, quantize_mark

# Partial-credit calibration switch, shared with the text grader (scripts/grading_calibration.py).
from grading_calibration import is_v2 as _calibration_is_v2

# Env-driven so diagram grading can A/B Qwen3-VL sizes with no code change. Falls back to EVAL_MODEL.
# NOTE: Pass 2 sends the page image, so this MUST be a VISION-capable model (Qwen3-VL, not text-only).
MODEL_ID = os.environ.get("DIAGRAM_EVAL_MODEL", os.environ.get("EVAL_MODEL", "qwen/qwen3-vl-30b-a3b-instruct"))
# Extended thinking for diagram grading (opt-in): set DIAGRAM_EVAL_MODEL to a -thinking slug +
# DIAGRAM_EVAL_REASONING_EFFORT. max_tokens is raised so reasoning never truncates the JSON verdict.
_DIAG_REASONING = os.environ.get("DIAGRAM_EVAL_REASONING_EFFORT") or None
_DIAG_MAX_TOKENS = int(os.environ.get("DIAGRAM_EVAL_MAX_TOKENS", "8192"))
# Tight per-call timeout + low retry budget: a stalled pass fast-fails (only that diagram degrades)
# rather than dragging the stage through the full 540s retry budget. Env DIAGRAM_LLM_TIMEOUT / -RETRIES.
_DIAG_TIMEOUT, _DIAG_RETRIES = diagram_llm_opts()

# STAGE BUDGET -- see the long note at its use in main(). Module-level so the value the stage actually
# runs on is readable (and testable) rather than recomputed by each caller. DOUBLE the per-call worst
# case because eval_single makes two SEQUENTIAL passes, and deliberately under the orchestrator's 420s
# watchdog for evaluate_diagrams.py so the stage self-limits and keeps partial results.
_STAGE_BUDGET = float(os.environ.get(
    "DIAGRAM_EVAL_STAGE_TIMEOUT", str(2 * _DIAG_TIMEOUT * (_DIAG_RETRIES + 1) + 30)))

# --- when to spend the vision audit (pass 2) -------------------------------------------------------
#
# MEASURED on the Science sheet, per call: pass 1 (text-only) costs 12-35s and ~500 output tokens;
# pass 2 (vision) costs 34-110s and ~1500-3800 output tokens for ~1700 chars of JSON -- the remainder
# is hidden reasoning. Pass 2 is ~75% of a stage that was the pipeline's critical path at 121-303s.
#
# A cascade (skip the audit on a confident, non-zero draft) is implemented here and DEFAULTS OFF,
# because measuring it showed it does NOT buy wall-clock:
#
#   arm        wall              vision calls   out tokens
#   always     122.6s / 132.7s   4, 4           9657, 5625
#   cascade    186.6s /  99.4s   2, 2           8587, 7968
#
# The reason is structural: max_workers is 10 and a sheet has a handful of diagram questions, so ALL
# the pass-2 calls already run CONCURRENTLY. Dropping 4 to 2 removes parallel work, not critical-path
# work -- the stage wall is set by the single slowest call (86-155s), which a cascade never shortens.
# It did not reliably cut tokens either. Kept behind the flag because it is a genuine cost lever for
# BATCH runs (where concurrency is the scarce resource, not latency), and because the zero_mark trigger
# is independently useful -- measured, Q22's text pass scored 0/2 and the audit it forced corrected it
# to 2/2. Triggers mirror _cascade_escalation_reason in evaluate.py; partial credit deliberately does
# NOT escalate (measuring that for text grading showed it moved marks AWAY from the teacher).
#
# DIAGRAM_EVAL_AUDIT=cascade enables it.
_AUDIT_MODE = os.environ.get("DIAGRAM_EVAL_AUDIT", "always").strip().lower()
# 0.8 is not a new number: evaluate_diagrams already calls anything below it `needs_review`, so the
# audit now runs exactly on the drafts the report would flag for a human anyway.
_AUDIT_CONF = float(os.environ.get("DIAGRAM_EVAL_AUDIT_CONFIDENCE", "0.8"))

# ---------------------------------------------------------------------------------------------------
# SINGLE-PASS MODE (opt-in; DEFAULT OFF -- the two-pass path above remains today's exact behaviour
# unless this is explicitly enabled).
#
# The two-pass design exists because pass 1 (text-only, comparing STUDENT FEATURES against EXPECTED
# FEATURES) can be wrong in ways only the actual image can catch -- a feature the extractor mis-read,
# or one it missed. Pass 2 re-sends the page image to catch that. Measured: it is ~75% of the stage's
# wall-clock (34-110s vs 12-35s for pass 1), because it is a SECOND sequential network round-trip that
# also re-transmits the (often multi-page) image.
#
# Single-pass mode collapses this into ONE vision call: the model sees the page image(s) AND the
# expected features AND the rubric simultaneously, and grades directly off the drawing instead of off
# a text description a separate extraction step already produced. This removes an entire sequential
# round-trip per diagram, which is the single biggest time cost this stage has.
#
# TRADE-OFF, stated plainly: single-pass mode has NOT been measured for accuracy the way the two-pass
# design's audit-cascade was (see the arm comparison above). It is a genuine, currently-unverified
# accuracy risk -- the audit pass exists because a text-only draft was shown to miss real diagram
# content, and single-pass mode never gets that second look. Enable it only after comparing marks
# against the two-pass path on enough real diagram-bearing answers to trust the difference (or its
# absence) is not overfitting to one sheet. DIAGRAM_EVAL_SINGLE_PASS=1 enables it.
# ---------------------------------------------------------------------------------------------------
_SINGLE_PASS = os.environ.get("DIAGRAM_EVAL_SINGLE_PASS", "0").strip().lower() not in ("0", "false", "no", "off", "")


def audit_reason(draft, max_marks):
    """Why pass 2 is worth its 34-110s for this draft -- or None to accept the draft as final."""
    if _AUDIT_MODE == "always":
        return "always"
    if _AUDIT_MODE == "never":
        return None
    if not isinstance(draft, dict):
        return "unparseable_draft"
    try:
        conf = float(draft.get("confidence_score", 0) or 0)
    except (TypeError, ValueError):
        return "non_numeric_confidence"
    if conf < _AUDIT_CONF:
        return "low_confidence"
    try:
        marks = float(draft.get("marks_awarded", 0) or 0)
    except (TypeError, ValueError):
        return "non_numeric_mark"
    try:
        mx = float(max_marks or 0)
    except (TypeError, ValueError):
        mx = 0.0
    if mx > 0 and marks <= 0:
        return "zero_mark"                       # never let a 0 stand on a text-only read
    return None


def _robust_parse(text):
    text = strip_reasoning(text).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end >= start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        parsed, _ = json.JSONDecoder().raw_decode(text)
        return parsed


def _diag_rubric_for(max_marks):
    """Per-feature partial-credit rubric text, shared by both the single-pass and two-pass prompts."""
    if not _calibration_is_v2():
        return ""
    return f"""
    MARKING RULES (CBSE step-marking -- award marks, do not deduct them):
    1. Split the Expected Features into individual features and give each an equal share of the
       {max_marks} marks.
    2. Award EACH feature independently: full share if the student's diagram shows it, HALF share if
       it is shown but incomplete, unlabelled or imprecise, zero only if it is absent or wrong.
    3. Sum the shares. A diagram that gets some features right MUST score above 0.
    4. Judge the DRAWING, not its notation: a correct shape, correct relative positions, correct
       connections and correct relationships earn their marks even when labels use different symbols
       or are missing. Never require an exact wording, letter or symbol match.
    5. Accept any orientation, scale, or drawing style that conveys the same structure.
    6. RESERVE ZERO for a missing diagram, or one that depicts something entirely different.
"""


def eval_single_pass(qid, image_paths, student_feats, db_answers, index):
    """SINGLE vision call: grade the diagram directly off the page image(s), the expected features,
    and the rubric, in one round-trip -- no separate text-only draft and no follow-up audit. See the
    module docstring above _SINGLE_PASS for the trade-off this makes. Same return shape and same
    error-handling contract as eval_single, so main()'s aggregation code is unaffected either way."""
    in_tok = out_tok = 0
    if qid not in db_answers:
        return qid, None, index, in_tok, out_tok

    max_marks = db_answers[qid]['marks']
    expected = db_answers[qid]['answer']
    feats = student_feats.get(qid, "")             # kept for context; the image is now authoritative
    rubric = _diag_rubric_for(max_marks)

    prompt = f"""
    You are grading a student's hand-drawn diagram. Look at the attached page image(s) directly and
    grade the diagram you see there against the expected features -- do not rely solely on any
    pre-extracted feature description; verify it against the actual drawing.

    Extracted Student Features (a machine's first read of the image -- verify, do not blindly trust):
    {feats}
    Expected Features: {expected}
    Maximum Marks: {max_marks}
    {rubric}
    Calculate marks awarded, provide justification and feedback, based on what the image actually shows.

    MARK GRANULARITY: "marks_awarded" MUST be a multiple of {MARK_STEP} (0, {MARK_STEP}, 1, 1.5, 2, ...)
    and must not exceed Maximum Marks. Never report a value like 0.8, 0.3, 0.7 or 2.25. If your
    assessment falls between two legal values choose the nearer one, rounding an exact halfway case UP.
    Your justification must describe the mark you actually report.

    Output JSON:
    {{
      "marks_awarded": float,
      "maximum_marks": float,
      "student_diagram_features": string,
      "correct_diagram_features": string,
      "justification": string,
      "feedback": string,
      "confidence_score": float
    }}
    """
    try:
        text, p_in, p_out = generate(model=MODEL_ID, parts=[{"text": prompt}],
                                     images=list(image_paths), json_mode=True, temperature=0.1,
                                     max_tokens=_DIAG_MAX_TOKENS, reasoning_effort=_DIAG_REASONING,
                                     timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
        in_tok += p_in
        out_tok += p_out
        result = _robust_parse(text)
        if not isinstance(result, dict):
            raise ValueError("single-pass reply was not a JSON object")
        result["marks_awarded"] = quantize_mark(result.get("marks_awarded", 0), max_marks)
        if "needs_review" not in result:
            result["needs_review"] = result.get("confidence_score", 1.0) < 0.8
        result["Audited"] = "single-pass"           # display marker: this diagram skipped the 2nd pass
        return qid, result, index, in_tok, out_tok
    except Exception as e:
        print(f"Error evaluating diagram (single-pass) for {qid}: {e}", file=sys.stderr)
        return qid, {
            "marks_awarded": 0.0,
            "maximum_marks": max_marks if 'max_marks' in locals() else 0.0,
            "student_diagram_features": "ERROR",
            "correct_diagram_features": "ERROR",
            "justification": "API or parsing error occurred during evaluation.",
            "feedback": f"System Error: {str(e)}",
            "confidence_score": 0.0,
            "needs_review": True,
        }, index, in_tok, out_tok


def eval_single(qid, image_paths, student_feats, db_answers, index):
    # Dispatch to single-pass mode when enabled; the two-pass path below is completely untouched
    # otherwise, so DIAGRAM_EVAL_SINGLE_PASS unset/0 reproduces today's exact behaviour.
    if _SINGLE_PASS:
        return eval_single_pass(qid, image_paths, student_feats, db_answers, index)

    in_tok = out_tok = 0
    if qid not in db_answers:
        return qid, None, index, in_tok, out_tok
        
    max_marks = db_answers[qid]['marks']
    expected = db_answers[qid]['answer'] 
    feats = student_feats.get(qid, "")
    
    # Per-feature partial credit. Without this the prompt was just "Calculate marks awarded" with no
    # rubric at all, and it graded all-or-nothing against a verbatim feature list: a diagram missing
    # only its axis labels scored 0 even when every structural relationship it showed was correct.
    _diag_rubric = _diag_rubric_for(max_marks)

    # Pass 1: Initial Scoring
    pass1_prompt = f"""
    Evaluate the student's diagram features against the expected features.

    Student Features: {feats}
    Expected Features: {expected}
    Maximum Marks: {max_marks}
    {_diag_rubric}
    Calculate marks awarded, provide justification and feedback.

    MARK GRANULARITY: "marks_awarded" MUST be a multiple of {MARK_STEP} (0, {MARK_STEP}, 1, 1.5, 2, ...)
    and must not exceed Maximum Marks. Never report a value like 0.8, 0.3, 0.7 or 2.25. If your
    assessment falls between two legal values choose the nearer one, rounding an exact halfway case UP.
    Your justification must describe the mark you actually report.

    Output JSON:
    {{
      "marks_awarded": float,
      "maximum_marks": float,
      "student_diagram_features": string,
      "correct_diagram_features": string,
      "justification": string,
      "feedback": string,
      "confidence_score": float
    }}
    """
    
    try:
        # Pass 1 is text-only (student features vs expected features) -> provider-agnostic call.
        text1, p_in, p_out = generate(model=MODEL_ID, prompt=pass1_prompt,
                                      json_mode=True, temperature=0.1,
                                      max_tokens=_DIAG_MAX_TOKENS, reasoning_effort=_DIAG_REASONING,
                                      timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
        in_tok += p_in
        out_tok += p_out

        draft = _robust_parse(text1)

        _reason = audit_reason(draft, max_marks)
        if _reason is None:
            # Draft is confident and non-zero: accept it and skip the vision audit. See _AUDIT_MODE.
            draft["marks_awarded"] = quantize_mark(draft.get("marks_awarded", 0), max_marks)
            draft["needs_review"] = draft.get("confidence_score", 1.0) < 0.8
            draft.setdefault("Audited", "no")
            return qid, draft, index, in_tok, out_tok

        # Pass 2: Verification Critique -- re-checks the draft against the actual page image(s),
        # so this is a VISION call. Text first, then image(s), preserving the original Gemini order.
        audit_prompt = f"""
        You are an auditor. Review this student diagram (which may span multiple pages) against the proposed evaluation draft.
        Did the initial pass miss a feature that the student actually drew? 
        Did it award a point for a feature that is illegible or missing?
        Correct the evaluation if necessary.
        
        Evaluation Draft: {json.dumps(draft)}
        {_diag_rubric}
        Return the FINAL corrected JSON in the same format.
        """
        # (image bytes are read inside llm_client from the paths passed below)
                
        # A FAILED AUDIT MUST NOT ERASE A GOOD DRAFT. This used to fall through to the except below and
        # return marks_awarded 0.0 -- so a question the text pass had already graded was silently
        # zeroed by a transport failure. Measured: pass 2 returns an EMPTY response (in=0, out=0, in
        # ~12s) for MULTI-PAGE diagram questions -- 2 pages is ~17k input tokens and ~3.5MB of image --
        # which hit 2 of the 4 diagram questions on one sheet, both scored 0. With
        # MAX_DIAGRAM_PAGES_PER_Q=4 that reaches any sheet whose diagram spans pages.
        final = None
        try:
            text2, p_in, p_out = generate(model=MODEL_ID, parts=[{"text": audit_prompt}],
                                          images=list(image_paths), json_mode=True, temperature=0.1,
                                          max_tokens=_DIAG_MAX_TOKENS, reasoning_effort=_DIAG_REASONING,
                                          timeout=_DIAG_TIMEOUT, max_retries=_DIAG_RETRIES)
            in_tok += p_in
            out_tok += p_out
            final = _robust_parse(text2)
        except Exception as _ae:
            print(f"Diagram audit failed for {qid} ({_ae}); keeping the first-pass grade.",
                  file=sys.stderr)
        if not isinstance(final, dict):
            final = dict(draft)
            final["Audited"] = "failed"
            # The audit is what checks the drawing itself, so an un-audited grade wants a human eye.
            final["needs_review"] = True
        else:
            final.setdefault("Audited", "yes")
        final.setdefault("Audit Reason", _reason)
        # Snap to the half-mark ladder and cap at the key's maximum before this is written to disk.
        final["marks_awarded"] = quantize_mark(final.get("marks_awarded", 0), max_marks)
        if "needs_review" not in final:
            final["needs_review"] = final.get("confidence_score", 1.0) < 0.8
        return qid, final, index, in_tok, out_tok
    except Exception as e:
        print(f"Error evaluating diagram for {qid}: {e}", file=sys.stderr)
        return qid, {
            "marks_awarded": 0.0,
            "maximum_marks": max_marks if 'max_marks' in locals() else 0.0,
            "student_diagram_features": "ERROR",
            "correct_diagram_features": "ERROR",
            "justification": f"API or parsing error occurred during evaluation.",
            "feedback": f"System Error: {str(e)}",
            "confidence_score": 0.0,
            "needs_review": True
        }, index, in_tok, out_tok

def load_json_arg(arg):
    if arg.endswith('.json') and os.path.isfile(arg):
        # student_features.json is the extractor's RAW stdout (not ASCII-escaped json.dump output), so
        # it genuinely contains non-ASCII -- name the encoding or Windows decodes it as cp1252.
        with open(arg, 'r', encoding="utf-8") as f:
            return json.load(f)
    return json.loads(arg)

def main():
    if len(sys.argv) < 4:
        print("Usage: python3 evaluate_diagrams.py <diagram_crops_json_or_file> <student_features_json_or_file> <db_answers_json_or_file>")
        sys.exit(1)
        
    crops = load_json_arg(sys.argv[1])
    student_feats = load_json_arg(sys.argv[2])
    db_answers = load_json_arg(sys.argv[3])
    
    # (no client object -- llm_client manages provider clients internally)
    results_list = []
    total_in = total_out = 0

    # Group crops by question ID so multi-page diagrams are evaluated as one unit
    grouped_crops = {}
    for crop in crops:
        qid = crop['question_id']
        if qid not in grouped_crops:
            grouped_crops[qid] = []
        if crop['image'] not in grouped_crops[qid]:
            grouped_crops[qid].append(crop['image'])
            
    # STAGE BUDGET -- same discipline as extract_features.py, and for the same reason.
    #
    # This stage used to be ALL-OR-NOTHING: a bare `with ThreadPoolExecutor(...)` joins every worker at
    # block exit, so ONE wedged call held the stage until the orchestrator's 420s watchdog killed the
    # process -- discarding every diagram that had already been graded, because nothing is printed until
    # the end. That is not hypothetical: DIAGRAM_EVAL_MAX_TOKENS is 12288 in .env, so a single runaway
    # generation is ~473s on its own (see the cap comment in extract_features.py -- the per-call timeout
    # measures idle gaps and cannot stop a steady stream).
    #
    # So: wait at the FUTURE level and keep whatever finished. The budget is DOUBLE the per-call worst
    # case because eval_single makes TWO SEQUENTIAL passes (grade, then audit), and stays under the
    # orchestrator's 420s ceiling so the stage self-limits and preserves partial results instead of
    # being killed with nothing to show. In single-pass mode there is only ONE round-trip per diagram,
    # so this budget is generously oversized rather than tight -- harmless, since it is a ceiling, not
    # a target.
    _budget = _STAGE_BUDGET

    _stalled_any = False
    qids = list(grouped_crops.keys())
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
    try:
        futures = [executor.submit(eval_single, qid, image_paths, student_feats, db_answers, idx)
                   for idx, (qid, image_paths) in enumerate(grouped_crops.items())]
        done, pending = concurrent.futures.wait(futures, timeout=_budget)
        for future in done:
            try:
                qid, res, index, in_tok, out_tok = future.result()
            except Exception as e:                       # a worker that raised despite its own guard
                print(f"Diagram evaluation worker failed: {e}", file=sys.stderr)
                continue
            total_in += in_tok
            total_out += out_tok
            if res:
                results_list.append({"index": index, "qid": qid, "res": res})
            else:
                # An unparseable/empty verdict used to vanish here with no trace, so a diagram that was
                # never assessed looked identical to one deliberately marked 0. Say so out loud; the
                # question keeps its WRITTEN-answer mark and full_evaluator's unassessed-diagram sidecar
                # flags it for review.
                print(f"[diagram-eval] {qid}: no usable verdict returned; keeping the written-answer "
                      f"mark and leaving the diagram unassessed.", file=sys.stderr)
        if pending:
            _stalled_any = True
            stalled = sorted({qids[i] for i, f in enumerate(futures) if f in pending})
            print(f"[diagram-eval] {len(pending)} of {len(futures)} diagram(s) did not return within "
                  f"{_budget:.0f}s and were abandoned (questions: {', '.join(stalled)}). "
                  f"Keeping the {len(results_list)} that completed.", file=sys.stderr)
            for f in pending:
                f.cancel()
    finally:
        # Never block shutdown on a wedged worker -- that is precisely the hang being fixed.
        executor.shutdown(wait=False, cancel_futures=True)

    # One ledger entry for the whole diagram-grading stage (2 calls per diagram -- or 1 in single-pass).
    _best, _nreal, _n = get_real_cost()
    log_cost("diagram_grading", MODEL_ID, total_in, total_out, cost_usd=(_best if _nreal > 0 else None))
                
    # Sort results by original index to maintain order for downstream processing
    results_list.sort(key=lambda x: x["index"])
    
    # Reconstruct final ordered dictionary
    evaluations = {item["qid"]: item["res"] for item in results_list}
            
    print(json.dumps(evaluations, indent=2))
    return bool(_stalled_any)


if __name__ == "__main__":
    _had_stalled = main()
    # A ThreadPoolExecutor's threads are NON-DAEMON and Python's atexit handler JOINS them, so a wedged
    # worker would hang the interpreter at exit even after shutdown(wait=False) -- the stage would still
    # be killed by the watchdog and its results still lost, defeating everything above. The evaluations
    # JSON is already on stdout, so once it is flushed there is nothing left to do.
    sys.stdout.flush()
    sys.stderr.flush()
    if _had_stalled:
        os._exit(0)