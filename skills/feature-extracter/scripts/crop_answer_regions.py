"""Per-answer region crops for the report (DISPLAY-ONLY).

The `[START_Q: n]` / `[END_Q: n]` tags the OCR emits are TEXT ONLY -- they carry no pixel coordinates,
and page_mapping.json is page-level -- so an answer's region has to be derived here:

  1. a vision call returns the y of each answer's START anchor on the page. It is ANCHORED by the
     question list page_mapping already knows, so the model only LOCATES, it never segments;
  2. each anchor is SNAPPED to the nearest whitespace gap (row ink-projection, per-page Otsu
     threshold) so a cut can never slice through a line of handwriting;
  3. the band for answer n runs anchor(n) -> anchor(n+1) (page bottom for the last one) at FULL page
     width, so consecutive bands share edges: nothing can fall between two crops, and there is no
     left/right clipping error. This is exactly the START_Q(n) -> START_Q(n+1) region;
  4. the band is ink-trimmed to its first/last inked row (+pad), which is what makes the crop hug the
     answer ("nothing outside it") and stops the last answer on a page trailing into blank paper.

Anything that fails validation falls back to the FULL PAGE, so the report shows a correct crop or the
whole page -- never a misleading crop.

DISPLAY-ONLY: never touches ocr_answers.json / db_answers.json / grading inputs / marks. If the API is
unavailable every entry degrades to a full-page image.

COORDINATE UNITS (the single most important detail here): Qwen-VL grounding is NATIVELY normalized to
0-1000 and it returns that scale even when asked for absolute pixels -- verified, it gave the same
~0-900 values on a 2730px page either way. Those values MUST be multiplied by page_height/1000. Reading
them as raw pixels collapsed every anchor into the top ~17% of the page (the printed header), which was
the true cause of the mis-cropping, not any weakness in the model's localisation.

Usage: crop_answer_regions.py <page_mapping.json> <preprocessed_dir> <out_manifest.json> [ocr_answers.json]
"""
import os
import re
import sys
import json
import concurrent.futures

import numpy as np
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

# Provider-agnostic LLM client + cost meter (live in scripts/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))
from llm_client import generate, strip_reasoning, get_real_cost  # noqa: E402
try:
    from llm_pricing import log_cost
except Exception:  # pragma: no cover - pricing is best-effort
    def log_cost(*a, **k):
        pass


def _env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


# Default to the SAME instruct model the OCR stage uses. Measured on a real 30-page sheet: the small
# 30B VL model degenerates on this task -- asked for ONE y it emitted hundreds of numbers until it hit
# the token cap (13/59 answers lost to truncated JSON). The 235B instruct answers in ~11 output tokens,
# so it is both far more reliable AND costs almost nothing here (output is a handful of integers).
CROP_MODEL = os.environ.get("ANSWER_CROP_MODEL",
                            os.environ.get("OCR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"))
MAX_WORKERS = _env_int("ANSWER_CROP_MAX_WORKERS", 8)
MAX_WIDTH = _env_int("ANSWER_CROP_MAX_WIDTH", 1100)
JPEG_QUALITY = _env_int("ANSWER_CROP_JPEG_QUALITY", 80)
PAD_PX = _env_int("ANSWER_CROP_PAD_PX", 8)
SNAP_WINDOW = _env_int("ANSWER_CROP_SNAP_WINDOW", 80)
MIN_BAND_PX = _env_int("ANSWER_CROP_MIN_BAND_PX", 40)
MIN_INK_ROWS = _env_int("ANSWER_CROP_MIN_INK_ROWS", 6)
# A band must contain at least one row DENSE enough to be handwriting, measured RELATIVE TO THIS PAGE's
# own densest row. Row COUNT alone is not enough: a curved or skewed ruled line survives the
# morphological strip (it is not a long straight run) and inks nearly every row of a thin band -- two
# real crops came out as 38px and 22px slivers of blank ruled paper that way, both passing the row count.
#
# Page-RELATIVE, not a fraction of page width: a first attempt used width and collapsed one sheet from
# 95% to 54%, because its pages are landscape (3509x2480) so real handwriting spans a much smaller
# fraction of the width than on a portrait sheet. Measured against the page's own peak the separation is
# two orders of magnitude and orientation-independent: blank-rule slivers 0.006-0.023, real answers
# 0.60-1.00.
try:
    MIN_PEAK_INK_RATIO = float(os.environ.get("ANSWER_CROP_MIN_PEAK_INK_RATIO", "0.10"))
except (TypeError, ValueError):
    MIN_PEAK_INK_RATIO = 0.10
# Qwen-VL grounding is NATIVELY normalized to 0-1000 regardless of image size.
COORD_SCALE = 1000
# Safety valve only. This was once 5, to dodge "dense objective pages always mis-crop" -- but that was
# the normalized-vs-pixel unit bug, not a localisation limit: with the coordinates read correctly a
# 6-answer Section-A page crops perfectly. Left high so it never fires on real papers.
MAX_STARTS_PER_PAGE = _env_int("ANSWER_CROP_MAX_STARTS_PER_PAGE", 99)
MAX_PAGES_PER_Q = _env_int("ANSWER_CROP_MAX_PAGES_PER_Q", 4)

_MAX_TOKENS = _env_int("ANSWER_CROP_MAX_TOKENS", 400)
# Re-sample a page whose reply fails validation. Measured on real sheets: the dominant failure was
# TRANSIENT -- two pages that each lost 6 answers to one bad sample passed 4/4 on re-sampling, while a
# genuinely ambiguous page failed 0/4. Only failing pages ever pay for this.
MAX_RETRIES = _env_int("ANSWER_CROP_MAX_RETRIES", 2)
# Retries warm up: a fixed prompt at temperature 0 is not reliably resampled, so a second identical
# attempt would often reproduce the same rejected reply.
try:
    RETRY_TEMPERATURE = float(os.environ.get("ANSWER_CROP_RETRY_TEMPERATURE", "0.3"))
except (TypeError, ValueError):
    RETRY_TEMPERATURE = 0.3

# Positional y-values ONLY. Asking the model to echo question ids made it answer with whatever labels
# it saw on the page ('1', '(ii)', 'Q31(i)'), which failed validation constantly; it only has to emit N
# numbers in the order we listed. Nothing else is asked for -- the continuation band starts at the page
# top and is ink-trimmed locally, so no extra field is needed.
_PROMPT_TMPL = """This is one page of a student's handwritten answer sheet.
{n} answer(s) begin on this page. In top-to-bottom order, each with the opening words already
transcribed from it:
{listing}

Find each answer by matching that transcribed text on the page.
IGNORE the printed form header entirely (roll number, class, subject, marks-obtained boxes, signature
boxes) and any printed rules -- anchor ONLY on the student's own handwriting.

Reply with ONLY this JSON object and nothing else:
{{"ys": [<int>, ...]}}

"ys" must hold exactly {n} integer(s), in the SAME top-to-bottom order as the list above. Each value is
the vertical position where that answer begins, as a NORMALIZED coordinate from 0 to 1000
(0 = the very top edge of the image, 1000 = the very bottom edge).
Every value must be a plain integer between 0 and 1000 -- never longer than 4 digits.
No other keys, no explanation, no transcription."""

# Continuation pages. These answers START on an earlier page, so there is no opening text to match on --
# the model is asked where each one ENDS instead. Without this a page could only be handed WHOLE to a
# single answer, which is why a page shared by two continuing answers silently dropped the second one.
_CONT_PROMPT_TMPL = """This is one page of a student's handwritten answer sheet.
{n} answer(s) that began on EARLIER pages continue onto this page, in this top-to-bottom order:
{listing}

They run from the top of this page downwards, one after another. Find where EACH one ENDS.
IGNORE the printed form header entirely (roll number, class, subject, marks boxes, signature boxes) and
any printed rules -- judge ONLY by the student's own handwriting.

Reply with ONLY this JSON object and nothing else:
{{"ys": [<int>, ...]}}

"ys" must hold exactly {n} integer(s), in the SAME order as the list above. Each value is the vertical
position where that answer ENDS, as a NORMALIZED coordinate from 0 to 1000 (0 = the very top edge of the
image, 1000 = the very bottom edge). Values must increase down the page. If the last answer runs to the
bottom of the page, give 1000 for it.
Every value must be a plain integer between 0 and 1000 -- never longer than 4 digits.
No other keys, no explanation, no transcription."""


# ---------------------------------------------------------------------------------------------
# Pure geometry / CV helpers (no I/O, no API -- unit-tested directly)
# ---------------------------------------------------------------------------------------------

def _otsu_threshold(gray):
    """Otsu threshold for a uint8 grayscale array (numpy only). Derived PER PAGE because the
    preprocessor's CLAHE pass leaves the paper background well below 255 -- a fixed threshold would
    misclassify ink on faint or high-contrast scans."""
    hist = np.bincount(np.asarray(gray, dtype=np.uint8).ravel(), minlength=256).astype(np.float64)
    total = float(hist.sum())
    if total <= 0:
        return 128
    levels = np.arange(256, dtype=np.float64)
    w0 = np.cumsum(hist)
    w1 = total - w0
    sum_total = float((hist * levels).sum())
    sum0 = np.cumsum(hist * levels)
    valid = (w0 > 0) & (w1 > 0)
    if not valid.any():
        return 128
    between = np.zeros(256, dtype=np.float64)
    m0 = sum0[valid] / w0[valid]
    m1 = (sum_total - sum0[valid]) / w1[valid]
    between[valid] = w0[valid] * w1[valid] * (m0 - m1) ** 2
    return int(np.argmax(between))


def _row_ink_profile(gray):
    """Per-row count of HANDWRITING ink. Returns (profile, eps), eps being the speckle tolerance below
    which a row counts as blank.

    Long horizontal rules AND long vertical structures (the ruled lines, the red margin rule, scan
    edges) are removed first. On a real ruled answer sheet they put ink in EVERY row, which flattened
    the profile completely -- no whitespace gaps were found, so both the snap-to-gap and the ink-trim
    silently did nothing and the crops were only as good as the raw model anchors."""
    arr = np.asarray(gray, dtype=np.uint8)
    thr = _otsu_threshold(arr)
    # `<=`: Otsu's class 0 is [0..thr], so the threshold level itself is ink. Using `<` silently
    # dropped every ink pixel on a cleanly bimodal page (all thresholds tie there and argmax picks
    # the lowest), which made the whole page read as blank.
    ink = (arr <= thr).astype(np.uint8)
    h, w = ink.shape
    try:
        import cv2
        hk, vk = max(15, w // 25), max(15, h // 25)
        rules = cv2.bitwise_or(
            cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, hk), np.uint8)),   # ruled lines
            cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((vk, 1), np.uint8)),   # margins / edges
        )
        ink = cv2.subtract(ink, rules)
    except Exception:
        pass        # unruled/clean pages still profile correctly without the morphology pass
    profile = ink.sum(axis=1).astype(np.int64)
    # ADAPTIVE blank-row bar. A fixed floor is far too low on a dense, CLAHE-contrasted page: residual
    # speckle keeps every row above it, so no gaps are found at all. Scale with the page's own ink
    # distribution (the low percentiles ARE the blank rows), keeping the floor for near-empty pages.
    eps = max(int(w * 0.002), int(np.percentile(profile, 20)))
    return profile, eps


def _whitespace_runs(profile, eps):
    """Contiguous [start, end) row ranges that are blank -- the gaps between handwritten lines."""
    blank = np.asarray(profile) <= eps
    runs, start = [], None
    for i, b in enumerate(blank):
        if b and start is None:
            start = i
        elif not b and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(blank)))
    return runs


def _snap_to_gap(y, runs, window=SNAP_WINDOW):
    """Snap an anchor to the CENTRE of the nearest whitespace gap within +/- window rows, so a cut
    lands between lines instead of through one. Returns y unchanged when no gap is close enough."""
    best, best_d = y, None
    for a, b in runs:
        if b <= a:
            continue
        centre = (a + b) // 2
        # distance to the run itself (0 when y already sits inside it)
        d = 0 if a <= y < b else min(abs(y - a), abs(y - (b - 1)))
        if d <= window and (best_d is None or d < best_d):
            best, best_d = centre, d
    return int(best)


def _build_bands(starts_with_y, page_h, continuation_qid=None, continuation_top=0,
                 continuation_bands=None):
    """Full-width bands covering the page. `starts_with_y` = [(qid, y)] in top-to-bottom order.
    Band n runs from its own anchor to the NEXT anchor (page bottom for the last), so consecutive
    bands share an edge and nothing is lost between them.

    `continuation_bands` = [(qid, top, bottom)] for answers that began on an EARLIER page, already
    ordered down the page. Passing a LIST is what fixes cross-page cropping: the caller used to hand in
    a single `continuation_qid`, so a page carrying two continuing answers silently dropped the second
    (7 such pages on one real 30-page sheet). The scalar arguments are still honoured so existing
    callers and tests behave exactly as before.
    """
    bands = []
    if continuation_bands:
        bands.extend((q, max(0, int(t)), int(b)) for q, t, b in continuation_bands)
    elif continuation_qid is not None:
        top = max(0, int(continuation_top or 0))
        bottom = starts_with_y[0][1] if starts_with_y else page_h
        bands.append((continuation_qid, top, bottom))
    for i, (qid, y) in enumerate(starts_with_y):
        bottom = starts_with_y[i + 1][1] if i + 1 < len(starts_with_y) else page_h
        bands.append((qid, int(y), int(bottom)))
    return bands


def _ink_trim(profile, top, bottom, eps, pad=PAD_PX, page_h=None):
    """Shrink a band to its first/last inked row (+pad). This is what makes the crop hug the answer
    and prevents the last answer on a page running on into blank paper. None when the band has no
    ink at all (caller falls back)."""
    page_h = len(profile) if page_h is None else page_h
    top = max(0, int(top))
    bottom = min(int(bottom), page_h)
    if bottom <= top:
        return None
    seg = np.asarray(profile[top:bottom])
    inked = np.nonzero(seg > eps)[0]
    if inked.size == 0:
        return None
    new_top = max(0, top + int(inked[0]) - pad)
    new_bottom = min(page_h, top + int(inked[-1]) + 1 + pad)
    if new_bottom <= new_top:
        return None
    return new_top, new_bottom


def _extract_json_obj(text):
    """First brace-block in the model's reply. Models sometimes wrap the JSON in prose or a code fence,
    or ramble on after it; the expected object has no nested braces, so a non-nested match is both
    sufficient and immune to trailing garbage."""
    s = strip_reasoning(text or "")
    m = re.search(r"\{[^{}]*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            pass
    # Salvage a TRUNCATED reply (the token cap cut the closing bracket off a runaway array): recover
    # the ys prefix so the length check rejects it explicitly as a degenerate run, rather than the
    # caller seeing a vague "not a list".
    m2 = re.search(r'"ys"\s*:\s*\[([^\]]*)', s, re.DOTALL)
    if m2:
        nums = re.findall(r"-?\d+", m2.group(1))
        if nums:
            return {"ys": [int(x) for x in nums]}
    return {}


def _validate_ys(ys, n, scale=COORD_SCALE):
    """Gate the vision output: exactly n integers, inside [0, scale], strictly top-to-bottom. Any
    failure -> the page falls back to full-page images, which is always safe. Returns (ok, ys|reason).

    `scale` is 1000, NOT the page height: Qwen-VL emits NORMALIZED 0-1000 coordinates natively and
    ignores a request for absolute pixels (verified -- it returned the same ~0-900 values on a 2730px
    page either way). Treating those as pixels put every anchor in the top ~17% of the page, i.e. the
    printed header, which was the real cause of the mis-crops."""
    if not isinstance(ys, list):
        return False, "ys not a list"
    if len(ys) != n:
        return False, f"expected {n} ys, got {len(ys)}"
    out = []
    for v in ys:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            return False, "non-integer y"
    if any(y < 0 or y > scale for y in out):
        # NOT a units problem, despite how it reads. Measured cause: token degeneracy -- the model
        # emitted `8` followed by ~200 zeros for one value while the rest were perfect. The old wording
        # ("y outside 0-1000") sent debugging straight back at the normalized-vs-pixel bug, which is a
        # different thing entirely. See _salvage_ys, which keeps the good values instead.
        return False, "unusable y (degenerate value, not a units error)"
    if any(out[i + 1] <= out[i] for i in range(len(out) - 1)):
        return False, "y not strictly increasing"
    return True, out


def _salvage_ys(ys, n, scale=COORD_SCALE):
    """Best-effort per-answer rescue: a length-n list where each entry is a usable y or None.

    Rejecting the whole page on one bad value was throwing away good anchors -- a real reply had five
    perfect values and one degenerate one, costing all six answers their crop. Only the answers whose
    anchor is None fall back; the rest still get tight bands.

    Ties (two EQUAL values) mean the model could not separate that pair -- seen deterministically, the
    same page returning [560, 560] on 4/4 samples. BOTH are dropped: splitting the difference would
    invent a boundary the model never saw, breaking the "correct crop or full page, never a wrong crop"
    guarantee. Everything else on the page is unaffected.
    """
    if not isinstance(ys, list):
        return [None] * n
    out = []
    for v in ys[:n]:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            out.append(None)
            continue
        out.append(iv if 0 <= iv <= scale else None)
    out += [None] * (n - len(out))

    # Drop equal neighbours as an ambiguous PAIR (compare on the raw values, before monotonic pruning,
    # so a tie is not mistaken for a mere ordering violation).
    for i in range(len(out) - 1):
        if out[i] is not None and out[i] == out[i + 1]:
            out[i] = out[i + 1] = None

    # Then enforce strictly increasing, dropping only the offenders.
    last = None
    for i, v in enumerate(out):
        if v is None:
            continue
        if last is not None and v <= last:
            out[i] = None
            continue
        last = v
    return out


def _page_roles(page_mapping):
    """Per page IN DOCUMENT ORDER: the ordered qids on it, which of them START there, and which are
    continuations. A qid's FIRST page is where it starts; later pages are continuations. Derived
    deterministically -- no inference. page_mapping preserves page order and, within a page, the
    top-to-bottom order the OCR encountered the [START_Q:] tags in."""
    pages = list(page_mapping.keys())
    first_page_of = {}
    for p in pages:
        for item in (page_mapping.get(p) or []):
            q = item.get("question_id")
            if q and q not in first_page_of:
                first_page_of[q] = p
    roles = []
    for idx, p in enumerate(pages, start=1):
        qids = [it.get("question_id") for it in (page_mapping.get(p) or []) if it.get("question_id")]
        roles.append({
            "page": p,
            "page_index": idx,
            "starts": [q for q in qids if first_page_of.get(q) == p],
            "continuations": [q for q in qids if first_page_of.get(q) != p],
        })
    return roles


def _page_number(path, fallback):
    m = re.search(r"page[_-]?(\d+)", os.path.basename(str(path)), re.IGNORECASE)
    return int(m.group(1)) if m else int(fallback)


def _safe_qid(qid):
    return "".join(c if (c.isalnum() or c in "()._-") else "_" for c in str(qid))


# ---------------------------------------------------------------------------------------------
# Page worker
# ---------------------------------------------------------------------------------------------

def _save_crop(img, box, crops_dir, qid, page_no):
    """Crop -> downscale -> JPEG. Returns the basename. Kept small on purpose: ~30 crops per sheet are
    served to the browser, so full-resolution PNGs would make the report crawl."""
    os.makedirs(crops_dir, exist_ok=True)
    out = img.crop(box)
    if out.width > MAX_WIDTH:
        h = max(1, int(out.height * (MAX_WIDTH / float(out.width))))
        out = out.resize((MAX_WIDTH, h), Image.LANCZOS)
    if out.mode not in ("L", "RGB"):
        out = out.convert("RGB")
    name = f"{_safe_qid(qid)}_p{page_no}.jpg"
    out.save(os.path.join(crops_dir, name), "JPEG", quality=JPEG_QUALITY, optimize=True)
    return name


def _answer_listing(starts, snippets):
    """Numbered 'Qid - "opening words"' listing. Handing the model the text the OCR already read makes
    this a MATCHING task instead of a guess, which is what stops it anchoring on the printed header."""
    out = []
    for i, q in enumerate(starts, start=1):
        snip = re.sub(r"\s+", " ", str((snippets or {}).get(q) or "")).strip()[:70]
        out.append(f'  {i}. {q} — "{snip}"' if snip else f"  {i}. {q}")
    return "\n".join(out)


def _process_page(role, preprocessed_dir, crops_dir, use_api, snippets=None):
    """Return (entries, in_tok, out_tok) for one page. Never raises."""
    entries, in_tok, out_tok = [], 0, 0
    page_path = role["page"]
    if not os.path.exists(page_path):
        page_path = os.path.join(preprocessed_dir, os.path.basename(str(role["page"])))
    page_no = _page_number(page_path, role["page_index"])
    starts, conts = role["starts"], role["continuations"]
    all_qids = conts + starts
    if not all_qids:
        return entries, in_tok, out_tok

    def _fallback(reason):
        """Whole page for every question on it -- always correct, just not tight."""
        out = []
        try:
            img = Image.open(page_path)
            for q in all_qids:
                out.append({"question_id": q, "page": page_no,
                            "crop_file": _save_crop(img, (0, 0, img.width, img.height),
                                                    crops_dir, q, page_no),
                            "method": "page", "reason": reason})
        except Exception as e:
            print(f"answer-crop: page {page_no} unusable ({e})", file=sys.stderr)
        return out

    try:
        img = Image.open(page_path)
        gray = np.array(img.convert("L"))       # force L: a teacher rotation rewrites the PNG as RGB
        page_h, page_w = gray.shape[0], gray.shape[1]
        profile, eps = _row_ink_profile(gray)
        # TWO thresholds, because the two jobs pull opposite ways: finding gaps needs a HIGH bar (or a
        # dense, noisy page yields none), while measuring an answer's extent needs a LOW one (or faint
        # handwriting gets trimmed away -- it cut a long answer down to its densest few lines).
        eps_content = max(1, int(page_w * 0.002))
        # The page's own densest row = a known-handwriting reference for the plausibility gate below.
        page_peak = int(np.asarray(profile).max()) if len(profile) else 0

        if len(starts) >= MAX_STARTS_PER_PAGE:
            return _fallback(f"dense objective page ({len(starts)} answers)"), in_tok, out_tok

        runs = _whitespace_runs(profile, eps)

        def _locate(prompt, qids, label):
            """Ask the model for one y per qid, retrying a REJECTED reply. Returns (ys_or_None list,
            in_tok, out_tok).

            Retries exist because the dominant failure was transient, not systematic: two pages that
            each lost 6 answers passed 4/4 on re-sampling, while a genuinely ambiguous page failed 0/4.
            Retrying costs nothing on the ~90% of pages that pass first time. Attempt 1 stays at
            temperature 0; later attempts warm up so the sample actually differs (a fixed prompt at
            temperature 0 is not reliably resampled). Every attempt's tokens are counted, so the cost
            report stays truthful.
            """
            i_tok = o_tok = 0
            best = [None] * len(qids)
            for attempt in range(1 + MAX_RETRIES):
                text, i_t, o_t = generate(
                    model=CROP_MODEL,
                    parts=[{"text": prompt}],
                    images=[page_path], json_mode=True,
                    temperature=(0.0 if attempt == 0 else RETRY_TEMPERATURE),
                    max_tokens=_MAX_TOKENS,
                )
                i_tok += i_t
                o_tok += o_t
                raw = _extract_json_obj(text).get("ys")
                ok, res = _validate_ys(raw, len(qids))
                if ok:
                    return res, i_tok, o_tok
                # Keep the best partial seen so far, so three degenerate replies still beat none.
                salvaged = _salvage_ys(raw, len(qids))
                if sum(v is not None for v in salvaged) > sum(v is not None for v in best):
                    best = salvaged
                print(f"answer-crop: page {page_no} {label} attempt {attempt + 1} rejected ({res}); "
                      f"salvaged {sum(v is not None for v in salvaged)}/{len(qids)}", file=sys.stderr)
            # CONFIDENCE GATE. Salvage is for "one bad apple in an otherwise coherent reply" -- the real
            # case was five perfect values and one degenerate. It is NOT for a reply the model muddled:
            # given two answers and the values [215, 45], one contradicts the other and keeping either is
            # a coin flip, which is exactly how a wrong crop would get shown. Require the survivors to be
            # a clear majority (>= 2/3), else discard the page and use full pages -- always safe.
            kept = sum(v is not None for v in best)
            if kept < -(-2 * len(qids) // 3):        # ceil(2/3 * n)
                if kept:
                    print(f"answer-crop: page {page_no} {label} discarded: only {kept}/{len(qids)} "
                          f"anchors survived, below the salvage threshold", file=sys.stderr)
                return [None] * len(qids), i_tok, o_tok
            return best, i_tok, o_tok

        def _to_px(y):
            # NORMALIZED 0-1000 -> pixels. This conversion is the whole ballgame: without it every
            # anchor collapsed into the top ~17% of the page (the printed header).
            return _snap_to_gap(int(y / COORD_SCALE * page_h), runs)

        if (starts or conts) and not use_api:
            return _fallback("no api key"), in_tok, out_tok

        # --- answers that BEGIN on this page ---------------------------------------------------
        snapped, unplaced_starts = [], []
        if starts:
            ys, i_t, o_t = _locate(
                _PROMPT_TMPL.format(n=len(starts), listing=_answer_listing(starts, snippets),
                                    w=page_w, h=page_h),
                starts, "starts")
            in_tok += i_t
            out_tok += o_t
            # Positional zip: we trust page_mapping for WHICH answers and their order, the model only
            # for WHERE. Anchors that did not survive validation fall back INDIVIDUALLY -- one bad
            # value used to discard the whole page, including answers whose anchors were perfect.
            for qid, y in zip(starts, ys):
                (snapped.append((qid, _to_px(y))) if y is not None else unplaced_starts.append(qid))
            snapped.sort(key=lambda t: t[1])

        # --- answers CONTINUING from an earlier page -------------------------------------------
        # They occupy the region above the first new answer (the whole page when none begins here).
        cont_region_bottom = snapped[0][1] if snapped else page_h
        cont_bands, unplaced_conts = [], []
        if conts:
            # A single continuation sharing the page with starts is already bounded by the first start
            # anchor, so it needs no call. Ask the model only when the boundary is genuinely unknown:
            # two or more answers sharing the region, or a page where nothing new begins.
            need_call = len(conts) > 1 or not starts
            ends = None
            if need_call:
                ends, i_t, o_t = _locate(
                    _CONT_PROMPT_TMPL.format(n=len(conts),
                                             listing=_answer_listing(conts, snippets)),
                    conts, "continuations")
                in_tok += i_t
                out_tok += o_t
            if ends is None:
                cont_bands = [(conts[0], 0, cont_region_bottom)]
                unplaced_conts = list(conts[1:])
            else:
                top = 0
                for qid, y in zip(conts, ends):
                    if y is None:
                        unplaced_conts.append(qid)
                        continue
                    bottom = min(_to_px(y), cont_region_bottom)
                    if bottom <= top:
                        unplaced_conts.append(qid)
                        continue
                    cont_bands.append((qid, top, bottom))
                    top = bottom

        bands = _build_bands(snapped, page_h, continuation_bands=cont_bands)

        multi = len(bands) > 1
        for qid, top, bottom in bands:
            trimmed = _ink_trim(profile, top, bottom, eps_content, PAD_PX, page_h)
            if trimmed is None:
                entries.append({"question_id": qid, "page": page_no,
                                "crop_file": _save_crop(img, (0, 0, page_w, page_h), crops_dir, qid, page_no),
                                "method": "page", "reason": "band had no ink"})
                continue
            t, b = trimmed
            # A band must actually CONTAIN handwriting. Without this a mis-anchored sliver sitting on
            # the printed header passed every other gate and was shown as a confident 27px "crop".
            seg = np.asarray(profile[t:b])
            inked_rows = int((seg > eps_content).sum())
            # Peak row density RELATIVE TO THIS PAGE, not just row COUNT: a residual ruled line inks
            # nearly every row of a thin band without a scrap of handwriting in it.
            peak = int(seg.max()) if seg.size else 0
            min_peak = page_peak * MIN_PEAK_INK_RATIO
            if ((b - t) < MIN_BAND_PX or (multi and (b - t) >= page_h * 0.98)
                    or inked_rows < MIN_INK_ROWS or peak < min_peak):
                entries.append({"question_id": qid, "page": page_no,
                                "crop_file": _save_crop(img, (0, 0, page_w, page_h), crops_dir, qid, page_no),
                                "method": "page",
                                "reason": (f"implausible band (h={b - t}, ink rows={inked_rows}, "
                                           f"peak={peak}/{int(min_peak)})")})
                continue
            entries.append({"question_id": qid, "page": page_no,
                            "crop_file": _save_crop(img, (0, t, page_w, b), crops_dir, qid, page_no),
                            "method": "band", "reason": ""})
        # Any question on this page the model never placed still gets the full page. Reasons are now
        # specific: an answer whose own anchor was rejected, versus one the model never returned at all.
        placed = {e["question_id"] for e in entries}
        rejected = set(unplaced_starts) | set(unplaced_conts)
        for q in all_qids:
            if q not in placed:
                entries.append({"question_id": q, "page": page_no,
                                "crop_file": _save_crop(img, (0, 0, page_w, page_h), crops_dir, q, page_no),
                                "method": "page",
                                "reason": "anchor rejected" if q in rejected else "not placed"})
    except Exception as e:
        print(f"answer-crop: page {page_no} failed ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return _fallback(f"error: {type(e).__name__}"), in_tok, out_tok
    return entries, in_tok, out_tok


def main():
    if len(sys.argv) < 4:
        print("Usage: python3 crop_answer_regions.py <page_mapping.json> <preprocessed_dir> "
              "<out_manifest.json> [ocr_answers.json]")
        sys.exit(1)
    mapping_path, preprocessed_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    crops_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "answer_crops")

    # Opening words of each answer, straight from the OCR. Optional -- without it the model is asked to
    # locate a bare question id, which is markedly less reliable on dense objective pages.
    snippets = {}
    if len(sys.argv) >= 5 and os.path.exists(sys.argv[4]):
        try:
            with open(sys.argv[4]) as f:
                for qid, val in (json.load(f) or {}).items():
                    if qid == "_instructions_":
                        continue
                    txt = val.get("answer", "") if isinstance(val, dict) else str(val)
                    if txt:
                        snippets[qid] = txt
        except Exception as e:
            print(f"Could not read OCR answers for crop hints: {e}", file=sys.stderr)

    try:
        with open(mapping_path) as f:
            page_mapping = json.load(f)
    except Exception as e:
        print(f"Could not read {mapping_path}: {e}", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump([], f)
        return

    roles = _page_roles(page_mapping)
    use_api = bool(os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
    if not use_api:
        print("No LLM API key; answer crops fall back to full pages.", file=sys.stderr)

    results, total_in, total_out = [None] * len(roles), 0, 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(roles) or 1))) as ex:
            futs = {ex.submit(_process_page, r, preprocessed_dir, crops_dir, use_api, snippets): i
                    for i, r in enumerate(roles)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    entries, i_tok, o_tok = fut.result()
                except Exception as e:
                    print(f"answer-crop: page worker crashed ({e})", file=sys.stderr)
                    entries, i_tok, o_tok = [], 0, 0
                results[i] = entries
                total_in += i_tok
                total_out += o_tok
    except Exception as e:
        print(f"Answer cropping unavailable ({e}); report falls back to no screenshots.", file=sys.stderr)

    # Flatten in page order, then cap pages-per-question (guards OCR over-assignment, as the diagram
    # stage does with MAX_DIAGRAM_PAGES_PER_Q).
    flat, per_q = [], {}
    for entries in results:
        for e in (entries or []):
            q = e["question_id"]
            per_q[q] = per_q.get(q, 0) + 1
            if per_q[q] <= MAX_PAGES_PER_Q:
                flat.append(e)

    if total_in or total_out:
        _best, _nreal, _n = get_real_cost()
        log_cost("answer_crop", CROP_MODEL, total_in, total_out, cost_usd=(_best if _nreal > 0 else None))
    with open(out_path, "w") as f:
        json.dump(flat, f, indent=2)
    n_band = sum(1 for e in flat if e.get("method") == "band")
    print(f"Answer region crops: {n_band}/{len(flat)} tightly cropped "
          f"(rest fall back to the full page). Saved to {out_path}")


if __name__ == "__main__":
    main()
