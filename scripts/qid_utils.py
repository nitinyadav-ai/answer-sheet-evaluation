"""Single source of truth for question-ID / answer-label parsing.

The pipeline used to extract a question number three different, inconsistent ways:
  - run_ocr.assemble_answers via `_leading_int` -> re.match(r'\\s*0*(\\d+)', tag)  [needs a LEADING digit]
  - full_evaluator.normalize_qid                -> first digit ANYWHERE
  - full_evaluator._base_qnum                   -> needs a literal 'Q'
The first one silently DROPPED any prefixed [START_Q] tag ("Q6", "A6", "Ans 6", "Ques 6") -> the
question was welded into the previous one and rendered BLANK (audit issue E1/E6).

This module unifies extraction and is PREFIX-TOLERANT. Critical design property (proven in
tests/test_qid_utils.py): `base_qnum` is BYTE-IDENTICAL to the old `_leading_int` on every input
that did NOT start with a label prefix (bare digits, sub-parts, roman-only, empty); it only ADDS a
correct result for the previously-broken prefixed tags. Likewise `canonical_qid` reproduces the old
`normalize_qid` on every working input and additionally cleans leading zeros and stray wrapping
punctuation. So the change can only fix drops -- never move a currently-correct capture.
"""
import re

# A leading answer/question LABEL word that may precede a question number. Stripped ONLY when a digit
# eventually follows (optionally across one separator/space), so a lone sub-part letter ("a") or a
# roman numeral ("ii") is NEVER eaten. Modeled on evaluate.py:_LABEL_PREFIX_RE.
_LABEL = re.compile(
    r'^\s*(?:q(?:ue|ues|uestion)?|ans(?:wer)?|sol(?:n|ution)?|s|a)\s*[\.\-:]?\s*(?=\d)',
    re.IGNORECASE,
)
# Same shape as the old run_ocr._leading_int: optional spaces, skip leading zeros, capture the run.
_LEADING_NUM = re.compile(r'\s*0*(\d+)')


def _strip_label(raw):
    """Drop a subject prefix ('AI10_') then a leading label word ('Q'/'Ans'/...) when a digit follows.
    Does NOT touch brackets -- so base_qnum stays byte-identical to the old _leading_int on '(1)'."""
    s = str(raw).split("_", 1)[-1].strip()
    return _LABEL.sub("", s, count=1)


def base_qnum(raw):
    """Leading question number, tolerant of a label prefix. Returns int or None.

    Byte-identical to the old run_ocr._leading_int on every non-prefixed tag; additionally resolves
    'Q6'/'A6'/'Ans 6'/'Ques 6' -> 6 (the E1 fix). 'ii'/'a'/''/'(1)' -> None, exactly as before."""
    if raw is None:
        return None
    m = _LEADING_NUM.match(_strip_label(raw))
    return int(m.group(1)) if m else None


def has_subpart(raw):
    """True when, after stripping a label prefix, the tag is NOT a pure bare number. This is BYTE-
    IDENTICAL to the old run_ocr `has_suffix` test (`re.fullmatch(r'\\s*0*\\d+\\s*', tag) is None`) on
    every non-prefixed tag -- so the weld decision is unchanged -- and is correctly False for a
    prefixed bare number like 'Q6' (was wrongly True before). Used ONLY for the weld decision."""
    return base_qnum(raw) is not None and re.fullmatch(r'\s*0*\d+\s*', _strip_label(raw)) is None


def subpart_of(raw):
    """The GENUINE sub-part suffix that follows the base number ('6.a'->'.a', 'Q6(ii)'->'(ii)',
    '6'->''), or '' when there is none. Bare trailing punctuation (')', ']', '.') is NOT a sub-part.
    Used to build a clean canonical db_key/key (so '1)'-> 'Q1', not 'Q1)')."""
    s = _strip_label(raw)
    m = _LEADING_NUM.match(s)
    if not m:
        return ""
    sub = s[m.end():].strip()
    return sub if re.search(r'[A-Za-z0-9]', sub) else ""


def canonical_qid(raw):
    """Canonical 'Q<base><subpart>' key. Replaces full_evaluator.normalize_qid: identical result on
    every working input, and additionally cleans leading zeros ('01'->'Q1') and stray wrapping
    punctuation ('(1)'/'1)'/'1.'->'Q1'), while preserving genuine sub-parts ('Q1(a)','Q5.b'). A
    string with no digit (e.g. '_instructions_', 'Section A') is returned unchanged, as before."""
    if raw == "_instructions_":
        return raw
    s = _strip_label(raw).lstrip("([{ \t")
    m = re.search(r'(\d+)', s)
    if not m:
        return raw
    base = int(m.group(1))                  # int() drops leading zeros
    sub = s[m.end():].strip()
    sub = sub if re.search(r'[A-Za-z0-9]', sub) else ""
    return f"Q{base}{sub}"
