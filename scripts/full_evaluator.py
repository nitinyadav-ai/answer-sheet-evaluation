import os
import sys
import json
import subprocess
import shutil
import re
import time
import threading
import signal
from pathlib import Path

# Single source of truth for question-ID canonicalisation (prefix/zero/punctuation-tolerant), shared
# with OCR assembly. Importable because this file lives in scripts/, which is on sys.path whenever it
# is run or imported. See tests/test_qid_utils.py for the byte-identical-on-working-inputs proof.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qid_utils import canonical_qid, subpart_of

# ---------------------------------------------------------------------------------------------------
# Platform portability. Spawning a stage and killing its tree are the ONLY OS-specific parts of the
# pipeline; both are defined once here so no caller has to branch.
#
# PYTHON_EXE -- stages used to be spawned as the literal "python3", which does not exist on Windows.
# Worse than a clean failure: Win10/11 ship an App Execution Alias named python3.exe that opens the
# Microsoft Store and exits, so every stage silently produced nothing. sys.executable is the
# interpreter already running this file, so it is correct on every platform and additionally pins
# stages to the SAME virtualenv as the orchestrator (previously "python3" resolved via PATH and could
# differ). Measured identical to PATH `python3` on the POSIX dev machine -> POSIX behaviour unchanged.
#
# Process groups -- POSIX puts each stage in its own session so ONE killpg takes down the whole tree,
# including grandchildren such as preprocess's ProcessPool workers. Windows has no equivalent, and
# os.killpg / os.getpgid / signal.SIGKILL do not exist there AT ALL: touching them raises
# AttributeError, which the callers' (ProcessLookupError, PermissionError, OSError) guards do not
# catch, so the watchdog and the cancel button would both take the run down with them. Windows uses
# `taskkill /T /F` instead, which is the real tree-kill.
# ---------------------------------------------------------------------------------------------------
IS_WINDOWS = (os.name == "nt")

PYTHON_EXE = sys.executable or ("python" if IS_WINDOWS else "python3")

def _new_group_kwargs():
    """Popen kwargs that isolate a stage into its own killable group. A function, not a constant, so both
    branches stay reachable under test by patching IS_WINDOWS. The Windows branch is never evaluated on
    POSIX -- which matters, because CREATE_NEW_PROCESS_GROUP only exists there."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc):
    """Force-kill a stage and every descendant it spawned. True if a kill was issued, False if even the
    direct-child fallback failed. Same outcome on POSIX as the killpg/proc.kill() pair it replaces."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        try:                                    # tree kill unavailable -> at least the direct child
            proc.kill()
            return True
        except Exception:
            return False


def natural_sort_key(s):
    """Sort key so page_2 precedes page_10 (filenames are not zero-padded)."""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', str(s))]


def normalize_qid(qid):
    """Canonicalise any question label to 'Q<n>' (+ sub-part suffix), regardless of how the
    student/OCR labelled it, so it aligns to the answer key BEFORE matching:
        'A1','Ans 1','Answer1','Sol 1','Q.1','Q-1','QA1','13' -> 'Q1';   'Q21(a)','A21.b' -> 'Q21(a)','Q21.b'
    Delegates to qid_utils.canonical_qid (the single source of truth shared with OCR assembly): same
    result as before on every working input, and additionally cleans leading zeros ('01'->'Q1') and
    stray wrapping punctuation ('(1)'/'1)'->'Q1'). Keys with no digit ('_instructions_') are returned
    unchanged. See tests/test_qid_utils.py for the equivalence proof."""
    return canonical_qid(qid)


def _canonicalize_db_keys(manual_db):
    """Canonicalize answer-key question IDs to the SAME scheme the OCR/student keys use, via
    normalize_qid ('1'->'Q1', '21(a)'->'Q21(a)', 'Q.1'->'Q1'). The answer-key parser is free to
    label questions as bare '1','2',... or 'Q1','Q2',...; without this, a bare-numbered key never
    aligns to OCR's 'Q1','Q2',... and every answer renders BLANK. Merges the rare collision
    (keeps the first entry; appends a distinct alternative answer)."""
    out = {}
    for k, v in (manual_db or {}).items():
        if k == "_instructions_":
            out[k] = v
            continue
        ck = normalize_qid(k)
        if ck in out and isinstance(out[ck], dict) and isinstance(v, dict):
            a_old, a_new = str(out[ck].get("answer", "") or ""), str(v.get("answer", "") or "")
            if a_new.strip() and a_new not in a_old:
                out[ck]["answer"] = (a_old + "\nOR\n" + a_new).strip()
        else:
            nv = dict(v) if isinstance(v, dict) else v
            if isinstance(nv, dict):
                nv["question_id"] = ck
            out[ck] = nv
    return out


def _canonicalize_choices(choices):
    """Normalize choice-group parent/member ids + inline ids to the OCR scheme so they line up with
    the canonicalized answer key (e.g. a sidecar member '21(a)' -> 'Q21(a)' to match key 'Q21(a)')."""
    if not choices:
        return choices
    groups = [{"parent": normalize_qid(g.get("parent", "")),
               "members": [normalize_qid(m) for m in (g.get("members") or [])],
               "required": g.get("required", 1)}
              for g in (choices.get("choice_groups") or [])]
    inline = [normalize_qid(x) for x in (choices.get("inline_choice_ids") or [])]
    uncertain = {normalize_qid(x) for x in (choices.get("uncertain") or set())}
    return {"choice_groups": groups, "inline_choice_ids": inline, "uncertain": uncertain}


_DEFAULT_NAMES = {"", "student", "unknownstudent"}


DIAGRAM_UNASSESSED_FILE = "diagram_unassessed.json"


def _flag_unassessed_diagrams(output_base, diagram_crops_path, features_json):
    """Record which diagram questions were NOT assessed, so the report can say so.

    A stalled crop is abandoned by extract_features rather than allowed to take the stage down, so
    the feature set can legitimately cover only some questions. Those questions still get their
    WRITTEN-answer mark (the diagram merge only touches questions present in diagram_evals), which is
    the right degradation -- but with nothing recorded it is indistinguishable from "the diagram was
    assessed and scored nothing". A teacher must be able to tell those apart.

    Written as a sidecar rather than onto the OCR entries because the repair layers rebuild those
    dicts in many places and would drop a new key (the pattern used by recovery_flags / symbol_flags).
    Never raises: diagnostics must not be able to fail a run.
    """
    try:
        with open(diagram_crops_path, encoding="utf-8") as f:
            crops = json.load(f)
        expected = {str(c.get("question_id")) for c in crops if isinstance(c, dict) and c.get("question_id")}
        got = set()
        if features_json:
            parsed = json.loads(features_json) if isinstance(features_json, str) else features_json
            got = {str(k) for k, v in (parsed or {}).items() if str(v or "").strip()
                   and not str(v).startswith("[SYSTEM ERROR")}
        missing = sorted(expected - got)
        if not missing:
            return
        note = ("The diagram for this question could not be read in time, so only the written "
                "answer was marked. Check the diagram against the sheet before finalising.")
        path = os.path.join(output_base, DIAGRAM_UNASSESSED_FILE)
        with open(path, "w") as f:
            json.dump({q: note for q in missing}, f, indent=2)
        print(f"[diagram] {len(missing)} question(s) were not diagram-assessed: {', '.join(missing)}")
    except Exception as e:                                   # noqa: BLE001 - never fail the run
        print(f"Warning: could not record un-assessed diagrams: {e}")


def _student_pii_extraction_enabled():
    """Mirror of run_ocr.student_pii_extraction_enabled so the orchestrator can decide, before
    spawning the subprocess, whether the identity-extraction call should happen at all.
    OCR_EXTRACT_STUDENT_PII=0 turns it off for every run."""
    return str(os.environ.get("OCR_EXTRACT_STUDENT_PII", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def _resolve_student_name(provided, sheet_name):
    """Teacher-provided name wins; fall back to the sheet's OCR name. Generic defaults
    ('Student', 'Student 3', 'UnknownStudent') count as 'not provided'."""
    p = (provided or "").strip()
    if p and p.lower() not in _DEFAULT_NAMES and not re.fullmatch(r'(?i)student\s*\(?\d+\)?', p):
        return p
    s = (sheet_name or "").strip()
    return "" if s.upper() == "BLANK" else s


def _first_db_subject(db):
    for v in (db or {}).values():
        if isinstance(v, dict) and v.get("subject"):
            return str(v["subject"]).strip()
    return ""


def _clean_meta(v):
    v = (v or "").strip()
    return "" if v.upper() == "BLANK" else v


def merge_subparts_into_parents(ocr_path, db_path, manual_db):
    """Collapse OCR sub-parts (Q37.i..v) into their single parent key (Q37) when the answer
    key stores only the parent entry, so the question is evaluated ONCE out of the key's marks
    instead of N x parent-marks. Runs AFTER diagram processing (which needs sub-part keys) and
    BEFORE text evaluation. Returns the list of merged parent keys.

    Case distinction (needs the original key): a sub-part is merged only when the key has the
    PARENT entry but NOT the sub-part (the inflation bug). Genuine per-sub-part key entries are
    left untouched. Internal-choice parents ("answer any M of N") are left split for best-of-N.
    """
    with open(ocr_path, encoding="utf-8") as f:
        ocr_data = json.load(f)

    internal_choice_parents = set()
    for inst in ocr_data.get("_instructions_", []):
        m = re.search(r'Q\.?\s*(\d+).*?[Aa]nswer\s+any\s+\d+\s+out\s+of', str(inst))
        if m:
            internal_choice_parents.add(f"Q{m.group(1)}")

    groups = {}
    for ocr_k in list(ocr_data.keys()):
        if ocr_k == "_instructions_" or "." not in ocr_k:
            continue
        base_k = ocr_k.split(".")[0]
        if ocr_k not in manual_db and base_k in manual_db and base_k not in internal_choice_parents:
            groups.setdefault(base_k, []).append(ocr_k)

    if not groups:
        return []

    with open(db_path, encoding="utf-8") as f:
        db_data = json.load(f)

    for parent, parts in groups.items():
        parts.sort(key=natural_sort_key)
        merged_lines = []
        bad = False
        for p in parts:
            entry = ocr_data.get(p, {})
            if not isinstance(entry, dict):
                entry = {"answer": str(entry)}
            ans = (entry.get("answer", "") or "").strip()
            suffix = p.split(".", 1)[1] if "." in p else ""
            merged_lines.append(f"({suffix}) {ans}" if suffix else ans)
            bad = bad or bool(entry.get("is_bad_handwriting", False))
        for p in parts:
            ocr_data.pop(p, None)
            db_data.pop(p, None)
        ocr_data[parent] = {"answer": "\n".join(merged_lines).strip(), "is_bad_handwriting": bad}
        db_data[parent] = dict(manual_db[parent])
        db_data[parent]["question_id"] = parent
        print(f"Merged {len(parts)} sub-parts {parts} into {parent} "
              f"(max {manual_db[parent].get('marks', 0)}).")

    with open(ocr_path, "w") as f:
        json.dump(ocr_data, f, indent=2)
    with open(db_path, "w") as f:
        json.dump(db_data, f, indent=2)
    return list(groups.keys())


def _base_qnum(qid):
    """Leading question number from ids like 'Q31', 'Q31(a)', 'Q31.b', 'AI10_Q31' -> '31'."""
    m = re.search(r'Q\s*0*(\d+)', str(qid), re.IGNORECASE)
    return m.group(1) if m else None


def _is_under(entry_id, member_id):
    """True when entry_id IS member_id, or a FINER sub-part of it (same base question, and the entry's
    sub-part string extends the member's). So a choice member 'Q34(a)' claims every 'Q34(a)(i)',
    'Q34(a)(iii)(II)', ... leaf even when the key parser split the alternative deeper than the member
    id names -- the granularity mismatch that made a choice count both alternatives (the 99.5 bug)."""
    ne, nm = normalize_qid(entry_id), normalize_qid(member_id)
    if _base_qnum(ne) != _base_qnum(nm):
        return False
    se, sm = subpart_of(ne), subpart_of(nm)
    return se == sm or se.startswith(sm + "(") or se.startswith(sm + ".")


def effective_choice_marks(leaf_marks, members):
    """Correct marks for ONE base question offering an 'answer any one' CHOICE, tolerant of the parser
    splitting each alternative into deeper sub-parts than the member id names.
      leaf_marks: {leaf_id: marks} for the base.   members: the choice alternatives' ids.
    Returns  sum(common leaves) + max over members(sum of that member's leaves) -- where a COMMON leaf
    is under NO member (an additive part the student answers regardless of the choice, e.g. the (a),(b)
    parts of a case study whose OR sits only in (c)). Returns None when fewer than 2 members resolve to
    any leaf (an unresolvable/garbled choice), so the caller falls back to a plain additive sum."""
    per_member = {}
    common = []
    for lid, mk in leaf_marks.items():
        owner = next((m for m in members if _is_under(lid, m)), None)
        if owner is None:
            common.append(_safe_float(mk))
        else:
            per_member.setdefault(owner, []).append(_safe_float(mk))
    alt_sums = [sum(v) for v in per_member.values() if v]
    if len(alt_sums) < 2:
        return None
    return sum(common) + max(alt_sums)


def _derive_question_id_set(answer_key_path, question_paper_path):
    """Authoritative set of base question numbers for this exam, used to ANCHOR OCR question
    separation. Union of the answer key's and (if present) the question paper's question numbers,
    using the same canonicalization the rest of the pipeline uses. Returns a sorted list[int], or
    None when neither source yields any number (so OCR runs unanchored, exactly as before).
    Reads throwaway local dicts only -- never mutates anything the later key-load relies on."""
    bases = set()
    for path in (answer_key_path, question_paper_path):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        is_key = (path == answer_key_path)
        src = _canonicalize_db_keys(data) if is_key else data
        for k in src:
            if k == "_instructions_":
                continue
            bn = _base_qnum(k if is_key else normalize_qid(k))
            if bn is not None:
                bases.add(int(bn))
    return sorted(bases) or None


def reconcile_ocr_to_question_set(ocr_answers, valid_base_numbers):
    """Conservative, FLAG-ONLY reconciliation of the (already canonicalized) OCR answers against the
    exam's authoritative question-number set. Never moves or re-assigns graded text.
      (a) An OCR key whose base number IS in the set -> left untouched.
      (b) An OCR key whose base number is NOT in the set (a definite misread/invented question
          number) -> keep the text and record a question_set_warning note.

    That note used to be delivered by ALSO setting is_bad_handwriting=True, borrowing the handwriting
    flag purely as a "route this to review" lever. It worked, but the teacher was told the handwriting
    was illegible when the actual finding was a misread question NUMBER. The note now travels on its
    own channel (symbol_flags.json -> 'OCR Symbol Warning'), which still forces review -- this IS real
    evidence -- but under an accurate name.
    Returns (ocr_answers, gap_numbers) where gap_numbers are in-set questions with no captured answer
    (caller decides whether to flag them, choice-aware). No-op when the set is empty."""
    if not valid_base_numbers:
        return ocr_answers, []
    valid = {int(n) for n in valid_base_numbers}
    present = set()
    for k, v in ocr_answers.items():
        if k == "_instructions_":
            continue
        bn = _base_qnum(k)
        if bn is None:
            continue
        n = int(bn)
        if n in valid:
            if str((v or {}).get("answer", "")).strip():
                present.add(n)
        elif isinstance(v, dict):
            v["question_set_warning"] = (
                f"OCR labelled this Q{bn}, which is not one of the exam's question numbers "
                f"({sorted(valid)}) -- likely a misread question number. Verify manually.")
    gap_numbers = sorted(valid - present)
    return ocr_answers, gap_numbers


# --- Qwen segmentation recovery -------------------------------------------------------------------
# Qwen-3-VL mis-segments two patterns the Gemini-tuned OCR assembler does not catch: (A) it wraps a
# whole objective-answer list ("A1. (d).. A2. (b).. .. A6. (d)..") in ONE [START_Q], dropping Q2..Q6;
# (C) it routes a misread sub-part header into a neighbour, leaving a real question empty. The two
# pure functions below repair both AFTER assembly -- additively, gated on the closed question set,
# and WITHOUT touching the carefully-tuned weld logic in run_ocr.assemble_answers. Local copies of the
# MCQ option regexes (kept in lock-step with evaluate.py:108-129) let the splitter classify an
# "option-like" chunk exactly as the grader will, without importing the heavy evaluator module.
_OPTION_RE = re.compile(r'^\s*\(?\s*([A-Za-z]{1,3}|\d{1,2})\s*[\.\)\:\-/]\s*(.*)$', re.DOTALL)
_LABEL_PREFIX_RE = re.compile(
    r'^\s*(?:(?:q(?:ue|ues|uestion)?|ans(?:wer)?|sol(?:n|ution)?)\s*\.?\s*\d*|a\s*\.?\s*\d+)\s*[\.\)\:\-]?\s*',
    re.IGNORECASE)
_BARE_NUM_THEN_OPTION_RE = re.compile(r'^\s*\(?\s*\d{1,3}\s*[\.\)\:\-]\s*(\(?\s*[A-Za-z]\b.*)$', re.DOTALL)
# A line-leading objective-answer label: optional A/Q/Ans word, then the question number, then EITHER a
# separator OR (lookahead) an option bracket. The separator used to be mandatory, so 'Q16. (C)' parsed
# but 'Q.16 (C)', 'Q16 (C)' and a bare '16 (C)' did not -- on the real Maths_Class12 objective run NONE
# of the six lines matched, and this splitter could never fire on it. The bracket lookahead keeps the
# widening tight: a number must still be followed by a separator or by an option in brackets, so ordinary
# prose starting with a number ('16 students were surveyed') still does not qualify as a label.
# The marker/separator slots match _Q_PREFIX below (widened separators + the 'No' compound), so a
# student writing 'Q-16 (C)' or 'Q.No.16 (C)' is read the same way here as by the header matcher.
_OBJ_LABEL_RE = re.compile(
    r'^[ \t]*(?:(?:A|Q|Ans|Answ|Answer|Sol|Soln|Solution|Att)[ \t]*[.\-:,;#/_]?[ \t]*'
    r'(?:N(?:o|os|umber)?[ \t]*[.\-:]?[ \t]*)?)?(\d{1,3})[ \t]*'
    r'(?:[\.\)\:\-][ \t]*|(?=[\(\[]))(.*)$',
    re.IGNORECASE)
# A structural content tag that ends an objective list (e.g. a diagram drawn after the MCQ answers).
_TAIL_TAG_RE = re.compile(r'^\s*\[(?:DIAGRAM|CODE|TABLE|IMAGE)\b', re.IGNORECASE)
_MAX_OPTION_CHUNK = 120


def _is_mcq_type(type_str):
    t = (type_str or "").lower()
    return "mcq" in t or "objective" in t


def _parse_option_id(ans):
    """The option identifier (e.g. 'd') from an MCQ answer, mirroring evaluate.parse_option's leading
    label / question-number stripping. None when no clean leading option is present."""
    s = (ans or "").strip()
    if not s:
        return None
    stripped = _LABEL_PREFIX_RE.sub('', s, count=1).strip()
    if stripped != s:
        s = stripped
    if s:
        mb = _BARE_NUM_THEN_OPTION_RE.match(s)
        if mb:
            s = mb.group(1).strip()
    if not s:
        return None
    m = _OPTION_RE.match(s)
    if m:
        return m.group(1)
    if re.fullmatch(r'[A-Za-z]|\d{1,2}', s):
        return s
    return None


def _chunk_is_option_like(chunk):
    """True when a chunk reads as a single short MCQ option (parse finds a clean option id, or the
    chunk carries an explicit '(a)'..'(d)' marker). Gates the split so a prose answer is never split."""
    j = (chunk or "").strip()
    if not j or len(j) > _MAX_OPTION_CHUNK:
        return False
    if _parse_option_id(j):
        return True
    return bool(re.search(r'\(?[A-Da-d]\)', j))


def _mirror_page_mapping(page_mapping, src_key, new_keys):
    """Give each new_key the same page image(s) the src_key's question occupied, so diagram detection
    and the report image column localise the split/recovered answers. Matches by base number so a
    'Q22' source still maps even when page_mapping holds diagram sub-regions 'Q22.a'. No-op if absent.

    A source with NO base number ('_unassigned_', the orphan-page holder) is matched by EXACT key
    instead -- without this, questions split out of a rescued page would inherit no page image and
    their answer crops / diagram detection would silently have nothing to work from."""
    if not page_mapping:
        return
    src_base = _base_qnum(src_key)
    def _is_src(it):
        qid = it.get("question_id", "")
        return _base_qnum(qid) == src_base if src_base else qid == src_key
    for img_path, items in page_mapping.items():
        if not any(_is_src(it) for it in items):
            continue
        base_img = items[0].get("image") if items else os.path.basename(img_path)
        existing = {it.get("question_id") for it in items}
        for nk in new_keys:
            if nk not in existing:
                items.append({"question_id": nk, "image": base_img})


def _recompute_gaps(ocr_answers, valid_base_numbers):
    """In-set question numbers with no non-empty captured answer -- re-derives reconcile's gap set
    after the splitter/recovery change which questions are present."""
    if not valid_base_numbers:
        return []
    valid = {int(n) for n in valid_base_numbers}
    present = set()
    for k, v in ocr_answers.items():
        if k == "_instructions_":
            continue
        bn = _base_qnum(k)
        if bn and int(bn) in valid and str((v or {}).get("answer", "")).strip():
            present.add(int(bn))
    return sorted(valid - present)


def split_objective_answer_lists(ocr_answers, manual_db, valid_base_numbers, page_mapping=None):
    """Split ONE OCR entry that is an enumerated run of objective answers for CONSECUTIVE in-set MCQ
    question numbers (e.g. a block holding 'A1. (d).. A2. (b).. .. A6. (d)..') into separate per-
    question entries Q1..Q6, so each grades against its own MCQ key. Trailing non-list content (a
    '[DIAGRAM:]' tail) is re-homed to the source key so a diagram is never lost.

    CONSERVATIVE -- a block splits ONLY when, after grouping its label-led lines: there are >= 3
    groups; the numbers are strictly ascending, unique, ALL in valid_base_numbers AND ALL MCQ-typed
    in manual_db (a long-answer's '1. 2. 3.' sub-points are not MCQ-typed, so never split); every
    chunk is short + option-like; and no target Q<n> already holds a captured answer (never clobbers).
    Returns (ocr_answers, split_map {src_key: [new_keys]}). No-op when set / MCQ-types are empty."""
    valid = {int(n) for n in (valid_base_numbers or [])}
    mcq_bases = {int(_base_qnum(k)) for k, v in (manual_db or {}).items()
                 if isinstance(v, dict) and _is_mcq_type(v.get("type")) and _base_qnum(k)}
    split_map = {}
    if not valid or not mcq_bases:
        return ocr_answers, split_map

    for src_key in list(ocr_answers.keys()):
        if src_key == "_instructions_":
            continue
        entry = ocr_answers.get(src_key)
        if not isinstance(entry, dict):
            continue
        lines = (entry.get("answer", "") or "").split("\n")

        groups = []        # [num, [chunk lines], header_idx]
        tail_start = None  # first tail line ([DIAGRAM:]/[CODE:] after the list)
        for i, ln in enumerate(lines):
            m = _OBJ_LABEL_RE.match(ln)
            if m:
                groups.append([int(m.group(1)), [m.group(2)], i])
            elif groups and _TAIL_TAG_RE.match(ln):
                tail_start = i
                break
            elif groups:
                groups[-1][1].append(ln)
        if len(groups) < 3:
            continue
        nums = [g[0] for g in groups]
        if nums != sorted(nums) or len(set(nums)) != len(nums):
            continue
        if any(n not in valid or n not in mcq_bases for n in nums):
            continue
        chunks = ["\n".join(g[1]).strip() for g in groups]
        if not all(_chunk_is_option_like(c) for c in chunks):
            continue
        # Never overwrite a question that already captured its own answer -- but "already captured" must
        # mean INDEPENDENT evidence. Two members of a glued objective run are not: the HOST itself (whose
        # own answer is the first line of the list it is holding) and any slot already lifted OUT of this
        # same host by an earlier repair layer. Counting those as captured aborted the whole split -- on
        # the real Maths_Class12 Q15 blob, every other gate passed and this one blocked it because Q15
        # (the host) and Q16 (recovered from Q15 minutes earlier) were both filled.
        src_base = _base_qnum(src_key)
        def _independently_filled(n):
            e = ocr_answers.get(f"Q{n}")
            if not (isinstance(e, dict) and str(e.get("answer", "")).strip()):
                return False
            if src_base is not None and int(src_base) == n:
                return False                                   # the host holding the list
            if e.get("recovered_from") == src_key or e.get("split_from") == src_key:
                return False                                   # came out of this very host
            return True
        if any(_independently_filled(n) for n in nums):
            continue

        bad = bool(entry.get("is_bad_handwriting", False))
        new_keys = []
        for num, chunk in zip(nums, chunks):
            k = f"Q{num}"
            ocr_answers[k] = {"answer": chunk, "is_bad_handwriting": bad, "split_from": src_key}
            new_keys.append(k)
        tail = "\n".join(lines[tail_start:]).strip() if tail_start is not None else ""
        if tail:
            if src_key not in new_keys:
                ocr_answers[src_key] = {"answer": tail, "is_bad_handwriting": bad}
            else:  # rare: the list was welded into one of its own targets -> keep tail with last chunk
                ocr_answers[new_keys[-1]]["answer"] = (ocr_answers[new_keys[-1]]["answer"] + "\n" + tail).strip()
        elif src_key not in new_keys:
            ocr_answers.pop(src_key, None)
        _mirror_page_mapping(page_mapping, src_key, new_keys)
        split_map[src_key] = new_keys
    return ocr_answers, split_map


# --- Question-label grammar -----------------------------------------------------------------------
# Students combine a MARKER, a SEPARATOR, the NUMBER and a TERMINATOR, so the surface strings are
# effectively unbounded while the SLOTS are not. These constants are the slots. They are shared by the
# header MATCHER and the header STRIPPER below -- the two must never drift, because a strip pattern
# narrower than the match pattern leaves the student's own label sitting inside the recovered answer
# (measured once already: "Q.17 (D) 43" was stored instead of "(D) 43").
#
# MARKER -- Q-side and multi-letter answer-side. Bare single-letter 'A' is deliberately NOT here; it
# needs a mandatory terminator (see _A_PREFIX) or it eats matrix cofactor notation: 'A11 = -2',
# 'A21 = -(1)', 'A31 = ...' appear in three of the archived Maths runs and would each open a question.
_Q_MARKER = r'(?:Q|Ques|Question|Ans|Answ|Answer|Sol|Soln|Solution|Att)'
# SEPARATOR between marker and number: none, or one of . - : , ; # / _ (all observed in real scripts),
# optionally followed by a 'No'/'Number' compound ('Q No 17', 'Q.No.17', 'Question Number 17').
_Q_SEP = r'\s*[.\-:,;#/_]?\s*(?:N(?:o|os|umber)?\s*[.\-:]?\s*)?'
# Full prefix: an optional opening bracket, then marker + separator.
_Q_PREFIX = r'[\(\[]?\s*' + _Q_MARKER + _Q_SEP
# Bare 'A' branch -- same shape, but the terminator AFTER the number is REQUIRED, which is exactly what
# separates the label 'A1.' from the matrix element 'A11 = -2'.
_A_PREFIX = r'[\(\[]?\s*A' + _Q_SEP
# A number followed by a date tail ('12.5.2024', '1.1.2025') is a DATE, never a question header. Without
# this, pat_inline's numeric sub-part branch reads '12.' + '5' as "question 12, sub-part 5".
_DATE_TAIL = r'[./-]\s*\d{1,2}\s*[./-]\s*\d{2,4}\b'


def _qnum_header_idx(lines, n):
    r"""Index of the first line that is a header for question n -- a standalone '37.'/'Q37'/'37)', a
    line that begins 'n.' immediately followed by a sub-part marker, or a PREFIXED label ('Q37',
    '(Ques 37)', 'Q37. <answer text>', 'Ans 37)', 'A37.') where the marker makes it a header even
    without a trailing separator or with answer text on the same line. None when absent.

    Built on the shared slot constants above, so it accepts the full range of separators students use
    ('Q.37', 'Q-37', 'Q:37', 'Q No 37', 'Q.No.37') and the answer-side markers ('Ans 37)', 'Sol 37',
    'A37.'), not just 'Q37'. The old `(?:Q|Ques\.?|Question)` let only 'Ques' take a dot -- bare 'Q'
    could not -- so 'Q.37', the commonest handwritten form of all, matched NOTHING and blinded this
    whole layer to exactly the labels students write (measured: 18 buried 'Q.<n>' headers invisible
    against 43 matchable; fixing it recovered 10 answers that scored 0).

    Two guards keep the widening honest, both measured against the archived corpus:
      * a bare content number can still never open a slot ('30 cm', 'theta = 30', '= 30 x 22'),
      * a marker followed by '=' is a VARIABLE, not a label -- in physics 'Q1' and 'Q2' are charges."""
    pat_standalone = re.compile(r'^\s*(?:%s)?0*%d\s*[.\):]\s*$' % (_Q_PREFIX, n), re.IGNORECASE)
    # E12: recognise extended sub-part forms -- roman, ANY single letter (a-z, not just a-d), and a
    # numeric sub-part "(1)"/"(2)" -- so a gap header like "37. (vi)" / "37. (e)" / "37. (1)" is found.
    pat_inline = re.compile(r'^\s*0*%d\s*[.\):]\s*[\(\[]?\s*(?:[ivxIVX]+|[a-zA-Z]|\d{1,3})\b' % n)
    # Cross-page glue (Tier 1): a PREFIXED header the student wrote to open the question mid-page --
    # 'Q30' (no separator, own line) or '(Ques 32) Aarush...' (leading bracket + inline answer text).
    # A marker is REQUIRED here, so a bare content number can NEVER match -- only a student-written
    # label opens a slot. \b after the number keeps 'Q30' from matching 'Q300' (or n=3).
    pat_prefixed = re.compile(r'^\s*%s0*%d\b\s*[.\):\-]?' % (_Q_PREFIX, n), re.IGNORECASE)
    pat_bare_a = re.compile(r'^\s*%s0*%d\s*[.\):\-]' % (_A_PREFIX, n), re.IGNORECASE)
    pat_date = re.compile(r'^\s*(?:%s)?0*%d\s*%s' % (_Q_PREFIX, n, _DATE_TAIL), re.IGNORECASE)
    pat_variable = re.compile(r'^\s*%s0*%d\b\s*=' % (_Q_PREFIX, n), re.IGNORECASE)
    for i, ln in enumerate(lines):
        if pat_date.match(ln) or pat_variable.match(ln):
            continue                                    # a date or an equation, not a question label
        if (pat_standalone.match(ln) or pat_prefixed.match(ln)
                or pat_bare_a.match(ln) or pat_inline.match(ln)):
            return i
    return None


def _next_qnum_header_idx(lines, start):
    """Index after `start` of the next standalone numeric question header ('38.'), else len(lines).
    Bounds a lifted fragment so it stops at the following question."""
    pat = re.compile(r'^\s*0*\d{1,3}\s*[.\):]\s*$')
    for j in range(start + 1, len(lines)):
        if pat.match(lines[j]):
            return j
    return len(lines)


def _starts_with_later_subpart(text):
    """True when `text` BEGINS with a CONTINUATION sub-part marker -- a later roman (ii, iii, iv, ..),
    a letter past 'a' (b..z), or a number past 1 (2..) -- but NOT an OPENING marker (a)/(i)/(1). A
    well-formed answer opens at its first sub-part, so a LATER marker appearing first is the tell-tale
    of a continuation the page-break swallowed from the previous question."""
    m = re.match(r'\s*[\(\[]?\s*([A-Za-z]+|\d+)\s*[\)\.\]\:]', text or "")
    if not m:
        return False
    tok = m.group(1).lower()
    if tok in ('a', 'i', '1'):                          # opening markers -> a fresh answer, not a tail
        return False
    if re.fullmatch(r'i{2,3}|iv|v|vi{0,3}|ix|x', tok):  # later roman numerals (ii..x)
        return True
    if re.fullmatch(r'[b-z]', tok):                     # later single letter (b..z)
        return True
    return tok.isdigit() and int(tok) >= 2              # later number (2..)


def _first_top_level_a_pos(text):
    """Position of the first LINE-LEADING top-level opener '(a)'/'a)'/'a.' (lowercase 'a' only -- the
    canonical CBSE part-(a) label) that is NOT at offset 0 -- i.e. where the next question's OWN answer
    begins. Lowercase + line-leading on purpose: it must not match a sub-enumerator '(I)'/'(1)' nor a
    prose 'A.'. None when absent (so an answer with no clean part-(a) split point is left untouched)."""
    for m in re.finditer(r'(?m)^[ \t]*[\(\[]?[ \t]*a[ \t]*[\)\.\]\:]', text or ""):
        if m.start() > 0:
            return m.start()
    return None


def reattach_leading_continuation(ocr_answers, valid_base_numbers, page_mapping=None):
    """Layer 2 (page-break continuation drift): when the OCR opened a question's [START_Q] one boundary
    too EARLY at a page break, the PREVIOUS question's trailing sub-part(s) get swallowed as a LEADING
    fragment of the next question (observed: Q34's (a)(iii) equations captured at the top of Q35, whose
    answer therefore begins '(iii) ...' before its own '(a)'). This moves such a leading fragment back
    to the immediately-preceding in-set question.

    Deterministic, structural, no LLM / no key. Fires for an in-set Q(n) ONLY when ALL hold, so it can
    only re-home a misplaced continuation, never split a well-formed answer:
      * Q(n)'s answer BEGINS with a CONTINUATION sub-part ((ii)/(iii)/(b)/(2)..), not an opener,
      * Q(n)'s OWN part '(a)' appears LATER in the text -> a clean, unambiguous split point,
      * the immediately-preceding question Q(n-1) is present (the continuation's true owner),
      * the split leaves BOTH slots non-empty (never empties Q(n), never fabricates).
    Returns (ocr_answers, moved:list[int], flagged:sorted list[int]). No-op when the set is empty."""
    if not valid_base_numbers:
        return ocr_answers, [], []
    valid = {int(n) for n in valid_base_numbers}
    present = {}
    for k, v in ocr_answers.items():
        if k == "_instructions_" or not isinstance(v, dict):
            continue
        b = _base_qnum(k)
        if b is not None and int(b) in valid and str(v.get("answer", "")).strip():
            present.setdefault(int(b), k)               # first (base) key per question number
    moved, flagged = [], set()
    for n in sorted(present):
        if (n - 1) not in present:
            continue
        key_n = present[n]
        text = ocr_answers[key_n]["answer"]
        if not _starts_with_later_subpart(text):
            continue
        pos = _first_top_level_a_pos(text)
        if pos is None or pos <= 0:
            continue
        prefix = text[:pos].strip()
        remainder = text[pos:].strip()
        if not prefix or not remainder:                 # never empty either slot
            continue
        prev_key = present[n - 1]
        prev_entry = ocr_answers[prev_key]
        bad = bool(prev_entry.get("is_bad_handwriting", False)) or \
            bool(ocr_answers[key_n].get("is_bad_handwriting", False))
        ocr_answers[prev_key] = {"answer": (str(prev_entry.get("answer", "")).rstrip() + "\n" + prefix).strip(),
                                 "is_bad_handwriting": bad}
        ocr_answers[key_n] = {"answer": remainder,
                              "is_bad_handwriting": bool(ocr_answers[key_n].get("is_bad_handwriting", False))}
        _mirror_page_mapping(page_mapping, key_n, [prev_key])
        moved.append(n)
        flagged.update((n - 1, n))
    return ocr_answers, moved, sorted(flagged)


def recover_gaps_by_position(ocr_answers, gap_numbers, page_mapping, manual_db):
    """For each in-set GAP question n (captured nothing), find a host answer containing a fragment
    LITERALLY headed by n's own number ('37.'/'Q37') and LIFT that fragment into Q<n>, leaving the
    host's own content intact. STRICTLY ADDITIVE + opportunistic: never empties a host (substantial
    content must remain), only lifts text the student themselves numbered n (cannot steal a neighbour),
    fabricates nothing (a gap with no number-bearing fragment stays BLANK). Returns
    (ocr_answers, recovered:list[int], flagged:sorted list[int], still_gap:list[int])."""
    recovered, flagged = [], set()
    still = [int(n) for n in (gap_numbers or [])]
    if not still:
        return ocr_answers, recovered, sorted(flagged), still
    for n in sorted(still):
        host_key = None
        for k in sorted(ocr_answers.keys(), key=natural_sort_key):
            bk = _base_qnum(k)
            if k == "_instructions_" or (bk and int(bk) == n):
                continue
            v = ocr_answers.get(k)
            if not isinstance(v, dict) or not str(v.get("answer", "")).strip():
                continue
            lines = v["answer"].split("\n")
            hidx = _qnum_header_idx(lines, n)
            if hidx is None:
                continue
            eidx = _next_qnum_header_idx(lines, hidx)
            frag = "\n".join(lines[hidx:eidx]).strip()
            # Strip the header the student wrote. Built from the SAME slot constants as
            # _qnum_header_idx (_Q_PREFIX / _A_PREFIX) so the two can never drift: a strip pattern
            # narrower than the match pattern leaves the label sitting inside the recovered answer
            # ("Q.17 (D) 43" instead of "(D) 43", or "Ans 21) a3b..." instead of "a3b...").
            frag = re.sub(r'^\s*(?:%s|%s)?0*%d\s*[.\):\-]?\s*' % (_Q_PREFIX, _A_PREFIX, n),
                          '', frag, count=1).strip()
            remaining = "\n".join(lines[:hidx] + lines[eidx:]).strip()
            if not frag or not remaining:     # never empty the host / never lift nothing
                continue
            bad = bool(v.get("is_bad_handwriting", False))
            # Rewriting the host must not drop its OWN provenance. A run of glued answers is recovered as
            # a CHAIN -- Q17 is lifted out of Q15, then Q18 out of Q17, and so on -- so a host is often a
            # slot that was itself just recovered. Rebuilding it as a bare {answer, is_bad_handwriting}
            # erased 'recovered_from'/'rehomed_to'/'split_from' and the report lost the badge for every
            # link but the last.
            _keep = {kk: vv for kk, vv in v.items() if kk not in ("answer", "is_bad_handwriting")}
            ocr_answers[k] = dict(_keep, answer=remaining, is_bad_handwriting=bad)
            ocr_answers[f"Q{n}"] = {"answer": frag, "is_bad_handwriting": bad, "recovered_from": k}
            _mirror_page_mapping(page_mapping, k, [f"Q{n}"])
            host_key = k
            break
        if host_key is not None:
            recovered.append(n)
            flagged.add(n)
            hb = _base_qnum(host_key)
            if hb:
                flagged.add(int(hb))
    still = [n for n in still if n not in recovered]
    return ocr_answers, recovered, sorted(flagged), still


def _append_mixed_answer_flags(ocr_dir, bases):
    """Append question base-numbers to the mixed-answer sidecar (mixed_answer_flags.json) the grader
    reads to raise 'Needs Review' on a slot whose boundary was uncertain. Merges with existing flags
    (from the OCR collision pass); no-op on empty / unwritable."""
    bases = [int(b) for b in (bases or [])]
    if not bases:
        return
    path = os.path.join(ocr_dir, "mixed_answer_flags.json")
    cur = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except Exception:
            cur = []
    merged = sorted({int(x) for x in (cur or [])} | set(bases))
    try:
        with open(path, "w") as f:
            json.dump(merged, f)
    except OSError:
        pass


def _append_recovery_flags(ocr_dir, reasons):
    """Record WHY each rescued question was rescued, in `recovery_flags.json` = {base: reason}.

    Separate from mixed_answer_flags.json on purpose: that flag means "this slot may MERGE two
    questions' answers (a misread number)", which is the wrong thing to tell a teacher about an answer
    that was successfully lifted back into its own slot. Both raise Needs Review; only the wording
    differs. Merges with any existing file; first reason for a base wins (the earliest layer to touch
    it is the one that actually moved the text). No-op on empty / unwritable."""
    reasons = {str(_base_qnum(k) or k): v for k, v in (reasons or {}).items() if v}
    if not reasons:
        return
    path = os.path.join(ocr_dir, "recovery_flags.json")
    cur = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
    if not isinstance(cur, dict):
        cur = {}
    for k, v in reasons.items():
        cur.setdefault(k, v)
    try:
        with open(path, "w") as f:
            json.dump(cur, f, indent=2)
    except OSError:
        pass


def _append_symbol_flags(ocr_dir, notes):
    """Merge {base: note} into `symbol_flags.json` -- the channel run_ocr uses for OCR findings that
    are NOT about handwriting legibility. Same shape and merge rule as _append_recovery_flags (first
    note for a base wins); evaluate._apply_symbol_flags reads it. No-op on empty / unwritable."""
    notes = {str(_base_qnum(k) or k): v for k, v in (notes or {}).items() if v}
    if not notes:
        return
    path = os.path.join(ocr_dir, "symbol_flags.json")
    cur = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
    if not isinstance(cur, dict):
        cur = {}
    for k, v in notes.items():
        cur.setdefault(k, v)
    try:
        with open(path, "w") as f:
            json.dump(cur, f, indent=2)
    except OSError:
        pass


def _expected_for(manual_db, qkey):
    """Concatenated expected-answer text for a base question (aggregating any sub-part key entries),
    capped for prompt use. '' when absent."""
    base = _base_qnum(qkey)
    if base is None:
        return ""
    parts = []
    for k, v in (manual_db or {}).items():
        if k == "_instructions_" or not isinstance(v, dict):
            continue
        if _base_qnum(k) == base:
            a = str(v.get("answer", "") or "").strip()
            if a:
                parts.append(a)
    return "\n".join(parts)[:800]


def _key_affinity(answer, expected):
    """Fraction of the KEY's word-tokens that appear in `answer` (0.0..1.0) -- a cheap, length-robust,
    LLM-free proxy for 'does this text answer that question'. 0.0 when the key is empty. Used to spot a
    slot whose captured answer is OFF-TOPIC for its own question but a strong fit for a blank one."""
    exp = set(re.findall(r"[a-z0-9_]+", str(expected or "").lower()))
    if not exp:
        return 0.0
    ans = set(re.findall(r"[a-z0-9_]+", str(answer or "").lower()))
    return len(ans & exp) / len(exp)


def _offtopic_rehome_hosts(ocr_answers, manual_db, blanks, valid):
    """Filled in-set slots whose captured answer is OFF-TOPIC for their OWN question yet a strong match
    for a still-BLANK question -- the fingerprint of a whole answer displaced into a DIFFERENT valid slot
    by a digit misread (e.g. Q34's SQL captured under the misread label '24'). Number-INDEPENDENT, so it
    reaches a displaced answer ANY distance from its blank -- the gap the +/-1 neighbour probe misses.

    A slot h qualifies only when some blank b satisfies BOTH: affinity(h, key[b]) >= MIN_CROSS (h really
    fits b) AND affinity(h, key[b]) - affinity(h, key[h]) >= MARGIN (h fits b better than its OWN
    question). The two guards keep it conservative -- an on-topic slot (high self-affinity) never
    qualifies -- and the downstream LLM matcher is still the final gate before any write. Returns host
    base numbers ranked by that margin, best first. PURE / offline: adds no LLM calls itself (only the
    ranked hosts it returns get the existing matcher probe, bounded by GLUE_MAX_PROBES)."""
    blanks = [int(b) for b in (blanks or [])]
    if not blanks:
        return []
    try:
        min_cross = float(os.environ.get("GLUE_OFFTOPIC_MIN_CROSS", "0.5"))
    except ValueError:
        min_cross = 0.5
    try:
        margin = float(os.environ.get("GLUE_OFFTOPIC_MARGIN", "0.25"))
    except ValueError:
        margin = 0.25
    blank_keys = [_expected_for(manual_db, f"Q{b}") for b in blanks]
    scored = []
    for h in sorted(int(n) for n in (valid or [])):
        if h in blanks:
            continue
        entry = ocr_answers.get(f"Q{h}")
        ans = str(entry.get("answer", "")).strip() if isinstance(entry, dict) else ""
        if not ans:
            continue
        self_aff = _key_affinity(ans, _expected_for(manual_db, f"Q{h}"))
        best_cross = max((_key_affinity(ans, exp) for exp in blank_keys), default=0.0)
        if best_cross >= min_cross and best_cross - self_aff >= margin:
            scored.append((best_cross - self_aff, h))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [h for _s, h in scored]


def _llm_glue_matcher(block_text, s_key, s_expected, candidates):
    """Default matcher for repair_glued_answers: ask a cheap LLM whether a CONTIGUOUS portion of a
    glued block actually answers one of the currently-unanswered questions. Returns
    (target_qid|None, foreign_text). Spends a few hundred tokens only when a glued slot exists. Raises
    on any failure so the caller leaves the slot flagged (never fabricates)."""
    from llm_client import generate, strip_reasoning
    # Content-matching a misread/glued fragment against the key needs a CAPABLE model -- the 30B
    # returns NONE on clear matches (e.g. a BPT proof glued under a misread '24' label). Default to the
    # proven 235B OCR/key tier; GLUE_MATCHER_MODEL overrides. Only fires on gaps, bounded by GLUE_MAX_PROBES.
    model = os.environ.get("GLUE_MATCHER_MODEL",
                           os.environ.get("SEGMENT_REPAIR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"))
    # Block cap: the old hardcoded 1500 truncated a real host (Maths_OSD Q36 is 2423 chars) so its
    # appended foreign answer was never even shown to the model. Still bounded, so a pathological block
    # cannot blow up the prompt.
    try:
        block_chars = int(os.environ.get("GLUE_BLOCK_CHARS", "6000"))
    except ValueError:
        block_chars = 6000
    cand_lines = "\n".join(f"{cid}: {(cexp or '(no expected answer on file)')[:300]}"
                           for cid, cexp in candidates)
    prompt = (
        "An OCR step sometimes GLUES two answers into one block. Decide whether a part of the block "
        "below actually answers a DIFFERENT question that was left unanswered.\n\n"
        f"The block was captured for {s_key}. {s_key}'s expected answer:\n{(s_expected or '')[:500]}\n\n"
        f"CAPTURED BLOCK ({s_key}):\n---\n{(block_text or '')[:block_chars]}\n---\n\n"
        f"These questions are currently UNANSWERED (id: expected answer):\n{cand_lines}\n\n"
        # The COMMON case is a block that holds BOTH answers: the student answered s_key, then answered
        # the next question, and the OCR never emitted a boundary between them. Saying only "if the whole
        # block answers s_key, return NONE" made the model reject exactly that shape -- measured on
        # Computer_Science_Class 12, where Q33's Python and Q34's SQL share one slot and it returned NONE.
        f"The block very often contains {s_key}'s OWN answer FOLLOWED BY another question's answer, "
        "because the OCR missed the boundary between them. Finding the host's own answer in there is "
        "therefore EXPECTED and is NOT a reason to answer NONE -- look past it for a portion that "
        "belongs to one of the unanswered questions listed above.\n"
        "If a CONTIGUOUS portion of the block clearly answers ONE of the unanswered questions, return "
        "that portion VERBATIM plus its id. Return target \"NONE\" only when NO portion of the block "
        f"answers any listed question -- i.e. the block is entirely {s_key}'s own work. "
        'Respond with ONLY JSON: '
        '{"target": "<question id or NONE>", "foreign_text": "<verbatim portion or empty>"}'
    )
    text, _i, _o = generate(model=model, prompt=prompt, temperature=0.0, max_tokens=2048, json_mode=True)
    if text and "<think" in str(text).lower():
        text = strip_reasoning(text)
    m = re.search(r'\{.*\}', text or "", re.DOTALL)
    if not m:
        return None, ""
    d = json.loads(m.group(0))
    target = str(d.get("target", "")).strip()
    foreign = str(d.get("foreign_text", "")).strip()
    if not target or target.upper() == "NONE":
        return None, ""
    return target, foreign


def _glue_hosts(ocr_answers, manual_db, blanks, valid, mixed_flags,
                probe_neighbors=True, probe_offtopic=True):
    """The slots worth asking the matcher about, for the CURRENT set of blanks, best-first.

    Sources, in probe order: the OCR-flagged glued slots (`mixed_flags`), then -- with probe_neighbors --
    the in-set NEIGHBOUR(s) (n-1 / n+1) of each blank that carry content (a silent glue sits in the slot
    just before/after its gap), then -- with probe_offtopic -- slots whose content is off-topic for their
    OWN question yet fits a blank one (`_offtopic_rehome_hosts`), which reaches an answer displaced ANY
    distance by a digit misread. Ranked so the clearest misplacement is probed before the probe cap.

    Extracted from repair_glued_answers so it can be recomputed EACH ROUND: the host set depends on which
    questions are still blank, and a slot that gets filled mid-run may itself be hiding the next answer.
    Pure -- reads only, no LLM calls."""
    def _has_content(nn):
        e = ocr_answers.get(f"Q{nn}")
        return isinstance(e, dict) and bool(str(e.get("answer", "")).strip())

    hosts = {int(f) for f in (mixed_flags or []) if int(f) in valid and _has_content(int(f))}
    if probe_neighbors:
        for b in blanks:
            for cand in (b - 1, b + 1):
                if cand in valid and cand not in blanks and _has_content(cand):
                    hosts.add(cand)
    ordered_hosts = sorted(hosts)
    if probe_offtopic:
        for h in _offtopic_rehome_hosts(ocr_answers, manual_db, blanks, valid):
            if h not in hosts:
                hosts.add(h)
                ordered_hosts.append(h)
    return ordered_hosts


def repair_glued_answers(ocr_answers, manual_db, valid_base_numbers, mixed_flags,
                         page_mapping=None, matcher=None, probe_neighbors=True, max_probes=None,
                         probe_offtopic=True):
    """Approach 1 (audit E5) + Tier 2 neighbour probe + Tier 3 off-topic re-home: recover a question
    whose answer the OCR GLUED (or mislabelled) into another slot, using the answer KEY to decide where
    an embedded foreign fragment belongs.

    Hosts examined = the OCR-flagged glued slots (mixed_flags) PLUS -- when probe_neighbors is on --
    the in-set NEIGHBOUR(s) (n-1 / n+1) of each still-BLANK gap that carry content, PLUS -- when
    probe_offtopic is on -- any FILLED slot whose content is OFF-TOPIC for its own question yet fits a
    blank one (_offtopic_rehome_hosts). The neighbour source catches a SILENT glue that sits in the
    slot just before/after a blank; the off-topic source catches a whole answer displaced into a
    DIFFERENT, non-adjacent valid slot by a digit misread ('34' read as '24' lands 10 slots from Q34,
    past the +/-1 probe). The number-independent, key-based matcher then lifts it either way.

    RUNS TO A FIXPOINT. The matcher returns ONE (target, foreign) per call, so a slot holding N foreign
    answers used to yield exactly one and be abandoned -- measured: Q15 held Q16..Q20 and only Q16 came
    back. Hosts were also chosen ONCE from the initial blank set, so a slot filled mid-run could never
    become a host. Now each ROUND recomputes the hosts from the CURRENT blanks and re-asks a host whose
    candidate set has shrunk; a round that recovers nothing ends the loop. Termination is guaranteed
    three ways: every write removes a blank (monotonic), a no-progress round breaks, and max_probes caps
    total calls. Attempts are counted per (host, current blanks) pair and capped at GLUE_HOST_ATTEMPTS,
    so an unchanged question is asked at most that many times -- the matcher is measurably unstable on
    some blocks, so one retry is worth it, but it can never spin.

    STRICTLY ADDITIVE + non-degrading: only ever FILLS a currently-BLANK in-set target, NEVER edits the
    source, and a WRONG match still writes to a slot that was 0 -- so the paper total can only rise or
    stay equal. That contract is what makes a larger probe budget safe. Bounded to max_probes matcher
    calls (default env GLUE_MAX_PROBES or 24; probes fire only where questions are actually missing, so
    a clean sheet costs nothing). A matcher exception leaves the slot flagged (no fabrication).
    Returns (ocr_answers, recovered, flagged)."""
    valid = {int(n) for n in (valid_base_numbers or [])}
    if not valid:
        return ocr_answers, [], []
    blanks = set(_recompute_gaps(ocr_answers, valid_base_numbers))
    if not blanks:
        return ocr_answers, [], []
    if max_probes is None:
        try:
            max_probes = int(os.environ.get("GLUE_MAX_PROBES", "24"))
        except ValueError:
            max_probes = 24
    if matcher is None:
        matcher = _llm_glue_matcher

    try:
        host_attempts = max(1, int(os.environ.get("GLUE_HOST_ATTEMPTS", "2")))
    except ValueError:
        host_attempts = 2
    recovered, flagged, probes = [], set(), 0
    # How many times each (host, candidate-set) pair has been asked with no result. Re-asking an
    # UNCHANGED question would normally be waste -- but the matcher is measurably UNSTABLE on some
    # blocks: sampled three times on the real Maths_Class12 Q37 block it returned NONE, NONE, Q38 (and
    # the previous prompt returned unparseable JSON 3/3). So each pair gets GLUE_HOST_ATTEMPTS tries
    # before it is written off. Set it to 1 for one-shot behaviour. Cost only rises where a question is
    # genuinely missing, and the cap still bounds the total.
    attempts = {}
    while blanks and probes < max_probes:
        # RECOMPUTED every round, not once up front. A slot that was blank at the start could not be a
        # host; after it is filled it may itself hold the NEXT buried answer, and _offtopic_rehome_hosts
        # re-ranks against the smaller blank set.
        ordered_hosts = _glue_hosts(ocr_answers, manual_db, blanks, valid, mixed_flags,
                                    probe_neighbors, probe_offtopic)
        if not ordered_hosts:
            break
        progressed = False
        # BREADTH-FIRST retries: pass 1 asks every host once, pass 2 re-asks only those that yielded
        # nothing. Retrying a host twice before its neighbours were asked at all starved the probe cap --
        # measured: Maths_Class12 spent all 24 probes and stopped BEFORE reaching Q37, the one slot
        # actually holding Q38's answer.
        for _pass in range(host_attempts):
            if not blanks or probes >= max_probes:
                break
            for n in ordered_hosts:
                if not blanks:
                    break
                if probes >= max_probes:
                    print(f"  glue-repair: hit probe cap ({max_probes}); stopped before Q{n}.")
                    break
                s_key = f"Q{n}"
                s_entry = ocr_answers.get(s_key)
                if not isinstance(s_entry, dict) or not str(s_entry.get("answer", "")).strip():
                    continue
                candidates = [(f"Q{b}", _expected_for(manual_db, f"Q{b}"))
                              for b in sorted(blanks) if b != n]
                if not candidates:
                    continue
                sig = (n, frozenset(blanks))
                if attempts.get(sig, 0) >= host_attempts:
                    continue                      # already had its budget for this candidate set
                attempts[sig] = attempts.get(sig, 0) + 1
                probes += 1
                try:
                    target, foreign = matcher(s_entry["answer"], s_key,
                                              _expected_for(manual_db, s_key), candidates)
                except Exception as e:
                    print(f"  glue-repair: matcher failed for {s_key} ({type(e).__name__}); "
                          f"leaving it flagged.")
                    continue
                if not target or not str(foreign or "").strip():
                    continue
                tb = _base_qnum(target)
                if tb is None:
                    continue
                tb = int(tb)
                # additive guardrails: the target must be a CURRENT blank in-set question, not the source.
                if tb == n or tb not in blanks:
                    continue
                tkey = f"Q{tb}"
                if str((ocr_answers.get(tkey) or {}).get("answer", "")).strip():
                    continue
                ocr_answers[tkey] = {"answer": str(foreign).strip(),
                                     "is_bad_handwriting": bool(s_entry.get("is_bad_handwriting", False)),
                                     "recovered_from": s_key}
                # Annotate the SOURCE slot additively (never touches its answer text) so the report can show
                # the displaced copy was matched + graded elsewhere. List-valued: one slot can seed >1 recovery.
                _rh = s_entry.get("rehomed_to")
                _rh = list(_rh) if isinstance(_rh, list) else ([] if _rh in (None, "") else [_rh])
                if tkey not in _rh:
                    _rh.append(tkey)
                s_entry["rehomed_to"] = _rh
                _mirror_page_mapping(page_mapping, s_key, [tkey])
                blanks.discard(tb)
                recovered.append(tb)
                flagged.update((tb, n))
                progressed = True
        if not progressed:          # a whole round asked nothing new -> nothing left to find
            break
    return ocr_answers, recovered, sorted(flagged)


def detect_choice_groups(manual_db):
    """Structural fallback when the key carries no parser-supplied choice metadata.
    - separate-entry OR: sibling entries 'Qn(a)'/'Qn(b)' with EQUAL marks -> an 'answer any 1' group.
    - inline OR: a single entry whose question/answer text contains a standalone uppercase 'OR'.
    Both are heuristic, so their ids go in `uncertain` to be flagged for manual review.
    Returns {"choice_groups": [...], "inline_choice_ids": [...], "uncertain": set()}.
    """
    by_base = {}
    for qid, v in (manual_db or {}).items():
        if qid == "_instructions_" or not isinstance(v, dict):
            continue
        m = re.fullmatch(r'(Q\d+)\s*\(\s*([A-Za-z0-9]+)\s*\)', str(qid).strip())
        if m:
            by_base.setdefault(m.group(1), []).append(qid)

    choice_groups, uncertain, grouped_ids = [], set(), set()
    for base, members in by_base.items():
        if len(members) < 2:
            continue
        marks = {float(manual_db[mm].get("marks", 0) or 0) for mm in members}
        if len(marks) == 1:  # equal marks across siblings -> internal choice (answer any one)
            members = sorted(members, key=natural_sort_key)
            choice_groups.append({"parent": base, "members": members, "required": 1})
            uncertain.add(base)
            grouped_ids.update(members)

    inline_ids = []
    for qid, v in (manual_db or {}).items():
        if qid == "_instructions_" or not isinstance(v, dict) or qid in grouped_ids:
            continue
        blob = f"{v.get('question', '')}\n{v.get('answer', '')}"
        if re.search(r'(?:^|\s)OR(?:\s|$)', blob):  # standalone, uppercase 'OR'
            inline_ids.append(qid)
            uncertain.add(qid)

    return {"choice_groups": choice_groups, "inline_choice_ids": inline_ids, "uncertain": uncertain}


def _load_or_detect_choices(answer_key_path, manual_db):
    """Prefer the parser-written sidecar (current_answer_key_choices.json beside the key);
    fall back to structural detection when it's absent/empty (e.g. a raw .json key upload)."""
    sidecar = os.path.join(os.path.dirname(answer_key_path), "current_answer_key_choices.json")
    if os.path.exists(sidecar):
        try:
            with open(sidecar, encoding="utf-8") as f:
                data = json.load(f) or {}
            groups = data.get("choice_groups") or []
            inline = data.get("inline_choice_ids") or []
            if groups or inline:  # parser-supplied -> authoritative (not uncertain)
                return {"choice_groups": groups, "inline_choice_ids": inline, "uncertain": set()}
        except Exception as e:
            print(f"Warning: could not read choices sidecar: {e}")
    return detect_choice_groups(manual_db)


def _overlay_question_paper(manual_db, question_paper_path):
    """Additively enrich the answer-key entries with the fuller question TEXT from the uploaded
    question paper, in place on manual_db. The teacher's answer key remains the SOLE source of
    expected answers and marks (there is no database / no DB fallback): ONLY the 'question' field
    is ever overlaid; answer/marks/type/subject are never touched. No-op when no question paper is
    present (so the CLI/legacy path behaves exactly as before). Matches by normalized id, then by
    base question number, so the paper's 'Q31' enriches the key's 'Q31(a)'/'Q31(b)'."""
    if not question_paper_path or not os.path.exists(question_paper_path):
        return manual_db
    try:
        with open(question_paper_path, encoding="utf-8") as f:
            qp = json.load(f)
    except Exception as e:
        print(f"Warning: could not read question paper, grading with the answer key only: {e}")
        return manual_db
    if not isinstance(qp, dict):
        return manual_db

    # Index the paper's question text by normalized id AND by base question number (parent wins).
    by_norm, by_base = {}, {}
    for qk, qv in qp.items():
        if qk == "_instructions_" or not isinstance(qv, dict):
            continue
        qtext = (qv.get("question") or "").strip()
        if not qtext:
            continue
        nqk = normalize_qid(qk)
        by_norm[nqk] = qtext
        bn = _base_qnum(nqk)
        if bn is not None:
            by_base.setdefault(bn, qtext)   # first (parent) match wins for the fallback

    enriched = 0
    for mk, mv in manual_db.items():
        if mk == "_instructions_" or not isinstance(mv, dict):
            continue
        nmk = normalize_qid(mk)
        text = by_norm.get(nmk)
        if not text:
            bn = _base_qnum(nmk)
            text = by_base.get(bn) if bn is not None else None
        if text:                     # overlay only when the paper has non-empty text for it
            mv["question"] = text
            enriched += 1
    if enriched:
        print(f"Enriched {enriched} answer-key entries with question-paper context.")
    return manual_db


def _choice_label(member_id):
    """The marker a student writes to pick an alternative = the LAST parenthesised token of the member
    id: 'Q31(A)'->'A', 'Q34(IV)(A)'->'A', 'Q28(b)'->'b'. Used to show only the attempted alternative."""
    toks = re.findall(r"\(([A-Za-z0-9]+)\)", str(member_id))
    return toks[-1] if toks else ""


def merge_choice_groups(ocr_path, db_path, manual_db, choice_groups):
    """Collapse each 'answer any ONE' choice group (e.g. Q31(a) OR Q31(b)) into a single parent
    key, so the student's chosen answer (often OCR'd as the bare 'Q31') is graded against a real
    key entry and the alternatives count ONCE. Mirrors merge_subparts_into_parents. The merged
    expected answer joins the alternatives with 'OR' and the entry is tagged is_choice=True.
    Returns the merged parent ids.
    """
    groups = [g for g in (choice_groups or []) if int(g.get("required", 1) or 1) == 1]
    if not groups:
        return []
    with open(ocr_path, encoding="utf-8") as f:
        ocr_data = json.load(f)
    with open(db_path, encoding="utf-8") as f:
        db_data = json.load(f)

    merged = []
    for g in groups:
        raw_members = g.get("members") or []
        parent = g.get("parent")
        # The marks-editor (and hand-pasted keys) may omit `parent`; derive it from the members'
        # shared base ('Q22(A)' -> 'Q22') so the group still collapses. Without this the choice is
        # silently skipped and the additive merge SUMS both alternatives -> inflated denominator
        # (e.g. a confirmed 70 grading out of 92).
        if not parent and raw_members:
            _b = _base_qnum(normalize_qid(raw_members[0]))
            parent = f"Q{_b}" if _b is not None else None
        if not parent or len(raw_members) < 2:
            continue
        base_num = _base_qnum(normalize_qid(parent))
        if base_num is None:
            continue

        # ALL key leaves for this base (marks come from the authoritative key = manual_db).
        base_leaves = [k for k in manual_db
                       if k != "_instructions_" and _base_qnum(normalize_qid(k)) == base_num]
        if not base_leaves:
            continue

        # Resolve each choice member to the leaves UNDER it, PREFIX-tolerantly: 'Q34(a)' claims
        # 'Q34(a)(i)'... even when the parser split the alternative deeper than the member id. This
        # is the fix for the choice that silently counted BOTH alternatives (10 not 5 -> the 99.5 bug).
        member_leaves, claimed = [], set()
        for m in raw_members:
            leaves = [k for k in base_leaves if _is_under(k, m)]
            if leaves:
                member_leaves.append((m, sorted(leaves, key=natural_sort_key)))
                claimed.update(leaves)
        if len(member_leaves) < 2:
            continue   # couldn't resolve >=2 alternatives -> leave for the additive merge / reconciler

        # COMMON leaves belong to NO alternative -- additive parts answered regardless of the choice
        # (e.g. the (a),(b) parts of a case study whose OR is only in (c)); they are SUMMED, not maxed.
        common = [k for k in base_leaves if k not in claimed]

        def _mk(k):
            return _safe_float(manual_db[k].get("marks"))

        def _lbl_ans(k):
            sp = subpart_of(normalize_qid(k))
            return (f"{sp} " if sp else "") + str(manual_db[k].get("answer", "")).strip()

        alt_sums = [sum(_mk(k) for k in leaves) for _m, leaves in member_leaves]
        marks = sum(_mk(k) for k in common) + max(alt_sums)   # additive commons + best single alternative

        # Expected answer: the shared additive parts, then the OR-joined alternatives.
        shared_txt = " ".join(_lbl_ans(k) for k in sorted(common, key=natural_sort_key))
        or_block = "\nOR\n".join(" ".join(_lbl_ans(k) for k in leaves) for _m, leaves in member_leaves)
        answer = ((shared_txt + "\n") if shared_txt else "") + or_block

        # Gather the student's answer from every OCR key sharing this base number.
        gathered, bad = [], False
        for ok in list(ocr_data.keys()):
            if ok == "_instructions_":
                continue
            if _base_qnum(normalize_qid(ok)) == base_num:
                entry = ocr_data.pop(ok)
                if not isinstance(entry, dict):
                    entry = {"answer": str(entry)}
                ans = (entry.get("answer", "") or "").strip()
                if ans and ans.upper() not in ("[BLANK]", "NA", "N/A", "NONE"):
                    gathered.append(ans)
                bad = bad or bool(entry.get("is_bad_handwriting", False))

        entry = dict(manual_db[base_leaves[0]])
        entry["question_id"] = parent
        entry["marks"] = marks
        entry["answer"] = answer.strip()
        entry["is_choice"] = True
        # Structured per-alternative answers (+ any shared additive parts) so the REPORT can show ONLY
        # the alternative the student attempted instead of the whole 'A OR B'. Display-only; the graded
        # `answer` above still carries every alternative for the grader to match against.
        entry["choice_shared"] = shared_txt
        entry["choice_alternatives"] = [
            {"label": _choice_label(_m), "answer": " ".join(_lbl_ans(k) for k in leaves).strip()}
            for _m, leaves in member_leaves
        ]
        if len(set(alt_sums)) > 1:
            entry["choice_marks_unequal"] = True
        # Remove EVERY leaf of this base from the key, then install the single bare parent, so the
        # later additive merge sees one entry and cannot re-sum the alternatives.
        for k in [k for k in list(db_data.keys()) if _base_qnum(normalize_qid(k)) == base_num]:
            db_data.pop(k, None)
        db_data[parent] = entry
        ocr_data[parent] = {"answer": "\n".join(gathered).strip(), "is_bad_handwriting": bad}
        merged.append(parent)
        print(f"Merged choice {[m for m, _ in member_leaves]} (+{len(common)} shared part(s)) -> "
              f"{parent} (marks {marks}, answer any 1).")

    with open(ocr_path, "w") as f:
        json.dump(ocr_data, f, indent=2)
    with open(db_path, "w") as f:
        json.dump(db_data, f, indent=2)
    return merged


def merge_additive_subparts(ocr_path, db_path):
    """Collapse ADDITIVE multi-part questions whose answer key was split by sub-part
    (e.g. Q37(a)+Q37(b)+Q37(c)) into a SINGLE parent entry 'Qn', SUMMING the sub-part marks, so the
    key matches the OCR's one-block-per-question granularity. OCR question separation is anchored to
    base question numbers and keeps sub-parts inline, so the student's whole multi-part answer arrives
    as ONE block; if the key is split by sub-part, the siblings the OCR did not separately label
    otherwise render BLANK -> 0 even though the student answered them (the case-study bug). Collapsing
    the key to one entry per question number guarantees both sides share the same granularity.

    Runs on the post-choice-merge db_answers.json + ocr_answers.json (so 'answer any one' choice pairs
    are already collapsed to a single BARE parent by merge_choice_groups and are skipped here). A base
    number that already has exactly one BARE entry 'Qn' (an MCQ / single question / a choice parent)
    is left untouched -> this is a NO-OP whenever the key already has one entry per question number,
    so a key that was not split keeps behaving exactly as before.

    Additive (sum) vs. choice (max): choice is handled upstream; everything else sharing a base number
    is additive. If a bare parent 'Qn' coexists with suffixed children (a redundant breakdown) its
    marks are authoritative -- children are NOT summed on top -- so the grand total can never inflate.
    The merged expected answer keeps each labelled sub-part (and, in the pure-additive case, its marks)
    so the grader still awards per part inside ONE call. The parent is NOT tagged is_choice (it is
    additive); any internal 'OR' within a sub-part survives verbatim in that sub-part's text. Returns
    the merged parent ids.
    """
    with open(db_path, encoding="utf-8") as f:
        db_data = json.load(f)
    with open(ocr_path, encoding="utf-8") as f:
        ocr_data = json.load(f)

    # Group the current key entries by base question number.
    groups = {}
    for k in db_data:
        if k == "_instructions_":
            continue
        bn = _base_qnum(k)
        if bn is not None:
            groups.setdefault(bn, []).append(k)

    merged = []
    for bn, members in groups.items():
        parent = f"Q{bn}"
        suffixed = [m for m in members if m != parent]
        bare = [m for m in members if m == parent]
        # Nothing to collapse: a single already-bare entry (MCQ / single Q / choice parent).
        if not suffixed:
            continue

        order = sorted(members, key=natural_sort_key)   # bare 'Q37' sorts before 'Q37(a)'
        pure_additive = not bare
        # Total marks: a bare parent (if present) is authoritative; else sum the sub-parts.
        total_marks = (float(db_data[bare[0]].get("marks", 0) or 0) if bare
                       else sum(float(db_data[m].get("marks", 0) or 0) for m in suffixed))

        exp_parts, q_texts, qtype, subject = [], [], None, None
        for m in order:
            e = db_data.get(m, {})
            if not isinstance(e, dict):
                e = {"answer": str(e)}
            sm = re.search(r'\(\s*([A-Za-z0-9]+)\s*\)\s*$', m)   # '(a)' label from 'Q37(a)'
            label = f"({sm.group(1)}) " if sm else ""
            ans = str(e.get("answer", "") or "").strip()
            mk = e.get("marks", "")
            # Per-part marks hint only in the pure-additive case (avoids a misleading double count
            # when a bare parent already states the whole-question marks).
            hint = ""
            if pure_additive and str(mk) != "":
                try:
                    fmk = float(mk)
                    nmk = int(fmk) if fmk.is_integer() else fmk
                    hint = f"[{nmk} mark{'' if fmk == 1 else 's'}] "
                except (TypeError, ValueError):
                    hint = ""
            piece = f"{label}{hint}{ans}".strip()
            if piece:
                exp_parts.append(piece)
            qt = str(e.get("question", "") or "").strip()
            if qt and qt not in q_texts:
                q_texts.append(qt)
            qtype = qtype or e.get("type")
            subject = subject or e.get("subject")

        # Gather the student's answer from EVERY ocr key sharing this base number (the whole block,
        # however OCR labelled it), OR the bad-handwriting flags.
        gathered, bad = [], False
        for ok in list(ocr_data.keys()):
            if ok == "_instructions_":
                continue
            if _base_qnum(ok) == bn:
                entry = ocr_data.pop(ok)
                if not isinstance(entry, dict):
                    entry = {"answer": str(entry)}
                a = str(entry.get("answer", "") or "").strip()
                if a and a.upper() not in ("[BLANK]", "NA", "N/A", "NONE"):
                    gathered.append(a)
                bad = bad or bool(entry.get("is_bad_handwriting", False))

        merged_entry = {
            "question_id": parent,
            "question": q_texts[0] if len(q_texts) == 1 else "\n".join(q_texts),
            "answer": "\n".join(exp_parts).strip(),
            "marks": total_marks,
            "type": qtype or "Long Answer",
        }
        if subject:
            merged_entry["subject"] = subject

        for m in members:
            db_data.pop(m, None)
        db_data[parent] = merged_entry
        ocr_data[parent] = {"answer": "\n".join(gathered).strip(), "is_bad_handwriting": bad}
        merged.append(parent)
        print(f"Collapsed additive sub-parts {order} -> {parent} (sum {total_marks}).")

    with open(db_path, "w") as f:
        json.dump(db_data, f, indent=2)
    with open(ocr_path, "w") as f:
        json.dump(ocr_data, f, indent=2)
    return merged


def _finalize_choice_flags(db_path, merged_parents, inline_ids, uncertain):
    """After both merges, stamp inline_or on inline-OR entries -- a MULTI-PART question whose OR sits in
    ONE sub-part, graded ADDITIVELY (NOT whole-question 'answer any one'; that misfire under-credited
    case studies like Q36/37/38) -- and is_choice_uncertain on heuristically-detected ids, so evaluate.py
    applies the right OR rule and flags uncertain ones for manual review. (Genuine whole-question choices
    are tagged is_choice separately by merge_choice_groups.)"""
    try:
        with open(db_path, encoding="utf-8") as f:
            db_data = json.load(f)
    except Exception:
        return
    uncertain = uncertain or set()
    for qid in (inline_ids or []):
        if isinstance(db_data.get(qid), dict):
            db_data[qid]["inline_or"] = True
    for qid in set(merged_parents or []) | set(inline_ids or []):
        if qid in uncertain and isinstance(db_data.get(qid), dict):
            db_data[qid]["is_choice_uncertain"] = True
    with open(db_path, "w") as f:
        json.dump(db_data, f, indent=2)


def _fmt_marks(x):
    """Render a marks value without a trailing '.0' (4.0 -> '4', 2.5 -> '2.5')."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f == int(f) else str(f)


def _safe_float(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def reconcile_marks_with_question_paper(db_path, question_paper_path, mode="raise"):
    """Deterministic safety net for answer-key PARSE errors, GENERALISED to every structural mistake a
    key parser can make -- not just the one that first surfaced.

    `mode` selects how much authority the QUESTION PAPER has over the key's marks (set by the teacher's
    "marks source" choice; default preserves the original conservative behaviour):
      * "raise" (default) -- conservative safety net. RAISE a shortfall to the paper; FLAG inflation
        (never silently lower, since a machine can't know which side is right); inject a dropped
        question; flag an unknown one. Lifts a wrong total UP to the truth, never lowers a correct one.
      * "align_to_paper" -- the teacher confirmed the PAPER is authoritative for marks, so set each
        question's max to the paper's value in BOTH directions (a doubled 'answer any one' choice worth
        10 is lowered to the paper's 5), inject dropped, flag unknown. This is what fixes an INFLATED
        key (the 99.5-vs-80 case) once a human has vouched for the paper.
      * "trust_key" -- the teacher confirmed the KEY's marks stand as parsed; make NO changes. A fixed regex/prompt can never
    anticipate every key layout, so instead of trusting the parse we cross-check it against a SECOND,
    independent source of truth: the uploaded QUESTION PAPER (parsed separately, structurally simpler
    -- one entry per question with the whole-question marks printed plainly -- and the authority on
    what each question is WORTH). After all key merges we compare, per BASE question number, the key's
    recorded maximum to the paper's, and handle EACH way the two can disagree:

      1. SHORTFALL (key < paper) -- the parser dropped a part (the Class X Science case: case studies
         Q37/38/39 lost their (a)/(b) parts, came back worth 2 not 4, silently shrinking the max to
         74). If exactly one key entry covers the base we RAISE it to the paper value (denominator +
         grand total corrected) and stamp marks_reconciled_from_qp; if several siblings share the base
         (ambiguous which to raise) we flag them instead.
      2. INFLATION (key > paper) -- a duplicated/unmerged alternative made the question worth MORE than
         the paper (e.g. a missed 'answer any one' choice counting both 5-mark options as 10). We do
         NOT silently lower it (the key might be right and the paper mis-parsed); we FLAG it for review.
      3. DROPPED QUESTION (in the paper, absent from the key) -- the parser lost a whole question. We
         INJECT a placeholder key entry at the paper's marks (so the denominator counts it) with no
         expected answer and a strong review flag, so the student's answer is graded manually.
      4. UNKNOWN QUESTION (in the key, absent from the paper) -- a numbering/parse mismatch. We FLAG it
         (marks untouched) so a misalignment can't pass unnoticed.
    Finally it asserts the GRAND TOTAL (key vs paper) and, when they still differ after fixes, logs a
    prominent warning. Marks are only ever AUTO-CHANGED in the two confident directions (raise a
    shortfall, restore a dropped question to the paper value); every other discrepancy is flagged for a
    human, never silently altered -- so this can lift a wrong total to the truth but can never lower a
    correct one.

    Content caveat: this validates STRUCTURE (marks + presence), not the correctness of the expected-
    answer TEXT -- a wrong answer with the right marks is a content error handled by the parser prompt
    and the format-agnostic LLM grader, not here. No-op when no paper is supplied (a LOUD warning is
    logged so the missing safety net is visible), it can't be read, or ids don't map. Gated by
    RECONCILE_KEY_MARKS_WITH_QP (default on). Writes a machine-readable key_integrity.json beside the
    db and returns a summary dict: {checked, adjusted, flagged, injected, qp_total, key_total}."""
    mode = mode if mode in ("raise", "align_to_paper", "trust_key") else "raise"
    empty = {"checked": False, "adjusted": [], "flagged": [], "injected": [],
             "qp_total": 0.0, "key_total": 0.0}
    if os.environ.get("RECONCILE_KEY_MARKS_WITH_QP", "1").strip().lower() in ("0", "false", "no", "off"):
        return dict(empty)
    if not question_paper_path or not os.path.exists(question_paper_path):
        print("Warning: no question paper available -- answer-key marks integrity was NOT cross-checked. "
              "Upload the question paper so parser drops/inflations are caught automatically.")
        return dict(empty)
    try:
        with open(question_paper_path, encoding="utf-8") as f:
            qp = json.load(f)
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    except Exception as e:
        print(f"Warning: marks reconciliation skipped (could not read inputs): {e}")
        return dict(empty)
    if not isinstance(qp, dict) or not isinstance(db, dict):
        return dict(empty)

    # Authoritative marks + a representative question text per BASE number from the paper. MAX across
    # any siblings so a paper that itself splits a question can only under-raise (never inflate).
    qp_marks, qp_text = {}, {}
    for qk, qv in qp.items():
        if qk == "_instructions_" or not isinstance(qv, dict):
            continue
        bn = _base_qnum(normalize_qid(qk))
        if bn is None:
            continue
        m = _safe_float(qv.get("marks"))
        if m > qp_marks.get(bn, 0.0):
            qp_marks[bn] = m
        if qv.get("question"):
            qp_text.setdefault(bn, str(qv.get("question")).strip())

    # Group the (post-merge) key entries by base number -- normally one entry per base.
    key_by_base = {}
    for qid, entry in db.items():
        if qid == "_instructions_" or not isinstance(entry, dict):
            continue
        bn = _base_qnum(normalize_qid(qid))
        if bn is not None:
            key_by_base.setdefault(bn, []).append(qid)

    adjusted, flagged, injected = [], [], []

    # "trust_key" makes NO changes (teacher vouched for the key). The other modes walk every question.
    for bn in (sorted(set(qp_marks) | set(key_by_base)) if mode != "trust_key" else []):
        paper_m = qp_marks.get(bn)
        qids = key_by_base.get(bn, [])

        if paper_m is None:                                   # (4) in key, not in paper
            for qid in qids:
                db[qid]["key_integrity_warning"] = (
                    "This question is not present in the question paper -- verify the numbering; a "
                    "parse/alignment mismatch can misplace marks.")
                flagged.append((qid, _safe_float(db[qid].get("marks")), None))
            continue

        if not qids:                                          # (3) dropped from the key -> inject
            inj = f"Q{bn}"
            if inj in db:
                inj = f"Q{bn}__restored"
            db[inj] = {
                "question_id": inj, "question": qp_text.get(bn, ""), "answer": "", "marks": paper_m,
                "type": "", "marks_reconciled_from_qp": True, "key_parse_missing": True,
                "reconcile_note": (
                    f"This question is worth {_fmt_marks(paper_m)} in the question paper but was MISSING "
                    f"from the answer key -- the key parse dropped it entirely. Its maximum has been "
                    f"restored so the total is correct; no expected answer is available, so grade the "
                    f"student's response manually."),
            }
            injected.append((inj, 0.0, paper_m))
            continue

        key_sum = sum(_safe_float(db[q].get("marks")) for q in qids)
        if paper_m - key_sum > 1e-6:                          # (1) shortfall
            if len(qids) == 1:
                qid = qids[0]
                old = _safe_float(db[qid].get("marks"))
                db[qid]["marks"] = paper_m
                db[qid]["marks_reconciled_from_qp"] = True
                db[qid]["reconcile_note"] = (
                    f"The question paper marks this question {_fmt_marks(paper_m)}, but the answer key "
                    f"only accounted for {_fmt_marks(old)} -- the key parse likely dropped one or more "
                    f"parts. The maximum has been corrected to {_fmt_marks(paper_m)}; expected-answer "
                    f"text for the missing part(s) is unavailable, so verify this answer and adjust the "
                    f"awarded marks manually.")
                adjusted.append((qid, old, paper_m))
            else:                                             # ambiguous which sibling -> flag all
                for qid in qids:
                    db[qid]["key_integrity_warning"] = (
                        f"The answer key sub-parts for this question total {_fmt_marks(key_sum)} but the "
                        f"paper marks it {_fmt_marks(paper_m)} -- a part may be missing; verify.")
                    flagged.append((qid, key_sum, paper_m))
        elif key_sum - paper_m > 1e-6:                        # (2) inflation
            if mode == "align_to_paper" and len(qids) == 1:
                # Teacher vouched for the paper -> LOWER the doubled/duplicated marks to the paper value.
                qid = qids[0]
                old = _safe_float(db[qid].get("marks"))
                db[qid]["marks"] = paper_m
                db[qid]["marks_reconciled_from_qp"] = True
                db[qid]["reconcile_note"] = (
                    f"The answer key marked this question {_fmt_marks(old)} but the question paper marks "
                    f"it {_fmt_marks(paper_m)} (often an 'answer any one' choice counted twice). The "
                    f"maximum has been set to the paper's {_fmt_marks(paper_m)}; verify the awarded marks.")
                adjusted.append((qid, old, paper_m))
            else:
                for qid in qids:
                    db[qid]["key_integrity_warning"] = (
                        f"The answer key totals {_fmt_marks(key_sum)} for this question but the paper marks "
                        f"it {_fmt_marks(paper_m)} -- a duplicated or unmerged alternative may have inflated "
                        f"it; verify (marks were left unchanged).")
                    flagged.append((qid, key_sum, paper_m))

    qp_total = sum(qp_marks.values())
    key_total = sum(_safe_float(v.get("marks")) for k, v in db.items()
                    if k != "_instructions_" and isinstance(v, dict))

    if adjusted or flagged or injected:
        with open(db_path, "w") as f:
            json.dump(db, f, indent=2)
    summary = {"checked": True, "adjusted": adjusted, "flagged": flagged, "injected": injected,
               "qp_total": qp_total, "key_total": key_total}
    try:
        with open(os.path.join(os.path.dirname(db_path), "key_integrity.json"), "w") as f:
            json.dump({**summary,
                       "adjusted": [[q, o, n] for q, o, n in adjusted],
                       "flagged": [[q, o, n] for q, o, n in flagged],
                       "injected": [[q, o, n] for q, o, n in injected]}, f, indent=2)
    except Exception as e:
        print(f"Warning: could not write key_integrity.json: {e}")

    if adjusted:
        _net = sum(n - o for _, o, n in adjusted)
        _sign = "+" if _net >= 0 else ""
        print(f"Reconciled {len(adjusted)} question(s) to the question-paper marks "
              f"(net {_sign}{_fmt_marks(_net)} to the denominator): "
              + ", ".join(f"{q} {_fmt_marks(o)}->{_fmt_marks(n)}" for q, o, n in adjusted))
    if injected:
        print(f"Restored {len(injected)} question(s) MISSING from the answer key (present in the paper), "
              f"flagged for manual grading: " + ", ".join(f"{q} (worth {_fmt_marks(n)})" for q, _, n in injected))
    if flagged:
        print(f"Flagged {len(flagged)} question(s) whose key marks disagree with the paper (left "
              f"unchanged, marked for review): " + ", ".join(q for q, _, _ in flagged))
    print(f"Answer-key integrity vs question paper: key total {_fmt_marks(key_total)} / "
          f"paper total {_fmt_marks(qp_total)}.")
    if abs(key_total - qp_total) > 1e-6:
        print(f"WARNING: after reconciliation the answer-key total ({_fmt_marks(key_total)}) still "
              f"differs from the question-paper total ({_fmt_marks(qp_total)}) -- review the flagged "
              f"questions above.")
    return summary


# Per-subprocess wall-clock timings for the current run [(label, seconds), ...]. Appended by
# run_command (thread-safe: list.append is atomic under the GIL), reset at the start of each
# full_evaluate() run, and printed + persisted as a profiling summary at the end of the run.
_STAGE_TIMINGS = []

# Per-stage WATCHDOG ceilings (seconds). A stage that exceeds its ceiling is KILLED and the call
# returns failure, so a stalled LLM stream can never hang the whole pipeline forever (the observed
# "stuck after evaluate_diagrams" bug). Ceilings are GENEROUS -- a healthy run never hits them, so
# there is zero latency/accuracy cost on normal runs; they only convert an infinite hang into a bounded,
# gracefully-degrading failure. Override any stage via env STAGE_TIMEOUT_<SCRIPT_UPPER_NO_EXT> (e.g.
# STAGE_TIMEOUT_EVALUATE=1200), or all stages via STAGE_TIMEOUT. Set to 0 to disable the watchdog.
_STAGE_TIMEOUTS = {
    "process_input.py": 300,
    "preprocess.py": 300,
    "run_ocr.py": 1200,          # many pages + optional orientation-vote re-OCR
    "detect_diagrams.py": 180,   # pure Python, fast
    "crop_answer_regions.py": 300,  # display-only per-answer crops (cheap vision + local CV)
    "crop_diagram_regions.py": 180,  # display-only diagram bboxes: a handful of cheap calls
    "extract_features.py": 420,  # diagram feature extraction (per-call tightening keeps it well under)
    "evaluate_diagrams.py": 420, # 2-pass diagram grading
    "evaluate.py": 1200,         # grading: many questions + extended thinking
}
_DEFAULT_STAGE_TIMEOUT_S = 1200


def _stage_timeout(label):
    """Resolve the watchdog ceiling (s) for a stage label. Precedence: STAGE_TIMEOUT_<NAME> env >
    global STAGE_TIMEOUT env > per-stage default > global default. A value <= 0 disables the watchdog."""
    key = "STAGE_TIMEOUT_" + re.sub(r"\.py$", "", label).upper()
    for env_key in (key, "STAGE_TIMEOUT"):
        v = os.environ.get(env_key)
        if v:
            try:
                return float(v)
            except ValueError:
                pass
    return _STAGE_TIMEOUTS.get(label, _DEFAULT_STAGE_TIMEOUT_S)


# ---------------------------------------------------------------------------------------------------
# Cancellation. A teacher who spots the wrong sheet mid-run needs the work to STOP, not just be ignored:
# grading is the expensive stage and every second costs real API credit.
#
# Stages are subprocesses spawned into their own process group (_new_group_kwargs()), so the whole tree
# (incl. grandchildren like preprocess's ProcessPool workers) dies with one kill_process_tree -- the
# same mechanism the stage watchdog already relies on, on both POSIX and Windows.
#
# The run_id reaches run_command through `env` rather than a thread-local: stages run on the main run
# thread, but the answer-crop pass runs on its OWN thread with a copied env, and a thread-local would
# silently fail to cancel it.
# ---------------------------------------------------------------------------------------------------
_CANCEL_LOCK = threading.Lock()
_CANCELLED = set()          # run_ids asked to stop
_RUN_PROCS = {}             # run_id -> set of live Popen owned by that run


def request_cancel(run_id):
    """Mark `run_id` cancelled and SIGKILL every stage subprocess tree it currently owns.

    Returns the number of process groups signalled. Safe to call for an unknown//finished run (0), and
    safe to call twice. The flag is set BEFORE killing so a stage finishing in the gap cannot start the
    next one.
    """
    if not run_id:
        return 0
    with _CANCEL_LOCK:
        _CANCELLED.add(run_id)
        procs = list(_RUN_PROCS.get(run_id, ()))
    killed = 0
    for p in procs:
        if kill_process_tree(p):
            killed += 1
    return killed


def is_cancelled(run_id):
    if not run_id:
        return False
    with _CANCEL_LOCK:
        return run_id in _CANCELLED


def clear_cancel(run_id):
    """Forget a cancellation so the same run_id (same file name) can be evaluated again."""
    with _CANCEL_LOCK:
        _CANCELLED.discard(run_id)
        _RUN_PROCS.pop(run_id, None)


def _track_proc(run_id, proc, add=True):
    if not run_id:
        return
    with _CANCEL_LOCK:
        if add:
            _RUN_PROCS.setdefault(run_id, set()).add(proc)
        else:
            s = _RUN_PROCS.get(run_id)
            if s:
                s.discard(proc)
                if not s:
                    _RUN_PROCS.pop(run_id, None)


CANCELLED_MSG = "Evaluation cancelled by the teacher."


def _cancelled_result():
    return {"status": "cancelled", "error": "Evaluation cancelled",
            "details": CANCELLED_MSG}


def run_command(command, cwd=None, env=None):
    run_id = (env or {}).get("RUN_ID")
    # Refuse to start another (billable) stage once cancelled -- checked before spawning so a cancel
    # landing between stages costs nothing.
    if is_cancelled(run_id):
        return False, CANCELLED_MSG
    label = next((os.path.basename(c) for c in command if str(c).endswith(".py")), command[0])
    to = _stage_timeout(label)
    to = to if (to and to > 0) else None
    print(f"Executing: {' '.join(command)}")
    _t0 = time.perf_counter()
    # The child gets its own process group so the watchdog can kill the WHOLE tree (incl. grandchildren
    # such as preprocess's ProcessPool workers), not just the direct child -- see _new_group_kwargs().
    # Both ends of the pipe are pinned to utf-8: without it Windows would encode the child's stdout as
    # cp1252 and every maths/chemistry glyph this pipeline transcribes becomes mojibake or a hard
    # UnicodeEncodeError. No-op on POSIX, whose locale encoding is already utf-8.
    env = {**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", cwd=cwd, env=env, **_new_group_kwargs())
    _track_proc(run_id, proc)
    # A cancel that arrived while this was being spawned would have found no process to kill, so kill
    # it here instead -- closes the race between the check above and registration.
    if is_cancelled(run_id):
        request_cancel(run_id)
    try:
        out, err = proc.communicate(timeout=to)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = "", ""
        dt = time.perf_counter() - _t0
        _STAGE_TIMINGS.append((label, round(dt, 2)))
        msg = (f"{label} exceeded its {to:.0f}s stage watchdog and was terminated -- the pipeline "
               f"degrades gracefully instead of hanging.")
        print(f"  -> WATCHDOG TIMEOUT: {msg}")
        if err:
            msg += "\n--- partial stderr ---\n" + err[-2000:]
        return False, msg
    finally:
        _track_proc(run_id, proc, add=False)
    dt = time.perf_counter() - _t0
    _STAGE_TIMINGS.append((label, round(dt, 2)))
    # A cancelled stage was SIGKILLed, so it looks like any other crash. Report it as a cancellation so
    # callers stop instead of treating it as a failure worth degrading around.
    if is_cancelled(run_id):
        print(f"  -> {label} stopped: {CANCELLED_MSG}")
        return False, CANCELLED_MSG
    print(f"  -> {label} finished in {dt:.1f}s")
    if proc.returncode != 0:
        print(f"Error executing command: {err}")
        return False, err
    return True, out


def _read_cost_ledger(path):
    """Total the per-run API cost ledger (JSON-lines) that each Gemini stage appends to via
    llm_pricing.log_cost. Returns (total_usd, breakdown) where breakdown maps stage ->
    {model, input_tokens, output_tokens, cost_usd, cost_source}. cost_source is "openrouter" only when
    EVERY record for the stage was priced by the provider's real usage.cost, else "estimate". Missing/empty
    ledger -> (0.0, {}). This is the authoritative per-paper cost (OCR + grading + diagrams): the real
    provider-billed amount where available, otherwise the per-model estimate."""
    total = 0.0
    by_stage = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                c = float(rec.get("cost_usd", 0) or 0)
                total += c
                s = rec.get("stage", "?")
                b = by_stage.setdefault(s, {"model": rec.get("model"),
                                           "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                           "cost_source": rec.get("cost_source", "estimate")})
                b["input_tokens"] += int(rec.get("input_tokens", 0) or 0)
                b["output_tokens"] += int(rec.get("output_tokens", 0) or 0)
                b["cost_usd"] = round(b["cost_usd"] + c, 6)
                # A stage counts as provider-billed only if EVERY record for it was (else fall back label).
                if rec.get("cost_source", "estimate") != "openrouter":
                    b["cost_source"] = "estimate"
    except FileNotFoundError:
        return 0.0, {}
    except Exception as e:
        print(f"Warning: could not read API cost ledger: {e}")
    return round(total, 6), by_stage

def full_evaluate(input_file, student_name="Student", answer_key_path=None, report_dir=None,
                  exam_class=None, exam_subject=None, question_paper_path=None, marks_source=None,
                  tester_id=None, env_overrides=None):
    # marks_source is the teacher's "which document is authoritative for MARKS" choice (web flow):
    #   "question_paper" -> align the key's per-question marks to the paper (lowers a doubled choice too)
    #   "answer_key"     -> trust the key's marks as parsed (no reconciliation changes)
    #   None (CLI/legacy) -> the conservative default net (raise a shortfall, flag inflation).
    _reconcile_mode = {"question_paper": "align_to_paper",
                       "answer_key": "trust_key"}.get(marks_source, "raise")
    # Dynamically resolve project root based on this script's location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    skills_dir = os.path.join(project_root, "skills")
    
    # Create unique output directory for this run
    run_id = Path(input_file).stem
    output_base = os.path.join(project_root, "output", run_id)
    os.makedirs(output_base, exist_ok=True)
    # run_id is the uploaded file's STEM, so re-uploading a corrected sheet under the same name lands
    # in this same folder. Clear it first -- the ingest/preprocess stages only overwrite the pages they
    # emit, and the stages downstream GLOB these folders (see _reset_run_dir).
    _reset_run_dir(output_base)

    # Paths for sub-steps
    images_dir = os.path.join(output_base, "images")
    preprocessed_dir = os.path.join(output_base, "preprocessed")
    ocr_dir = os.path.join(output_base, "ocr_output")
    ocr_answers_path = os.path.join(ocr_dir, "ocr_answers.json")
    db_answers_path = os.path.join(output_base, "db_answers.json")
    page_mapping_path = os.path.join(ocr_dir, "page_mapping.json")
    
    # Setup environment
    env = os.environ.copy()
    env_file = os.path.join(project_root, ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if v[:1] not in ('"', "'"):
                        v = re.sub(r'\s+#.*$', '', v).strip()  # drop an inline comment from unquoted values
                    v = v.strip('"').strip("'")
                    env[k] = v

    # Per-call env overrides (e.g. the batch layer splitting the concurrency caps across parallel
    # sheets) win over BOTH os.environ AND the .env overlay -- applied LAST so a caller can pin a
    # stage's env for THIS run only, without mutating any global state.
    if env_overrides:
        for _ok, _ov in env_overrides.items():
            env[str(_ok)] = str(_ov)

    # Per-run API cost ledger: every Gemini stage (OCR, grading, diagrams) appends its model +
    # tokens + cost here via llm_pricing.log_cost, so we can report the TRUE per-paper cost,
    # priced per the model each stage actually uses. Start each run with a fresh ledger.
    cost_ledger_path = os.path.join(output_base, "api_costs.jsonl")
    try:
        if os.path.exists(cost_ledger_path):
            os.remove(cost_ledger_path)
    except OSError:
        pass
    env["API_COST_LOG"] = cost_ledger_path
    # Lets run_command attribute each stage subprocess to this run so a cancel can kill its process
    # group. Carried in `env` (not a thread-local) so the answer-crop pass, which runs on its own
    # thread with a COPY of this env, is cancellable too.
    env["RUN_ID"] = run_id

    # Reset per-run profiling counters + start the wall-clock (consumed by run_command + the timing
    # summary printed just before the success return).
    _STAGE_TIMINGS.clear()
    _wall_start = time.perf_counter()

    # 1. Ingestion
    print("\n--- [1/5] Ingestion ---")
    ingestion_script = os.path.join(skills_dir, "ingestion-handler/scripts/process_input.py")
    success, output = run_command([PYTHON_EXE, ingestion_script, input_file, "--output-dir", images_dir], env=env)
    if not success: return {"error": "Ingestion failed", "details": output}

    # 2. Preprocessing
    print("\n--- [2/5] Preprocessing ---")
    preprocess_script = os.path.join(skills_dir, "img-preprocessing/scripts/preprocess.py")
    image_files = [str(p) for p in Path(images_dir).glob("*.png")]
    success, output = run_command([PYTHON_EXE, preprocess_script] + image_files + ["--output-dir", preprocessed_dir], env=env)
    if not success: return {"error": "Preprocessing failed", "details": output}

    return _evaluate_from_preprocessed(
        run_id, output_base, skills_dir, preprocessed_dir, ocr_dir, ocr_answers_path,
        db_answers_path, page_mapping_path, env, cost_ledger_path, _reconcile_mode, _wall_start,
        student_name, answer_key_path, question_paper_path, report_dir, exam_class, exam_subject,
        tester_id)


# User-facing steps for the live "Processing Paper" checklist (single-student flow). The orchestrator
# stamps the current step into output/<run_id>/progress.json as each pipeline stage begins; the Flask
# /orient-status route surfaces it and the UI renders a checklist (done / active / pending).
_PROGRESS_STEPS = ["Reading handwriting (OCR)", "Analyzing diagrams", "Grading & building report"]


def _write_progress(output_base, index):
    """Record the current pipeline step (1-based) for the live UI checklist. Best-effort -- a failed
    progress write must NEVER interrupt grading."""
    try:
        idx = max(1, min(int(index), len(_PROGRESS_STEPS)))
        path = os.path.join(output_base, "progress.json")
        with open(path + ".tmp", "w") as f:
            json.dump({"index": idx, "total": len(_PROGRESS_STEPS),
                       "label": _PROGRESS_STEPS[idx - 1], "steps": _PROGRESS_STEPS}, f)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def _evaluate_from_preprocessed(run_id, output_base, skills_dir, preprocessed_dir, ocr_dir,
                                ocr_answers_path, db_answers_path, page_mapping_path, env,
                                cost_ledger_path, _reconcile_mode, _wall_start, student_name,
                                answer_key_path, question_paper_path, report_dir, exam_class,
                                exam_subject, tester_id=None):
    """OCR -> segmentation-repair -> ground-truth align -> diagrams -> grading -> report. Extracted
    verbatim from full_evaluate so the straight-through path AND the orientation-gated resume share
    byte-identical grading. Inputs are the run's dirs/paths + the fully-initialised env; nothing from
    ingestion/preprocessing leaks in beyond these (only run_id, not the original input_file)."""
    # The segmentation-repair glue-matcher below runs IN-PROCESS and reads os.environ (subprocess
    # stages get `env` explicitly, but in-process code does not). Propagate the run's overlaid config
    # (API key, provider routing, matcher model) so the matcher can always reach the API in the web app
    # too; setdefault never overrides anything already set in the environment.
    for _ek, _ev in (env or {}).items():
        if isinstance(_ev, str):
            os.environ.setdefault(_ek, _ev)
    # 3. Vision OCR
    if is_cancelled(run_id):
        return _cancelled_result()
    print("\n--- [3/5] Vision OCR ---")
    _write_progress(output_base, 1)                       # step 1: Reading handwriting (OCR)
    ocr_script = os.path.join(skills_dir, "vision-ocr/scripts/run_ocr.py")
    preprocessed_files = sorted([str(p) for p in Path(preprocessed_dir).glob("*.png")], key=natural_sort_key)
    ocr_cmd = [PYTHON_EXE, ocr_script] + preprocessed_files + ["--output-dir", ocr_dir]
    # Anchor OCR question separation to this exam's real question numbers (from the key + question
    # paper) so misreads/invented question numbers are constrained at the source. Gated: when no set
    # is derivable, the command is byte-for-byte the same as before.
    question_id_set = _derive_question_id_set(answer_key_path, question_paper_path)
    if question_id_set:
        os.makedirs(ocr_dir, exist_ok=True)
        qids_path = os.path.join(ocr_dir, "question_ids.json")
        try:
            with open(qids_path, "w") as f:
                json.dump(question_id_set, f)
            ocr_cmd += ["--question-ids-file", qids_path]
        except Exception as e:
            print(f"Warning: could not write question_ids.json; OCR will run unanchored: {e}")
    # PRIVACY: the header call is the only request whose PURPOSE is extracting a child's identity.
    # When the teacher has already named the student, `_resolve_student_name` discards the OCR'd
    # name anyway (teacher-provided wins), so the call is pure exposure -- skip it. This sends
    # strictly less personal data for exactly the same result, and saves a request.
    if _resolve_student_name(student_name, "") or not _student_pii_extraction_enabled():
        ocr_cmd.append("--no-header-pii")
    # Give OCR the key's STRUCTURE (which sub-parts each question declares) so it can detect an answer
    # that was captured but TRUNCATED and re-read that page. This is the one failure the BLANK-gated
    # recovery layers below cannot see -- see recover_incomplete_answers in run_ocr.py. Key content
    # never influences transcription, and omitting the flag leaves OCR byte-for-byte as it was.
    if answer_key_path and os.path.exists(answer_key_path):
        ocr_cmd += ["--answer-key-file", answer_key_path]
    success, output = run_command(ocr_cmd, env=env)
    if not success: return {"error": "OCR failed", "details": output}

    # Canonicalise OCR question ids (drop the subject prefix AND normalise label variants like
    # A1/Ans 1/Answer 1/Sol 1 -> Q1) so they align to the answer key regardless of how the student
    # labelled their answers. Two labels collapsing to the same question are merged.
    if os.path.exists(ocr_answers_path):
        with open(ocr_answers_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)
        clean_ocr = {}
        for k, v in ocr_data.items():
            if k == "_instructions_":
                clean_ocr[k] = v
                continue
            ck = normalize_qid(k)
            if ck in clean_ocr and isinstance(clean_ocr[ck], dict) and isinstance(v, dict):
                # Distinct labels mapped to the same question -> concatenate, OR the bad-hw flag.
                clean_ocr[ck]["answer"] = (str(clean_ocr[ck].get("answer", "")) + "\n"
                                           + str(v.get("answer", ""))).strip()
                clean_ocr[ck]["is_bad_handwriting"] = (bool(clean_ocr[ck].get("is_bad_handwriting"))
                                                       or bool(v.get("is_bad_handwriting")))
            else:
                clean_ocr[ck] = v
        with open(ocr_answers_path, "w") as f:
            json.dump(clean_ocr, f, indent=2)

    # Reconcile the canonicalized OCR answers against the exam's authoritative question set
    # (FLAG-ONLY, never moves graded text). Out-of-set misread/invented question numbers are flagged
    # for review via the symbol_flags.json sidecar -> 'OCR Symbol Warning' (it used to borrow
    # is_bad_handwriting as the review lever, which mislabelled a misread question NUMBER as illegible
    # handwriting); in-set questions with no captured answer (possible OCR drops) are logged for
    # visibility. Gated on a derived question set -> when absent this whole block is skipped and
    # grading behaves byte-for-byte as before.
    if question_id_set and os.path.exists(ocr_answers_path):
        with open(ocr_answers_path, encoding="utf-8") as f:
            _ocr = json.load(f)
        _ocr, gap_numbers = reconcile_ocr_to_question_set(_ocr, question_id_set)
        with open(ocr_answers_path, "w") as f:
            json.dump(_ocr, f, indent=2)
        flagged = [k for k in _ocr if k != "_instructions_" and isinstance(_ocr.get(k), dict)
                   and _ocr[k].get("question_set_warning")]
        if flagged:
            # Route the note through the sidecar rather than the entry: the repair layers below rebuild
            # OCR entries from scratch and would drop it. This replaces the old is_bad_handwriting lever.
            _append_symbol_flags(os.path.dirname(ocr_answers_path),
                                 {k: _ocr[k]["question_set_warning"] for k in flagged})
            print(f"OCR labels outside the question set (flagged for review): {flagged}")
        if gap_numbers:
            print(f"Question-set gaps -- no OCR answer captured for question number(s): {gap_numbers}")

    if os.path.exists(page_mapping_path):
        with open(page_mapping_path, "r", encoding="utf-8") as f:
            pm_data = json.load(f)
        for img_k, img_list in pm_data.items():
            for item in img_list:
                if "question_id" in item:
                    item["question_id"] = normalize_qid(item["question_id"])
        with open(page_mapping_path, "w") as f:
            json.dump(pm_data, f, indent=2)

    # 4. Fetch Ground Truth (Manual Key Only)
    if is_cancelled(run_id):
        return _cancelled_result()
    print("\n--- [4/5] Fetching Ground Truth ---")
    if answer_key_path and os.path.exists(answer_key_path):
        print(f"Using manual answer key from: {answer_key_path}")
        
        with open(ocr_answers_path, encoding="utf-8") as f:
            ocr_keys = list(json.load(f).keys())
            
        with open(answer_key_path, encoding="utf-8") as f:
            manual_db = json.load(f)

        # Canonicalize the answer-key IDs to the OCR/student scheme ('1'->'Q1', '21(a)'->'Q21(a)')
        # so the key aligns no matter how the parser labelled it. (OCR keys were already normalized
        # above.) Without this, a bare-numbered key matches nothing and every answer renders BLANK.
        manual_db = _canonicalize_db_keys(manual_db)

        # --- Qwen segmentation recovery (gated on the closed question set; additive, flag-on-doubt) ---
        # (Phase 2) Split a collapsed objective-answer list (one OCR block holding A1..A6) back into
        # separate MCQ entries; (Phase 3) recover an in-set question that captured nothing by lifting a
        # fragment the student numbered with that question's own number out of a neighbouring answer.
        # Both no-op when question_id_set is absent or nothing matches; neither touches assemble_answers.
        if question_id_set and os.path.exists(ocr_answers_path):
            with open(ocr_answers_path, encoding="utf-8") as f:
                _ocr = json.load(f)
            _pm = None
            if os.path.exists(page_mapping_path):
                with open(page_mapping_path, encoding="utf-8") as f:
                    _pm = json.load(f)
            _ocr, split_map = split_objective_answer_lists(_ocr, manual_db, question_id_set, _pm)
            if split_map:
                print(f"Split collapsed objective-answer list(s) into MCQ entries: {split_map}")
            # Every rescued answer is flagged for a teacher's eye, with an accurate reason. A question
            # split out of the orphan-page holder came off a page the OCR could not label at all, so it
            # is the least certain of the lot -- say exactly that rather than reusing the mixed-answer
            # wording (which means something else: two answers merged into one slot).
            _rescue_reasons = {}
            for _src, _new in (split_map or {}).items():
                for _nk in _new:
                    _rescue_reasons[_nk] = (
                        "This answer was recovered from a scanned page that carried no readable question "
                        "number, so the pipeline could not place it on its own. Check it against the "
                        "original sheet."
                        if str(_src).startswith("_") else
                        "This answer was separated out of a combined list of objective answers. "
                        "Verify it belongs to this question.")
            # 4b. Layer 2 -- re-home a previous question's trailing sub-part(s) that a page break
            # swallowed into the TOP of the next question (e.g. Q34's (a)(iii) equations captured under
            # Q35). Structural + additive: only splits an answer that opens with a later sub-part before
            # its own '(a)', moving the dangling prefix back to the prior in-set question.
            _ocr, _reattached, _reflag = reattach_leading_continuation(_ocr, question_id_set, _pm)
            if _reattached:
                print(f"Re-homed page-break leading continuation(s) to the prior question: {_reattached}")
            _gaps = _recompute_gaps(_ocr, question_id_set)
            _ocr, _recovered, _flagged, _gaps = recover_gaps_by_position(_ocr, _gaps, _pm, manual_db)
            _flagged = sorted(set(_flagged) | set(_reflag))
            for _rn in _recovered:
                _rescue_reasons.setdefault(f"Q{_rn}", (
                    "This answer was found inside another question's captured text, headed by this "
                    "question's own number, and moved back here. Verify the split."))
            if _recovered:
                print(f"Recovered in-set gap question(s) from a neighbouring answer: {_recovered}")
            if _gaps:
                print(f"Question-set gaps remaining (no OCR answer captured): {_gaps}")
            # 4c. Approach 1 (E5): content/key recovery for GLUED slots (the OCR collision flags).
            # Additive -- fills only a still-BLANK in-set target from a flagged slot, never edits a
            # source; no-op when there are no flags or no blanks. Reads the OCR collision sidecar.
            _glue_flags = []
            _mf_path = os.path.join(ocr_dir, "mixed_answer_flags.json")
            if os.path.exists(_mf_path):
                try:
                    with open(_mf_path, encoding="utf-8") as f:
                        _glue_flags = json.load(f)
                except Exception:
                    _glue_flags = []
            # Ordering fix (Layer 3): recover_gaps_by_position above flags the GLUED HOST it lifted a
            # fragment out of (e.g. Q35) -- exactly the slot glue-repair must inspect to reassign ANY
            # OTHER question still welded inside it -- but those flags (_flagged) are written to disk
            # only AFTER this block. Union them in NOW so glue-repair sees them this run, instead of
            # short-circuiting on the (often empty) on-disk OCR-collision set. Purely additive:
            # repair_glued_answers still only ever fills a still-BLANK in-set target, never edits a
            # source slot, so the paper total can only rise or stay equal.
            _glue_flags = sorted({int(x) for x in (_glue_flags or [])} | {int(x) for x in (_flagged or [])})
            # Tier 2: also probe each remaining blank's in-set NEIGHBOURS (catches a SILENT glue the OCR
            # never flagged -- e.g. a misread question number). GLUE_PROBE_NEIGHBORS=0 restores the
            # flags-only path. repair_glued_answers no-ops when there are no blanks/hosts, so calling it
            # unconditionally is safe (and required -- a silent glue leaves _glue_flags empty).
            _probe = os.environ.get("GLUE_PROBE_NEIGHBORS", "1").strip().lower() not in ("0", "false", "no", "off")
            # Tier 3: also re-home a whole answer displaced into a NON-adjacent valid slot by a digit
            # misread (off-topic-for-itself host -> blank). GLUE_PROBE_OFFTOPIC=0 restores the Tier-1/2 path.
            _offtopic = os.environ.get("GLUE_PROBE_OFFTOPIC", "1").strip().lower() not in ("0", "false", "no", "off")
            _ocr, _g_recovered, _g_flagged = repair_glued_answers(
                _ocr, manual_db, question_id_set, _glue_flags, _pm,
                probe_neighbors=_probe, probe_offtopic=_offtopic)
            if _g_recovered:
                print(f"Glue-repair recovered question(s) from a glued slot: {_g_recovered}")
            for _rn in _g_recovered:
                _rescue_reasons.setdefault(f"Q{_rn}", (
                    "This answer was matched out of another question's captured text by comparing it "
                    "against the answer key. Verify it belongs to this question."))
            _flagged = sorted(set(_flagged) | set(_g_flagged))
            with open(ocr_answers_path, "w") as f:
                json.dump(_ocr, f, indent=2)
            if _pm is not None:
                with open(page_mapping_path, "w") as f:
                    json.dump(_pm, f, indent=2)
            _append_mixed_answer_flags(ocr_dir, _flagged)
            _append_recovery_flags(ocr_dir, _rescue_reasons)
            # Refresh the OCR key list so the aligned_db build below sees the split/recovered keys.
            with open(ocr_answers_path, encoding="utf-8") as f:
                ocr_keys = list(json.load(f).keys())

        # Detect internal-choice groups from the UN-enriched key text FIRST, so the question-paper
        # overlay below can never perturb the structural OR-scan; reused later for the merges. Their
        # ids are canonicalized to match the now-canonical key (sidecar member '21(a)' -> 'Q21(a)').
        choices = _canonicalize_choices(_load_or_detect_choices(answer_key_path, manual_db))
        # Overlay the question paper's fuller question text onto the key entries (question field
        # only). Runs BEFORE alignment + both merges so the richer text propagates everywhere via
        # dict(manual_db[...]) copies. No-op when no question paper was uploaded.
        manual_db = _overlay_question_paper(manual_db, question_paper_path)

        # Normalise IDs based on OCR keys
        aligned_db = {}
        for ocr_k in ocr_keys:
            if ocr_k == "_instructions_": continue
            # Check for exact match (Q1) or base match (Q1.a -> Q1)
            base_k = ocr_k.split(".")[0] if "." in ocr_k else ocr_k
            
            if ocr_k in manual_db:
                aligned_db[ocr_k] = dict(manual_db[ocr_k])
            elif base_k in manual_db:
                aligned_db[ocr_k] = dict(manual_db[base_k])
                aligned_db[ocr_k]["question_id"] = ocr_k
                
        # Append any un-matched manual keys
        for mk, mv in manual_db.items():
            if mk not in aligned_db and f"{mk}" not in [k.split(".")[0] for k in aligned_db.keys()]:
                aligned_db[mk] = mv
                
        with open(db_answers_path, "w") as f:
            json.dump(aligned_db, f, indent=2)
            
    else:
        print("ERROR: Database fallback is completely disabled. Missing Answer Key.")
        return {"error": "Missing Answer Key", "details": "The database fallback has been completely removed from the pipeline. You MUST provide a manual answer key to evaluate the student paper."}

    # 4.9 Per-answer region crops (DISPLAY-ONLY, opt-in via ANSWER_CROPS). Launched in a BACKGROUND
    # thread so the vision pass overlaps grading below and costs ~zero wall-clock. Mirrors the diagram
    # thread's discipline: stale state cleared first, sentinel ALWAYS dropped in `finally`, and the env
    # vars set ONLY when the thread actually started. Unlike diagrams this NEVER falls back to running
    # inline -- screenshots are cosmetic, so a launch failure degrades to "report without screenshots"
    # rather than adding latency. Touches no grading input and no marks.
    crop_thread = None
    answer_crops_path = os.path.join(output_base, "answer_crops.json")
    answer_crops_sentinel = answer_crops_path + ".done"
    if str(env.get("ANSWER_CROPS", "0")).strip().lower() not in ("0", "false", "no", "off", ""):
        crop_script = os.path.join(skills_dir, "feature-extracter/scripts/crop_answer_regions.py")
        crop_env = dict(env)          # isolated snapshot: the main thread mutates `env` further below
        for _cp in (answer_crops_path, answer_crops_sentinel):
            try:
                if os.path.exists(_cp):
                    os.remove(_cp)
            except OSError:
                pass

        def _run_answer_crops():
            try:
                ok, out = run_command([PYTHON_EXE, crop_script, page_mapping_path, preprocessed_dir,
                                       answer_crops_path, ocr_answers_path], env=crop_env)
                if not ok:
                    print(f"Warning: answer region cropping failed: {out}")
            finally:
                try:
                    open(answer_crops_sentinel, "w").close()
                except OSError:
                    pass

        try:
            env["ANSWER_CROPS_JSON"] = answer_crops_path
            env["ANSWER_CROPS_SENTINEL"] = answer_crops_sentinel
            crop_thread = threading.Thread(target=_run_answer_crops, daemon=True)
            crop_thread.start()
            print("Answer region cropping launched in background (overlaps grading).")
        except Exception as _ce:
            print(f"Warning: answer-crop launch failed ({_ce}); continuing without screenshots.")
            env.pop("ANSWER_CROPS_JSON", None)
            env.pop("ANSWER_CROPS_SENTINEL", None)
            crop_thread = None

    # 5. Diagram Processing (Detection -> Extraction -> Evaluation)
    if is_cancelled(run_id):
        return _cancelled_result()
    print("\n--- [5/6] Diagram Processing ---")
    _write_progress(output_base, 2)                       # step 2: Analyzing diagrams
    diagram_crops_path = os.path.join(output_base, "diagram_crops.json")
    student_features_path = os.path.join(output_base, "student_features.json")
    diagram_evals_path = os.path.join(output_base, "diagram_evals.json")
    
    detect_script = os.path.join(skills_dir, "feature-extracter/scripts/detect_diagrams.py")
    success, output = run_command([PYTHON_EXE, detect_script, ocr_answers_path, page_mapping_path, preprocessed_dir, diagram_crops_path], env=env)
    
    diagrams_found = False
    if os.path.exists(diagram_crops_path):
        with open(diagram_crops_path, encoding="utf-8") as f:
            if len(json.load(f)) > 0:
                diagrams_found = True

    # Diagram feature-extraction + evaluation -- the two slow vision sub-stages. With PARALLEL_EVAL on
    # (default) they run in a BACKGROUND THREAD so they OVERLAP grading's LLM calls below: the two are
    # data-independent until diagram marks are folded into the report. The thread reads a SNAPSHOT of
    # the un-merged key (db_answers_diagram.json) so the merges below -- which rewrite db_answers_path --
    # cannot perturb its inputs; diagram grading stays byte-identical to the sequential path (only the
    # SCHEDULING changes). A sentinel (diagram_evals.json.done) signals completion to evaluate.py, which
    # waits on it before merging. Set PARALLEL_EVAL=0 to force the old strictly-sequential path.
    diag_thread = None
    parallel_eval = str(env.get("PARALLEL_EVAL", "1")).strip().lower() not in ("0", "false", "no", "")
    if diagrams_found:
        extract_script = os.path.join(skills_dir, "feature-extracter/scripts/extract_features.py")
        eval_diag_script = os.path.join(skills_dir, "diagram_evaluator/scripts/evaluate_diagrams.py")
        diagram_sentinel = diagram_evals_path + ".done"

        # DISPLAY path. `diagram_crops.json` is misnamed: detect_diagrams.py writes a question -> full
        # PAGE map, nothing is cropped there. With DIAGRAM_CROPS on, crop_diagram_regions.py turns it
        # into diagram_display_crops.json (adds a tight `crop` per entry) and the report is pointed at
        # THAT; evaluate.py:2154 already prefers `crop` and falls back to the full page `image`.
        #
        # Grading is untouched BY CONSTRUCTION, not by convention: extract_features.py and
        # evaluate_diagrams.py receive `diagram_crops_path` as ARGV below, so repointing this env var
        # cannot reach them. A bad crop therefore can never change a mark.
        crop_diag_script = os.path.join(skills_dir, "feature-extracter/scripts/crop_diagram_regions.py")
        diagram_display_path = os.path.join(output_base, "diagram_display_crops.json")
        want_diagram_crops = str(env.get("DIAGRAM_CROPS", "1")).strip().lower() not in ("0", "false", "no", "off", "")
        env["DIAGRAM_CROPS_JSON"] = diagram_crops_path       # full pages unless the seed below succeeds
        if want_diagram_crops:
            # SEED the display file on THIS thread, before grading launches. `env` is snapshotted when
            # the grading subprocess starts, so a value set later from the background thread would never
            # reach evaluate.py. Seeding with crop=None means the file always exists and is valid: if
            # cropping then fails or is killed, the report shows full pages exactly as it does today.
            # The cropper overwrites it ATOMICALLY, so evaluate.py can never read a half-written file.
            try:
                with open(diagram_crops_path, encoding="utf-8") as _dcf:
                    _seed = [{"question_id": _e.get("question_id"), "image": _e.get("image"),
                              "crop": None, "reason": "not cropped yet"} for _e in json.load(_dcf)]
                with open(diagram_display_path, "w") as _sf:
                    json.dump(_seed, _sf)
                env["DIAGRAM_CROPS_JSON"] = diagram_display_path
            except Exception as _se:
                print(f"Warning: could not seed diagram display crops ({_se}); report shows full pages.")
                want_diagram_crops = False
        # Isolated env copy for the diagram subprocesses, so later mutations of `env` on the main thread
        # (student details, report dir) can't race the background thread's subprocess launches.
        diag_env = dict(env)

        def _display_crops():
            ok_c, out_c = run_command([PYTHON_EXE, crop_diag_script, diagram_crops_path,
                                       diagram_display_path], env=diag_env)
            if not ok_c:
                print(f"Warning: diagram display cropping failed ({out_c}); "
                      f"report falls back to the seeded full pages.")

        def _run_diagrams(db_for_diagrams):
            # features -> evaluation, with display crops ALONGSIDE; ALWAYS drop the sentinel last (even
            # on failure) so a stalled or failed diagram job degrades to "grade without diagrams" rather
            # than hanging the grader.
            crop_t = None
            try:
                # Display crops run CONCURRENTLY with the graders, not ahead of them. They used to run
                # first, on the claim that being inside this background job cost no critical-path time
                # -- true only while GRADING was the long pole. It is not: evaluate.py blocks on this
                # job's sentinel, so this job IS the critical path, and a display-only pass was
                # measured adding ~33s in front of a stage that never reads its output (both graders
                # take `diagram_crops_path`, the full-page map, via ARGV -- so a crop still cannot
                # change a mark). Safe to overlap: the cropper writes diagram_display_crops.json
                # atomically (temp + os.replace), and both readers here only READ diagram_crops.json.
                if want_diagram_crops:
                    crop_t = threading.Thread(target=_display_crops, daemon=True)
                    crop_t.start()
                ok, out = run_command([PYTHON_EXE, extract_script, diagram_crops_path], env=diag_env)
                if not ok:
                    print(f"Warning: Diagram feature extraction failed: {out}")
                    _flag_unassessed_diagrams(output_base, diagram_crops_path, features_json=None)
                else:
                    # `out` is the stage's RAW stdout, not json.dump output, so it still holds real
                    # non-ASCII (diagram features are full of "->", "H2O" subscripts, degree signs).
                    # Without an explicit encoding Windows writes it as cp1252 and raises. Same for
                    # diagram_evals below, and evaluate.py reads both back as utf-8.
                    with open(student_features_path, "w", encoding="utf-8") as f:
                        f.write(out)
                    # A crop can stall and be abandoned (see extract_features), so features may cover
                    # only SOME of the diagram questions. Record the gap: those questions keep their
                    # written-answer mark, and a silently un-assessed diagram must never look like a
                    # deliberate 0 to the teacher.
                    _flag_unassessed_diagrams(output_base, diagram_crops_path, features_json=out)
                    ok2, out2 = run_command([PYTHON_EXE, eval_diag_script, diagram_crops_path,
                                             student_features_path, db_for_diagrams], env=diag_env)
                    if not ok2:
                        print(f"Warning: Diagram evaluation failed: {out2}")
                    else:
                        with open(diagram_evals_path, "w", encoding="utf-8") as f:
                            f.write(out2)
            finally:
                # Join the display cropper BEFORE the sentinel drops: evaluate.py reads
                # DIAGRAM_CROPS_JSON only after this sentinel, so that ordering is what guarantees it
                # never sees a half-finished crop set -- the same guarantee the old serial order gave,
                # now for free (cropping is far shorter than features+evaluation, so this join is
                # normally instant). Bounded by the cropper's own watchdog; on timeout the seeded
                # full-page entries stand and the report simply shows whole pages.
                if crop_t is not None:
                    crop_t.join(timeout=(_stage_timeout("crop_diagram_regions.py") or 180) + 10)
                    if crop_t.is_alive():
                        print("Warning: diagram display cropping did not finish in time; "
                              "report falls back to the seeded full pages.")
                try:
                    open(diagram_sentinel, "w").close()
                except OSError:
                    pass

        # Clear stale evals/sentinel from a prior run so a failure can't reuse old diagram marks.
        for _p in (diagram_evals_path, diagram_sentinel):
            try:
                if os.path.exists(_p):
                    os.remove(_p)
            except OSError:
                pass

        if parallel_eval:
            try:
                db_snapshot = os.path.join(output_base, "db_answers_diagram.json")
                shutil.copyfile(db_answers_path, db_snapshot)     # un-merged key, isolated for diagrams
                env["DIAGRAM_EVALS_SENTINEL"] = diagram_sentinel  # grading subprocess blocks on this
                diag_thread = threading.Thread(target=_run_diagrams, args=(db_snapshot,), daemon=True)
                diag_thread.start()
                print("Diagram processing launched in background (overlaps grading).")
            except Exception as _pe:
                print(f"Warning: parallel diagram launch failed ({_pe}); running sequentially.")
                env.pop("DIAGRAM_EVALS_SENTINEL", None)
                diag_thread = None
                _run_diagrams(db_answers_path)
        else:
            _run_diagrams(db_answers_path)

    # 5.4 Internal-choice (OR) handling: collapse "answer any ONE" alternatives (e.g. Q31(a) OR
    # Q31(b)) into one parent key so the student's chosen answer is graded against a real entry
    # and the group counts once. Parser-supplied choices win; structural detection is the fallback.
    # `choices` was computed above from the un-enriched key (before question-paper overlay).
    merged_choice = merge_choice_groups(ocr_answers_path, db_answers_path, manual_db,
                                        choices.get("choice_groups"))
    if merged_choice:
        print(f"Merged internal-choice groups: {merged_choice}")

    # 5.45 Collapse ADDITIVE multi-part questions whose key was split by sub-part (e.g.
    # Q37(a)+Q37(b)+Q37(c)) into a single parent key Qn, summing the sub-part marks, so the key
    # matches the OCR's one-block-per-question granularity. Without this, the sibling sub-parts the
    # OCR did not separately label render BLANK -> 0 even though the student answered them (the
    # case-study bug). Runs AFTER the choice merge (so 'answer any one' pairs are already collapsed
    # to a bare parent and skipped) and BEFORE the OCR-side sub-part merge. No-op when the key
    # already has one entry per question number, so unsplit keys behave exactly as before.
    merged_additive = merge_additive_subparts(ocr_answers_path, db_answers_path)
    if merged_additive:
        print(f"Collapsed additive multi-part questions: {merged_additive}")

    # 5.5 Collapse multi-part answers into their parent key BEFORE evaluation, so recorded
    # maximum marks never exceed the answer key (sub-parts were each inheriting full parent
    # marks). Runs after diagram processing so diagrams stay correctly localized per sub-part.
    merged_parents = merge_subparts_into_parents(ocr_answers_path, db_answers_path, manual_db)
    if merged_parents:
        print(f"Collapsed multi-part questions for evaluation: {merged_parents}")

    # 5.6 Stamp choice flags AFTER the sub-part merge (which rebuilds parent entries from the key):
    # inline_or on inline-OR (multi-part, sub-part OR -> graded additively), is_choice_uncertain on
    # heuristic detections. Genuine whole-question choices were tagged is_choice by merge_choice_groups.
    _finalize_choice_flags(db_answers_path, merged_choice, choices.get("inline_choice_ids"),
                           choices.get("uncertain") or set())

    # 5.7 Deterministic safety net against answer-key PARSE gaps. The key parser can silently drop a
    # part of a multi-part question, shrinking the denominator (the Science Q37/38/39 case: 2 marks
    # each instead of 4 -> report max 74 not 80, with no warning). Anchor each question's MAXIMUM to
    # the independently-parsed question paper (raise-only) and flag any question we had to correct, so
    # a future key whose parts parse differently can never again silently deflate the total. Runs LAST
    # so it is the final write to db_answers.json before grading; no-op without a question paper.
    integrity = reconcile_marks_with_question_paper(db_answers_path, question_paper_path,
                                                    mode=_reconcile_mode)
    if integrity.get("injected"):
        # A dropped question was re-injected into the key -> the OCR/question-set derivation ran without
        # it, but grading iterates the key, so it will still be graded (as [BLANK] if unanswered) and
        # counted. Nothing more to do here; the per-question review flag carries the explanation.
        pass

    # 6. Evaluation & Report
    if is_cancelled(run_id):
        return _cancelled_result()
    print("\n--- [6/6] Final Evaluation ---")
    _write_progress(output_base, 3)                       # step 3: Grading & building report
    evaluate_script = os.path.join(skills_dir, "answer-evaluator-and-report-generation/scripts/evaluate.py")

    # Assemble the student details used to NAME the report ({Name}_{RollNo}) and to render the
    # details block. Name: teacher-provided wins, else the sheet's OCR name. Roll: from the sheet
    # header. Class + Subject: from the answer key (exam_class/exam_subject; subject falls back to
    # the key's per-question subject). Written to a JSON the evaluator reads via STUDENT_DETAILS_JSON.
    sheet_meta = {}
    meta_path = os.path.join(ocr_dir, "student_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                sheet_meta = json.load(f)
        except Exception:
            sheet_meta = {}
    resolved_name = _resolve_student_name(student_name, _clean_meta(sheet_meta.get("Name")))
    # Exam metadata (question-paper name, section, date, time, duration) collected once at Step 1
    # upload -- saved to current_exam_metadata.json by /parse-question-paper. Read here (not passed
    # as a function arg) so every evaluation path -- single, batch, regrade -- picks it up the same
    # way without threading a new parameter through each caller.
    _exam_meta = {}
    _exam_meta_path = os.path.join(os.path.dirname(os.path.dirname(output_base)), "evaluation_app",
                                    "uploads", "current_exam_metadata.json")
    if os.path.exists(_exam_meta_path):
        try:
            with open(_exam_meta_path) as _mf:
                _exam_meta = json.load(_mf) or {}
        except (OSError, json.JSONDecodeError):
            _exam_meta = {}

    student_details = {
        "name": resolved_name,
        "roll_no": _clean_meta(sheet_meta.get("Roll No")),
        "class": (exam_class or "").strip() or _exam_meta.get("class", ""),
        "subject": (exam_subject or "").strip() or _first_db_subject(manual_db) or _exam_meta.get("subject", ""),
        "tester_id": (tester_id or "").strip(),   # who ran this eval (teacher/school) -- for report collection
        "section": _exam_meta.get("section", ""),
        "qp_name": _exam_meta.get("qp_name", ""),
        "exam_date": _exam_meta.get("date", ""),
        "exam_time": _exam_meta.get("time", ""),
        "exam_duration": _exam_meta.get("duration", ""),
    }
    details_path = os.path.join(output_base, "student_details.json")
    with open(details_path, "w") as f:
        json.dump(student_details, f, indent=2)
    env["STUDENT_DETAILS_JSON"] = details_path

    # When a confirmed report folder is provided, evaluate.py saves "{Name}_{RollNo}.pdf" there.
    if report_dir:
        env["REPORT_OUTPUT_DIR"] = os.path.expanduser(report_dir)

    eval_args = [PYTHON_EXE, evaluate_script, resolved_name or student_name, ocr_answers_path, db_answers_path]
    # Pass the diagram-evals path so the grader can fold in diagram marks. In parallel mode the file may
    # not exist YET (background thread still running); pass it anyway -- evaluate.py blocks on
    # DIAGRAM_EVALS_SENTINEL until the diagram thread finishes (or a bounded timeout). In sequential
    # mode, pass it only when present (unchanged behaviour).
    if diag_thread is not None or os.path.exists(diagram_evals_path):
        eval_args.append(diagram_evals_path)

    success, output = run_command(eval_args, env=env)
    if diag_thread is not None:
        diag_thread.join(timeout=10)   # grading already waited on the sentinel; cleanup only
    if crop_thread is not None:
        crop_thread.join(timeout=10)   # ditto: evaluate.py waited on ANSWER_CROPS_SENTINEL
    if not success: return {"error": "Evaluation failed", "details": output}

    try:
        json_blocks = re.findall(r'\{.*\}', output, re.DOTALL)
        report_data = {}
        for block in json_blocks:
            try:
                candidate = json.loads(block)
                if "report_path" in candidate:
                    report_data = candidate
                    break
            except:
                continue
        
        if not report_data:
            clean_json = output.split('{', 1)[-1].rsplit('}', 1)[0] + '}'
            report_data = json.loads(clean_json)

        # Persist a pristine snapshot of this run so the teacher review/override step can reload
        # the authoritative AI marks and regenerate the report without re-OCR/re-grading. Written
        # once and never mutated afterwards, so the DB's "original marks" stay truthful across
        # repeated reviews.
        try:
            review_state = {
                "review_id": run_id,
                "student_name": resolved_name or student_name,
                "evaluations": report_data.get("evaluations", []),
                "student_details": report_data.get("student_details") or student_details,
                "report_dir": os.path.expanduser(report_dir) if report_dir else "",
                "report_path": report_data.get("report_path"),
                "exam_class": exam_class or "",
                "exam_subject": exam_subject or "",
                "tester_id": (tester_id or "").strip(),
            }
            with open(os.path.join(output_base, "review_state.json"), "w") as f:
                json.dump(review_state, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not persist review state: {e}")

        # Reproducibility stamp for report collection (report_sync): the exact models + key flags THIS run
        # graded with, so an archived report stays attributable even if .env later changes. Best-effort --
        # a failed stamp must never interrupt grading; report_sync falls back to reading .env when absent.
        try:
            _RM_MODELS = ("OCR_MODEL", "EVAL_MODEL", "KEY_PARSER_MODEL", "QP_PARSER_MODEL",
                          "DIAGRAM_EVAL_MODEL", "DIAGRAM_FEATURES_MODEL", "SEPARATOR_MODEL")
            _RM_FLAGS = ("EVAL_CASCADE", "EVAL_POINTWISE", "EVAL_VOTES", "EVAL_MAX_TOKENS",
                         "EVAL_REASONING_EFFORT", "OCR_ORIENT_VOTE", "OCR_ARBITRATE", "OCR_VERIFY_MATH",
                         "LLM_PROVIDER_SORT", "LLM_PROVIDER_ORDER", "RECONCILE_KEY_MARKS_WITH_QP",
                         "LLM_USAGE_ACCOUNTING", "ANSWER_CROPS", "DIAGRAM_CROPS")
            with open(os.path.join(output_base, "run_meta.json"), "w") as _rmf:
                json.dump({"models": {k: env[k] for k in _RM_MODELS if env.get(k)},
                           "flags": {k: env[k] for k in _RM_FLAGS if env.get(k)}}, _rmf, indent=2)
        except Exception as _rm_e:
            print(f"Warning: could not write run_meta.json: {_rm_e}")

        # Per-paper cost = sum of every stage's ledger entry (OCR + grading + diagrams): the REAL
        # provider-billed amount (OpenRouter usage.cost) where available, else the per-model estimate.
        cost_total, cost_breakdown = _read_cost_ledger(cost_ledger_path)

        # Profiling: per-subprocess wall-clock + run total. In parallel mode diagram + grading overlap,
        # so the per-stage times can SUM to more than the wall-clock total -- that gap IS the speedup.
        try:
            _wall_total = time.perf_counter() - _wall_start
            _by_label = {}
            for _lbl, _sec in _STAGE_TIMINGS:
                _by_label[_lbl] = round(_by_label.get(_lbl, 0.0) + _sec, 2)
            print("\n--- Stage timings (subprocess wall-clock seconds) ---")
            for _lbl, _sec in sorted(_by_label.items(), key=lambda kv: -kv[1]):
                print(f"  {_sec:7.1f}s  {_lbl}")
            print(f"  {_wall_total:7.1f}s  TOTAL wall-clock (diagram+grading overlap in parallel mode)")
            with open(os.path.join(output_base, "stage_timings.json"), "w") as _tf:
                json.dump({"total_wall_s": round(_wall_total, 2), "per_stage_s": _by_label,
                           "calls": _STAGE_TIMINGS, "parallel_eval": parallel_eval}, _tf, indent=2)
        except Exception as _te:
            print(f"Warning: could not write stage timings: {_te}")

        return {
            "status": "success",
            "report_path": report_data.get("report_path"),
            "cost": f"${cost_total:.6f}",
            "cost_breakdown": cost_breakdown,
            "evaluations": report_data.get("evaluations", []),
            "student_details": report_data.get("student_details") or student_details,
            "review_id": run_id
        }
    except Exception as e:
        print(f"Error parsing evaluation output: {e}")
        return {"status": "error", "error": "Failed to parse evaluation result", "details": output}


# ---------------------------------------------------------------------------
# Human-in-the-loop orientation gate (web flow): pause between preprocess and OCR so a teacher can
# confirm/fix each page's orientation, then resume into the SAME OCR->report tail full_evaluate uses.
# prepare_orientation + resume_after_orientation reuse the run's setup + preprocessed dir; when the
# teacher confirms zero rotations the images are byte-identical to today -> grading is unchanged.
# ---------------------------------------------------------------------------

_RUN_RESET_PRESERVE = ("batch_sheet_args.json",)


def _reset_run_dir(output_base, preserve=_RUN_RESET_PRESERVE):
    """Wipe a run's derived artifacts so a FRESH run starts from an empty folder.

    Needed because `run_id` is the uploaded file's stem: re-uploading a corrected sheet under the same
    name reuses the same output folder. Nothing downstream truncates it -- `process_input.py` and
    `preprocess.py` both `makedirs(exist_ok=True)` and only overwrite the pages they emit -- while both
    `_ingest_and_preprocess` and the OCR stage **glob** those folders. So a 5-page sheet replaced by a
    2-page one left pages 3-5 of the OLD sheet on disk and OCR read all five. Verified before the fix:
    5 images in, 2-page re-upload, 5 images still there.

    Two further hazards this closes: a stale `review_state.json` meant a run that died mid-pipeline
    still served the PREVIOUS grading as if current, and per-run JSON (page_mapping, diagram/answer-crop
    manifests) could describe pages the new sheet does not have.

    Everything under `output/<run_id>/` is regenerated by the pipeline, so this removes all of it except
    `preserve` -- `batch_sheet_args.json` is the batch parent's IPC input and is written before the
    subprocess that calls this even starts.

    MUST NOT be called from `resume_after_orientation`: phase 2 of the orientation gate consumes the
    `preprocessed/` images and `orientation_review.json` that phase 1 left behind.
    """
    if not os.path.isdir(output_base):
        return 0
    keep = set(preserve or ())
    removed = 0
    for name in os.listdir(output_base):
        if name in keep:
            continue
        path = os.path.join(output_base, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as e:
            # Best-effort: a locked/undeletable leftover must not abort the evaluation.
            print(f"Warning: could not clear stale {name}: {e}")
    if removed:
        print(f"Cleared {removed} stale item(s) from a previous run of this sheet.")
    return removed


def _setup_run(run_id, marks_source=None, env_overrides=None, reset=False):
    """Per-run setup shared by the orientation gate: resolve dirs, overlay the project .env, reset
    the cost ledger + profiling. `run_id` is the output-folder name (Path(input_file).stem). Mirrors
    full_evaluate's inline setup exactly so a resumed run feeds OCR the same dirs/env a straight run
    would. Returns a ctx dict."""
    reconcile_mode = {"question_paper": "align_to_paper",
                      "answer_key": "trust_key"}.get(marks_source, "raise")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    skills_dir = os.path.join(project_root, "skills")

    output_base = os.path.join(project_root, "output", run_id)
    os.makedirs(output_base, exist_ok=True)
    if reset:
        _reset_run_dir(output_base)

    images_dir = os.path.join(output_base, "images")
    preprocessed_dir = os.path.join(output_base, "preprocessed")
    ocr_dir = os.path.join(output_base, "ocr_output")
    ocr_answers_path = os.path.join(ocr_dir, "ocr_answers.json")
    db_answers_path = os.path.join(output_base, "db_answers.json")
    page_mapping_path = os.path.join(ocr_dir, "page_mapping.json")

    env = os.environ.copy()
    env_file = os.path.join(project_root, ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if v[:1] not in ('"', "'"):
                        v = re.sub(r'\s+#.*$', '', v).strip()  # drop an inline comment from unquoted values
                    v = v.strip('"').strip("'")
                    env[k] = v

    # Per-call overrides win over os.environ AND the .env overlay (applied last) -- see full_evaluate.
    if env_overrides:
        for _ok, _ov in env_overrides.items():
            env[str(_ok)] = str(_ov)

    cost_ledger_path = os.path.join(output_base, "api_costs.jsonl")
    try:
        if os.path.exists(cost_ledger_path):
            os.remove(cost_ledger_path)
    except OSError:
        pass
    env["API_COST_LOG"] = cost_ledger_path
    # Lets run_command attribute each stage subprocess to this run so a cancel can kill its process
    # group. Carried in `env` (not a thread-local) so the answer-crop pass, which runs on its own
    # thread with a COPY of this env, is cancellable too.
    env["RUN_ID"] = run_id

    _STAGE_TIMINGS.clear()
    wall_start = time.perf_counter()

    return {"run_id": run_id, "skills_dir": skills_dir, "output_base": output_base,
            "images_dir": images_dir, "preprocessed_dir": preprocessed_dir, "ocr_dir": ocr_dir,
            "ocr_answers_path": ocr_answers_path, "db_answers_path": db_answers_path,
            "page_mapping_path": page_mapping_path, "env": env,
            "cost_ledger_path": cost_ledger_path, "wall_start": wall_start,
            "reconcile_mode": reconcile_mode}


def _ingest_and_preprocess(ctx, input_file):
    """Stages 1-2 (ingest -> preprocessed/*.png), same commands full_evaluate runs. Returns None on
    success, else the same error dict full_evaluate would return."""
    skills_dir, env = ctx["skills_dir"], ctx["env"]
    print("\n--- [1/5] Ingestion ---")
    ingestion_script = os.path.join(skills_dir, "ingestion-handler/scripts/process_input.py")
    ok, out = run_command([PYTHON_EXE, ingestion_script, input_file, "--output-dir", ctx["images_dir"]], env=env)
    if not ok:
        return {"error": "Ingestion failed", "details": out}
    print("\n--- [2/5] Preprocessing ---")
    preprocess_script = os.path.join(skills_dir, "img-preprocessing/scripts/preprocess.py")
    image_files = [str(p) for p in Path(ctx["images_dir"]).glob("*.png")]
    ok, out = run_command([PYTHON_EXE, preprocess_script] + image_files + ["--output-dir", ctx["preprocessed_dir"]], env=env)
    if not ok:
        return {"error": "Preprocessing failed", "details": out}
    return None


def _resume_tail(ctx, student_name, answer_key_path, question_paper_path, report_dir,
                 exam_class, exam_subject, tester_id=None):
    """Invoke the shared OCR->report tail from a ctx (used by resume_after_orientation)."""
    return _evaluate_from_preprocessed(
        ctx["run_id"], ctx["output_base"], ctx["skills_dir"], ctx["preprocessed_dir"],
        ctx["ocr_dir"], ctx["ocr_answers_path"], ctx["db_answers_path"], ctx["page_mapping_path"],
        ctx["env"], ctx["cost_ledger_path"], ctx["reconcile_mode"], ctx["wall_start"],
        student_name, answer_key_path, question_paper_path, report_dir, exam_class, exam_subject,
        tester_id)


def prepare_orientation(input_file, student_name="Student", answer_key_path=None, report_dir=None,
                        exam_class=None, exam_subject=None, question_paper_path=None, marks_source=None,
                        tester_id=None):
    """Phase 1 of the orientation gate: ingest + preprocess, then compute a per-page cardinal
    orientation SUGGESTION (Tesseract OSD, local) WITHOUT modifying any image, and STOP before OCR.
    Returns a review manifest for the teacher; resume_after_orientation continues once they confirm.
    On a Stage 1/2 failure returns the same error dict full_evaluate would. Unused args are accepted
    so a caller can pass the identical kwargs it passes to full_evaluate/resume."""
    # reset=True: this is a FRESH upload, so clear anything a previous run of the same sheet left
    # behind (see _reset_run_dir). resume_after_orientation deliberately does NOT reset -- it consumes
    # the preprocessed pages this phase produces.
    ctx = _setup_run(Path(input_file).stem, marks_source, reset=True)
    err = _ingest_and_preprocess(ctx, input_file)
    if err:
        return err

    print("\n--- Orientation review (assisted first pass) ---")
    review_path = os.path.join(ctx["output_base"], "orientation_review.json")
    orient_script = os.path.join(ctx["skills_dir"], "orientation-correction/scripts/orient_pages.py")
    ok, out = run_command([PYTHON_EXE, orient_script, ctx["preprocessed_dir"], review_path], env=ctx["env"])
    if not ok or not os.path.exists(review_path):
        return {"error": "Orientation review failed", "details": out}
    try:
        with open(review_path, encoding="utf-8") as f:
            pages = json.load(f).get("pages", [])
    except Exception as e:
        return {"error": "Orientation review unreadable", "details": str(e)}
    return {"status": "orient_review", "run_id": ctx["run_id"], "output_base": ctx["output_base"],
            "preprocessed_dir": ctx["preprocessed_dir"], "pages": pages}


def _apply_page_rotation(path, deg):
    """Rotate one preprocessed PNG in place by `deg` clockwise (0/90/180/270). deg==0 is a true
    no-op (bytes untouched) so a zero-rotation confirm keeps OCR input identical to today. PIL's
    +ve angle is CCW, hence -deg for clockwise."""
    deg = int(deg) % 360
    if deg == 0:
        return
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        rotated = im.convert("RGB").rotate(-deg, expand=True)
    rotated.save(path)


def resume_after_orientation(run_id, rotations=None, student_name="Student", answer_key_path=None,
                             report_dir=None, exam_class=None, exam_subject=None,
                             question_paper_path=None, marks_source=None, tester_id=None,
                             env_overrides=None):
    """Phase 2 of the orientation gate: apply the teacher's confirmed per-page ABSOLUTE clockwise
    rotations to the pristine preprocessed images, then run the SAME OCR->report tail full_evaluate
    runs. `rotations` maps page index (1-based; str or int key) to an angle in {0,90,180,270}. The
    index->file map is read from orientation_review.json written by prepare_orientation."""
    ctx = _setup_run(run_id, marks_source, env_overrides=env_overrides)
    rotations = rotations or {}

    review_path = os.path.join(ctx["output_base"], "orientation_review.json")
    try:
        with open(review_path, encoding="utf-8") as f:
            pages = json.load(f).get("pages", [])
    except Exception as e:
        return {"error": "Orientation review missing",
                "details": f"Run prepare_orientation before resuming (could not read {review_path}: {e})"}

    applied = []
    for p in pages:
        idx, fname = p.get("index"), p.get("file")
        if idx is None or not fname:
            continue
        deg = rotations.get(str(idx), rotations.get(idx, 0)) or 0
        try:
            deg = int(deg) % 360
        except (TypeError, ValueError):
            deg = 0
        if deg:
            _apply_page_rotation(os.path.join(ctx["preprocessed_dir"], fname), deg)
            applied.append((idx, deg))
    if applied:
        print(f"Applied teacher-confirmed rotations to {len(applied)} page(s): {applied}")

    return _resume_tail(ctx, student_name, answer_key_path, question_paper_path, report_dir,
                        exam_class, exam_subject, tester_id)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 full_evaluator.py <input_file> [student_name]")
        sys.exit(1)
    
    res = full_evaluate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "Student")
    print(json.dumps(res, indent=2))
