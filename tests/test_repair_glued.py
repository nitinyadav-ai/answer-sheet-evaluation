"""Unit tests for full_evaluator.repair_glued_answers (Approach 1, audit E5). A stub matcher is
injected so these are offline / zero-cost. They pin the non-degradation contract: additive-only,
fills only blank targets, never edits the source, no-op without flags/blanks, no fabrication."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import full_evaluator as fe  # noqa: E402


def _stub(target, foreign):
    def m(block, s_key, s_expected, candidates):
        return target, foreign
    return m


def _db():
    return {"Q35": {"type": "Short Answer", "answer": "reproductive system"},
            "Q36": {"type": "Short Answer", "answer": "refraction"},
            "Q37": {"type": "Short Answer", "answer": "2NaCl + 2H2O -> 2NaOH"}}


def test_recovers_foreign_fragment_into_blank_target():
    ocr = {"Q35": {"answer": "my Q35 answer ... 37. zinc reaction"}, "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None,
                                         matcher=_stub("Q37", "2NaCl reaction lifted"))
    assert rec == [37]
    assert o["Q37"]["answer"] == "2NaCl reaction lifted"
    assert o["Q37"]["recovered_from"] == "Q35"
    assert o["Q35"]["answer"] == "my Q35 answer ... 37. zinc reaction"   # source untouched
    assert fl == [35, 37]                                                # both flagged for review


def test_flags_only_mode_is_noop_without_flags():
    # probe_neighbors=False restores the original Approach-1 contract: no OCR flags -> no-op.
    ocr = {"Q35": {"answer": "x"}, "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [], None,
                                         matcher=_stub("Q37", "y"), probe_neighbors=False)
    assert rec == [] and fl == [] and o["Q37"]["answer"] == ""


def test_neighbour_probe_recovers_silent_glue_without_any_flag():
    # Tier 2 (the KRISHNA-Q34 shape: misread number -> nothing flagged): with NO OCR flags, the blank
    # Q37's content-bearing NEIGHBOUR Q36 is still probed and the (stubbed key) matcher lifts the fragment.
    ocr = {"Q36": {"answer": "my Q36 answer ... plus a reaction that really answers 37"},
           "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [], None,
                                         matcher=_stub("Q37", "zinc reaction lifted"))
    assert rec == [37]
    assert o["Q37"]["answer"] == "zinc reaction lifted"
    assert o["Q36"]["answer"].startswith("my Q36")          # source untouched


def test_neighbour_probe_respects_max_probes_cap():
    # Many blanks -> the host set is bounded by max_probes; the matcher is called at most that many times.
    calls = {"n": 0}
    def counting(block, s_key, s_expected, candidates):
        calls["n"] += 1
        return None, ""
    ocr = {f"Q{n}": {"answer": (f"content {n}" if n % 2 else "")} for n in range(30, 42)}
    fe.repair_glued_answers(ocr, _db(), list(range(30, 42)), [], None, matcher=counting, max_probes=3)
    assert calls["n"] <= 3


def test_noop_when_no_blank_targets():
    ocr = {"Q35": {"answer": "x"}, "Q37": {"answer": "already answered"}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 37], [35], None, matcher=_stub("Q37", "y"))
    assert rec == [] and o["Q37"]["answer"] == "already answered"        # never overwritten


def test_rejects_target_that_is_not_blank():
    ocr = {"Q35": {"answer": "glued"}, "Q36": {"answer": ""}, "Q37": {"answer": "answered"}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None, matcher=_stub("Q37", "y"))
    assert rec == [] and o["Q37"]["answer"] == "answered"                # Q37 not blank -> rejected


def test_rejects_source_pointing_at_itself():
    ocr = {"Q35": {"answer": "glued"}, "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None, matcher=_stub("Q35", "y"))
    assert rec == [] and o["Q35"]["answer"] == "glued"


def test_matcher_exception_leaves_slot_flagged_no_fabrication():
    def _boom(*a):
        raise RuntimeError("no network")
    ocr = {"Q35": {"answer": "glued"}, "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None, matcher=_boom)
    assert rec == [] and o["Q37"]["answer"] == ""                        # nothing fabricated


def test_even_a_wrong_blank_target_is_additive_only():
    # Non-degradation: a wrong match writes into a BLANK target (was 0) and never touches the source,
    # so the total can only rise or stay equal.
    ocr = {"Q35": {"answer": "glued"}, "Q36": {"answer": ""}, "Q37": {"answer": ""}}
    o, rec, fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None,
                                         matcher=_stub("Q36", "possibly-wrong content"))
    assert rec == [36]
    assert o["Q36"]["answer"] == "possibly-wrong content"
    assert o["Q35"]["answer"] == "glued"                                 # source never emptied


# ---- Tier 3: off-topic re-home (a whole answer displaced into a NON-adjacent valid slot by a digit
# misread -- the live Ritika shape: Q34's SQL captured under the misread label '24', 10 slots away) -----
_SQL_ANS = ("I. Select customer_name from Hotels, Bookings where city = 'Delhi'; "
            "II. Select Bookings.* from Hotels, Bookings where city in ('Mumbai','Chennai','Kolkata'); "
            "III. Delete from Bookings where check_in < '2024-12-03'; "
            "IV. Select * from Hotels, Bookings;")


def _sql_db():
    return {
        "Q24": {"type": "Short Answer",
                "answer": "A. I. index = review.find('good') II. L1.sort(reverse=True) "
                          "B. ('Learn Python','with','fun and practice')"},
        "Q33": {"type": "Long Answer", "answer": "import csv def Accept(): product_id = input(); writer"},
        "Q34": {"type": "Long Answer",
                "answer": "I. SELECT Customer_Name FROM Hotels, Bookings WHERE City = 'Delhi'; "
                          "II. SELECT Bookings.* FROM Hotels, Bookings WHERE City IN ('Mumbai','Chennai','Kolkata'); "
                          "III. DELETE FROM Bookings WHERE Check_In < '2024-12-03'; "
                          "IV. SELECT * FROM Hotels, Bookings;"},
        "Q35": {"type": "Long Answer", "answer": "import mysql.connector connect host localhost cursor"},
    }


def _sql_ocr():
    # The SQL (really Q34) was captured under the misread label '24'; Q33/Q35 hold their OWN answers; Q34 blank.
    return {
        "Q24": {"answer": _SQL_ANS},
        "Q33": {"answer": "import csv def Accept(): product_id = input(); writer.writerow(row)"},
        "Q34": {"answer": ""},
        "Q35": {"answer": "import mysql.connector connect host localhost cursor execute"},
    }


def _hotels_stub(block, s_key, s_expected, candidates):
    # Key-based matcher stand-in: only the Hotels/Bookings block answers Q34; the csv/mysql hosts don't.
    if "hotels" in str(block).lower() and any(str(cid) == "Q34" for cid, _ in candidates):
        return "Q34", block
    return None, ""


def test_offtopic_host_recovers_distant_misread():
    valid = [24, 33, 34, 35]
    # The +/-1 neighbour probe alone can't reach Q24 (10 slots from blank Q34): nothing recovered.
    o1, rec1, _ = fe.repair_glued_answers(_sql_ocr(), _sql_db(), valid, [], None,
                                          matcher=_hotels_stub, probe_neighbors=True, probe_offtopic=False)
    assert rec1 == [] and o1["Q34"]["answer"] == ""

    # The off-topic host source surfaces Q24 (off-topic for itself, fits Q34); the matcher lifts it.
    o2, rec2, fl2 = fe.repair_glued_answers(_sql_ocr(), _sql_db(), valid, [], None,
                                            matcher=_hotels_stub, probe_neighbors=True, probe_offtopic=True)
    assert rec2 == [34]
    assert "Hotels" in o2["Q34"]["answer"]
    assert o2["Q34"]["recovered_from"] == "Q24"
    assert o2["Q24"]["answer"] == _SQL_ANS          # source answer untouched (strictly additive)
    assert o2["Q24"]["rehomed_to"] == ["Q34"]       # source annotated for the report
    assert set(fl2) >= {24, 34}


def test_offtopic_prefilter_skips_on_topic_host():
    # Only the off-topic slot (Q24) qualifies; Q33/Q35 match their OWN key, so they're never surfaced
    # (guards against false re-homes and wasted probes).
    hosts = fe._offtopic_rehome_hosts(_sql_ocr(), _sql_db(), {34}, {24, 33, 34, 35})
    assert hosts == [24]


def test_offtopic_gated_off_is_noop():
    # GLUE_PROBE_OFFTOPIC path off + no flags + no reachable neighbour match -> byte-identical old behaviour.
    o, rec, fl = fe.repair_glued_answers(_sql_ocr(), _sql_db(), [24, 33, 34, 35], [], None,
                                         matcher=_hotels_stub, probe_neighbors=True, probe_offtopic=False)
    assert rec == [] and fl == [] and o["Q34"]["answer"] == "" and "rehomed_to" not in o["Q24"]


def test_offtopic_ranks_by_margin_best_first():
    db = {"Q10": {"answer": "alpha beta gamma delta"},
          "Q20": {"answer": "one two three four"},
          "Q11": {"answer": "own private words"},        # Q11's own (distinct) key
          "Q21": {"answer": "solo alone"}}               # Q21's own (distinct) key
    ocr = {"Q10": {"answer": ""}, "Q20": {"answer": ""},
           "Q11": {"answer": "alpha beta gamma delta"},   # 100% Q10, 0% own  -> margin ~1.0
           "Q21": {"answer": "one two three four solo"}}  # 100% Q20, 50% own -> margin ~0.5
    assert fe._offtopic_rehome_hosts(ocr, db, {10, 20}, {10, 11, 20, 21}) == [11, 21]


def test_key_affinity_containment_and_empty():
    assert fe._key_affinity("select from hotels bookings", "select hotels") == 1.0
    assert fe._key_affinity("a x", "a b") == 0.5
    assert fe._key_affinity("anything", "") == 0.0          # empty key -> 0 (never a false match)
    assert fe._key_affinity("", "abc def") == 0.0


# ---- fixpoint recovery: a host holding MORE THAN ONE buried answer -------------------------------
# The matcher returns ONE (target, foreign) per call, and hosts used to be chosen once from the initial
# blank set, so a slot holding N foreign answers yielded exactly one and was abandoned. Measured on real
# data: Maths_Class12 Q15 held Q16..Q20 and only Q16 came back; Q17-Q20 scored 0 as "No answer captured".

def _seq_matcher(pairs, calls=None):
    """Yields the given (target, foreign) results in order, then ('NONE', '') forever. Records the
    (host, candidate-ids) of every call so tests can assert WHICH slots were asked and how often."""
    seq = list(pairs)
    def m(block, s_key, s_expected, candidates):
        if calls is not None:
            calls.append((s_key, tuple(c for c, _e in candidates)))
        return seq.pop(0) if seq else (None, "")
    return m


def _multi_db(nums):
    return {f"Q{n}": {"type": "Short Answer", "answer": f"expected {n}"} for n in nums}


def test_one_host_yields_every_buried_answer():
    ocr = {"Q15": {"answer": "Q15 body\n16 body\n17 body\n18 body"},
           "Q16": {"answer": ""}, "Q17": {"answer": ""}, "Q18": {"answer": ""}}
    o, rec, _fl = fe.repair_glued_answers(
        ocr, _multi_db([15, 16, 17, 18]), [15, 16, 17, 18], [15], None,
        matcher=_seq_matcher([("Q16", "16 body"), ("Q17", "17 body"), ("Q18", "18 body")]))
    assert sorted(rec) == [16, 17, 18]                     # before the fix this returned only [16]
    assert o["Q16"]["answer"] == "16 body"
    assert o["Q18"]["answer"] == "18 body"
    assert o["Q15"]["answer"] == "Q15 body\n16 body\n17 body\n18 body"   # source never edited
    # One slot seeds SEVERAL recoveries -- the whole point of the fix. (Which of them are attributed to
    # Q15 vs to a slot recovered from it depends on probe order, so assert the shape, not the exact set.)
    assert len(o["Q15"]["rehomed_to"]) >= 2
    assert set(o["Q15"]["rehomed_to"]) <= {"Q16", "Q17", "Q18"}


def test_a_slot_filled_mid_run_becomes_the_next_host(monkeypatch):
    """Host selection is recomputed each round. Q16 is BLANK at the start, so it CANNOT be a host in
    round 1 -- the old code froze the host list there and Q17 was unreachable. Q16 is filled in round 1,
    and only then can it be asked about the answer buried inside IT. The stub answers per host, so Q15
    genuinely has nothing more to give and the run only succeeds by probing Q16."""
    monkeypatch.setenv("GLUE_HOST_ATTEMPTS", "1")

    calls = []

    def by_host(block, s_key, s_expected, candidates):
        calls.append((s_key, tuple(c for c, _e in candidates)))
        if s_key == "Q15":
            return ("Q16", "16 body then 17 body")     # Q15 only ever yields Q16
        if s_key == "Q16":
            return ("Q17", "17 body")                  # the second answer lives in the RECOVERED slot
        return (None, "")

    ocr = {"Q15": {"answer": "Q15 body + 16 body + 17 body"},
           "Q16": {"answer": ""}, "Q17": {"answer": ""}}
    o, rec, _fl = fe.repair_glued_answers(ocr, _multi_db([15, 16, 17]), [15, 16, 17], [15], None,
                                          matcher=by_host)
    assert sorted(rec) == [16, 17]
    assert o["Q17"]["recovered_from"] == "Q16"
    assert any(host == "Q16" for host, _c in calls), f"Q16 was never probed as a host: {calls}"


def test_stops_after_a_round_that_finds_nothing():
    calls = []
    ocr = {"Q35": {"answer": "only my own answer"}, "Q36": {"answer": ""}, "Q37": {"answer": ""}}
    o, rec, _fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None,
                                          matcher=_seq_matcher([], calls))
    assert rec == []
    assert o["Q36"]["answer"] == "" and o["Q37"]["answer"] == ""       # nothing fabricated
    assert len(calls) <= 2, f"a dead host must not be re-probed forever: {calls}"


def test_never_spins_when_the_matcher_keeps_naming_a_filled_target():
    """A matcher stuck on an already-filled slot must terminate, not loop forever."""
    ocr = {"Q35": {"answer": "host"}, "Q36": {"answer": "already answered"}, "Q37": {"answer": ""}}
    o, rec, _fl = fe.repair_glued_answers(ocr, _db(), [35, 36, 37], [35], None,
                                          matcher=_stub("Q36", "some text"))
    assert rec == []
    assert o["Q36"]["answer"] == "already answered"                     # never overwritten
    assert o["Q37"]["answer"] == ""


def test_probe_cap_still_bounds_total_calls():
    calls = []
    ocr = {f"Q{n}": {"answer": f"body {n}"} for n in range(1, 12)}
    ocr.update({f"Q{n}": {"answer": ""} for n in range(12, 30)})
    fe.repair_glued_answers(ocr, _multi_db(range(1, 30)), list(range(1, 30)),
                            list(range(1, 12)), None,
                            matcher=_seq_matcher([], calls), max_probes=5)
    assert len(calls) <= 5, f"probe cap ignored: {len(calls)} calls"


def test_a_host_is_retried_but_only_within_its_attempt_budget(monkeypatch):
    """The matcher is measurably unstable on some blocks, so an unchanged question is asked
    GLUE_HOST_ATTEMPTS times before being written off -- and never more."""
    monkeypatch.setenv("GLUE_HOST_ATTEMPTS", "2")
    calls = []
    ocr = {"Q35": {"answer": "host body"}, "Q36": {"answer": ""}}
    fe.repair_glued_answers(ocr, _db(), [35, 36], [35], None, matcher=_seq_matcher([], calls))
    per_host = [h for h, _c in calls]
    assert per_host.count("Q35") == 2, f"expected exactly 2 attempts, got {per_host}"


def test_attempt_budget_of_one_restores_single_shot(monkeypatch):
    monkeypatch.setenv("GLUE_HOST_ATTEMPTS", "1")
    calls = []
    ocr = {"Q35": {"answer": "host body"}, "Q36": {"answer": ""}}
    fe.repair_glued_answers(ocr, _db(), [35, 36], [35], None, matcher=_seq_matcher([], calls))
    assert [h for h, _c in calls].count("Q35") == 1


def test_candidates_shrink_as_answers_are_recovered():
    """Each round asks about FEWER unanswered questions -- that shrinking is what makes re-asking a host
    a genuinely different question rather than a repeat."""
    calls = []
    ocr = {"Q15": {"answer": "big blob"}, "Q16": {"answer": ""}, "Q17": {"answer": ""}}
    fe.repair_glued_answers(ocr, _multi_db([15, 16, 17]), [15, 16, 17], [15], None,
                            matcher=_seq_matcher([("Q16", "a"), ("Q17", "b")], calls))
    sizes = [len(c) for _h, c in calls]
    assert sizes == sorted(sizes, reverse=True), f"candidate sets should only shrink: {calls}"
