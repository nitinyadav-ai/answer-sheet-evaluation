"""Report-display + choice fixes (three reported issues). All offline / no network:

  Issue 1 -- code rendered as mangled math. Snake_case identifiers (push_element) must NOT become
             subscripts/KaTeX math, while genuine short subscripts (x_1, H_2O, a_i) still convert;
             untagged multi-line code is routed to a verbatim `code` segment.
  Issue 2 -- an 'answer any one' choice showed BOTH alternatives. merge_choice_groups now stores the
             per-alternative answers, and the report shows ONLY the alternative the student attempted
             (by the label they wrote), falling back to the full expected answer when ambiguous.
  Issue 3 -- OCR uncertainty markers ([ambiguous:]/[smudged:]/[illegible]) force a manual-review flag.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "answer-evaluator-and-report-generation", "scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit):  # pragma: no cover
    ev = None
try:
    import full_evaluator as fe
except (ImportError, SystemExit):  # pragma: no cover
    fe = None

pytestmark = pytest.mark.skipif(ev is None or fe is None, reason="evaluate/full_evaluator unavailable")


# ------------------------- Issue 1: code vs math -------------------------

def test_snake_case_identifiers_not_subscripted():
    assert ev.humanize_math("def push_element(L):") == "def push_element(L):"
    assert ev.humanize_math("def Pop_element(product):") == "def Pop_element(product):"
    assert ev.humanize_math("remove_first_last = emp_id") == "remove_first_last = emp_id"


def test_genuine_math_subscripts_still_convert():
    assert ev.humanize_math("x_1") == "x₁"          # x sub 1
    assert ev.humanize_math("a_i") == "aᵢ"          # a sub i (single-letter subscript kept)
    assert ev.humanize_math("H_2O") == "H₂O"        # chemistry subscript (letter_digit)


def test_web_wraps_math_but_not_identifiers():
    assert "\\(" not in ev.latexify_for_web("def push_element(L):")   # identifier -> plain text
    assert "\\(" in ev.latexify_for_web("x_1 + y^2")                  # real math -> KaTeX run


def test_autotag_routes_multiline_code_to_code_segment():
    code = "L = [1, 2]\ndef push_element(L):\n    for i in L:\n        product.append(i)"
    segs = ev.format_answer(code)
    codesegs = [s for s in segs if s["type"] == "code"]
    assert codesegs and "push_element" in codesegs[0]["content"]      # verbatim, underscore intact


def test_autotag_leaves_prose_and_equations_as_text():
    for prose in ("The velocity is v = u + at, so the object accelerates.",
                  "v = u + at\ns = ut + 0.5 a t^2"):                  # physics, not code
        assert all(s["type"] == "text" for s in ev.format_answer(prose))


def test_single_definitive_code_line_is_wrapped():
    segs = ev.format_answer("SELECT department, COUNT(*) FROM employees GROUP BY department;")
    assert any(s["type"] == "code" for s in segs)


# ---- Issue 1c: a whole code answer renders as ONE uniform code block (no plain-text / code split) ----
# Reported: a DB-connection answer showed its import + assignment lines as PLAIN TEXT while only the
# method-call lines got a code box. `_is_code_line` now also recognises imports (case-insensitively)
# and string / dotted-method-call assignments, and a predominantly-code answer coheres into one block.

_MYSQL_ANSWER = (
    "Import mysql.connector as ms\n"
    "connection = ms.connect(host='localhost', user='admin_user', password='warehouse2024', database='warehouseDB')\n"
    "cursor = connection.cursor()\n"
    'update_query = "UPDATE product_inventory SET Quantity = 91 WHERE Item_code=268"\n'
    "cursor.execute(update_query)\n"
    "connection.commit()\n"
    'print("Data updated successfully.")\n'
    "cursor.close()\n"
    "connection.close()"
)


def test_full_code_answer_is_one_code_segment():
    segs = ev.format_answer(_MYSQL_ANSWER)
    assert [s["type"] for s in segs] == ["code"]                 # not ["text", "code"] (the reported split)
    assert "Import mysql.connector" in segs[0]["content"]        # the import line is INSIDE the code box
    assert "connection.close()" in segs[0]["content"]


def test_is_code_line_detects_import_and_call_string_assignments():
    assert ev._is_code_line("Import mysql.connector as ms")      # capitalised import
    assert ev._is_code_line("from os import path")
    assert ev._is_code_line("cursor = connection.cursor()")      # dotted-method-call assignment
    assert ev._is_code_line('q = "SELECT * FROM t"')             # string assignment


def test_math_and_prose_never_flagged_as_code():
    assert ev._is_code_line("v = u + at") is False               # physics equation
    assert ev._is_code_line("y = sin(x)") is False               # a BARE call (no dot) -> math, not code
    assert ev._is_code_line("Area = pi * r^2") is False
    assert all(s["type"] == "text" for s in ev.format_answer("The result v = u + at is derived above."))


def test_predominantly_code_coheres_but_keeps_prose_preamble():
    # An intro sentence + a block of code: the code coheres into ONE block; the sentence stays text.
    ans = "My program:\nimport sys\nx = obj.run()\ncursor.execute(q)\nprint(x)"
    segs = ev.format_answer(ans)
    assert [s["type"] for s in segs] == ["text", "code"]         # preamble text + one code block
    assert "import sys" in segs[1]["content"] and "print(x)" in segs[1]["content"]


# ---- Issue 1d: LEADING PART/OPTION LABELS defeated code detection (SQL answers shown as plain text) ----
# Across multiple CS sheets, labeled code -- `A. def f()`, `1. import csv`, `II. Select ...`, `IV. A. Select`
# -- fell through to plain text (the `^`-anchored regexes never saw the code) while whole SQL answers
# rendered as prose. `_is_code_line` now peels leading labels before matching; a lone label is neutral.

def test_labels_stripped_so_labeled_code_is_detected():
    assert ev._is_code_line("A. def remove_element(l, n):")
    assert ev._is_code_line("1. import csv")
    assert ev._is_code_line("II. Select name from t")
    assert ev._is_code_line("IV. A. Select * from Hotels;")     # nested labels
    assert ev._is_code_line("(a) print(x)")
    assert ev._is_code_line("display_line()")                    # standalone snake_case call


def test_lone_label_is_neutral_not_a_code_break():
    assert ev._is_code_line("1.") is None
    assert ev._is_code_line("A.") is None
    assert ev._is_code_line("(a)") is None
    assert ev._is_code_line("IV.") is None


def test_labeled_math_and_prose_still_never_code():
    assert ev._is_code_line("1. v = u + at") is False
    assert ev._is_code_line("2. Encapsulation binds data and methods") is False
    assert ev._is_code_line("A. the answer is 5") is False


_LABELED_SQL = (
    'I. Select customer_name from Hotels, Bookings where Hotels.H_ID = Bookings.H_ID and city = "Delhi";\n\n'
    'II. Select Bookings.* from Hotels, Bookings where Hotels.H_ID = Bookings.H_ID and city in ("Mumbai");\n\n'
    "III. Delete from Bookings where check_in < '2014-12-03'\n\n"
    "IV. A. Select * from Hotels, Bookings;"
)


def test_labeled_sql_answer_is_one_code_block():
    segs = ev.format_answer(_LABELED_SQL)
    assert [s["type"] for s in segs] == ["code"]                 # all four labeled queries -> ONE block
    assert "Delete from Bookings" in segs[0]["content"]


def test_labeled_python_answer_coheres_to_code():
    ans = "A. def remove_element(l, n):\n    l.remove(n)\n    return l\nprint(remove_element([1, 2], 2))"
    segs = ev.format_answer(ans)
    assert [s["type"] for s in segs] == ["code"]
    assert "def remove_element" in segs[0]["content"]


def test_lone_label_header_kept_but_code_stays_one_block():
    # A bare `1.` part label above a code block: the label is a text header; the code is ONE uniform box
    # (not the reported plain-text/code split).
    segs = ev.format_answer("1.\nimport pickle\ndef f():\n    return 1")
    assert [s["type"] for s in segs] == ["text", "code"]
    assert segs[0]["content"].strip() == "1."
    assert "import pickle" in segs[1]["content"] and "def f" in segs[1]["content"]


# --------------- Issue 1b: code / program-output answers render VERBATIM (no ^ -> superscript) ------
# A "predict the output" answer is a literal program-output string; the '^' the program prints must
# survive to the report, never be humanized into a superscript. The bug: 'A. QP^-14' rendered 'QP⁻14'.

def test_code_output_question_detected():
    assert ev._is_code_output_question("A. Predict the output of the following Python code:\nprint('x')")
    assert ev._is_code_output_question("What will be the output of the program given below?")
    assert ev._is_code_output_question("Write the output produced by the code segment below.")
    # embedded code + the word 'output' (no canonical phrase) still counts
    assert ev._is_code_output_question("Consider the code:\ndef f(x):\n    return x\nGive its output.")


def test_non_code_questions_not_flagged():
    assert not ev._is_code_output_question("")
    assert not ev._is_code_output_question("Write the formula for kinetic energy and evaluate x^2 + 1.")
    assert not ev._is_code_output_question("Explain the working of a transformer with a diagram.")
    # mentions 'output' but no code and no output-prediction phrasing (e.g. a hardware Q)
    assert not ev._is_code_output_question("Name one output device of a computer.")


def test_verbatim_preserves_caret_literally():
    for raw in ("A. QP^-14", "A. QP-^14"):                 # student OCR + answer-key literals
        segs = ev.format_answer(raw, verbatim=True)
        assert segs == [{"type": "code", "content": raw}]  # single verbatim code segment, '^' intact
        assert "web" not in segs[0]                         # no KaTeX run -> browser can't superscript it
        assert "⁻" not in ev._segments_to_text(segs) and "^" in ev._segments_to_text(segs)


def test_verbatim_off_still_humanizes_real_math():
    # The default path is unchanged: genuine math still converts to Unicode superscripts.
    assert ev.format_answer("x^2", verbatim=False)[0]["content"] == "x²"
    assert "⁻" in ev.format_answer("A. QP^-14", verbatim=False)[0]["content"]  # the old (non-verbatim) behaviour


def test_verbatim_keeps_diagram_and_code_tags():
    segs = ev.format_answer("output:\n[CODE: print(2**3)]\n[DIAGRAM: a tree]", verbatim=True)
    kinds = [s["type"] for s in segs]
    assert "code" in kinds and "diagram" in kinds        # tags still split out; no '**' humanization
    assert any("2**3" in s.get("content", "") for s in segs)


# ------------------------- Issue 2: choice shows only the attempted part -------------------------

def test_choice_label_is_last_paren_token():
    assert fe._choice_label("Q31(A)") == "A"
    assert fe._choice_label("Q34(IV)(A)") == "A"
    assert fe._choice_label("Q28(b)") == "b"


def _run_choice_merge(tmp_path, manual_db, groups, student):
    ocr_path = os.path.join(str(tmp_path), "ocr.json")
    db_path = os.path.join(str(tmp_path), "db.json")
    bases = sorted({fe._base_qnum(fe.normalize_qid(k)) for k in manual_db}, key=lambda x: int(x))
    json.dump({f"Q{b}": {"answer": student} for b in bases}, open(ocr_path, "w"))
    json.dump(dict(manual_db), open(db_path, "w"))
    fe.merge_choice_groups(ocr_path, db_path, manual_db, groups)
    return json.load(open(db_path))


def test_merge_stores_choice_alternatives(tmp_path):
    manual_db = {"Q31(A)": {"marks": 3, "answer": "QP-1 4"}, "Q31(B)": {"marks": 3, "answer": "['K','R']"}}
    db = _run_choice_merge(tmp_path, manual_db, [{"members": ["Q31(A)", "Q31(B)"]}], "A. QP-1 4")
    entry = db["Q31"]
    assert entry["is_choice"] is True and entry["marks"] == 3          # counted once
    labels = [a["label"] for a in entry["choice_alternatives"]]
    assert labels == ["A", "B"] and len(entry["choice_alternatives"]) == 2


def test_attempted_choice_selects_written_label():
    entry = {"choice_alternatives": [{"label": "A", "answer": "ansA"}, {"label": "B", "answer": "ansB"}],
             "choice_shared": ""}
    assert ev._attempted_choice_answer("A. something", entry) == "ansA"
    assert ev._attempted_choice_answer("(B) other", entry) == "ansB"


def test_attempted_choice_ambiguous_shows_all():
    entry = {"choice_alternatives": [{"label": "A", "answer": "ansA"}, {"label": "B", "answer": "ansB"}],
             "choice_shared": ""}
    assert ev._attempted_choice_answer("no label here", entry) is None      # none -> show all
    assert ev._attempted_choice_answer("A. x\nB. y", entry) is None         # both attempted -> show all


def test_attempted_choice_keeps_shared_parts():
    entry = {"choice_alternatives": [{"label": "A", "answer": "altA"}, {"label": "B", "answer": "altB"}],
             "choice_shared": "(i) shared part"}
    assert ev._attempted_choice_answer("A.", entry) == "(i) shared part\naltA"


# ------------------------- Issue 3: OCR ambiguity -> review -------------------------

def test_ocr_ambiguity_markers_detected():
    assert ev._OCR_AMBIG_RE.search("output is [ambiguous: Q/8] then P")
    assert ev._OCR_AMBIG_RE.search("value [smudged: 5]")
    assert ev._OCR_AMBIG_RE.search("word [illegible] here")
    assert not ev._OCR_AMBIG_RE.search("a clean confident answer")
