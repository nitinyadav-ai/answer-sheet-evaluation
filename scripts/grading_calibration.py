"""Single source of truth for the grading-calibration switch.

Why this module exists
----------------------
The grader was measurably over-strict: 18.7% of ATTEMPTED answers across the archived corpus
scored 0, including answers carrying a complete and correct method. Four independent defects
caused it, and the fixes for them are spread over three files:

  * `evaluate.py`            -- the rubric the grader reads, and the off-topic / cascade gates
  * `evaluate_diagrams.py`   -- per-feature partial credit for a drawing
  * both                     -- whether a diagram verdict may overwrite a correct written answer

`EVAL_GRADING_CALIBRATION` turns all of them on or off together, so a run is never a mixture of
old and new marking behaviour:

  v2      (default) CBSE-accurate partial credit -- directives-based rubrics, per-feature diagram
                    credit, off-topic requires disagreement with the ANSWER KEY, 1-mark zeros get
                    a thinking re-check.
  legacy            the previous behaviour: head-truncated rubric, diagram verdict wins outright,
                    off-topic zeroes on question mismatch alone, 2-mark cascade threshold.

The predicate lives here rather than being re-parsed in each file because two hand-rolled copies of
`os.environ.get(...) != "legacy"` are exactly how the two halves of a switch drift apart -- and a
half-applied calibration is worse than either setting, since no report would be explainable.

What `legacy` does NOT restore
------------------------------
The rubric FILES also had self-contradictory rules -- `subjective_rubric.md` said both "No negative
marking. Never deduct marks" (Golden Rule 4) and "must incur a 1/2 mark deduction per question"
(Rule 9), and its 2-mark procedure zeroed a "present but lacks depth" value point that its own
4-mark procedure awarded half credit for. Those corrections are permanent: the old text was not a
stricter policy, it was two policies at once, and reverting to it would restore an ambiguity rather
than a behaviour. `legacy` reverts the code paths and the rubric-DELIVERY mechanism.
"""

import os

CALIBRATION_ENV = "EVAL_GRADING_CALIBRATION"
LEGACY = "legacy"
V2 = "v2"


def calibration():
    """The active calibration name, lowercased and stripped. Unknown values are treated as v2 --
    the default has to be the safe one, and a typo ('V2 ', 'new') must not silently resurrect the
    over-strict path."""
    raw = str(os.environ.get(CALIBRATION_ENV, V2)).strip().lower()
    return LEGACY if raw == LEGACY else V2


def is_v2():
    """True when the CBSE-accurate partial-credit calibration is active (the default)."""
    return calibration() == V2
