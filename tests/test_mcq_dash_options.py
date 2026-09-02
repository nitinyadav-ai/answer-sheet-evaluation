"""Answer-key options separated by an en-dash/em-dash ("A – text", "C — text") must still yield the
option LETTER so a correct MCQ answer matches on identifier. Regression from the Class X Science key,
where "A – Salt and water formed" parsed to (None, ...) and zeroed correct answers. Offline/no network."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "skills/answer-evaluator-and-report-generation/scripts"))

try:
    import evaluate as ev
except (ImportError, SystemExit) as e:
    ev = None
    _ERR = str(e)

pytestmark = pytest.mark.skipif(ev is None, reason="evaluate.py unavailable in this env")


def test_en_dash_option_extracts_letter():
    assert ev.parse_option("A – Salt and water formed") == ("A", "Salt and water formed")


def test_em_dash_option_extracts_letter():
    assert ev.parse_option("C — A true, R false") == ("C", "A true, R false")


def test_existing_separators_unchanged():
    # slash, paren, and hyphen keep working exactly as before (no regression)
    assert ev.parse_option("A / Salt and water is formed") == ("A", "Salt and water is formed")
    assert ev.parse_option("(a) Salt and water are formed") == ("a", "Salt and water are formed")
    assert ev.parse_option("(c)") == ("c", "")


def test_dash_key_matches_paraphrased_student_on_letter():
    # The bug: key "A – ..." vs student "(a) <paraphrase>" scored 0 because no letter was extracted.
    id_match, _label_match = ev.mcq_match("A – Salt and water formed", "(a) Salt and water are formed")
    assert id_match is True        # a == A on identifier -> correct, regardless of paraphrased text
