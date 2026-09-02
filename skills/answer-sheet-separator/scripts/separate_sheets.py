import os
import sys
import json
import re
import argparse
import concurrent.futures
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Missing dependencies. Run: pip install PyMuPDF openai python-dotenv")
    sys.exit(1)

# Provider-agnostic LLM client + cost meter (live in scripts/; add it to the path like other skills).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))
from llm_client import generate, strip_reasoning
try:
    from llm_pricing import estimate_cost
except Exception:
    def estimate_cost(m, i, o): return (int(i or 0) / 1e6) * 0.075 + (int(o or 0) / 1e6) * 0.30


# ----------------------------------------------------------------------------
# Config (env-tunable; all have safe defaults so no .env change is required).
# Kept on Gemini Flash + a cropped top-of-page render to keep cost low at the
# scale of a whole class scanned into one PDF (one cheap call per page).
# ----------------------------------------------------------------------------
SEPARATOR_MODEL = os.environ.get("SEPARATOR_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
CROP_FRACTION = float(os.environ.get("SEPARATOR_CROP_FRACTION", "0.38"))  # top portion sent to the classifier
DETECT_DPI = int(os.environ.get("SEPARATOR_DPI", "150"))                  # render DPI for the cropped classifier image
THUMB_DPI = int(os.environ.get("SEPARATOR_THUMB_DPI", "90"))             # render DPI for review thumbnails
MAX_WORKERS = int(os.environ.get("SEPARATOR_MAX_WORKERS", "12"))
LOW_CONFIDENCE = float(os.environ.get("SEPARATOR_LOW_CONFIDENCE", "0.5"))

# Generation settings are passed inline to generate() in process_page: temperature=0 (deterministic,
# so a re-run "Scan Again" reproduces the split), json_mode, max_tokens=512 (the verdict is tiny),
# thinking_budget=0 (classification, not reasoning -- a Gemini-only knob, ignored by Qwen).

SEPARATOR_PROMPT = """You are analysing the TOP portion of ONE page taken from a scanned stack of student exam answer sheets. Several students' sheets are concatenated back-to-back; each student's sheet begins with a student-details section.

Decide whether THIS page is the FIRST page of a student's answer sheet. It is a first page when its top contains a student-details section, which looks like EITHER:
  (A) a printed or handwritten HEADER with fields such as Name, Subject, Roll No, Class, Section, Date; OR
  (B) an OMR / bubble sheet area used to encode student information (grids of bubbles for name / roll number / subject).

A page that merely CONTINUES answers (ruled lines, question numbers, rough work) with NO student-details section at its top is NOT a first page.

If it is a first page, extract the student's NAME and the SUBJECT when they are present and legible.

Respond with ONLY a JSON object (no markdown, no commentary) in EXACTLY this shape:
{"is_sheet_start": true or false, "is_omr": true or false, "name": "<student name or null>", "subject": "<subject or null>", "confidence": <number 0.0-1.0>}

Use null (do not guess) for any field you cannot read. "confidence" is how sure you are about is_sheet_start."""


def _clean_field(val):
    """Normalise an extracted name/subject: strip, drop bracket noise, collapse whitespace."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("null", "none", "n/a", "na", "blank", ""):
        return ""
    s = re.sub(r'[<>{}\[\]]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _parse_json(text):
    """Tolerant JSON parse: handles bare JSON, fenced blocks, or JSON embedded in prose."""
    if not text:
        return {}
    t = strip_reasoning(text).strip()
    if t.startswith("```"):
        t = re.sub(r'^```[a-zA-Z]*', '', t).strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r'\{.*\}', t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


def process_page(pdf_path, page_index, thumbs_dir):
    """Render this page's thumbnail + top crop, then ask Gemini Flash if it starts a new sheet.

    Opens its own fitz document (fitz objects are not thread-safe to share), mirroring the
    per-call open in ingestion-handler/process_input.py.
    """
    tokens = {"prompt": 0, "completion": 0}
    try:
        doc = fitz.open(pdf_path)
        page = doc.load_page(page_index)
        rect = page.rect

        # Review thumbnail (full page, low DPI).
        thumb_mat = fitz.Matrix(THUMB_DPI / 72, THUMB_DPI / 72)
        thumb_pix = page.get_pixmap(matrix=thumb_mat)
        thumb_path = os.path.join(thumbs_dir, f"page_{page_index + 1}.png")
        thumb_pix.save(thumb_path)

        # Classifier image: only the top CROP_FRACTION of the page (where the details section sits).
        clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height * CROP_FRACTION)
        crop_mat = fitz.Matrix(DETECT_DPI / 72, DETECT_DPI / 72)
        crop_pix = page.get_pixmap(matrix=crop_mat, clip=clip)
        crop_bytes = crop_pix.tobytes("png")
        doc.close()

        text, p_in, p_out = generate(
            model=SEPARATOR_MODEL,
            parts=[{"text": SEPARATOR_PROMPT}, {"image_png": crop_bytes}],
            temperature=0.0, json_mode=True, max_tokens=512, thinking_budget=0,
        )
        tokens = {"prompt": p_in, "completion": p_out}
        data = _parse_json(text)
        return {
            "index": page_index,
            "is_sheet_start": bool(data.get("is_sheet_start", False)),
            "is_omr": bool(data.get("is_omr", False)),
            "name": _clean_field(data.get("name")),
            "subject": _clean_field(data.get("subject")),
            "confidence": data.get("confidence"),
            "tokens": tokens,
            "error": None,
        }
    except Exception as e:
        # On error treat the page as a continuation (do NOT invent a boundary) and flag nothing;
        # page 1 is force-started below regardless, so a failed first page still yields one sheet.
        return {
            "index": page_index,
            "is_sheet_start": False,
            "is_omr": False,
            "name": "",
            "subject": "",
            "confidence": None,
            "tokens": tokens,
            "error": str(e),
        }


def build_sheets(results, num_pages):
    """Turn per-page verdicts into contiguous per-student sheets (1-indexed page ranges)."""
    by_index = {r["index"]: r for r in results}
    start_pages = sorted(r["index"] for r in results if r["is_sheet_start"])

    # A combined PDF must begin a sheet on page 1. If the model didn't flag it (or leading pages
    # are orphans before the first detected header), force page 0 to start and flag for review.
    forced_first = False
    if not start_pages or start_pages[0] != 0:
        forced_first = True
        start_pages = sorted(set([0] + start_pages))

    sheets = []
    for i, s in enumerate(start_pages):
        e = (start_pages[i + 1] - 1) if i + 1 < len(start_pages) else (num_pages - 1)
        cls = by_index.get(s, {})
        name = cls.get("name") or ""
        subject = cls.get("subject") or ""
        conf = cls.get("confidence")
        try:
            low_conf = conf is not None and float(conf) < LOW_CONFIDENCE
        except (TypeError, ValueError):
            low_conf = False
        needs_review = (not name) or low_conf or (i == 0 and forced_first)
        sheets.append({
            "id": f"sheet_{i + 1}",
            "name": name or f"Student {i + 1}",
            "subject": subject,
            "start_page": s + 1,
            "end_page": e + 1,
            "page_count": e - s + 1,
            "is_omr": bool(cls.get("is_omr", False)),
            "needs_review": bool(needs_review),
            "confidence": conf,
        })
    return sheets


def separate(pdf_path, output_dir):
    # Qwen3 via OpenRouter (or a local OpenAI-compatible server). OpenRouter requires an API key;
    # a local vLLM/SGLang server accepts any value (set LLM_API_KEY to a dummy then).
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        print("Error: LLM_API_KEY is not set (OpenRouter API key required).", file=sys.stderr)
        sys.exit(1)

    if Path(pdf_path).suffix.lower() != ".pdf":
        print(f"Error: expected a .pdf, got {pdf_path}", file=sys.stderr)
        sys.exit(1)

    thumbs_dir = os.path.join(output_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    num_pages = len(doc)
    doc.close()
    if num_pages == 0:
        print("Error: PDF has no pages.", file=sys.stderr)
        sys.exit(1)

    print(f"Separator: {SEPARATOR_MODEL} | pages: {num_pages} | crop: {CROP_FRACTION} | dpi: {DETECT_DPI}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, num_pages)) as executor:
        futures = {
            executor.submit(process_page, pdf_path, i, thumbs_dir): i
            for i in range(num_pages)
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r["index"])
    for r in results:
        if r["error"]:
            print(f"Warning: page {r['index'] + 1} classification failed: {r['error']}", file=sys.stderr)

    sheets = build_sheets(results, num_pages)

    manifest = {
        "source_pdf": os.path.abspath(pdf_path),
        "num_pages": num_pages,
        "sheets": sheets,
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    total_prompt = sum(r["tokens"]["prompt"] for r in results)
    total_completion = sum(r["tokens"]["completion"] for r in results)
    # Priced per the model actually used (Gemini or Qwen) via the shared cost meter.
    est_cost = estimate_cost(SEPARATOR_MODEL, total_prompt, total_completion)

    print(f"Detected {len(sheets)} sheet(s) across {num_pages} page(s).")
    for s in sheets:
        flag = " [NEEDS REVIEW]" if s["needs_review"] else ""
        print(f"  {s['id']}: pages {s['start_page']}-{s['end_page']} | "
              f"name='{s['name']}' subject='{s['subject']}' omr={s['is_omr']}{flag}")
    print(f"Tokens: prompt={total_prompt} completion={total_completion} | est. cost ${est_cost:.6f}")
    print(f"Manifest: {manifest_path}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Split a combined multi-student PDF into per-student sheets.")
    parser.add_argument("pdf", help="Path to the combined PDF")
    parser.add_argument("--output-dir", required=True, help="Directory for manifest.json + thumbs/")
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    separate(args.pdf, args.output_dir)


if __name__ == "__main__":
    main()
