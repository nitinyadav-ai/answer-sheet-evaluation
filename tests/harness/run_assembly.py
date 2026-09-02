"""No-network harness to iterate on the Qwen segmentation-recovery logic at zero API cost.

Feeds canned ocr_answers / answer-key dicts through the two pure repair functions and prints the
result, so the split + gap-recovery behaviour can be eyeballed without re-running OCR. By default it
exercises the real Science_Class_X fixture if present, else a small synthetic case.

    python3 tests/harness/run_assembly.py [path/to/output/<exam>]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import full_evaluator as fe  # noqa: E402


def run(ocr, db, qids, pm=None):
    print("--- before ---")
    print("  keys:", sorted(ocr.keys(), key=fe.natural_sort_key))
    ocr, split_map = fe.split_objective_answer_lists(ocr, db, qids, pm)
    print("split_map:", split_map)
    gaps = fe._recompute_gaps(ocr, qids)
    print("gaps after split:", gaps)
    ocr, recovered, flagged, still = fe.recover_gaps_by_position(ocr, gaps, pm, db)
    print("recovered:", recovered, "| flagged:", flagged, "| still_gap:", still)
    print("--- after ---")
    for k in sorted(ocr.keys(), key=fe.natural_sort_key):
        a = (ocr[k].get("answer", "") if isinstance(ocr[k], dict) else "").replace("\n", " / ")
        print(f"  {k:10}: {a[:70]}")
    return ocr


def _from_fixture(base):
    ocr = json.load(open(os.path.join(base, "ocr_output", "ocr_answers.json")))
    db = json.load(open(os.path.join(base, "db_answers.json")))
    qids = json.load(open(os.path.join(base, "ocr_output", "question_ids.json")))
    pmp = os.path.join(base, "ocr_output", "page_mapping.json")
    pm = json.load(open(pmp)) if os.path.exists(pmp) else None
    return ocr, db, qids, pm


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "output", "Science_Class_X")
    if os.path.isdir(base):
        print(f"== fixture: {base} ==")
        run(*_from_fixture(base))
    else:
        print("== synthetic ==")
        ocr = {"Q22": {"answer": "A1. (a) x\nA2. (b) y\nA3. (c) z\n[DIAGRAM: a sketch]"}}
        db = {f"Q{n}": {"type": "MCQ", "marks": 1} for n in range(1, 21)}
        db["Q22"] = {"type": "Short Answer", "marks": 2}
        run(ocr, db, list(range(1, 40)))
