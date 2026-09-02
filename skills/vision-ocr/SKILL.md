---
name: vision-ocr
description: Precision OCR extraction engine for exam answer sheets. Uses Gemini 3.5 Flash with deterministic decoding (temperature 0) and high media resolution to extract handwritten text and student metadata while faithfully preserving acronyms and code symbols. Outputs structured digital text as PDF and Word documents. Use this right after img-preprocessing to transcribe handwritten answers into digital formats.
---

# Vision OCR Handler

This skill transcribes preprocessed plain ruled answer sheet images into structured digital text using Gemini OCR. 

## Quick Start

Ensure your `.env` file (`~/.openclaw/workspace/.env`) contains `GEMINI_API_KEY=your_api_key` and `DB_TABLE=sci_class10` (or similar) to automatically prefix question keys.

Run the script by passing the preprocessed image files in the correct sequential order.

```bash
python3 scripts/run_ocr.py "path/to/page1.png" "path/to/page2.png" --output-dir "/path/to/output"
```

### Optional: anchor question separation to the known question set

`--question-ids-file <path>` points at a JSON list of the exam's real base question numbers (e.g. `[1, 2, 3, ... 38]`). When provided, the page-OCR prompt is anchored to that closed set, so the model only starts a question for a real question number and treats out-of-set labels as misread sub-parts/digits (transcribed inline). When the flag is **absent**, OCR behaves exactly as before. `full_evaluate` derives this list from the answer key (+ question paper) and passes it automatically; supply it manually only for standalone runs.

```bash
python3 scripts/run_ocr.py "page1.png" "page2.png" --output-dir "/out" --question-ids-file "/out/question_ids.json"
```

## Configuration

OCR behaviour is controlled by environment variables (set in `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `OCR_MODEL` | `gemini-3.5-flash` | Vision model. Kept on Flash for cost at scale; swappable to a Pro tier for high-stakes batches. |
| `OCR_MEDIA_RESOLUTION` | `HIGH` | Per-page image detail (`LOW`/`MEDIUM`/`HIGH`). Main per-page cost dial; `HIGH` sharpens tiny symbols. |
| `OCR_VERIFY_CODE` | `1` | Run a second, code-only OCR pass over code-bearing answers (feeds the reconcile below). |
| `OCR_VERIFY_MATH` | `1` | Run a second, math-only OCR pass over equation-bearing answers (feeds the reconcile below). |
| `OCR_ARBITRATE` | `1` | Master switch for the RECONCILE step: on a genuine disagreement, an arbiter pass resolves the symbols and rewrites the answer. `0` reverts to the old flag-only behaviour. |
| `OCR_THINKING_BUDGET` | `0` | Model thinking budget. Transcription is not a reasoning task, so `0` minimises cost/latency. |

Decoding is deterministic (`temperature=0`, `max_output_tokens=32768`) so repeated runs are reproducible.

## How It Works

1. **Header Extraction:** Extracts the student metadata (Name, Date, etc.) from the first page using a specific metadata-targeting prompt.
2. **Parallel Main OCR Processing:** Dispatches all page images to Gemini concurrently using `ThreadPoolExecutor`.
3. **Compilation:** Results are reassembled in their original sequential order, and the script concatenates the text to safely handle multi-page answers without spillover.
4. **Binary Logic for Markers:** The prompt explicitly instructs Gemini to strictly preserve Markdown formatting, detect handwritten question markers, and output exact boundaries `[START_Q: num]` and `[END_Q: num]`.
4. **Data Generation:** It generates `ocr_answers.json` for evaluation and `page_mapping.json` for diagram detection.
5. **Code & Math Reconcile (`reconcile_answers`):** For answers containing code or equations, a second focused pass re-reads that content. **Agreement-gate:** only where the two independent temperature-0 reads genuinely DISAGREE (whitespace-normalised) does an *arbiter* pass look at the image and decide the correct symbols — so a token both passes read the same (even a real student bug) is never "fixed". A correction is written back into the answer only when it changes **symbols/digits/superscripts but no spelled-out word and no `[TAG]`** (word- and tag-multiset invariance); code is spliced through the shared balanced `[CODE:]` scanner (`scripts/tag_utils.py`) on single-page/single-block answers, and math applies only to predominantly-math lines (prose is never rewritten). Anything uncertain — multi-page/multi-block, a guard failure, a word-level disagreement — degrades to the old behaviour: set `is_bad_handwriting` and route to manual review. `OCR_ARBITRATE=0` disables the rewrite entirely.
5. **Document Generation:** The transcribed text is automatically saved as both `.docx` and `.pdf` inside `/Users/nidhishchettri/OCR_Text`.
6. **Dynamic Filenames:** Files are named automatically based on the extracted metadata (`StudentName_Date.pdf` and `StudentName_Date.docx`).

## Dependencies

Required pip modules (already handled during creation):
```bash
python3 -m pip install openai python-docx fpdf2
```