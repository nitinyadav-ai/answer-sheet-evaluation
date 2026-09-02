"""Single source of truth for the marks GRANULARITY rule: a mark is always a multiple of 0.5.

Why this module exists
----------------------
Nothing in the pipeline used to round a mark. The grader's raw float was clamped to
[0, Maximum Marks] and written straight to the report, so a model that replied
`"Marks Awarded": 0.8` produced a report showing 0.8 -- a value no examiner can defend and no
mark sheet can hold. `EVAL_POINTWISE=1` makes this reachable by construction: it tells the model
to treat the marking scheme as value-points "each worth a share of the marks", and a 4-mark
question with 5 value-points is 0.8 a point.

The rule is enforced in TWO places on purpose:
  * in the grading prompt (see evaluate.py) so the model emits a legal mark natively and its
    justification matches the number it gave, and
  * here, at every write site, so an illegal value can never reach a report even if the model
    ignores the instruction, a legacy file is re-read, or a teacher types one by hand.

Rounding rule
-------------
Nearest 0.5, with an exact tie (x.25 / x.75) going UP -- the student's favour, and the usual
convention when a scheme is halved. Note this is NOT Python's built-in round(), which is
round-half-to-EVEN: round(0.25, 1) is 0.2 and round(2.5) is 2, which would quietly move marks
DOWN on exactly the values most likely to be produced by a per-point split.
"""

import math

# The only granularity a mark may take. Every valid mark is an integer multiple of this.
MARK_STEP = 0.5

# Absorbs binary-float dust so a value that is mathematically an exact tie (0.25, 0.75) is not
# pushed to the wrong side by its representation.
_EPS = 1e-9


def is_valid_mark(value):
    """True when `value` is a finite, non-negative multiple of MARK_STEP.

    Used by the tests and by the upload pre-flight, which REPORTS a bad maximum rather than
    rewriting it -- an answer key is the teacher's ground truth, so a 0.8-mark question is a
    parse error to surface, not a number to silently correct.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(v) or v < 0:
        return False
    return abs(v / MARK_STEP - round(v / MARK_STEP)) <= _EPS


def quantize_mark(value, maximum=None, default=0.0):
    """Snap `value` to the nearest MARK_STEP multiple, then clamp to [0, maximum].

    Returns an int when the result is whole (2, not 2.0) so a 2-mark MCQ still renders "2 / 2"
    exactly as it does today; otherwise a float on the half (1.5).

    `value` that is None, non-numeric, NaN or infinite yields `default` -- these come from a
    truncated or malformed grader reply, and 0 is the safe reading (the caller has already
    flagged the question for review).

    Clamping happens AFTER snapping, and to the raw `maximum`: if a key somehow carries a
    non-half maximum the result is that maximum, because never exceeding the question's ceiling
    outranks the granularity rule. `upload_validation` flags such a key at upload time so it is
    fixed at the source rather than papered over here.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(default)
    if not math.isfinite(v):
        v = float(default)
    if v < 0:
        v = 0.0

    # floor(v/step + 1/2) == round-half-UP; see the module docstring for why not round().
    snapped = math.floor(v / MARK_STEP + 0.5 + _EPS) * MARK_STEP

    if maximum is not None:
        try:
            hi = float(maximum)
        except (TypeError, ValueError):
            hi = None
        if hi is not None and math.isfinite(hi):
            hi = max(hi, 0.0)
            if snapped > hi:
                snapped = hi

    return int(snapped) if float(snapped).is_integer() else float(snapped)
