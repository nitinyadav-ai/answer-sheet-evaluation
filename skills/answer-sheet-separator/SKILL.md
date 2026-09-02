---
name: answer-sheet-separator
description: Splits a single combined PDF containing many students' exam answer sheets into individual per-student sheets. Detects each sheet's student-details section (a printed/handwritten header OR an OMR bubble front page) using a Gemini 3.5 Flash classifier on the cropped top of every page, then uses those as page boundaries. Emits a manifest (per-student name, subject, page range) plus page thumbnails for a teacher review screen. Runs BEFORE ingestion/OCR; pairs with scripts/batch_evaluator.py to grade every separated sheet.
---

# Answer Sheet Separator

This skill is the pre-OCR stage for batch grading. Schools scan a whole class into one combined PDF; this skill finds where each student's sheet begins and splits the PDF into individual answer sheets, each identified by **Name** and **Subject**.

## Quick Start

Ensure your `.env` contains `GEMINI_API_KEY=your_api_key`.

```bash
python3 scripts/separate_sheets.py "path/to/combined.pdf" --output-dir "output/<batch_id>/separation"
```

This writes:
- `output/<batch_id>/separation/manifest.json` — the authoritative split (see below).
- `output/<batch_id>/separation/thumbs/page_<n>.png` — one low-DPI thumbnail per source page, for the review UI.

## How It Works

1. **Per-page render (PyMuPDF):** for each page it renders a review thumbnail and a *cropped top portion* (where the student-details section sits) — cropping keeps the per-page token cost low.
2. **Flash classifier:** the top crop goes to Gemini Flash (`temperature=0`, JSON output) which returns `{is_sheet_start, is_omr, name, subject, confidence}`. It recognises both a printed/handwritten header (Name/Subject/Roll/Class) and an OMR bubble information block.
3. **Boundaries → sheets:** pages flagged `is_sheet_start` become the start of a student's sheet; following pages belong to it. Page 1 is always forced to start a sheet (and flagged `needs_review`) so leading pages are never dropped.
4. **Manifest:** contiguous, 1-indexed page ranges covering every page. Each sheet carries `needs_review=true` when the name is unreadable, confidence is low, or page 1 was force-started — surfacing it for teacher correction.

## Manifest shape

```json
{
  "source_pdf": "/abs/path/combined.pdf",
  "num_pages": 12,
  "sheets": [
    {"id": "sheet_1", "name": "Asha Rao", "subject": "Computer Science",
     "start_page": 1, "end_page": 5, "page_count": 5,
     "is_omr": false, "needs_review": false, "confidence": 0.95}
  ]
}
```

The manifest is the single source of truth: the teacher review UI edits it (names, subjects, merge/split boundaries) and `scripts/batch_evaluator.py` slices the source PDF per sheet from it, then runs the existing `full_evaluate()` pipeline once per student.

## Configuration

All env-tunable with safe defaults (no `.env` change required):

| Variable | Default | Purpose |
|---|---|---|
| `SEPARATOR_MODEL` | `gemini-3.5-flash` | Classifier model. Kept on Flash for cost at scale. |
| `SEPARATOR_CROP_FRACTION` | `0.38` | Fraction of page height (from the top) sent to the classifier. |
| `SEPARATOR_DPI` | `150` | Render DPI of the cropped classifier image. |
| `SEPARATOR_THUMB_DPI` | `90` | Render DPI of review thumbnails. |
| `SEPARATOR_MAX_WORKERS` | `12` | Parallel page-classification workers. |
| `SEPARATOR_LOW_CONFIDENCE` | `0.5` | Below this `confidence`, a sheet is flagged `needs_review`. |

Decoding is deterministic (`temperature=0`), so re-running ("Scan Again") on the same PDF reproduces the same split.

## Dependencies

```bash
python3 -m pip install PyMuPDF openai python-dotenv
```
