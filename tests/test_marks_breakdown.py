"""Review & fix marks editor — backend. Offline: UPLOAD_FOLDER + every state-file path (incl. the
three new marks-editor constants) are redirected to a tmp dir, so no LLM runs and real uploads/ is
untouched. These lock:
  - build_marks_breakdown / apply_marks_corrections helpers (edit, add ±answer, remove, choice group);
  - GET /marks-breakdown rows + mismatch; POST /confirm-marks-breakdown bakes corrections INTO the key
    and marks it confirmed (source=answer_key, edited); POST /reset-marks-breakdown restores the parse;
  - the grading gate blocks /evaluate on a real, unconfirmed mismatch and unblocks after confirm.
"""
import os
import sys
import io
import json

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "evaluation_app"))

from upload_validation import (  # noqa: E402
    build_marks_breakdown, apply_marks_corrections, _suggest_choice_groups,
    _collapse_to_alternatives)

try:
    import app as webapp
except (ImportError, SystemExit):  # pragma: no cover
    webapp = None

pytestmark = pytest.mark.skipif(webapp is None, reason="web app unavailable")


@pytest.fixture
def client(tmp_path, monkeypatch):
    up = str(tmp_path)
    monkeypatch.setitem(webapp.app.config, "UPLOAD_FOLDER", up)
    monkeypatch.setattr(webapp, "QUESTION_PAPER_PATH", os.path.join(up, "current_question_paper.json"))
    monkeypatch.setattr(webapp, "ANSWER_KEY_PATH", os.path.join(up, "current_answer_key.json"))
    monkeypatch.setattr(webapp, "MARKS_SOURCE_PATH", os.path.join(up, "marks_source_state.json"))
    monkeypatch.setattr(webapp, "REPORT_STATE_PATH", os.path.join(up, "report_path_state.json"))
    monkeypatch.setattr(webapp, "CHOICES_PATH", os.path.join(up, "current_answer_key_choices.json"))
    monkeypatch.setattr(webapp, "KEY_PARSED_PATH", os.path.join(up, "current_answer_key_parsed.json"))
    monkeypatch.setattr(webapp, "CHOICES_PARSED_PATH", os.path.join(up, "current_answer_key_choices_parsed.json"))
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    c._up = up
    return c


def _post(client, url, obj):
    return client.post(url, data=json.dumps(obj), content_type="application/json")


def _read(client, name):
    return json.load(open(os.path.join(client._up, name)))


# ------------------------- pure helpers (no Flask) -------------------------

def test_apply_corrections_edit_add_remove_group():
    key = {"Q1": {"question_id": "Q1", "answer": "a", "marks": 1, "subject": "S"},
           "Q2": {"question_id": "Q2", "answer": "b", "marks": 2, "subject": "S"},
           "Q3(a)": {"question_id": "Q3(a)", "answer": "c", "marks": 3, "subject": "S"},
           "Q3(b)": {"question_id": "Q3(b)", "answer": "d", "marks": 3, "subject": "S"}}
    choices = {"choice_groups": [], "inline_choice_ids": []}
    corr = {"marks": {"Q2": 5}, "removed": ["Q1"],
            "added": [{"q": "Q9", "marks": 4, "answer": "ans"}, {"q": "Q10", "marks": 2}],
            "choice_groups": [["Q3(a)", "Q3(b)"]]}
    nk, nc = apply_marks_corrections(key, choices, corr)
    assert "Q1" not in nk                                   # removed
    assert nk["Q2"]["marks"] == 5                           # edited
    with_ans = [v for v in nk.values() if v.get("teacher_added") and v.get("answer") == "ans"]
    no_ans = [v for v in nk.values() if v.get("teacher_added") and v.get("key_parse_missing")]
    assert with_ans and with_ans[0]["marks"] == 4 and not with_ans[0].get("key_parse_missing")
    assert no_ans and no_ans[0]["marks"] == 2 and no_ans[0]["answer"] == ""   # blank -> manual grading
    # The sidecar carries `parent` (derived from the members' base) + `required` so the grader's
    # merge_choice_groups actually collapses the pair instead of skipping it and double-counting.
    assert nc["choice_groups"] == [{"parent": "Q3", "members": ["Q3(a)", "Q3(b)"], "required": 1}]


# --- editor selection -> real choice ALTERNATIVES (the multi-part-alternative under-marking bug) ---
def test_collapse_multipart_alternatives_to_branches():
    """Q32 = Part A (I-IV) OR Part B (I-IV), 4 marks each. The editor tags the 8 ROWS, which would
    declare 8 mutually-exclusive alternatives -> max(1,1,..) = 1, capping a 4-mark question at 1."""
    sel = [f"Q32({p})({r})" for p in ("A", "B") for r in ("I", "II", "III", "IV")]
    assert _collapse_to_alternatives(sel) == ["Q32(A)", "Q32(B)"]


@pytest.mark.parametrize("sel", [
    ["Q34(IV)(A)", "Q34(IV)(B)"],      # OR nested INSIDE a sub-part: must NOT fold to a single Q34(IV)
    ["Q37(V)(A)", "Q37(V)(B)"],
    ["Q22(a)", "Q22(b)"],              # flat pair: identity (locks existing sidecars/tests)
    ["Q3(a)", "Q3(b)"],
    ["Q5.a", "Q5.b"],                  # dotted sub-parts
])
def test_collapse_is_identity_when_rows_already_are_alternatives(sel):
    """Going deeper ONLY when the selection does not split is what keeps a nested OR intact -- a blanket
    first-token collapse would turn Q34(IV)(A)/Q34(IV)(B) into one 'Q34(IV)' and destroy the group."""
    assert _collapse_to_alternatives(sel) == sel


@pytest.mark.parametrize("sel", [["Q9", "Q9"], ["Q9(a)"], ["weird", "other"], []])
def test_collapse_passes_through_what_it_cannot_split(sel):
    """Never invent or reshape a group it cannot interpret -- pass the selection through untouched."""
    assert _collapse_to_alternatives(sel) == sel


def test_confirm_collapses_multipart_group_and_uncaps_the_marks():
    """End-to-end on the helper: posting the 8 leaves must persist BRANCH members and score 4, not 1."""
    key = {f"Q32({p})({r})": {"question_id": f"Q32({p})({r})", "answer": "x", "marks": 1, "subject": "S"}
           for p in ("A", "B") for r in ("I", "II", "III", "IV")}
    sel = [f"Q32({p})({r})" for p in ("A", "B") for r in ("I", "II", "III", "IV")]
    _nk, nc = apply_marks_corrections(key, {"choice_groups": [], "inline_choice_ids": []},
                                      {"marks": {}, "added": [], "removed": [], "choice_groups": [sel]})
    assert nc["choice_groups"] == [{"parent": "Q32", "members": ["Q32(A)", "Q32(B)"], "required": 1}]
    bd = build_marks_breakdown(key, nc, {"Q32": {"marks": 4}})
    assert abs(bd["key_total"] - 4) < 1e-6          # was 1 -> the question was capped at 1 of 4 marks
    assert bd["mismatch"] is False


def test_build_breakdown_regroups_deeper_leaves_on_reload():
    """A saved group's members are ALTERNATIVE ids, so its finer leaves must still come back grouped --
    otherwise the teacher's saved choice looks dissolved the moment the panel reloads."""
    key = {f"Q32({p})({r})": {"marks": 1, "answer": "x"}
           for p in ("A", "B") for r in ("I", "II", "III", "IV")}
    choices = {"choice_groups": [{"parent": "Q32", "members": ["Q32(A)", "Q32(B)"], "required": 1}],
               "inline_choice_ids": []}
    rows = build_marks_breakdown(key, choices, {"Q32": {"marks": 4}})["rows"]
    assert all(r["group"] == 0 for r in rows), [r["qid"] for r in rows if r["group"] is None]


def test_build_breakdown_choice_counts_once():
    key = {"Q1": {"marks": 1, "answer": "a"},
           "Q22(a)": {"marks": 2, "answer": "x"}, "Q22(b)": {"marks": 2, "answer": "y"}}
    choices = {"choice_groups": [{"members": ["Q22(a)", "Q22(b)"]}], "inline_choice_ids": []}
    qp = {"Q1": {"marks": 1}, "Q22": {"marks": 2}}
    bd = build_marks_breakdown(key, choices, qp)
    assert len(bd["rows"]) == 3
    # effective key total counts the choice ONCE (1 + max(2,2) = 3) == qp 3 -> no mismatch
    assert abs(bd["key_total"] - 3) < 1e-6 and abs(bd["qp_total"] - 3) < 1e-6
    assert bd["mismatch"] is False


# --- _suggest_choice_groups: auto-detect an ungrouped 'answer any one' pair (feeds the #4 card) ---
# The signature is "inflated in the key AND grouping lands EXACTLY on the paper's marks" -- so a real
# OR-pair is caught while additive parts and genuine misreads are not.

def test_suggest_groups_flags_equal_or_pair():
    key = {"Q22(a)": {"marks": 2, "answer": "x"}, "Q22(b)": {"marks": 2, "answer": "y"}}
    choices = {"choice_groups": [], "inline_choice_ids": []}
    sug = _suggest_choice_groups(key, choices, {"Q22": {"marks": 2}})
    assert len(sug) == 1
    s = sug[0]
    assert s["base"] == "22" and abs(s["paper"] - 2) < 1e-6 and abs(s["current_sum"] - 4) < 1e-6
    assert [m["qid"] for m in s["members"]] == ["Q22(a)", "Q22(b)"]


def test_suggest_groups_ignores_additive_parts():
    # (i)+(ii) legitimately SUM to the paper's 5 -> not inflated -> never suggested.
    key = {"Q5(i)": {"marks": 2, "answer": "x"}, "Q5(ii)": {"marks": 3, "answer": "y"}}
    assert _suggest_choice_groups(key, {"choice_groups": [], "inline_choice_ids": []},
                                  {"Q5": {"marks": 5}}) == []


def test_suggest_groups_ignores_already_grouped():
    key = {"Q22(a)": {"marks": 2, "answer": "x"}, "Q22(b)": {"marks": 2, "answer": "y"}}
    choices = {"choice_groups": [{"parent": "Q22", "members": ["Q22(a)", "Q22(b)"], "required": 1}],
               "inline_choice_ids": []}
    assert _suggest_choice_groups(key, choices, {"Q22": {"marks": 2}}) == []


def test_suggest_groups_ignores_genuine_misread():
    # 3+3+3 inflated over paper 5, but max alternative (3) != 5 -> a duplicate, NOT a curable choice.
    key = {"Q9(a)": {"marks": 3}, "Q9(b)": {"marks": 3}, "Q9(c)": {"marks": 3}}
    assert _suggest_choice_groups(key, {"choice_groups": [], "inline_choice_ids": []},
                                  {"Q9": {"marks": 5}}) == []


def test_suggest_groups_unequal_alternatives_max_equals_paper():
    key = {"Q10(a)": {"marks": 5}, "Q10(b)": {"marks": 3}}
    sug = _suggest_choice_groups(key, {"choice_groups": [], "inline_choice_ids": []},
                                 {"Q10": {"marks": 5}})
    assert len(sug) == 1 and abs(sug[0]["paper"] - 5) < 1e-6
    marks = {m["qid"]: m["marks"] for m in sug[0]["members"]}
    assert marks == {"Q10(a)": 5, "Q10(b)": 3}


def test_suggest_groups_deep_subpart_alternative():
    # Alternatives split deeper than the member id: (a)(i)+(a)(ii) collapse into one alternative 'Q34(a)'.
    key = {"Q34(a)(i)": {"marks": 2}, "Q34(a)(ii)": {"marks": 3}, "Q34(b)": {"marks": 6}}
    sug = _suggest_choice_groups(key, {"choice_groups": [], "inline_choice_ids": []},
                                 {"Q34": {"marks": 6}})
    assert len(sug) == 1
    marks = {m["qid"]: m["marks"] for m in sug[0]["members"]}
    assert marks == {"Q34(a)": 5, "Q34(b)": 6}


def test_build_breakdown_adds_suggested_groups_key():
    # Backward-compat: existing keys intact; suggested_groups present, empty for a clean additive key.
    key = {"Q1": {"marks": 1, "answer": "a"}, "Q2": {"marks": 2, "answer": "b"}}
    bd = build_marks_breakdown(key, {"choice_groups": [], "inline_choice_ids": []},
                               {"Q1": {"marks": 1}, "Q2": {"marks": 2}})
    for k in ("rows", "groups", "key_total", "qp_total", "mismatch"):
        assert k in bd
    assert isinstance(bd["suggested_groups"], list) and bd["suggested_groups"] == []


def test_build_breakdown_includes_qp_question():
    # Each row carries the PAPER's question text (for the editor's guided cards), preferring the paper
    # over the key's own `question` field -- which is polluted to the answer for objective questions.
    key = {"Q1": {"marks": 1, "answer": "True", "question": "True"},
           "Q30(a)": {"marks": 2, "answer": "x", "question": ""},
           "Q30(b)": {"marks": 2, "answer": "y", "question": ""}}
    qp = {"Q1": {"marks": 1, "question": "State whether the statement is true or false."},
          "Q30": {"marks": 6, "question": "Answer the following about SQL joins."}}
    by = {r["qid"]: r for r in build_marks_breakdown(
        key, {"choice_groups": [], "inline_choice_ids": []}, qp)["rows"]}
    assert by["Q1"]["qp_question"] == "State whether the statement is true or false."   # paper, not "True"
    # every part of a multi-part base gets the base's (stem) question text
    assert by["Q30(a)"]["qp_question"] == "Answer the following about SQL joins."
    assert by["Q30(b)"]["qp_question"] == "Answer the following about SQL joins."
    # a base stem is preferred over a finer sub-part entry for the same base
    qp2 = {"Q2(a)": {"marks": 1, "question": "part a only"},
           "Q2": {"marks": 2, "question": "Full stem about loops and iteration."}}
    bd2 = build_marks_breakdown({"Q2": {"marks": 2, "answer": "z"}},
                                {"choice_groups": [], "inline_choice_ids": []}, qp2)
    assert bd2["rows"][0]["qp_question"] == "Full stem about loops and iteration."


def test_build_breakdown_flags_key_only_base():
    # A base present in the answer key but NOT in the question paper -> its row carries qp_marks=None
    # and the breakdown reports a mismatch. This is the contract the editor's "in the key, not in the
    # paper" card keys off (the live CS Class 12 case: paper parse missing the Q1-Q8 objective section).
    key = {"Q1": {"marks": 1, "answer": "a"}, "Q2": {"marks": 2, "answer": "b"}}
    qp = {"Q2": {"marks": 2}}   # paper is missing Q1
    bd = build_marks_breakdown(key, {"choice_groups": [], "inline_choice_ids": []}, qp)
    by = {r["qid"]: r for r in bd["rows"]}
    assert by["Q1"]["qp_marks"] is None          # in key, not in paper
    assert by["Q2"]["qp_marks"] == 2
    assert bd["mismatch"] is True                 # key 3 vs paper 2
    assert abs(bd["key_total"] - 3) < 1e-6 and abs(bd["qp_total"] - 2) < 1e-6


# ------------------------- routes -------------------------

def _seed_mismatch(client):
    """QP Q1=5, key Q1=2 -> total mismatch (5 vs 2)."""
    _post(client, "/paste-question-paper", {"questions": {
        "Q1": {"question_id": "Q1", "question": "x", "marks": 5, "type": "Short Answer"}}})
    _post(client, "/paste-answer-key",
          {"Q1": {"question_id": "Q1", "answer": "a", "marks": 2, "type": "Short Answer", "subject": "S"}})


def test_marks_breakdown_route_reports_mismatch(client):
    _seed_mismatch(client)
    d = client.get("/marks-breakdown").get_json()
    assert d["available"] is True and d["has_question_paper"] is True
    assert d["mismatch"] is True and d["confirmed"] is False
    row = next(r for r in d["rows"] if r["qid"] == "Q1")
    assert row["key_marks"] == 2 and row["qp_marks"] == 5


def test_marks_breakdown_route_includes_suggested_groups(client):
    # An ungrouped equal OR pair (key counts 2+2=4, paper 2) surfaces as a #4 suggestion on the route.
    _post(client, "/paste-question-paper", {"questions": {
        "Q22": {"question_id": "Q22", "question": "(a) OR (b)", "marks": 2, "type": "SA"}}})
    _post(client, "/paste-answer-key", {
        "Q22(a)": {"question_id": "Q22(a)", "answer": "x", "marks": 2, "type": "SA", "subject": "S"},
        "Q22(b)": {"question_id": "Q22(b)", "answer": "y", "marks": 2, "type": "SA", "subject": "S"}})
    d = client.get("/marks-breakdown").get_json()
    assert isinstance(d.get("suggested_groups"), list) and len(d["suggested_groups"]) == 1
    assert d["suggested_groups"][0]["base"] == "22"
    assert [m["qid"] for m in d["suggested_groups"][0]["members"]] == ["Q22(a)", "Q22(b)"]


def test_confirm_bakes_marks_into_key_and_confirms(client):
    _seed_mismatch(client)
    r = _post(client, "/confirm-marks-breakdown", {"marks": {"Q1": 5}, "added": [], "removed": [], "choice_groups": []})
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert _read(client, "current_answer_key.json")["Q1"]["marks"] == 5      # baked into the key
    st = _read(client, "marks_source_state.json")
    assert st["confirmed"] is True and st["source"] == "answer_key" and st["edited"] is True


def test_confirm_add_without_answer_flags_manual(client):
    _seed_mismatch(client)
    _post(client, "/confirm-marks-breakdown",
          {"marks": {"Q1": 5}, "added": [{"q": "Q2", "marks": 3}], "removed": [], "choice_groups": []})
    key = _read(client, "current_answer_key.json")
    q2 = next(v for k, v in key.items() if v.get("teacher_added"))
    assert q2["marks"] == 3 and q2.get("key_parse_missing") is True and q2["answer"] == ""


def test_confirm_choice_group_written_to_sidecar(client):
    _post(client, "/paste-question-paper", {"questions": {"Q22": {"question_id": "Q22", "question": "(a) OR (b)", "marks": 2, "type": "SA"}}})
    _post(client, "/paste-answer-key", {
        "Q22(a)": {"question_id": "Q22(a)", "answer": "x", "marks": 2, "type": "SA", "subject": "S"},
        "Q22(b)": {"question_id": "Q22(b)", "answer": "y", "marks": 2, "type": "SA", "subject": "S"}})
    _post(client, "/confirm-marks-breakdown", {"marks": {"Q22(a)": 2, "Q22(b)": 2}, "added": [], "removed": [],
                                               "choice_groups": [["Q22(a)", "Q22(b)"]]})
    ch = _read(client, "current_answer_key_choices.json")
    assert {"parent": "Q22", "members": ["Q22(a)", "Q22(b)"], "required": 1} in ch["choice_groups"]
    # after grouping, the effective key total counts the choice once (2), matching the paper
    d = client.get("/marks-breakdown").get_json()
    assert abs(d["key_total"] - 2) < 1e-6


def test_confirm_multipart_choice_group_via_route(client):
    """The reported bug, through the real route: ticking the 8 Q32 rows and confirming must persist
    branch members and leave the base worth 4 (it scored 1, dragging a 70-mark paper down to 67)."""
    _post(client, "/paste-question-paper",
          {"questions": {"Q32": {"question_id": "Q32", "question": "(A) OR (B)", "marks": 4, "type": "SA"}}})
    _post(client, "/paste-answer-key",
          {f"Q32({p})({r})": {"question_id": f"Q32({p})({r})", "answer": "x", "marks": 1,
                              "type": "SA", "subject": "S"}
           for p in ("A", "B") for r in ("I", "II", "III", "IV")})
    sel = [f"Q32({p})({r})" for p in ("A", "B") for r in ("I", "II", "III", "IV")]
    _post(client, "/confirm-marks-breakdown",
          {"marks": {q: 1 for q in sel}, "added": [], "removed": [], "choice_groups": [sel]})

    ch = _read(client, "current_answer_key_choices.json")
    assert ch["choice_groups"] == [{"parent": "Q32", "members": ["Q32(A)", "Q32(B)"], "required": 1}]
    d = client.get("/marks-breakdown").get_json()
    assert abs(d["key_total"] - 4) < 1e-6 and d["mismatch"] is False
    # and every leaf row still renders as grouped after the reload
    assert all(r["group"] == 0 for r in d["rows"])


def test_reset_restores_parsed_marks(client):
    _seed_mismatch(client)
    _post(client, "/confirm-marks-breakdown", {"marks": {"Q1": 5}, "added": [], "removed": [], "choice_groups": []})
    assert _read(client, "current_answer_key.json")["Q1"]["marks"] == 5
    r = client.post("/reset-marks-breakdown")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert _read(client, "current_answer_key.json")["Q1"]["marks"] == 2      # back to the parse


def test_evaluate_gate_blocks_then_unblocks(client):
    _seed_mismatch(client)
    # satisfy the earlier prereqs so /evaluate reaches the marks gate.
    json.dump({"confirmed": True, "path": client._up, "class": "", "subject": "S"},
              open(os.path.join(client._up, "report_path_state.json"), "w"))
    blocked = client.post("/evaluate", data={"student_name": "T", "file": (io.BytesIO(b"x"), "s.pdf")},
                          content_type="multipart/form-data").get_json()
    assert blocked["status"] == "error" and "Marks" in blocked["error"]      # gate fired
    # confirming the breakdown flips marks_source_state -> confirmed (the gate now passes)
    _post(client, "/confirm-marks-breakdown", {"marks": {"Q1": 5}, "added": [], "removed": [], "choice_groups": []})
    assert _read(client, "marks_source_state.json")["confirmed"] is True
