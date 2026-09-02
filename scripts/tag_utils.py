"""Balanced [CODE:]/[DIAGRAM:] span scanning, shared between the OCR reconcile pass
(skills/vision-ocr/scripts/run_ocr.py) and the report renderer (evaluate._split_on_code).

A depth counter is what makes code survive intact: `[CODE: return arr[i] - 1]` has an inner ']' that a
naive `find("]")` would stop at. run_ocr needs the SAME segmentation the renderer later re-parses, so a
symbol correction spliced here can never desync from how grading reads it. `tag_spans` is the single
source of truth; a parity test (tests/test_ocr_reconcile.py) pins it to evaluate._split_on_code.
"""
import re

# Opening marker for a balanced tag. Only CODE/DIAGRAM carry internal brackets, so only they need the
# depth scan; every other OCR tag ([STRIKETHROUGH:], [BOXED:], [ambiguous:], [illegible], ...) is a flat
# single-bracket span handled by callers with a plain regex.
TAG_OPEN_RE = re.compile(r"\[(CODE|DIAGRAM):")


def tag_spans(s):
    """Ordered balanced CODE/DIAGRAM spans in `s`.

    Returns a list of dicts {name, start, end, inner, terminated}:
      • name       -- "CODE" or "DIAGRAM"
      • start      -- index of the opening '['
      • end        -- index AFTER the matching ']' (or len(s) when never balanced)
      • inner      -- raw content between the marker's ':' and the closing ']' (NOT stripped, so a
                      caller can splice the exact original span)
      • terminated -- False when the brackets never balanced (the scan ran to end-of-string). An
                      unterminated span is unsafe to splice (it would swallow trailing prose).

    Mirrors the depth-counted scan in evaluate._split_on_code so the two stay byte-compatible.
    """
    spans, i, n = [], 0, len(s)
    while i < n:
        m = TAG_OPEN_RE.search(s, i)
        if not m:
            break
        start = m.start()
        depth, j = 0, start
        while j < n:
            if s[j] == "[":
                depth += 1
            elif s[j] == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        terminated = j < n
        inner = s[m.end():j] if terminated else s[m.end():]
        end = j + 1 if terminated else n
        spans.append({"name": m.group(1), "start": start, "end": end,
                      "inner": inner, "terminated": terminated})
        i = end
    return spans


def brackets_balanced(s):
    """True when every '[' in `s` closes and no ']' precedes its '['. Used to reject an arbiter code
    correction that would produce an unbalanced [CODE: ...] span (which would desync _split_on_code)."""
    depth = 0
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
