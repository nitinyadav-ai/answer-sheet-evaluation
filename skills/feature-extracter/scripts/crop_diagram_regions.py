"""Display-only diagram cropping (vision bounding box).

Reads diagram_crops.json -- which, despite the name, is the question -> full preprocessed PAGE map
produced by detect_diagrams.py, NOT crops -- and for each entry asks a vision model to bound the
student's hand-drawn diagram on that page, then crops it tightly with PIL. Writes
diagram_display_crops.json = [{question_id, image, crop}], where `crop` is the tight crop path
(or null on any failure -> the report falls back to the full page `image`).

DISPLAY-ONLY. It never touches ocr_answers.json / db_answers.json, and the diagram GRADERS
(extract_features.py, evaluate_diagrams.py) keep receiving the full pages via argv -- only the report's
DIAGRAM_CROPS_JSON env var is pointed here. A bad crop therefore cannot change a mark.

TWO BUGS THIS FILE SHIPPED WITH, both measured on real sheets (0 of 2 pages cropped before the fix):

  1. It asked for {"xmin","ymin","xmax","ymax"}. Qwen-VL answers in its NATIVE grounding format,
     {"bbox_2d": [x0, y0, x1, y1], "label": ...}, so every box hit the KeyError guard and was silently
     discarded. Both shapes are accepted now.
  2. It treated the numbers as PIXELS. They are NORMALIZED 0-1000 -- verified: [558, 785, 811, 940]
     on a 3509x2480 page. Read as pixels every box collapses into the top-left corner. This is the same
     units bug that caused the answer-region mis-cropping; see crop_answer_regions.py, which carries the
     full write-up.

FALSE POSITIVES ARE THE REAL RISK. detect_diagrams.py assigns EVERY page of a question to that
question's diagram list, so a text-only continuation page arrives here with no diagram on it -- and the
model happily boxes blocks of handwriting rather than returning []. Measured on a real sheet, size
separates them cleanly: true diagrams covered 3.4-5.0% of the page, false positives 26.6-31.2%. Hence
MAX_AREA_FRAC (0.20). The old guard only rejected boxes >92% of the page, which caught neither.
"""
import concurrent.futures
import json
import os
import sys

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

# Reuse the ink profiler from the answer-region cropper (same directory): rules/margins stripped, Otsu
# per page. Sharing it keeps the "is this actually handwriting?" test identical across both croppers.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from crop_answer_regions import _row_ink_profile
except Exception:  # pragma: no cover - degrade to no ink test rather than fail the stage
    _row_ink_profile = None


def _env_float(key, default):
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


# Same 235B instruct model the OCR and answer-crop stages use. The small 30B VL model degenerates on
# this class of task (see crop_answer_regions.py).
CROP_MODEL = os.environ.get("DIAGRAM_CROP_MODEL",
                            os.environ.get("OCR_MODEL", "qwen/qwen3-vl-235b-a22b-instruct"))
# Qwen-VL grounding is NATIVELY normalized to 0-1000 regardless of image size.
COORD_SCALE = 1000.0
# A box larger than this fraction of the page is the model boxing text, not a figure (measured: real
# diagrams 3.4-5.0%, text-block false positives 26.6-31.2%).
MAX_AREA_FRAC = _env_float("DIAGRAM_CROP_MAX_AREA", 0.20)
# ...and one smaller than this is a stray scribble, not a figure. Measured: a crop of the pen-mark
# "30." came out at 0.96% of the page, while every genuine diagram was >= 3.6%.
MIN_AREA_FRAC = _env_float("DIAGRAM_CROP_MIN_AREA", 0.02)
# ...and one smaller than this is a stray mark / QR code. Normalized, so it is resolution-independent
# (the old guard was in pixels, which is meaningless against a 0-1000 reply).
MIN_SIDE_NORM = _env_float("DIAGRAM_CROP_MIN_SIDE", 40.0)
# A box far wider than it is tall is a LINE OF TEXT, not a figure. The area gate cannot catch these --
# a single line is small. Measured on real sheets: chemical equations came back at aspect 8.4 and a
# boxed "Total Surface Area = 160 cm2" at 6.4, while every genuine figure was <= 3.3 EXCEPT one row of
# three labelled test tubes, which measured 4.8 on one run and 5.6 on another.
#
# 6.0 is therefore a deliberately narrow split, and it is set on the safe side: rejecting a real figure
# only costs tightness (the page falls back to the full image, i.e. today's behaviour), whereas keeping
# a line of text shows the teacher something that is not the diagram at all.
MAX_ASPECT = _env_float("DIAGRAM_CROP_MAX_ASPECT", 6.0)
# Padding around the box, as a fraction of the page's longest side.
PAD_FRAC = _env_float("DIAGRAM_CROP_PAD_FRAC", 0.012)
# A kept box must hold a row as dense as this fraction of the PAGE's own densest row. Page-relative,
# not a fraction of width: a width-based threshold collapses on landscape pages, which this project has.
MIN_PEAK_INK_RATIO = _env_float("DIAGRAM_CROP_MIN_PEAK_INK_RATIO", 0.10)
MAX_RETRIES = _env_int("DIAGRAM_CROP_MAX_RETRIES", 1)
RETRY_TEMPERATURE = _env_float("DIAGRAM_CROP_RETRY_TEMPERATURE", 0.3)
MAX_WORKERS = _env_int("DIAGRAM_CROP_MAX_WORKERS", 8)

_PROMPT = """Find every hand-drawn DIAGRAM, figure, sketch, graph or ray-diagram on this page.

A diagram is a DRAWING: lines, shapes, arrows, axes, ray paths, labelled constructions. Blocks of
handwritten words, equations or working are NOT diagrams. Ignore printed text, ruled lines, the
"Page No / Date" box, page numbers and any QR code.

Reply with ONLY a JSON array, the largest/most complete diagram first:
[{"bbox_2d": [x0, y0, x1, y1]}]

Coordinates are NORMALIZED 0 to 1000 (0 = left/top edge of the image, 1000 = right/bottom edge).
Box only the drawing itself, not the paragraphs around it.
If this page has no hand-drawn diagram at all, reply with exactly: []"""


def _write_atomic(path, payload):
    """Write JSON via a temp file + os.replace, so a concurrent reader sees old or new, never partial."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _box_candidates(data):
    """Every shape the reply arrives in, normalised to a list of candidate box items.

    Shapes observed from Qwen-VL under json_mode: a bare object `{"bbox_2d": [...]}` (the common one),
    an object wrapping the array under some key, the requested array of objects, and a bare
    `[x0,y0,x1,y1]`. `{}` means "no diagram" and must stay empty.
    """
    if isinstance(data, dict):
        if "bbox_2d" in data or all(k in data for k in ("xmin", "ymin", "xmax", "ymax")):
            return [data]
        # {"diagrams": [...]} / {"boxes": [...]} -- take the first list of plausible boxes.
        for v in data.values():
            if isinstance(v, list) and v:
                return v
        return []
    if isinstance(data, list):
        # A bare [x0, y0, x1, y1] is ONE box, not four candidates.
        if len(data) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in data):
            return [data]
        return data
    return []


def _parse_boxes(raw_text):
    """Boxes from the model reply as normalized [x0, y0, x1, y1], tolerating both shapes it emits.

    Qwen-VL answers `{"bbox_2d": [...]}` natively and ignores a request for xmin/ymin keys -- assuming
    the requested shape is what made this stage silently produce nothing.
    """
    txt = strip_reasoning(raw_text or "").strip()
    if not txt:
        return []
    # Parse the WHOLE reply first. The prompt asks for an array, but this call sets json_mode=True and
    # the provider then forces a top-level OBJECT, so the real reply is a bare
    #   {"bbox_2d": [198, 507, 843, 763]}
    # Slicing between the outermost [ and ] on that text yields the COORDINATE array, whose elements
    # are four numbers -- none of which is a box -- so every page silently parsed to [] and the whole
    # stage reported "no diagram found" while the model was locating diagrams correctly. Measured: 9 of
    # 9 pages on one sheet, every crop lost.
    data = None
    try:
        data = json.loads(txt)
    except (ValueError, TypeError):
        start, end = txt.find("["), txt.rfind("]")          # fenced / prose-wrapped array
        if start != -1 and end > start:
            try:
                data = json.loads(txt[start:end + 1])
            except (ValueError, TypeError):
                data = None
    if data is None:
        return []
    out = []
    for b in _box_candidates(data):
        if isinstance(b, dict) and isinstance(b.get("bbox_2d"), (list, tuple)) and len(b["bbox_2d"]) == 4:
            vals = b["bbox_2d"]
        elif isinstance(b, dict) and all(k in b for k in ("xmin", "ymin", "xmax", "ymax")):
            vals = [b["xmin"], b["ymin"], b["xmax"], b["ymax"]]
        elif isinstance(b, (list, tuple)) and len(b) == 4:
            vals = b
        else:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in vals)
        except (TypeError, ValueError):
            continue
        out.append([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
    return out


def _pick_box(boxes, page_w, page_h):
    """Largest box that passes the guards, as pixels; None if nothing qualifies.

    Returns (box_px, reason) so a rejection can be reported rather than looking like "no diagram".
    """
    best, best_area, reason = None, 0.0, "no diagram found"
    for x0, y0, x1, y1 in boxes:
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            continue
        if w < MIN_SIDE_NORM and h < MIN_SIDE_NORM:
            reason = "box too small (stray mark)"
            continue
        area = (w / COORD_SCALE) * (h / COORD_SCALE)
        if area < MIN_AREA_FRAC:
            reason = f"box covers only {area * 100:.1f}% of the page (a stray mark, not a figure)"
            continue
        if area > MAX_AREA_FRAC:
            # The dominant real-world failure: a page of handwritten working, boxed as a "diagram".
            reason = f"box covers {area * 100:.0f}% of the page (text, not a figure)"
            continue
        # Scale to the page before judging shape: the normalized space is square, so a box that looks
        # 1:1 in 0-1000 is actually as wide as the page is wide.
        aspect = (w / COORD_SCALE * page_w) / max(1.0, h / COORD_SCALE * page_h)
        if aspect > MAX_ASPECT:
            reason = f"box is {aspect:.1f}:1 wide (a line of text, not a figure)"
            continue
        if area > best_area:
            best, best_area = (x0, y0, x1, y1), area
    if best is None:
        return None, reason
    pad = PAD_FRAC * max(page_w, page_h)
    px = (max(0, int(best[0] / COORD_SCALE * page_w - pad)),
          max(0, int(best[1] / COORD_SCALE * page_h - pad)),
          min(page_w, int(best[2] / COORD_SCALE * page_w + pad)),
          min(page_h, int(best[3] / COORD_SCALE * page_h + pad)))
    if px[2] <= px[0] or px[3] <= px[1]:
        return None, "degenerate box after scaling"
    return px, ""


def _has_ink(gray, box):
    """True when the box holds a row as dense as MIN_PEAK_INK_RATIO of the page's densest row.

    Guards against a confidently-returned box over blank ruled paper -- the exact failure the answer
    cropper hit, where a curved rule survives the morphological strip and inks every row of a region.
    """
    if _row_ink_profile is None:
        return True
    try:
        profile, _ = _row_ink_profile(gray)
        page_peak = int(np.asarray(profile).max()) if len(profile) else 0
        if page_peak <= 0:
            return True
        seg = np.asarray(profile[box[1]:box[3]])
        return bool(seg.size) and int(seg.max()) >= page_peak * MIN_PEAK_INK_RATIO
    except Exception:
        return True


def _crop_one(qid, page_path, index, crops_dir, use_api):
    """Return (index, {question_id, image, crop|None, reason}, in_tok, out_tok). Never raises."""
    in_tok = out_tok = 0
    result = {"question_id": qid, "image": page_path, "crop": None, "reason": ""}
    if not use_api:
        result["reason"] = "no api key"
        return index, result, in_tok, out_tok
    try:
        img = Image.open(page_path)
        gray = np.array(img.convert("L"))
        page_h, page_w = gray.shape[0], gray.shape[1]
        box, reason = None, "no diagram found"
        for attempt in range(1 + MAX_RETRIES):
            text, i_tok, o_tok = generate(
                model=CROP_MODEL, parts=[{"text": _PROMPT}], images=[page_path],
                json_mode=True, temperature=(0.0 if attempt == 0 else RETRY_TEMPERATURE),
            )
            in_tok += i_tok
            out_tok += o_tok
            box, reason = _pick_box(_parse_boxes(text), page_w, page_h)
            if box:
                break
        if box and not _has_ink(gray, box):
            box, reason = None, "box has no handwriting in it"
        if box:
            os.makedirs(crops_dir, exist_ok=True)
            safe_qid = "".join(c if (c.isalnum() or c in "()._-") else "_" for c in str(qid))
            crop_path = os.path.join(crops_dir, f"{safe_qid}_p{index}.png")
            img.crop(box).save(crop_path)
            result["crop"] = crop_path
        else:
            result["reason"] = reason
    except Exception as e:
        result["reason"] = f"error: {type(e).__name__}"
        print(f"Diagram crop failed for {qid} ({page_path}): {e}", file=sys.stderr)
    return index, result, in_tok, out_tok


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 crop_diagram_regions.py <diagram_crops.json> <output_display_crops.json>")
        sys.exit(1)
    crops_in_path, out_path = sys.argv[1], sys.argv[2]
    try:
        with open(crops_in_path) as f:
            entries = json.load(f)
    except Exception as e:
        print(f"Could not read {crops_in_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(entries, list) or not entries:
        _write_atomic(out_path, [])
        print("No diagram entries to crop.")
        return

    crops_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "region_crops")
    use_api = bool(os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
    if not use_api:
        print("No LLM API key; diagram cropping skipped (report shows full pages).", file=sys.stderr)

    results, total_in, total_out = [None] * len(entries), 0, 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(entries)))) as ex:
            futs = [ex.submit(_crop_one, e.get("question_id"), e.get("image"), i, crops_dir, use_api)
                    for i, e in enumerate(entries)]
            for fut in concurrent.futures.as_completed(futs):
                idx, res, i_tok, o_tok = fut.result()
                results[idx] = res
                total_in += i_tok
                total_out += o_tok
    except Exception as e:
        print(f"Diagram cropping unavailable ({e}); falling back to full pages.", file=sys.stderr)

    for i, e in enumerate(entries):
        if results[i] is None:
            results[i] = {"question_id": e.get("question_id"), "image": e.get("image"),
                          "crop": None, "reason": "not processed"}

    # A page whose content is not a figure is DROPPED rather than kept as a full-page fallback. Those
    # entries exist only because detect_diagrams.py assigns EVERY page of a question to that question,
    # so keeping them would show a redundant whole page beside the real figure.
    #
    # Decided PER QUESTION, not globally: a page is only dropped when that question still has a real
    # crop somewhere else. A question with no successful crop keeps its entries and degrades to the
    # full page, exactly as today -- so a genuine figure we simply failed to bound is never lost.
    _NOT_A_FIGURE = ("no diagram", "box covers", "box is")
    has_crop = {r.get("question_id") for r in results if r.get("crop")}
    kept, dropped = [], 0
    for r in results:
        if (r.get("crop") is None
                and r.get("reason", "").startswith(_NOT_A_FIGURE)
                and r.get("question_id") in has_crop):
            dropped += 1
            continue
        kept.append(r)

    if total_in or total_out:
        _best, _nreal, _n = get_real_cost()
        log_cost("diagram_crop", CROP_MODEL, total_in, total_out, cost_usd=(_best if _nreal > 0 else None))
    # ATOMIC: the orchestrator seeds this path with full-page fallbacks before grading starts, and the
    # report reads it from another process. A plain write could be observed half-finished.
    _write_atomic(out_path, kept)
    n_crop = sum(1 for r in kept if r.get("crop"))
    print(f"Diagram region crops: {n_crop}/{len(kept)} tightly cropped"
          f"{f', {dropped} page(s) had no diagram and were dropped' if dropped else ''}"
          f" (rest fall back to the full page). Saved to {out_path}")


if __name__ == "__main__":
    main()
