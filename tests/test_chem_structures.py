"""2-D chemical structures must render as ONE aligned monospace block.

Reported symptom: a hand-drawn structural formula came out shredded —

      H   H   H   H          the indented atom/bond rows match _INDENT_CODE_RE ("^ {4,}\\S"), so each
      |   |   |   |          PAIR was boxed as CODE, while the backbone starts at column 0, matches
H - C - C - C = C - H        no code pattern, and rendered as proportional prose. One structure became
                             three differently-styled pieces and the bonds stopped meeting their atoms.

Two things are pinned here: the block is detected and kept whole, and its COLUMNS are right. The
second matters because OCR transcribes row by row and drifts — on the real Q34 every satellite row
sat +2 columns right, so each bond pointed at a bond instead of at its carbon.

The sharp edge is the negatives. A first cut keyed on "contains |, / or \\" and swept over the 544
archived answers it claimed 8 blocks of ordinary maths working (`S₃₀ = 30/2 [2(1000) + 29(100)]` — `S`
is an element symbol and `/` is division), which would have been boxed as monospace AND diverted
around the maths renderer.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit):                                   # pragma: no cover
    ev = None

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate unavailable")


def _kinds(text):
    return [s["type"] for s in ev.format_answer(text)]


def _structures(text):
    return [s["content"] for s in ev.format_answer(text) if s["type"] == "structure"]


# --- the shapes students actually draw -----------------------------------------------------------

BUT_1_ENE = ("      H   H   H   H\n"
             "      |   |   |   |\n"
             "H - C - C - C = C - H\n"
             "      |   |\n"
             "      H   H")

ETHANOIC = ("    H     O\n"
            "    |     ||\n"
            "H - C  -  C - O - H\n"
            "    |\n"
            "    H")

BENZENE = ("     C\n"
           "   //  \\\n"
           "  C     C\n"
           "  |     ||\n"
           "  C     C\n"
           "   \\  //\n"
           "     C")

AMMONIA = ("    H\n"
           "    |\n"
           "H - N - H")

CHLORO = ("      H   H\n"
          "      |   |\n"
          "H - C - C - Cl\n"
          "      |   |\n"
          "      H   H")


@pytest.mark.parametrize("name,block", [
    ("but-1-ene", BUT_1_ENE), ("ethanoic acid", ETHANOIC), ("benzene ring", BENZENE),
    ("ammonia", AMMONIA), ("chloroethane (2-letter symbol)", CHLORO),
])
def test_structure_is_one_whole_block(name, block):
    got = _structures(block)
    assert len(got) == 1, f"{name}: expected one block, got {len(got)}"
    assert got[0].count("\n") == block.count("\n"), f"{name}: lost a line"


@pytest.mark.parametrize("block", [BUT_1_ENE, ETHANOIC, BENZENE, AMMONIA, CHLORO])
def test_structure_never_becomes_code_or_prose(block):
    assert "code" not in _kinds(block)
    assert _kinds(block) == ["structure"]


def test_every_character_survives():
    """Display-only: the renderer may re-indent a row, but it may never add, drop or change a glyph."""
    out = _structures(BUT_1_ENE)[0]
    assert out.replace(" ", "") == BUT_1_ENE.replace(" ", "")


def test_the_reported_case_end_to_end():
    """The actual answer from the report the user flagged."""
    path = os.path.join(ROOT, "output/Science_Class_X/ocr_output/ocr_answers.json")
    if not os.path.exists(path):
        pytest.skip("archived run not present")
    answer = json.load(open(path))["Q34"]["answer"]
    segs = ev.format_answer(answer)
    structures = [s["content"] for s in segs if s["type"] == "structure"]
    assert len(structures) == 2                       # But-1-ene and But-2-ene, each whole
    for block in structures:
        assert len(block.split("\n")) == 5
    assert "code" not in [s["type"] for s in segs]    # nothing boxed as code any more


# --- column alignment ----------------------------------------------------------------------------

def _misaligned(block):
    """Satellite glyphs that do NOT sit on one of the backbone's atom columns."""
    lines = block.split("\n")
    back = max(lines, key=lambda l: len(ev._atom_columns(l)))
    anchors = set(ev._atom_columns(back))
    return [(i, k) for i, l in enumerate(lines) if l is not back
            for k, ch in enumerate(l) if not ch.isspace() and k not in anchors]


def test_bonds_are_snapped_onto_their_atoms():
    """OCR drifts row-by-row; on the real data every satellite row sat +2 columns right, so each
    vertical bond pointed at a bond rather than at its carbon."""
    assert _misaligned(BUT_1_ENE), "fixture should start misaligned, else this proves nothing"
    assert _misaligned(_structures(BUT_1_ENE)[0]) == []


def test_an_already_aligned_structure_is_left_exactly_alone():
    aligned = ("    H   H\n"
               "    |   |\n"
               "H - C - C - H\n"
               "    |   |\n"
               "    H   H")
    assert _structures(aligned)[0] == aligned


def test_a_ring_is_not_snapped():
    """A ring has no single backbone — guessing one would move atoms the student placed correctly."""
    assert _structures(BENZENE)[0] == BENZENE


def test_a_row_that_cannot_be_placed_is_left_untouched():
    """Never guess: if a shift can't put EVERY glyph on an atom column, the row stays as written."""
    odd = ("     H  H H\n"          # irregular spacing: no single shift fits
           "     |  | |\n"
           "H - C - C - C - H")
    out = _structures(odd)[0]
    assert out.split("\n")[0] == "     H  H H"


# --- negatives: what must NOT become a structure --------------------------------------------------

MATHS_NEGATIVES = [
    # Every one of these is real working measured in the archived corpus.
    "         S₃₀ = 30/2 [2(1000) + 29(100)]\n              = 15 (4900)",
    "   = 1 - 6/1,00,000\n   = (1,00,000 - 6)/1,00,000 = 99994/100,000",
    "   = 35 + 60 / 39\n   = 35 + 1.46",
    "   P = ( (1(-3) + 2(-1)) / 3 , (1(-2) + 2(4)) / 3 )\n      = ( (-3-2)/3 , (-2+8)/3 )",
    "   = 35 + 25/21\n   = 35 + 1.19",
]


@pytest.mark.parametrize("text", MATHS_NEGATIVES)
def test_maths_working_is_never_boxed_as_a_structure(text):
    assert _structures(text) == []


@pytest.mark.parametrize("text", [
    "H - C ≡ C - H",                                     # single line: no alignment to protect
    "CH₂=CH-CH₃ + H₂ → CH₃-CH₂-CH₃",                     # reaction equation
    "C₂H₅OH + 3O₂ → 2CO₂ + 3H₂O\nCH₃COOH + C₂H₅OH → CH₃COOC₂H₅ + H₂O",
    "(I) 1-Chloropropane\n(II) Butanone.",
    "A. B. C. D.",                                       # MCQ options: A and D are not elements
    "The answer is C\nand then B",
])
def test_prose_and_formulas_stay_text(text):
    assert _structures(text) == []


def test_real_code_still_renders_as_code():
    code = ("def total(rows):\n"
            "    n = 0\n"
            "    for r in rows:\n"
            "        n = n + r\n"
            "    return n")
    kinds = _kinds(code)
    assert "code" in kinds and "structure" not in kinds


def test_a_structure_next_to_prose_splits_cleanly():
    text = "(i) But-1-ene\n" + BUT_1_ENE + "\n\nand, But-2-ene\n" + BUT_1_ENE
    kinds = _kinds(text)
    assert kinds.count("structure") == 2
    assert kinds[0] == "text"                            # the label stays prose


# --- the PDF must not re-break what the screen fixed ----------------------------------------------

@pytest.mark.parametrize("ch", ["≡", "→", "–", "—", "·", "×", "₂", "²"])
def test_pdf_transliteration_preserves_column_count(ch):
    """The normal _to_latin1 widens 56 characters ('≡' -> '=='), which would shift every column to
    its right in a monospace block — re-breaking the alignment this feature exists to protect."""
    assert len(ev._to_latin1_monospace(ch)) == 1


def test_pdf_transliteration_keeps_plain_ascii_identical():
    line = "H - C - C - C = C - H"
    assert ev._to_latin1_monospace(line) == line


def test_pdf_renders_structure_segments_monospace():
    """The PDF branch cannot be unit-called (it lives inside generate_pdf_report); guard the call
    site so structures can't silently fall back to the proportional prose font."""
    src = open(os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts/evaluate.py")).read()
    assert 'if seg.get("type") == "structure":' in src
    assert "_to_latin1_monospace(content)" in src


def test_web_renderer_handles_structure_segments():
    src = open(os.path.join(ROOT, "evaluation_app/templates/index.html")).read()
    assert 'seg.type === "structure"' in src
    assert "report-structure" in src
    assert "white-space: pre" in src


# --- guards that only bite on specific shapes (each fixture below is built to discriminate) --------

def test_a_lone_bond_row_is_not_a_structure():
    """Pins the >=2-line rule: a stray row of pipes with no atoms is not a drawing."""
    assert _structures("    |    |") == []


def test_letters_that_are_not_elements_are_not_a_structure():
    """Pins the real-element list. A letter diagram is not a chemical structure — Q and R are not
    element symbols, so this must not be claimed (and mislabelled) as one."""
    diagram = ("   P     Q\n"
               "   |     |\n"
               "   R --- S")
    assert _structures(diagram) == []


def test_a_partially_fitting_row_is_left_alone():
    """Pins 'EVERY glyph must land on an atom column'. Here no single shift can place both H's:
    -1 puts one on an atom and one between two, so a laxer 'any glyph fits' rule would shift the row
    and silently move a hydrogen off its carbon."""
    block = ("     H    H\n"
             "     |    |\n"
             "H - C - C - H")
    assert _structures(block)[0].split("\n")[0] == "     H    H"


def test_two_competing_backbones_are_never_snapped():
    """Pins the ambiguous-backbone guard. The lone bond belongs to the SECOND chain (its column
    matches an atom there); snapping it to the first chain would attach it to the wrong molecule."""
    block = ("H - C - C - C - H\n"
             "      |\n"
             "  H - C - C - C - H")
    assert _structures(block)[0] == block
