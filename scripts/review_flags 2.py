"""Single source of truth for WHY a question was flagged for review.

A flagged question used to say only *that* it needs review: the report's top banner listed bare
question numbers, and the per-question badge's tooltip read "Needs your review". The reasons were
already in the data and simply never rendered -- `Capture Status` is set on 73 questions across the
archived runs (every "No answer captured" case among them) and appears ZERO times in index.html.

This module turns a graded result into an ordered, de-duplicated list of reasons, and groups those
reasons across a whole paper for the summary at the top of the report. It is the ONE definition used
by the web report, the PDF, and the read-time backfill for runs graded before it existed.

Where reasons live
------------------
Most reasons sit in a dedicated field (`Capture Status`, `Mixed Answer Warning`, ...). A second
family -- answer-key/question-paper integrity notes and the OCR ambiguous-symbol note -- was only
ever appended into `Justification` prose wrapped in square brackets. New runs also record those in a
proper `Review Notes` list; archived runs are recovered by scanning the justification.

That scan classifies by DISTINCTIVE MACHINE-GENERATED PHRASE, never by "it was in brackets".
Measured on the archived corpus, a bare `\\[...\\]` regex also matches student content -- e.g.
`[10, 20, 10, 30, 10, 20, 30]` and `[(1-tan2x)/(1+tan2x)]` -- which would present a student's own
working to the teacher as a system warning. Anything that doesn't match a known phrase is ignored.

De-duplication is by `code`, first occurrence winning, with fields processed before notes. That
matters: all 41 archived mixed-answer cases carry BOTH the `Mixed Answer Warning` field and a
`[Mixed-answer check: ...]` note, so without it every one of them would report the reason twice.
"""

import re

DANGER, WARNING, INFO = "danger", "warning", "info"

_SEVERITY_ORDER = {DANGER: 0, WARNING: 1, INFO: 2}

# Rank within a severity -- the order a teacher most likely wants to act in.
_CODE_ORDER = (
    "injection",
    "no_answer_captured", "misplaced_answer", "question_number_misread", "not_in_paper", "key_integrity",
    "illegible", "ocr_symbol_uncertain", "ocr_ambiguous", "mixed_answer", "boundary", "orientation",
    "marks_capped", "optional_status", "uncertain_choice", "incomplete_reply",
    "recovered", "recovered_from", "rehomed", "grading_spread", "low_confidence",
    "needs_review",
)
_CODE_RANK = {c: i for i, c in enumerate(_CODE_ORDER)}

# Short label for a badge / summary group heading.
LABELS = {
    "injection":          "Possible attempt to influence the grader",
    "no_answer_captured": "No answer captured",
    "misplaced_answer":   "Possible misplaced answer",
    "not_in_paper":       "Not in the question paper",
    "key_integrity":      "Answer key and question paper disagree",
    "illegible":          "Illegible handwriting",
    "ocr_symbol_uncertain": "Symbols may be misread",
    "question_number_misread": "Question number may be misread",
    "ocr_ambiguous":      "Unclear symbol in the handwriting",
    "mixed_answer":       "Answer may merge two questions",
    "boundary":           "May continue the previous answer",
    "orientation":        "Page orientation uncertain",
    "marks_capped":       "Awarded above the maximum; capped",
    "optional_status":    "Optional-question check failed",
    "uncertain_choice":   "Possible internal choice",
    "incomplete_reply":   "The grader's reply was cut off",
    "recovered":          "Answer recovered by the system",
    "recovered_from":     "Recovered from another question's slot",
    "rehomed":            "Matched to another question",
    "grading_spread":     "Graders disagreed on the mark",
    "low_confidence":     "The AI was unsure",
    "needs_review":       "Flagged for review",
}

SEVERITIES = {
    "injection": DANGER,
    "recovered": INFO, "recovered_from": INFO, "rehomed": INFO,
    "grading_spread": INFO, "low_confidence": INFO,
}   # everything else is WARNING (see _severity)

# Fallback wording for a flag whose source field carries no sentence of its own.
_DEFAULT_DETAIL = {
    "illegible":        ("The handwriting was hard to read, so some of this answer may have been "
                         "transcribed wrongly or missed. Check it against the sheet."),
    "uncertain_choice": ("This looks like an 'answer any one' question, but the structure could not be "
                         "confirmed. Check that the right alternative was graded."),
    "incomplete_reply": ("The grader's reply was cut off and the mark was recovered from the partial "
                         "response. Confirm the mark is right."),
    "low_confidence":   "The AI reported low confidence in this grade.",
    "needs_review":     ("This answer was flagged for review, but no specific reason was recorded. "
                         "Check it against the sheet."),
    "injection":        ("This answer contains text that looks like an attempt to influence the AI "
                         "grader. The instruction was ignored and only the academic content graded."),
}

# Distinctive phrases from notes our OWN code generates (full_evaluator.reconcile_marks_with_question_paper
# and evaluate.py's OCR-ambiguity pass). Substring matches, so the surrounding sentence may vary; each
# phrase is specific enough that no student answer would contain it.
_NOTE_RULES = (
    ("not_in_paper",  "is not present in the question paper"),
    ("key_integrity", "was MISSING from the answer key"),
    ("key_integrity", "the key parse likely dropped one or more parts"),
    ("key_integrity", "but the paper marks it"),
    ("key_integrity", "but the question paper marks it"),
    ("key_integrity", "a part may be missing; verify"),
    ("ocr_ambiguous", "OCR flagged an ambiguous"),
    ("mixed_answer",  "Mixed-answer check:"),
    ("boundary",      "Boundary check:"),
)

# Bracketed spans in a justification are only CANDIDATES; each must still match a phrase above.
_BRACKETED = re.compile(r'\[([^\[\]]+)\]')


def _severity(code):
    return SEVERITIES.get(code, WARNING)


def _rank(code):
    """Total sort order: severity first, then the code's rank within it."""
    return (_SEVERITY_ORDER[_severity(code)] * 1000
            + _CODE_RANK.get(code, len(_CODE_ORDER)))


def _clean(value):
    return str(value or "").strip()


def _is_yes(value):
    return _clean(value).upper().startswith("Y")


def _is_true(value):
    return value is True or _clean(value).lower() == "true"


def classify_note(text):
    """Flag code for one of our generated notes, or None when the text isn't one.

    None is the important case: it is what stops a student's own bracketed working from being shown
    to the teacher as a system warning.
    """
    t = _clean(text)
    for code, phrase in _NOTE_RULES:
        if phrase in t:
            return code
    return None


def _notes_of(res):
    """Every generated note attached to a result: the structured `Review Notes` list written by new
    runs, plus bracketed spans recovered from `Justification` for runs graded before that existed."""
    out = []
    raw = res.get("Review Notes")
    if isinstance(raw, (list, tuple)):
        out.extend(_clean(n) for n in raw if _clean(n))
    elif _clean(raw):
        out.append(_clean(raw))
    for span in _BRACKETED.findall(str(res.get("Justification", "") or "")):
        span = _clean(span)
        if span and span not in out:
            out.append(span)
    return out


def derive_flags(res):
    """Ordered, de-duplicated reasons for a single graded result.

    Returns [{code, label, detail, severity}]. Empty when nothing is wrong -- an answer that graded
    cleanly gains no UI at all. `detail` is always a non-empty sentence, so a flag is never wordless.
    """
    if not isinstance(res, dict):
        return []

    found = {}                      # code -> detail (first non-empty wins)

    def add(code, detail=""):
        detail = _clean(detail) or _DEFAULT_DETAIL.get(code, "")
        if code not in found or (not found[code] and detail):
            found[code] = detail

    # --- dedicated fields (processed FIRST so they win de-duplication against notes) -------------
    if _is_yes(res.get("Prompt Injection Detected")):
        warn = _clean(res.get("Injection Warning"))
        add("injection", (f"{_DEFAULT_DETAIL['injection']} It wrote: “{warn}”"
                          if warn else _DEFAULT_DETAIL["injection"]))

    capture = _clean(res.get("Capture Status"))
    if capture:
        low = capture.lower()
        if low.startswith("no answer captured"):
            add("no_answer_captured",
                "Nothing was captured for this question. It may be blank, or its page may be "
                "missing or unreadable in the scan — check the sheet before accepting a zero.")
        elif "misplaced" in low:
            add("misplaced_answer", capture)
        # "Not Attempted (Optional)" is a normal outcome, not a flag -> deliberately ignored.

    if _is_yes(res.get("Off-Topic (Yes/No)")):
        add("misplaced_answer",
            "What was captured here does not appear to answer this question — most likely the "
            "scan assigned it to the wrong question. Check the sheet.")

    if _is_true(res.get("Bad Handwriting Flag")):
        add("illegible")

    # An OCR finding that is NOT about legibility: the two passes disagree on symbols and could not be
    # reconciled, or the question NUMBER itself looks misread. Both used to be delivered by setting
    # `is_bad_handwriting`, which reported them to the teacher as "Illegible handwriting".
    symbol_note = _clean(res.get("OCR Symbol Warning"))
    if symbol_note:
        add("question_number_misread" if "not one of the exam's question numbers" in symbol_note
            else "ocr_symbol_uncertain", symbol_note)

    add_if = (
        ("mixed_answer",    res.get("Mixed Answer Warning")),
        ("recovered",       res.get("Recovery Warning")),
        ("boundary",        res.get("Boundary Warning")),
        ("orientation",     res.get("Orientation Warning")),
        ("marks_capped",    res.get("Calculation Warning")),
        ("grading_spread",  res.get("Grading Spread")),
    )
    for code, value in add_if:
        if _clean(value):
            add(code, value)

    opt = _clean(res.get("Optional Status"))
    if opt and "sanity check failed" in opt.lower():
        add("optional_status", opt)

    rec_from = _clean(res.get("Recovered From"))
    if rec_from:
        add("recovered_from",
            f"The question number was misread, so this answer was recovered here from the "
            f"{rec_from} slot.")

    rehomed = res.get("Rehomed To")
    rehomed = [_clean(r) for r in rehomed if _clean(r)] if isinstance(rehomed, (list, tuple)) \
        else ([_clean(rehomed)] if _clean(rehomed) else [])
    if rehomed:
        add("rehomed",
            f"This looks like an answer to {', '.join(rehomed)}; it was matched there and graded on "
            f"that question.")

    # --- generated notes (key-integrity / OCR ambiguity), classified by phrase ------------------
    for note in _notes_of(res):
        code = classify_note(note)
        if code:
            add(code, note)

    # --- structural / confidence signals --------------------------------------------------------
    if _is_true(res.get("Choice Uncertain")):
        add("uncertain_choice")
    if _is_true(res.get("Incomplete Grader Reply")):
        add("incomplete_reply")
    # Low confidence is only worth SAYING when nothing else explains the flag. Measured on the
    # archived corpus it co-occurs with a real reason 61 times out of 62 -- listing it every time
    # would pad each question and each summary group with a line that adds nothing. The one case
    # where it stands alone still gets a reason instead of a wordless badge.
    if _clean(res.get("Confidence (Low/Medium/High)")).lower() == "low" \
            and not any(_severity(c) != INFO for c in found):
        add("low_confidence")

    # --- last resort: a flagged question must never show a wordless badge -----------------------
    if not found and _is_yes(res.get("Needs Review (Yes/No)")):
        add("needs_review")

    # `rank` is the sort key, carried on the flag itself so the browser can order and group these
    # without re-deciding what outranks what -- the ordering policy lives here and nowhere else.
    flags = [{"code": c, "label": LABELS.get(c, c.replace("_", " ").capitalize()),
              "detail": d, "severity": _severity(c), "rank": _rank(c)} for c, d in found.items()]
    flags.sort(key=lambda f: f["rank"])
    return flags


def needs_attention(res):
    """True when a result belongs in the report's top summary: the grader asked for review, or an
    injection was detected (which always warrants a look, whatever the review flag says)."""
    if not isinstance(res, dict):
        return False
    return _is_yes(res.get("Needs Review (Yes/No)")) or _is_yes(res.get("Prompt Injection Detected"))


def summarise_flags(evaluations):
    """Group the reasons across one paper for the summary at the top of the report.

    Returns [{code, label, severity, detail, qids}] ordered by severity then code rank, where `qids`
    keeps the paper's question order. Only questions that `needs_attention` contribute, so an
    informational badge on an otherwise clean answer doesn't pad the summary.

    `detail` is the first detail seen for that code -- useful for a one-line group explanation; the
    per-question blocks carry each question's own wording.
    """
    groups = {}
    for item in (evaluations or []):
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        qid, res = str(item[0]), item[1]
        if not needs_attention(res):
            continue
        for f in (res.get("Review Flags") or derive_flags(res)):
            if not isinstance(f, dict) or not f.get("code"):
                continue
            g = groups.setdefault(f["code"], {
                "code": f["code"],
                "label": f.get("label") or LABELS.get(f["code"], f["code"]),
                "severity": f.get("severity") or _severity(f["code"]),
                "rank": f.get("rank", _rank(f["code"])),
                "detail": _clean(f.get("detail")),
                "qids": [],
            })
            if not g["detail"]:
                g["detail"] = _clean(f.get("detail"))
            if qid not in g["qids"]:
                g["qids"].append(qid)

    out = list(groups.values())
    out.sort(key=lambda g: g["rank"])
    return out


def attach_flags(evaluations, overwrite=False):
    """Stamp `Review Flags` onto each result in an `[[qid, res], ...]` list, in place.

    Idempotent, and by default it does NOT overwrite flags already present -- so the read-time
    backfill enriches a run graded before this existed without disturbing a fresh one.
    """
    for item in (evaluations or []):
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        res = item[1]
        if overwrite or not isinstance(res.get("Review Flags"), list):
            res["Review Flags"] = derive_flags(res)
    return evaluations
