"""Shared negative corpus: lines that must NEVER be read as a question label.

Two sources, both kept together so every layer that recognises a label is held to the same bar:

1. The published Indian-exam label taxonomy, section 13 ("False positives to explicitly exclude") --
   dates, page numbers, in-answer enumeration, equation and step numbering, figure/table references,
   cross-references, chemical formulae, marks notation, years and quantities, paper codes, rough work.

2. The false positives MEASURED in this project's own corpus, which section 13 does not contain
   because it has no mathematical prose. These were found by running the taxonomy's own reference
   regex over 3,603 real answer lines: it produced 155 matches of which 56 were wrong, every one from
   a marker letter followed by roman-numeral letters inside an ordinary word --
       'AC = 5cm'                    -> marker 'A' + number 'C'  = question 100
       'Also, P(E|B) = 3/7'          -> 'A' + 'l'                = question 50
       'According to the question :' -> 'A' + 'cc'
       'Solving (6) and (7), we get' -> 'Sol' + 'vi'             = question 6
   plus matrix cofactor notation ('A11 = -2'), which is why the bare-'A' label branch requires a
   terminator after the number.

Each entry is (line, question_number) -- the number the line must NOT be taken as a header for.
"""

# --- taxonomy section 13 -------------------------------------------------------------------------
TAXONOMY_NEGATIVES = [
    # dates
    ("12.5.2024", 12), ("12/5/24", 12), ("12-05-2024", 12), ("1.1.2025", 1),
    ("15.8.1947 was independence day", 15),
    # page numbers
    ("Page 3", 3), ("- 3 -", 3),
    # enumeration inside a long answer
    ("1. First reason for the failure", 1), ("2. Second reason", 2),
    # equation / step numbering
    ("(1)", 1), ("(2)", 2), ("Step 1", 1), ("Step-2", 2),
    # figure / table references
    ("Fig. 1 shows the ray diagram", 1), ("Table 2 gives the values", 2),
    ("Diagram 3", 3), ("Graph 1", 1),
    # cross-references
    ("Eq. (3)", 3), ("from (2)", 2), ("by (1)", 1),
    # chemical formulae
    ("H2O is water", 2), ("CO2 rises", 2),
    # marks notation
    ("(5 marks)", 5), ("[3]", 3), ("5M", 5),
    # years / quantities
    ("In 1947 India", 47), ("Rs. 12 only", 12), ("12 kg of sugar", 12),
    # question-paper codes / rough work
    ("Set-1", 1), ("Code 65/1/1", 65), ("Series JBB", 1), ("Rough work", 1),
]

# --- measured in this corpus ----------------------------------------------------------------------
CORPUS_NEGATIVES = [
    ("AC = 5cm", 100), ("AC = 10/2 = 5cm", 100), ("AC = 5 cm", 100),
    ("Also, P(E|B) = 3/7 and P(E|B2) = 8/14", 50), ("Also,", 50), ("Also f(3) = -1/2", 50),
    ("According to the question :", 100), ("According to cond of 1/gm", 100),
    ("Solving (6) and (7), we get lambda = -1", 6),
    ("angles is 360 degrees", 50),
    ("q is also devided by 5", 1),
    # matrix cofactors -- the reason bare 'A' needs a terminator after the number
    ("A11 = -2, A12 = -(2) = -2, A13 = +(3) = 3", 11),
    ("A21 = -(1) = -1, A22 = -1, A23 = -(-2) = 2", 21),
    ("A31 = -(-4 + 1) = -3, A32 = -(2) = -2", 31),
    # geometry point labels, not question labels
    ("    x Q     3", 3), ("    P Q = 7 k", 7), ("Let x = 17 be the value", 17),
    ("radius r = 30 cm, angle theta = 30", 30), ("area = 30 x 22/7 x 42", 30),
    ("(a) some unrelated answer with no 99 header", 99),
]

# --- physics: Q1 / Q2 are CHARGE symbols, not question labels -------------------------------------
PHYSICS_NEGATIVES = [
    ("Q1 = 5 microcoulomb", 1), ("Q2 = 3Q1", 2), ("Q17 = 5 x 10^-6 C", 17),
]

ALL_NEGATIVES = TAXONOMY_NEGATIVES + CORPUS_NEGATIVES + PHYSICS_NEGATIVES
