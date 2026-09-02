# AI Answer Evaluator — System Architecture

> **Detailed developer edition.** For a plain-language explanation aimed at non-technical
> readers, see [ARCHITECTURE-plain.md](ARCHITECTURE-plain.md).

An automated grading pipeline for handwritten exam answer sheets. It ingests a student
answer sheet (PDF/image), transcribes it with a vision LLM (Qwen3‑VL via OpenRouter),
aligns it to a teacher‑supplied answer key cross‑checked against the question paper,
grades every question (objective deterministically, subjective/diagram via LLM), and
produces an interactive web report plus a downloadable PDF. A teacher‑in‑the‑loop review
layer lets a human confirm/override every mark before it is final.

This document is exhaustive: it covers every stage, substep, module, data file, config
knob and dependency. Line references are `path:line`.

---

## 1. Technology stack

| Layer | Technology |
|---|---|
| Web app | Flask 3.1 + Flask‑CORS, served by gunicorn (`gthread`) in Docker |
| Orchestration | Plain Python 3.12, subprocess pipeline + in‑process daemon threads |
| Vision / LLM | Qwen3‑VL through an **OpenAI‑compatible** endpoint (OpenRouter default; local vLLM/SGLang supported) via `openai` SDK |
| Image / PDF | PyMuPDF (`fitz`) rasterisation, OpenCV (`opencv-python-headless`) + NumPy + Pillow preprocessing, `pytesseract` (OSD, optional) |
| Reports | `fpdf2` (PDF), `python-docx` (OCR transcript), self‑hosted KaTeX (web math) |
| Persistence | Filesystem JSON state machine (primary) + PostgreSQL (`psycopg2`) for the rejected‑answer audit trail and a legacy answer bank |
| Parsers | PyPDF2 / python‑docx text extraction feeding the LLM key/paper parsers |

There is **no ORM and no message queue**: the pipeline is a *filesystem state machine*.
Each run owns a directory under `output/<run_id>/`; long jobs run as background daemon
threads that write JSON status files the browser polls. This is why gunicorn runs a small
fixed thread pool and deliberately omits `--max-requests` (recycling a worker mid‑job would
orphan a background thread).

---

## 2. Repository map

```
.
├── evaluation_app/
│   ├── app.py                     # Flask web layer: 31 routes, background jobs, gates (1509 ln)
│   ├── templates/index.html       # Single‑page wizard UI + report renderer (2918 ln)
│   ├── static/katex/              # self‑hosted KaTeX
│   └── uploads/                   # current session state (parsed key/paper, marks-source, report path)
├── scripts/                       # orchestration + shared infra (NOT the stage engines)
│   ├── full_evaluator.py          # THE pipeline orchestrator + segmentation-repair + marks reconcile (2243 ln)
│   ├── batch_evaluator.py         # multi-student batch orchestration (279 ln)
│   ├── review_corrections.py      # teacher working-copy model + Postgres audit (410 ln)
│   ├── extract_json_from_key.py   # answer-key LLM parser (single-call) (376 ln)
│   ├── extract_json_from_question_paper.py  # question-paper LLM parser (per-page parallel) (170 ln)
│   ├── parallel_parse.py          # per-page parallel parse engine (160 ln)
│   ├── upload_validation.py       # pre-flight upload gates + marks breakdown (564 ln)
│   ├── llm_client.py              # the single provider-facing generate() (372 ln)
│   ├── llm_pricing.py             # pricing table + cost ledger (86 ln)
│   ├── qid_utils.py               # question-id canonicalisation (82 ln)
│   ├── marks_policy.py            # THE marks granularity rule: every mark a multiple of 0.5 (79 ln)
│   ├── review_flags.py            # THE reason a question is flagged (derive/summarise/attach) (320 ln)
│   └── tag_utils.py               # balanced [CODE:]/[DIAGRAM:] span scanner (66 ln)
├── skills/                        # the STAGE ENGINES (subprocess-invoked "skills")
│   ├── answer-sheet-separator/    # batch: split multi-student PDF (separate_sheets.py)
│   ├── ingestion-handler/         # Stage 1: PDF/image -> PNG pages (process_input.py)
│   ├── img-preprocessing/         # Stage 2: OpenCV conditioning (preprocess.py)
│   ├── orientation-correction/    # manual orientation gate (orient_pages.py, orientation.py)
│   ├── vision-ocr/                # Stage 3: Qwen3-VL OCR engine (run_ocr.py, 1792 ln)
│   ├── feature-extracter/         # Stage 5a/b: diagram detect + feature extraction
│   ├── diagram_evaluator/         # Stage 5c: 2-pass diagram grading (evaluate_diagrams.py)
│   ├── answer-evaluator-and-report-generation/  # Stage 6: grading + PDF/JSON (evaluate.py, 2260 ln)
│   └── answer-retrieval/          # LEGACY: DB answer bank (fetch_answers.py) — superseded
├── output/<run_id>/               # per-run artifacts (images, ocr, db_answers, reports, review state)
├── output/batch_<id>/             # per-batch artifacts (separation manifest, thumbs, sliced PDFs, status)
├── .parse_cache/                  # content-hash cache of parsed answer keys
├── tests/                         # 40 files, 358 test functions + tests/harness/
├── docs/                          # deploy + upload guides (+ this file)
├── Dockerfile, docker-entrypoint.sh, run-public.sh   # deployment
└── .env / .env.example / .env.public                 # configuration
```

**Key architectural idea — "skills":** the heavy stage engines live under `skills/` and are
invoked as **independent subprocesses** by the orchestrator (`run_command` in
`full_evaluator.py:1439`). Each is a standalone CLI reading files + env and writing files or
stdout. This gives per‑stage isolation, a per‑stage watchdog timeout, and the ability to
A/B a different model per stage purely through env vars. `scripts/` holds the orchestrator
and the shared, in‑process libraries (LLM client, parsers, review model).

---

## 3. End‑to‑end flow (bird's eye)

```
                 ┌─────────────────────── TEACHER SETUP (web wizard) ───────────────────────┐
  Step 1  Question Paper ─► extract_json_from_question_paper.py ─► current_question_paper.json
  Step 2  Answer Key     ─► extract_json_from_key.py ───────────► current_answer_key.json (+choices)
                          ├─ upload pre-flight gates (upload_validation.py)
                          ├─ cross-check key⇄paper marks  ─► marks_source_state.json
                          ├─ report folder confirm        ─► report_path_state.json
                          └─ (optional) marks-breakdown editor bakes corrections into the key
  Step 3  Answer Sheet ─► SINGLE or BATCH
                 └──────────────────────────────────────────────────────────────────────────┘
                                        │
        ┌───────────────── SINGLE ──────┴──────── BATCH ─────────────────┐
        │                                                                │
  full_evaluate() / prepare+resume                     separate_sheets.py -> manifest.json
        │                                              (teacher edits boundaries/names)
        │                                                       │
        ▼                                              batch_evaluate(): slice PDF per student,
  ┌── PER-SHEET PIPELINE (full_evaluator.py) ──┐        then run PER-SHEET PIPELINE for each
  │ 1 Ingestion   process_input.py             │◄───────────────┘
  │ 2 Preprocess  preprocess.py                │
  │  [Orientation gate: prepare -> STOP]       │  (human confirms rotations, then resume)
  │ 3 Vision OCR  run_ocr.py                    │  -> ocr_answers.json, page_mapping.json
  │    + canonicalise + reconcile-to-question-set + segmentation repair (7 layers)
  │ 4 Ground truth: canonicalise key, detect choices, overlay question paper, align -> db_answers.json
  │ 5 Diagrams: detect_diagrams -> (bg thread) extract_features -> evaluate_diagrams (+ .done sentinel)
  │ 5.x Key merges (choice / additive / subpart) + marks reconcile vs paper -> key_integrity.json
  │ 6 Grading+Report  evaluate.py  (MCQ deterministic | LLM prose | cascade; folds diagrams)
  └────────────────────────────────────────────┘
        │
        ▼
  review_state.json (pristine)  ─►  interactive web report + {Name}_{Roll}.pdf
        │
  TEACHER REVIEW ─► review_render.json (working copy): accept / change-marks / re-evaluate one question
        └─► PDF regenerated in place; rejected answers -> Postgres
```

Two things flow *around* the per‑sheet pipeline: the **orientation gate** (an optional
human confirmation inserted between preprocess and OCR) and the **teacher review** layer
(a post‑grading working copy). Both are designed so that the untouched path is
byte‑identical to having neither.

---

## 4. Teacher setup (Stage 0) — parsing, validation, marks authority

Before any sheet is graded, the teacher completes a 3‑step wizard. Steps 1–2 parse and
validate the two ground‑truth documents.

### 4.1 Question‑paper parsing (`extract_json_from_question_paper.py`)
- Invoked as a subprocess by `POST /parse-question-paper` (`app.py:447`).
- Text is extracted locally (PyPDF2 text layer / python‑docx paragraphs+tables) — **no OCR**.
- **Per‑page parallel** parse when the PDF has >1 text page and `PARSER_PARALLEL_PAGES` is on
  (`extract_json_from_question_paper.py:148-157`): each page is parsed concurrently by a
  `ThreadPoolExecutor` (`parallel_parse.py:104`, up to `PARSER_MAX_WORKERS`=8), then merged.
  Cross‑page merges keep `marks = MAX` (never summed) so a continuation can't double marks
  (`parallel_parse.py:139-141`).
- Output schema: one entry per printed question id — `{question_id, question(full verbatim
  text), marks, type}` and **no `answer` field** (`:46-47` "do not invent answers"). The
  paper is the *clean per‑question marks authority* and the source of the full question text.
- Explicitly keeps objective **Section A (Q1–Q8)** — the prompt forbids skipping a whole
  section or mistaking objective questions/instructions for non‑questions (`:48-51`), and
  propagates section‑header marks ("each carries 1 mark").

### 4.2 Answer‑key parsing (`extract_json_from_key.py`)
- Invoked by `POST /parse-answer-key` (`app.py:494`).
- **Intentionally a single whole‑document LLM call — NEVER per‑page** (`:359-363`). Rationale:
  marking‑scheme marks sit in a right‑hand column; a page fragment loses the answer↔marks
  association (measured: 24/56 answers came back 0 marks). `parse_key_parallel` exists but is
  deliberately unused. This asymmetry vs the question paper is the single most important
  parsing decision.
- Output: `metadata{class, subject, choice_groups[], inline_choice_ids[]}` +
  `questions{qid: {question_id, question(**forced ""** — paper supplies it), answer, type,
  subject, marks}}`.
- **Marks semantics are prompt‑driven** (`:130-186`): distinguishes (A) whole‑question OR
  choice (each alternative full marks + a `choice_groups` entry), (B) additive multi‑part
  (each part its own marks), (C) additive with an internal OR in one part (one question,
  add to `inline_choice_ids`).
- **Content‑hash cache** `.parse_cache/key_<sha256>.json` (`:280-330`): the SHA‑256 key is
  `version \0 model \0 raw_text`, so a byte‑identical re‑parse is instant and a model change
  invalidates. `_CACHE_VERSION="2"` = the "empty question field" schema.
- JSON robustness: `_sanitize_json_escapes` doubles stray backslashes so LaTeX survives, plus
  brace‑extraction fallback.

### 4.3 Upload pre‑flight validation (`upload_validation.py`)
Three layers, run in‑process before/after each parse. Severity `ERROR` blocks; `WARNING`
surfaces but allows.
- **RAW** — `validate_raw_file` (`:123`): rejects a scanned/no‑text‑layer PDF (0 chars →
  `no_text_layer` ERROR — the #1 failure mode), unsupported type, unreadable, or warns on
  very little text. Runs *before* the paid LLM call.
- **PARSED** — `validate_parsed_questions` (`:155`): no questions → ERROR; `marks<=0` on
  ≥50% of questions → ERROR else WARNING (`missing_marks`); a maximum that is **not a multiple
  of 0.5** → WARNING (`marks_not_half_step`) — almost always a misread (`[2]`→`[0.8]`), and a
  question whose ceiling is illegal can never be scored legally. **Reported, never rewritten**:
  the key is the teacher's ground truth, so silently correcting it would hide a parse error.
- **CROSS‑CHECK** — `cross_check` (`:320`): compares key effective marks vs paper marks per
  base question — total mismatch, missing/under‑marked/over‑marked/unknown questions (all
  WARNING; the grading‑time reconciler fixes them). `validate_question_paper_structure`
  flags a leading/internal numbering gap (dropped Section A).
- **LOST CHOICE DATA** — `choices_lost_issues` / `ungrouped_choice_bases`: a key parse that drops its
  `metadata` yields `choice_groups == []`, and `key_effective_marks_by_base` then falls back to a plain
  additive sum — counting **both** branches of every OR‑pair. Measured on a real CBSE Science X key:
  **106 instead of 80** (the teacher‑reported symptom was 114). Nothing caught it, because `cross_check`
  needs a question paper and the UI announced the inflated figure as "✓ Marks verified". Fires **only**
  when the key declares no choice group at all *and* ≥1 base has ≥2 top‑level alternatives, so a key
  whose choices parsed is never second‑guessed (0 false positives on live data; all 9 real choice bases
  detected when the sidecar is wiped). Deliberately **does not** rewrite the total — with the choice data
  gone there is no way to know which alternative the paper offers — it surfaces `choices_missing` so
  `/marks-breakdown` can render an amber *not verified* line instead of the green one.
  *Rejected refinement:* requiring alternatives to be worth the **same** — a case study whose OR sits
  only in its last part scores `[1, 1, 4]`, which missed 3 of 9 real choices.
- Uses the **same** canonicalisation and `effective_choice_marks` model as the grader
  (imported from `full_evaluator`) so pre‑flight and grade‑time agree.

### 4.4 Marks authority (`marks_source_state.json`)
`compute_marks_mismatch` produces `{mismatch, key_total, qp_total, recommended, per_question}`
(recommended = `question_paper` unless the paper has no usable marks). The teacher confirms a
**marks source** via `POST /confirm-marks-source`, which sets the downstream reconciliation
mode:
- `question_paper` → `align_to_paper` (paper authoritative both directions)
- `answer_key` → `trust_key` (no changes)
- unset/CLI → `raise` (conservative: raise shortfalls, flag inflation)

### 4.5 Marks‑breakdown guided editor (optional)
`GET /marks-breakdown` builds editable rows (`build_marks_breakdown`); the UI renders five
card types (conflict / OR‑choice suggestion / residual multi‑part / extra / full table).
`POST /confirm-marks-breakdown` calls `apply_marks_corrections`, **overwrites
`current_answer_key.json` + choices in place**, and forces `source=answer_key, edited=true`
→ `trust_key` at grade time. `POST /reset-marks-breakdown` restores from the pristine
`_parsed` snapshots.

**OR‑choice grouping — rows are not alternatives.** The editor tags the ROWS a teacher ticked, which
are *not* the choice alternatives when an alternative is itself multi‑part (Q32 = Part A (I–IV) OR
Part B (I–IV)). Posting the 8 leaves declared 8 mutually‑exclusive alternatives, so
`effective_choice_marks` returned `max(1,1,…)=1` instead of `max(4,4)=4` — **capping a 4‑mark question
at 1** and dropping a 70‑mark paper to 67. `_collapse_to_alternatives` (`upload_validation.py`) fixes
this at the single save‑time choke point `_editor_choice_group`, so every editor path is covered and
what is *persisted and graded* is correct whatever the UI posts (this is also the only layer that is
testable — the repo has no JS test infra). Rule: the **shallowest sub‑part depth at which the selection
splits into ≥2 distinct alternatives**; going deeper only when needed is load‑bearing, since a blanket
first‑token collapse would fold `Q34(IV)(A)/(B)` into one `Q34(IV)` and destroy a nested OR. Flat pairs
are identity. Two companions are required, not optional: `build_marks_breakdown`'s `group_of` matches
members **prefix‑tolerantly** (`_is_under`) or a saved branch‑member group's finer leaves return
`group: null` and look dissolved on reload; and `mbCurrentTotal` (index.html) resolves alternatives
rather than maxing raw rows — it was a second, independent instance of the same bug. The per‑group
badge now prints what the group resolved to (`Q32(A) (4) OR Q32(B) (4) → counts 4`), since a grouped
base is excluded from the residual cards and otherwise showed no arithmetic at all.

### 4.6 Report folder
`_finalize_answer_key` derives Class/Subject → a suggested `~/Desktop/{Class}/{Subject}`
folder; `POST /confirm-report-path` persists the (possibly edited) folder to
`report_path_state.json`.

Grading is gated on **all** of: question paper present, answer key present, report folder
confirmed, `validate_for_evaluation` non‑blocking, marks source confirmed.

---

## 5. The per‑sheet pipeline (`full_evaluator.py`)

`full_evaluate(input_file, student_name, answer_key_path, report_dir, exam_class,
exam_subject, question_paper_path, marks_source)` (`:1511`) is the straight‑through entry.
`run_id = Path(input_file).stem`; all artifacts live in `output/<run_id>/`. It builds a
subprocess `env` by overlaying the project `.env`, resets the per‑run cost ledger
(`api_costs.jsonl` via `API_COST_LOG`) and profiling, runs Stages 1–2, then delegates to
`_evaluate_from_preprocessed` (`:1608`) for Stages 3–6. The orientation‑gated path
(`prepare_orientation` / `resume_after_orientation`) shares that exact same tail so grading
is identical.

**Re‑upload of a corrected sheet — `_reset_run_dir(output_base)`.** Because `run_id` is the uploaded
file's *stem*, a teacher replacing a wrongly‑uploaded sheet under the same name reuses the same output
folder. Nothing downstream truncated it: Stage 1 and Stage 2 both `makedirs(exist_ok=True)` and only
overwrite the pages they emit, while `_ingest_and_preprocess` (`:2219`) and the OCR stage (`:1638`)
**glob** those folders. Replacing a 5‑page sheet with a 2‑page one therefore left pages 3–5 of the OLD
sheet on disk and OCR read all five — reproduced before the fix, and the graded result would have mixed
two students' work. Two further hazards: a stale `review_state.json` meant a run that died mid‑pipeline
kept serving the PREVIOUS grading as current, and per‑run manifests (`page_mapping.json`, diagram and
answer‑crop manifests) could describe pages the new sheet does not have.

Everything under `output/<run_id>/` is pipeline‑derived, so a fresh run clears all of it except
`batch_sheet_args.json` (the batch parent's IPC input, written before the child starts). Called from
`full_evaluate` and from `prepare_orientation` via `_setup_run(..., reset=True)`; **never** from
`resume_after_orientation`, which consumes the `preprocessed/` pages and `orientation_review.json` that
phase 1 left behind — `_setup_run` defaults to `reset=False` for exactly that reason. The answer key and
question paper live in `evaluation_app/uploads/`, outside the run folder, so they survive untouched and
a re‑upload re‑grades against the same key. Reports are written as `{base}.pdf`, so re‑grading the same
student overwrites rather than accumulating.

**Replacing an evaluation when the corrected sheet has a DIFFERENT file name.** A different name means a
different `run_id`, so the in‑place reset above does not apply and the superseded run would linger with
its own report. `/evaluate` and `/prepare-orientation` therefore accept an optional `replaces_run_id`
form field, populated by a Step‑3 picker fed from `GET /previous-evaluations` (every run holding a
`review_render.json`/`review_state.json`, newest first — batch runs included).

- `_validated_replaces(raw, new_run_id)` runs **before** grading so a bad id fails fast. Blank is the
  normal case, not an error. `old == new_run_id` is silently ignored rather than rejected: the same
  filename already reuses and resets that folder, so honouring it would delete the run being produced.
- `_supersede_run(old, new, new_report_path)` runs **only after `status == "success"`** — deleting up
  front would leave a teacher whose re‑upload then failed with neither evaluation. In the orientation‑
  gated flow the id rides through `orient_status.params` so phase 2 can act on it at completion.
  Best‑effort: a failed removal must never turn a successful grading into a reported error.
- The old report PDF is deleted **only when the new run wrote to a different path**. Reports are named
  after the student, so an unchanged name means both runs share one file that the new grading has
  already overwritten — removing it then would delete the report just produced.

Both properties are mutation‑tested (`tests/test_replace_evaluation.py`): rewriting the guard to
supersede regardless of outcome makes the ordering test fail.

**Cancelling an in‑flight evaluation — `POST /cancel-evaluation/<run_id>`.** The replace flow only helps
*before* Start Evaluation; a teacher who spots the wrong sheet during the orientation review or
mid‑grading needs the work to **stop**, since grading is the billable stage. Cancellation therefore
kills rather than merely flags:

- `request_cancel(run_id)` marks the run and `kill_process_tree`s every stage subprocess it owns — the
  same process‑group mechanism the stage watchdog uses (`_new_group_kwargs()`; see §12 *Platform
  support* for the POSIX/Windows split).
- `run_id` reaches `run_command` through **`env["RUN_ID"]`, not a thread‑local**: stages run on the run
  thread, but the answer‑crop pass runs on its own thread with a *copied* env, and a thread‑local would
  silently fail to cancel it.
- `run_command` refuses to spawn once cancelled (so a cancel between stages costs nothing), re‑kills
  after registering to close the spawn race, and reports `CANCELLED_MSG` instead of a crash.
  `_evaluate_from_preprocessed` also guards the four stage boundaries so the pipeline aborts promptly
  rather than grinding through the stages that deliberately degrade gracefully.
- The route then removes the run folder, so the wrong sheet leaves nothing behind and — having no
  `review_state.json` — never appears in the replace picker.
- **The cancel flag is deliberately NOT cleared during teardown.** `_write_orient_status` recreates the
  run folder, so a worker still unwinding would otherwise resurrect the directory just deleted and
  report a failure for work the teacher chose to stop. It is cleared instead when a new run claims the
  same `run_id` (`/evaluate`, `/prepare-orientation`) — the real lifecycle, and what lets the corrected
  sheet re‑use the same file name.

UI: a *Cancel evaluation* button on the progress screen and *Wrong sheet — cancel* on the orientation
review; both confirm, stop the pollers via `evalCancelled` (otherwise they poll a deleted run, 404, and
show a spurious error), then `resetToUpload()` clears the file input and re‑shows Step 3 **without**
going through `goToStep()`, which would re‑run the wizard gate and pop a spurious alert.

The kill is mutation‑tested (`tests/test_cancel_evaluation.py`): downgrading `request_cancel` to
flag‑only makes the real‑subprocess test fail.

`_write_progress(output_base, index)` (`:1594`) stamps `progress.json` with a 3‑step
user‑facing checklist ("Reading handwriting (OCR)" → "Analyzing diagrams" → "Grading &
building report") that the web UI polls.

Each subprocess stage runs under a **watchdog** (`run_command`): `_new_group_kwargs()`
puts it in its own process group so a timeout force‑kills the whole tree; per‑stage ceilings
(`_STAGE_TIMEOUTS`, e.g. `run_ocr.py`=1200s, `evaluate.py`=1200s) are overridable via
`STAGE_TIMEOUT_<NAME>` / `STAGE_TIMEOUT`. A timed‑out stage degrades gracefully.

### Stage 1 — Ingestion (`skills/ingestion-handler/scripts/process_input.py`)
- Rasterises the input to PNG pages. `render_pdf_page` opens its own `fitz` document per
  page (process‑safe) at **300 DPI** (`zoom = 300/72`), writes `<stem>_page_<N>.png`
  (1‑indexed). Standalone images are validated (`img.verify()`) and passed through.
- Parallelised with a `ProcessPoolExecutor`; results gathered in submission order so page
  order is preserved. Output → `output/<run_id>/images/`.
- (`crop_diagrams_cv.py` is a dormant QR‑driven OpenCV cropper, not in the live path.)

### Stage 2 — Preprocessing (`skills/img-preprocessing/scripts/preprocess.py`)
Per‑page OpenCV conditioning in `process_single_image` (`:124`), `ProcessPoolExecutor` over
pages, output `preprocessed_<name>.png` in `output/<run_id>/preprocessed/`:
1. Optional force‑landscape — **OFF by default** (`_maybe_force_landscape`; blind rotation
   once corrupted orientation, so orientation is deferred).
2. Grayscale.
3. Perspective correction — only warps on a 4‑point contour covering >85% of the page (guards
   against cropping inner answer boxes).
4. Deskew — `minAreaRect` angle folded to [‑45,45]; **skips** if `|angle|<0.5` (not worth
   resample blur) or `>15` (ruled lines drove the estimate).
5. CLAHE contrast (clip 2.0, 8×8 tiles).
6. **8‑bit grayscale output, no Otsu binarisation** — keeps stroke‑weight cues so the vision
   model can tell `_` from `-`. Adaptive upscale to `PREPROCESS_LONG_EDGE_TARGET`=3500px
   (never downscales).
7. Lossless PNG, no DPI tag (the model consumes pixels).

(`orientation_fix.py` is a dormant VLM‑based rotator, not wired in.)

### Orientation gate (optional human‑in‑the‑loop, between Stage 2 and 3)
Split of the pipeline so a teacher confirms each page's rotation before spending OCR credit.
- `prepare_orientation` (`:2158`): runs Stages 1–2, then `orient_pages.py` writes
  `orientation_review.json`, and **STOPS before OCR**. As of 2026‑07‑21 the gate is **fully
  manual**: `build_review` emits `suggested_rot=0 / confidence=ok / method=manual` for every
  page (the Tesseract‑OSD detector `orientation.py:suggest_rotation` still exists but is
  **bypassed**). The preview shows pages exactly as uploaded.
- `resume_after_orientation(run_id, rotations, …)` (`:2199`): applies the teacher's confirmed
  absolute rotations (`_apply_page_rotation`, `deg==0` = true no‑op) to the pristine
  preprocessed PNGs, then runs the shared OCR→report tail.
- Web endpoints: `/prepare-orientation`, `/orient-status/<run_id>` (polled),
  `/orient-preview/<run_id>/<page>` (serves the un‑rotated image; the browser rotates via CSS
  so the preview matches what OCR grades), `/confirm-orientation/<run_id>`. Batch variants
  mirror this grouped by student.

### Stage 3 — Vision OCR (`skills/vision-ocr/scripts/run_ocr.py`, 1792 ln)
The largest engine. Transcribes preprocessed pages to per‑question answers.

**Dispatch & core call.** One `ThreadPoolExecutor` (`OCR_MAX_WORKERS`=12) runs a header pass
plus all page‑OCR tasks concurrently; each page task receives its predecessor's path for
*pair‑context* (page‑break continuation). The primitive `_ocr_generate` (`:297`) sends
`[context_image, page_image]` (base64 data‑URLs, built in `llm_client.generate`) with
`temperature=0, top_p=0.95, max_tokens=32768`, then strips `<think>` blocks. Per page the
model returns free text with inline `[START_Q: n]`/`[END_Q: n]` boundary tags and structural
content tags — **not JSON**.

**Prompts.** `HEADER_PROMPT` (Name/Class/Roll/Date/Max Marks only), `MAIN_PROMPT` (the full
transcription contract: markers, math super/subscript fidelity, glyph disambiguation O↔0
l↔1 S↔5, code fidelity preserving `_`, correction/strikethrough handling, legibility tags),
and `build_main_prompt` which appends a **CLOSED QUESTION SET** section when the exam's
question numbers are known (anchoring segmentation to real question numbers).

**Assembly — `assemble_answers` (`:1394`)** welds per‑page fragments into per‑question
answers: `[START_Q]` detection, no‑marker/leading‑text pages weld onto the active question
(cross‑page continuation), per‑chunk classification (duplicate / backward / implausible /
out‑of‑set / has‑subpart), and a conservative weld decision that never opens a stale key on a
misread number. Forward duplicates of a non‑active question are recorded as **collision
bases** (the mixed‑answer tell‑tale). Returns `(ocr_answers, page_mapping, qid→image,
full_text, collision_bases)`.

**Orphan‑page rescue (`UNASSIGNED_QID`, `OCR_KEEP_ORPHAN_PAGES`, default on).** Text with no
`[START_Q]` and **no active question** — typically the first content page — previously had no
branch and the whole page was **silently discarded** (`:1510`, and leading text at `:1520`).
It is now parked under the digit‑free key `_unassigned_` and `_map_page`d, so the repair layers
can mine it (`split_objective_answer_lists` fans an objective run straight into `Q1..Qn` and
consumes the holder) and a teacher can see text we could not place. The key **must** stay
digit‑free: `normalize_qid('_unassigned_p2')` canonicalises to `Q2`. Blank text creates no
holder; `evaluate.py` skips all `_`‑prefixed keys so a leftover holder is never graded as a
phantom question. Pages that hit this path are listed in `ocr_output/orphan_pages.json` and
printed as a stage warning.

**Code/math reconciliation** (`reconcile_answers` `:1219`, gated by `OCR_VERIFY_CODE`,
`OCR_VERIFY_MATH`, `OCR_ARBITRATE`, all default‑on): focused re‑reads of code/math regions
(`_ocr_code_only`, `_ocr_math_only`) feed an **agreement‑gated arbiter** that may correct
only symbols/digits/scripts — never a word or a `[TAG]` (WORD+TAG multiset invariance so
prose like `rang` is never "fixed" to `range`). Parallelised with a `ThreadPoolExecutor` +
`_memo_reread` Future‑memoisation so each page image is re‑read once and charged once. Failed
guards set `is_bad_handwriting` + a symbol‑warning sidecar → routes to review. (This
parallelisation fixed the Maths‑batch OCR 21s→177s regression.)

**Outputs** (`output/<run_id>/ocr_output/`): `ocr_answers.json` (`{qid:{answer,
is_bad_handwriting}}` + `_instructions_`), `page_mapping.json`, `mixed_answer_flags.json`,
`orientation_flags.json`, `student_meta.json` (Name/Roll/Date), and `{Name}_{Date}.docx`/
`.pdf` transcript exports.

**In‑assembly repairs owned here:** out‑of‑set one‑digit snap (`_resolve_out_of_set_qnum`),
pair‑context page‑break weld, collision detection. **Orientation‑vote** (`OCR_ORIENT_VOTE`,
default OFF): re‑OCRs suspect pages at 180/90/270 and accepts a rotation only if it reduces
the sheet‑wide out‑of‑set count (a wrong guess can never make it worse). Two further
orientation mechanisms (content‑fallback probe, per‑page autofix) also exist and are
default‑OFF.

**Structural content tags** are model‑emitted, not CV‑detected: `[DIAGRAM: desc]`,
`[CODE: …]` (balanced‑bracket‑safe via `tag_utils.tag_spans` so inner `]` survives). Tables
render as markdown; there is no `[TABLE]`/`[IMAGE]` tag. Legibility markers `[BAD_HANDWRITING]`
and `[ambiguous: a/b]` route to Needs‑Review.

**Post‑OCR normalisation in the orchestrator** (`_evaluate_from_preprocessed`):
1. Derive the authoritative question‑number set from key ∪ paper (`_derive_question_id_set`)
   → written to `question_ids.json`, passed to OCR as `--question-ids-file` to anchor
   segmentation.
2. Canonicalise OCR ids (`normalize_qid`: `A1`/`Ans 1`/`Q.1`/`13`→`Q1`), merging collisions.
3. `reconcile_ocr_to_question_set` (flag‑only): out‑of‑set ids get `is_bad_handwriting` +
   `question_set_warning`; in‑set gaps are logged.
4. Canonicalise `page_mapping.json` ids.

### Stage 4 — Ground‑truth alignment
With the answer key present ("Manual Key Only" — the legacy DB fetch is disabled):
1. Load key, `_canonicalize_db_keys` (`1`→`Q1`, dedup collisions with `OR`).
2. **Qwen segmentation recovery** (gated on the closed question set; all additive,
   flag‑on‑doubt, none touch `assemble_answers`):
   - `split_objective_answer_lists` — split one OCR block holding `A1..A6` back into per‑MCQ
     entries (only when ≥3 strictly‑ascending in‑set MCQ‑typed numbers, all option‑like).
     `_OBJ_LABEL_RE` (`:274`) no longer demands a separator AFTER the number: a bracketed option
     may follow directly, so `Q.16 (C)`, `Q16 (C)` and a bare `16 (C)` parse alongside
     `Q16. (C)`. Prose opening with a number still does not qualify (no bracket follows).
     The never‑clobber guard (`:404`) now ignores two non‑independent "filled" slots — the HOST
     itself (its own answer is the first line of the list it holds) and any slot carrying
     `recovered_from`/`split_from == src_key` (already lifted out of this same host). Both fixes
     were needed: on the real `Maths_Class12` Q15 blob every other gate passed and the guard
     alone aborted the split.
   - `reattach_leading_continuation` — re‑home a trailing sub‑part a page break swallowed into
     the next question's top.
   - **Label grammar (shared slots).** `_Q_MARKER` / `_Q_SEP` / `_Q_PREFIX` / `_A_PREFIX` /
     `_DATE_TAIL` are module constants consumed by BOTH `_qnum_header_idx` and the header‑strip
     in `recover_gaps_by_position`, so the matcher and the stripper cannot drift (a narrower
     stripper leaves the student's label inside the recovered answer — that regression happened
     once and was caught only by a test). Accepted: separators `. - : , ; # / _`, `No`/`Number`
     compounds (`Q No 17`, `Q.No.17`, `Question Number 17`), and answer‑side markers
     (`Ans 17)`, `Sol 17`, `Soln`, `Solution`, `Att`, and bare `A17.`). **Bare `A` requires a
     terminator after the number** — without it, matrix cofactor notation (`A11 = -2`,
     `A21 = -(1)`, present in three archived Maths runs) opens a question. Two guards run first:
     a **date tail** (`12.5.2024` opened Q12 via `pat_inline`'s numeric sub‑part branch — a real
     bug found by importing the taxonomy's negative set) and a **`=` follower**, because in
     physics `Q1`/`Q2` are charge symbols. Corpus sweep: **+18 matches, all genuine answer
     labels, 0 removed.** `_OBJ_LABEL_RE` (`:279`) carries the same marker/separator slots.
     The published taxonomy's own reference regex was measured and **rejected**: over 3,603 real
     lines it yielded 155 matches of which **56 were wrong** (its roman‑numeral branch eats
     letters inside words — `AC = 5cm` → question 100). Its coverage was adopted, its pattern
     was not; its §13 negatives now live in `tests/label_negatives.py`.
   - `recover_gaps_by_position` — lift a fragment the student numbered with a gap question's
     own number out of a neighbouring answer. `_qnum_header_idx` (`:427`) accepts the prefix
     **with or without a dot** (`Q37` *and* `Q.37`); the old pattern let only `Ques` carry one,
     so `Q.17` — the commonest handwritten label — matched nothing and this whole layer was blind
     to it (measured: 18 buried `Q.<n>` headers vs 43 matchable; fixing it recovers 10 archived
     answers that scored 0). The header‑strip regex (`:582`) accepts the same forms, or a lifted
     fragment keeps its own label as answer text. A run with no standalone numeric header is
     recovered as a **chain** (Q17 out of Q15, Q18 out of Q17, …), so the host rewrite preserves
     its own `recovered_from` / `rehomed_to` / `split_from` keys.
   - `repair_glued_answers` — fill a still‑BLANK in‑set target from a glued/collision slot;
     Tier 1 flags, Tier 2 probes in‑set neighbours (`GLUE_PROBE_NEIGHBORS`), Tier 3 re‑homes
     an off‑topic‑for‑itself host (`GLUE_PROBE_OFFTOPIC`) using an LLM glue‑matcher
     (`GLUE_MATCHER_MODEL`, bounded by `GLUE_MAX_PROBES`).
     **Runs to a FIXPOINT.** The matcher returns ONE `(target, foreign)` per call and hosts used
     to be chosen once from the initial blank set, so a slot holding N buried answers yielded
     exactly one (Q15 held Q16–Q20; only Q16 came back). Each ROUND now recalls
     `_glue_hosts(...)` against the CURRENT blanks — a slot filled mid‑run can itself become the
     host of the next find — and a round that recovers nothing ends the loop. Retries are
     **breadth‑first** (`GLUE_HOST_ATTEMPTS`, default 2): pass 1 asks every host once, pass 2
     re‑asks only those that yielded nothing, because retrying one host twice before its
     neighbours were asked at all starved `GLUE_MAX_PROBES` (default raised 8 → **24**).
     The matcher is measurably **unstable** — sampled 3× on the real `Maths_Class12` Q37 block it
     answered NONE, NONE, Q38 — which is what the retry is for. Attempts are counted per
     `(host, frozenset(blanks))`, so an unchanged question is never asked more than
     `GLUE_HOST_ATTEMPTS` times and the loop cannot spin. `GLUE_BLOCK_CHARS` (default 6000,
     was a hardcoded 1500) stops truncating a long host before its appended foreign answer.
     Measured over the archived runs: **6–7 of the 9 remaining append‑glue losses recovered
     (+23 to +27 marks), zero answers lost.**
3. `_load_or_detect_choices` — choice groups from the key metadata, else structural OR‑scan.
4. `_overlay_question_paper` — overlay the paper's full `question` text onto key entries
   (question field only), since the key deliberately emits `question=""`.
5. Align: for each OCR id, exact/base match to the key → build `aligned_db` →
   write `db_answers.json`.

#### Completeness recovery — a PRESENT but TRUNCATED answer

Every recovery layer in Stage 4b is gated on a question being **BLANK**: `_recompute_gaps`
(`full_evaluator.py:415`) counts a question as present when `str(answer).strip()` is non-empty. So a
half-captured answer is invisible to `recover_gaps_by_position`, `repair_glued_answers`,
`_offtopic_rehome_hosts` and `reattach_leading_continuation` alike.

Measured on Vinayak's Science sheet, Q37 (4 marks): the preprocessed page images are **byte-identical**
between two runs, yet the page carrying part (c) dropped that whole block in **1 of 3** controlled
re-reads (2 of 5 counting the real runs). Q37 went 688 → 301 chars and lost 3.5 marks. The failing run
was **structurally perfect** — no gap, no orphan page, no out-of-set number, empty collision flags — so
nothing reported anything. Nothing anywhere checked *completeness*.

The unused signal is the answer key, which declares the question's sub-parts.
`run_ocr.recover_incomplete_answers` (called right after `assemble_answers`, gated on
`--answer-key-file` + `OCR_COMPLETENESS`) re-reads the pages behind any answer missing a key-declared
top-level part, swaps the better page read into `results`, and lets **assemble_answers run again** — so
every existing placement rule still applies rather than this layer splicing text itself.

**The OR-choice subtlety is what makes it usable.** `"(a) … OR (b) …"` means the parts are
*alternatives*; a student answering one has omitted nothing. `_key_subparts_by_base` splits on OR and
takes the **intersection**, keeping only what every alternative demands. On the real sheet that is the
difference between **5 flags and 1**: Q23/Q25/Q34/Q35/Q36 intersect to nothing, while Q37 repeats "(c)"
on both sides of its OR — exactly the part the bad read dropped. Measured: **0 flags on the good
capture, exactly Q37 on the truncated one.**

Two safety properties, both behaviourally tested rather than grepped:
- `answers_shortened_by` / `commit_completeness_repair` — a re-read is a fresh sample of a
  non-deterministic model, so it can be better in one place and worse in another. Any shrinkage
  **anywhere** vetoes the whole repair.
- A restored answer is written to `recovery_flags.json`, raising Needs Review so a teacher confirms it.

Use `base_qnum` (`qid_utils`), never a bare digit search, on OCR ids: `assemble_answers` emits
**subject-prefixed** keys and `re.search(r'\d{1,3}')` on `AI10_Q37` returns **10**, silently checking
the wrong question on every real run. Bare-`Q37` unit tests cannot see that; only the live stage did.

The prompt was hardened alongside: a sub-part label may never open a slot (the model tagged Q37's "(c)"
as `[START_Q: 8]`, which only landed correctly because the collision handler re-homed it — working by
accident), and it must transcribe every block on the page. Page 16 has captured part (c) in **7 of 7**
reads since; encouraging, not yet conclusive.

### Stage 5 — Diagram processing
1. `detect_diagrams.py`: keys off OCR `[DIAGRAM:]` tags × `page_mapping.json` → a list of
   `{question_id, image(full page path)}`, capped at `MAX_DIAGRAM_PAGES_PER_Q`=4. (Despite the
   name `diagram_crops.json`, these are full pages.)
2. If any diagram found, and `PARALLEL_EVAL` is on (default), the two slow vision sub‑stages
   run in a **background daemon thread** overlapping grading (`_run_diagrams` `:1857`):
   - `extract_features.py`: one **answer‑blind** vision call per page — "list every label,
     shape, axis, arrow, relationship" → `student_features.json`. Output is capped by
     `DIAGRAM_FEATURES_MAX_TOKENS` (1536) — see "Why a runaway needs a token cap" below.
   - `evaluate_diagrams.py`: **2‑pass** grading — Pass 1 text‑only (student features vs
     expected vs max marks → JSON verdict), Pass 2 **vision audit** (sends the actual image +
     the Pass‑1 draft to catch a missed/wrongly‑awarded feature). `needs_review` if
     `confidence < 0.8`. → `diagram_evals.json`. Bounded by `_STAGE_BUDGET`
     (`DIAGRAM_EVAL_STAGE_TIMEOUT`, default 390s).
   - The thread **always** drops a `diagram_evals.json.done` sentinel (even on failure) so the
     grader never hangs. It reads an isolated snapshot `db_answers_diagram.json` so later key
     merges can't perturb its inputs.

**This job IS the critical path.** `evaluate.py` blocks on `DIAGRAM_EVALS_SENTINEL` before folding in
diagram marks (`evaluate.py:2676`), so grading's wall time hides *inside* this thread. Measured on two
real runs, the diagram chain accounted for **99.9%** (Vinayak, Science X) and **98.9%** (Aramya, CS XII)
of total wall time — residual 0.5s / 3.6s. Anything serialised in front of `extract_features` is
therefore paid in full by the user, which is why display cropping now runs alongside it (Stage 5.2).

#### Why a runaway needs a token cap (and a timeout cannot help)

Both runs above recorded `extract_features` at **exactly 210.0s** — not work, but
`DIAGRAM_LLM_TIMEOUT(90) × (retries 1+1) + 30`, the stage budget giving up. Replaying the stalled crops:

| crop | wall | output tokens |
|---|---|---|
| Q31 (stalled) | **625.3s** | **16384** ← the provider's default cap |
| Q22 / Q36 / Q37 (healthy) | 19.6 / 29.8 / 23.4s | 615 / 816 / 591 |

The model entered a **repetition loop**. The generation *rate* was normal throughout (26 vs 31 tok/s) —
nothing was slow, it simply would not stop. `timeout` is an httpx **read** timeout, i.e. the longest
allowed *silence between bytes*; a steady stream never trips it. Measured: a probe with `timeout=200`
ran **625s** to completion. Only `max_tokens` ends a runaway, and `extract_features` was the one vision
call in the pipeline without one (OCR 32768, grading 12288, diagram‑eval 12288, separator 512,
orientation 8); `generate()` supplies no default, so it inherited the provider's 16384.

After the cap, the same four real crops: **210.1s → 75.7s, and Q31 is now captured** (it used to be
abandoned and its diagram lost entirely). The cap does bind on dense pages — a chemistry page's feature
list truncates at the tail — which is acceptable because Pass 2 of diagram grading sends the page image
itself, so the features are an aid rather than the sole input.

#### The vision audit (pass 2): cost, and the failure that zeroed questions

Measured per call on the Science sheet:

| pass | wall | output tokens | note |
|---|---|---|---|
| 1 — text only (features vs key) | 12–35s | 383–722 | cheap |
| 2 — vision audit (re-sends the page) | 34–110s | 1,400–4,300 | ~75% of the stage |

Pass 2's JSON verdict is only ~1,700 chars; the rest of those output tokens is **hidden reasoning**
(`-thinking` model at `reasoning_effort=low`, `DIAGRAM_EVAL_MAX_TOKENS=12288`).

**Pass 2 fails outright on MULTI-PAGE diagram questions.** Two pages is ~17k input tokens and ~3.5MB of
image; the request is rejected and returns empty (`in=0, out=0`, ~12s):

| question | pages | size | pass 2 |
|---|---|---|---|
| Q22 / Q31 | 1 | 1.3–1.4 MB | works |
| Q28 / Q36 | 2 | 3.4–3.5 MB | **empty** |

That failure used to fall through to the generic handler, which returns `marks_awarded: 0.0` — so a
question the text pass had already graded was **silently scored 0**. It hit 2 of 4 diagram questions on
one sheet, and with `MAX_DIAGRAM_PAGES_PER_Q=4` it reaches any sheet whose diagram spans pages. A failed
audit now **keeps the first-pass grade** and sets `needs_review`, since the audit is what checks the
drawing itself.

**A cascade was implemented and defaults OFF** (`DIAGRAM_EVAL_AUDIT=cascade` enables it) because
measurement showed it does not buy wall-clock: `always` 122.6/132.7s vs `cascade` 186.6/99.4s, with no
reliable token saving either. The reason is structural — `max_workers` is 10 and a sheet has a handful
of diagram questions, so **all pass-2 calls already run concurrently**. Dropping 4 to 2 removes parallel
work, not critical-path work; the stage wall is set by the single slowest call (86–155s). It is kept
behind the flag because it is a real **cost** lever for batch runs, where concurrency rather than
latency is the scarce resource, and because its `zero_mark` trigger is independently useful (measured:
Q22's text pass scored 0/2 and the audit it forced corrected it to 2/2).

**Turning the audit's reasoning off was measured and REJECTED.** It is ~2× SLOWER, not faster — with
`reasoning_effort=None` the model emits *more* content tokens instead of fewer:

| arm | wall median | range | slowest vision call |
|---|---|---|---|
| reasoning=low (current) | **115.7s** | 105–162s | 101.2s |
| reasoning off | 223.2s | 194–269s | 202.5s |

(A single earlier probe suggested the opposite; three alternated rounds did not. One sample off this
stage is worthless.)

**Do not read a single timing off this stage.** Identical inputs have measured 99s, 101s, 105s, 116s,
119s, 122s, 132s, 162s, 184s, 186s, 210s and 303s. Compare medians across alternated rounds, and treat
call counts and token totals as the reliable signals.

**Diagram MARKS are non-deterministic at a level that dwarfs any tuning here.** Same inputs, same
settings, three rounds:

| Q | reasoning=low | reasoning off |
|---|---|---|
| Q22 (2) | 2, 2, 2 | 2, 2, 0 |
| Q28 (3) | 2.5, 2.5, 2.5 | 2, 2, 2 |
| **Q31 (3)** | **0, 0, 3** | 2.5, 1.5, 1.5 |
| Q36 (5) | 1.5, 1, 0.5 | 0.5, 1.5, 0.5 |

Q31 scored **0, 0 and 3 out of 3** on identical input. Within-arm spread exceeds every between-arm
difference, so no configuration comparison on this stage can be trusted at n≤3 — and diagram-mark
stability is a bigger open problem than diagram-stage latency.

`evaluate_diagrams.py` had **none** of this hardening: a bare `with ThreadPoolExecutor(...)` joins every
worker at block exit, so one wedged call held the stage until the orchestrator's 420s watchdog killed
it — discarding every diagram already graded, since nothing is printed until the end. With
`DIAGRAM_EVAL_MAX_TOKENS=12288` a single runaway pass is ~473s on its own, and there are two sequential
passes. It now mirrors `extract_features`: future‑level `wait(timeout=_budget)`, keep what finished,
name the abandoned questions, `shutdown(wait=False)` + `os._exit(0)` (pool threads are non‑daemon and
`atexit` joins them, which would otherwise re‑introduce the hang). The budget is **2×** the per‑call
worst case for the two passes and stays under the 420s watchdog, so the stage self‑limits and preserves
partial results instead of being killed with nothing. An unparseable verdict is now also reported rather
than silently dropped — Vinayak's Q36 had features extracted but vanished from `diagram_evals.json`
with no trace, making an un‑assessed diagram indistinguishable from a deliberate 0.

### Stage 5.2 — Diagram display crops (`crop_diagram_regions.py`, `DIAGRAM_CROPS`, default on)

`diagram_crops.json` is **misnamed**: `detect_diagrams.py` only decides *which* questions have a
diagram (regex on the OCR's `[DIAGRAM: …]` marker) and writes a question→**full page** map. Nothing was
cropped, so the report rendered whole pages — verified live: Q24 of `Maths_Class12` carried **two
full‑page data‑URIs** on its diagram segment.

`crop_diagram_regions.py` existed but was **dormant and silently produced nothing** (0/2 on real data):
it asked for `{"xmin","ymin","xmax","ymax"}` while Qwen‑VL answers `{"bbox_2d": […]}` (every box hit the
`KeyError` guard), and it read the numbers as **pixels** when they are **normalized 0–1000** — the same
units bug as the answer crops. Both fixed; it now writes `diagram_display_crops.json`
(`[{question_id, image, crop}]`).

**A THIRD shape bug of the same family, found 2026-07-30 — array vs object.** The prompt asks for a JSON
*array* `[{"bbox_2d": …}]`, but this call sets `json_mode=True`, so the provider forces a top-level
*object* and the real reply is a bare `{"bbox_2d": [198, 507, 843, 763]}`. `_parse_boxes` sliced between
the outermost `[` and `]`, which on that text yields the **coordinate array**, then iterated its four
**numbers** looking for boxes — so every page parsed to `[]` and the stage reported `"no diagram found"`
while the model was locating diagrams perfectly. Measured on one sheet: **9 of 9 pages lost**; after the
fix, **4 of 4 real diagrams cropped** (3.3–19.3% of page) with the 5 text-only continuation pages
correctly dropped. `_box_candidates` now normalises every observed shape — bare object, object wrapping
the array under a key, the requested array, and a bare `[x0,y0,x1,y1]` — while `{}` still means "no
diagram" and `[true,false,true,false]` is rejected (`bool` subclasses `int`).

**`reason` is what distinguishes the two failure modes.** A gate rejection reports
`"box covers 26% of the page (text, not a figure)"`; the bare initial `"no diagram found"` means the
parser got nothing — a *parsing* problem, not a detection one. Read it before blaming the model.

**The real risk is false positives, not missed diagrams.** `detect_diagrams.py` assigns *every* page of
a question, so text‑only continuation pages arrive here and the model boxes handwriting rather than
returning `[]`. Three measured gates separate them, all page‑relative or normalized so they survive
landscape pages:

| gate | default | measured basis |
|---|---|---|
| `DIAGRAM_CROP_MAX_AREA` | 0.20 | real figures 3.4–5.0% of page; text blocks 26.6–31.2% |
| `DIAGRAM_CROP_MIN_AREA` | 0.02 | a `30.` pen‑mark cropped at 0.96%; real figures ≥3.6% |
| `DIAGRAM_CROP_MAX_ASPECT` | 6.0 | chemical equations 8.4:1, a boxed answer 6.4:1; widest real figure (three labelled test tubes) 4.8–5.6:1 |

Plus the shared page‑relative ink check (`_row_ink_profile` imported from `crop_answer_regions.py`) and
one retry. A page whose content is not a figure is **dropped**, but only when that question already has
a real crop elsewhere — a question with no successful crop keeps its full page, so a figure we merely
failed to bound is never lost.

**Wiring — display‑only is structural, not a convention.** The cropper runs **concurrently with** the
graders inside `_run_diagrams` (`full_evaluator.py:2346`), which covers *both* the `PARALLEL_EVAL`
thread and the sequential fallback. It used to run *first*, on the reasoning that being inside this
background job cost no critical‑path time — true only while **grading** was the long pole. It is not
(see Stage 5): grading blocks on this job's sentinel, so a display‑only pass was measured adding **~33s
in front of a stage that never reads its output**. It is now started in its own thread and `join()`ed in
the `finally` **before the sentinel drops**, which preserves the ordering guarantee that `evaluate.py`
never sees a half‑finished crop set — and costs nothing, since cropping is far shorter than
features+evaluation. Overlapping is safe because the cropper writes `diagram_display_crops.json`
atomically (temp + `os.replace`) while both graders only *read* `diagram_crops.json`.
`extract_features.py` / `evaluate_diagrams.py` receive `diagram_crops_path` as
**argv**, so repointing `DIAGRAM_CROPS_JSON` cannot reach them — a grep confirms `evaluate.py:2148` is
its **only** runtime consumer. A bad crop therefore cannot move a mark.

Two ordering subtleties:
- The display manifest is **seeded on the main thread** with `crop: None` before grading launches.
  `env` is snapshotted when the grading subprocess starts, so a value set later from the background
  thread would never reach `evaluate.py`; seeding also means a failed/killed cropper degrades to full
  pages rather than losing diagram images entirely.
- The cropper **writes atomically** (`_write_atomic`, temp + `os.replace`), because the report reads
  that file from another process. No new sentinel is needed: `evaluate.py` already waits on
  `DIAGRAM_EVALS_SENTINEL` (`:2113`) *before* reading `DIAGRAM_CROPS_JSON` (`:2148`).

Measured across every run that has diagrams (`Maths_Class12`, both Science runs, `sheet_3_AKSHIT_SHARMA`,
`maths_Ans_sheet__merged`): **19 crops, 18 correct, 0 full‑page fallbacks**, 8 no‑diagram pages dropped.
The one false positive is a block of handwritten algebra at 1.1:1 — square, so no size or shape gate can
distinguish it from a figure; it appears beside the question's correct diagram rather than instead of it.

### Stage 4.9 — Per‑answer screenshots (`crop_answer_regions.py`, opt‑in `ANSWER_CROPS`)

DISPLAY‑ONLY. Runs in a **daemon background thread overlapping grading** (same sentinel discipline as
the diagram thread: stale state cleared, sentinel dropped in `finally`, env var set only when the
thread started) so it costs ~zero wall‑clock; `evaluate.py` waits on `ANSWER_CROPS_SENTINEL`.

**Wall‑clock cost — measured, not assumed** (`output/Class_X_Ujjawal/stage_timings.json`, 20 pages):
`crop_answer_regions.py` **18.49 s** against `evaluate.py` **157.12 s**, with `total_wall_s` **234.71 s**
vs a stage sum of **247.27 s** — the 12.6 s shortfall *is* the proof of overlap. Only **14 of 20** pages
make an API call at all (continuation‑only pages are cropped locally, zero network). It is free for a
second, independent reason: crops run on the **instruct** model while grading runs on the **thinking**
one, so they never contend for the ~370 tok/s grading throughput ceiling that actually bounds a batch.
Non‑zero costs are money (+$0.0098/sheet, ~7%) and disk (~5.8 MB/sheet ⇒ ~350 MB per 60‑sheet batch).

The **only** way this lands on the critical path is `evaluate.py` blocking on the sentinel *after*
grading has finished, so two bounds guard it:

- `ANSWER_CROPS_WAIT_TIMEOUT` defaults to **90 s, not the crop stage's own 300 s budget**. By the time
  this wait begins the crop pass has already had the whole grading window (150–350 s); anything still
  outstanding is a stalled/rate‑limited provider, and screenshots are cosmetic. Deliberate trade‑off:
  on timeout the sheet's report omits screenshots **entirely** (the manifest is written once, at the end
  of the stage), so 90 s sits clear of the ~65 s worst case under `BATCH_SHEET_CONCURRENCY` rather than
  being trimmed to the ~18 s average. Extracted as `wait_for_crops_sentinel()` so the bound is testable.
- `_scaled_caps` floors `ANSWER_CROP_MAX_WORKERS` at **4** (higher than the eval/OCR floors) — see
  §"Batch" below.

**The model is non‑deterministic at any resolution**, and that is by design absorbed downstream:
identical repeated calls returned `840` vs `890` on one page (a ~175 px swing) and `528` vs `520` on
another. Snap‑to‑gap + ink‑trim are what convert that wobble into stable crops — verified by running the
whole stage three times: the `band`/`page` method split was identical every time (36/3), while a handful
of JPEGs differed byte‑wise in *both* the 2‑vs‑4‑worker comparison **and** the 4‑vs‑4 control. Raw anchor
precision is therefore not a useful lever to optimise.

**Downscaling before upload was measured and rejected.** The stage uploads full‑res (~1.5 MB) pages, so
shrinking them looks like an easy win — and the normalized coordinates make it *mathematically*
answer‑neutral. It is not worth it: input tokens are **identical at full/1800/1400 px** (2855/2855/2855 —
the provider already normalizes to a fixed patch budget), so there are **zero token savings**; at 1000 px
tokens do drop 41% but accuracy visibly degrades — on a dense 8‑answer page the gap‑spacing CV collapses
0.071 → 0.013 (perfectly uniform spacing = the model **interpolating instead of locating**) and anchors
drift up to ~70 px. 1400 px matches full‑res and saves 57% upload bytes, a bandwidth win only. An initial
"1.77× faster" reading was **provider warm‑up variance**, not resolution — repeats erased it.

`[START_Q:]`/`[END_Q:]` are **text tags with no coordinates** and `page_mapping.json` is page‑level, so
the region is derived: a vision call returns one **y per answer** (positional `{"ys":[…]}` — asking it
to echo question ids made it answer with page labels like `(ii)`), each anchor is **snapped to a
whitespace gap**, the band runs anchor→next‑anchor at full width, then is **ink‑trimmed** to its
first/last inked row. Every validation failure → **full page**, never a wrong crop.

**COORDINATE UNITS — the single most important detail.** Qwen‑VL grounding is **natively normalized to
0–1000** and returns that scale *even when asked for absolute pixels* (verified: identical ~0–900 values
on a 2730 px page either way). The reply MUST be scaled by `page_height/1000` (`COORD_SCALE`). Reading it
as raw pixels collapses every anchor into the top ~17% of the page — the printed header — which was the
true cause of the mis‑cropping, **not** any weakness in the model's localisation. Fixing this alone took
the measured tight‑crop rate from 53% → **90%**, and made the dense Section‑A objective pages (the worst
previous failure) crop perfectly one answer per band.

Two findings the profiler depends on: ruled lines/margins put ink in *every* row, so long horizontal
**and** vertical runs are morphologically removed first (otherwise no gaps are found and both the snap
and the trim silently no‑op); and gap‑finding uses an adaptive (percentile) bar while ink‑trim uses a
low fixed one — one shared threshold either finds no gaps or trims faint handwriting away.
`ANSWER_CROP_MAX_STARTS_PER_PAGE` (skip localisation on very dense pages) is a **dormant safety valve**,
default 99: it existed to dodge the unit bug and is no longer needed.

**Accuracy pass (retry / salvage / cross‑page).** Measured before it: one sheet scored **61% tight**
where the same code gave 92% on another. The cause was *variance*, not content — two pages that each
lost 6 answers passed **4/4** on re‑sampling, while a genuinely ambiguous page failed **0/4**. A single
flaky sample was discarding a whole page.

- **Retry** (`ANSWER_CROP_MAX_RETRIES`, default 2) re‑samples only on validation failure. Attempt 1 is
  `temperature=0`; retries use `ANSWER_CROP_RETRY_TEMPERATURE` (0.3) because a fixed prompt at
  temperature 0 is not reliably resampled. Every attempt's tokens are counted.
- **`_salvage_ys`** returns per‑answer anchors with `None` for unusable ones, so one bad value no longer
  costs the page — a real reply had five perfect values and one degenerate one (`8` followed by ~200
  zeros). Equal neighbours are dropped as an ambiguous **pair**: splitting the difference would invent a
  boundary the model never saw. A **confidence gate** then discards the reply entirely unless ≥⅔ of
  anchors survive — salvage is for one bad apple, not for a reply the model muddled (with 2 answers and
  `[215, 45]`, keeping either is a coin flip).
- **Cross‑page**: `_build_bands` now takes an ordered `continuation_bands` list. The old
  `cont_qid = conts[0]` silently dropped every *second* answer continuing onto a page — 1, 3 and 7 such
  pages on three real sheets, and the sole cause of every residual `not placed`. A **second vision call**
  (`_CONT_PROMPT_TMPL`, asking where each continuation *ends*) fires when 2+ answers continue **or** the
  page starts nothing new. **This retires the old "continuation‑only pages are free" optimisation** — it
  was real, but it is exactly why cross‑page crops were imprecise. A single continuation sharing a page
  with starts still needs no call (already bounded by the first start anchor).
- Continuations no longer die with the starts verdict; the reason string `y outside 0-1000` was renamed
  because it read like a units fault (a different bug, already fixed) when the cause is token degeneracy.
- **`MIN_PEAK_INK_RATIO`** (0.10) requires a band to hold one row as dense as 10% of *that page's* own
  densest row. Row count alone let two crops through as 38px and 22px slivers of blank ruled paper — a
  curved rule survives the morphological strip and inks nearly every row. **Page‑relative, not a
  fraction of page width**: a width‑based first attempt collapsed a sheet from 95% → 54%, because its
  pages are landscape (3509×2480) so real handwriting spans less of the width. Measured separation is
  two orders of magnitude: rule‑only slivers 0.006–0.023, real answers 0.60–1.00.

Measured on three real sheets (crop stage re‑run alone against existing `preprocessed/`):

| sheet | before | after |
|---|---|---|
| `organized_1` | 90.2% (61% on the run the teacher saw) | **95.1%** |
| `Class_X_Ujjawal` | 92.3% | **97.4%** |
| `sheet_3_AKSHIT_SHARMA` | 89.8% | **93.2%** |

`not placed` is now **zero** on all three, and three consecutive runs of `organized_1` all returned
95.1% — the sheet‑to‑sheet luck is gone. Remaining fallbacks are honest ones: anchors the model could
not separate.

Output `answer_crops.json` + `answer_crops/*.jpg` → `res["Answer Screenshots"]` (a **top‑level field of
filenames**, not a segment: a non‑text segment would flip every short answer out of the compact
side‑by‑side layout, and base64 would add ~4–5 MB to `review_state.json`). Served by
`GET /answer-crop/<run_id>/<file>`; rendered as a full‑width card after `answerPairHTML`. The PDF is
fpdf2‑generated from known fields, so it is online‑only with no flag.

### Stage 5.x — Key merges & marks reconciliation (before grading)
Run in order on `db_answers.json`:
- **5.4 `merge_choice_groups`** — collapse "answer any ONE" (`required==1`) alternatives into
  one `parent` key tagged `is_choice`; marks = `sum(common additive parts) + max(alternative
  sums)`; prefix‑tolerant member resolution (`Q34(a)` claims `Q34(a)(i)` — the "99.5 bug"
  fix); display‑only `choice_alternatives` so the report shows only the attempted alternative.
- **5.45 `merge_additive_subparts`** — collapse an additively split multi‑part (`Q37(a)+(b)+(c)`)
  into one parent summing the marks (the case‑study BLANK bug).
- **5.5 `merge_subparts_into_parents`** — collapse OCR sub‑parts into the parent when the key
  has only the parent (prevents each sub‑part inheriting full parent marks).
- **5.6 `_finalize_choice_flags`** — stamp `inline_or` / `is_choice_uncertain`.
- **5.7 `reconcile_marks_with_question_paper`** (`:1203`, gated `RECONCILE_KEY_MARKS_WITH_QP`,
  default on) — the deterministic safety net against key **parse** errors. Per base question,
  compares key vs paper marks and handles all four disagreements: **shortfall** (raise a
  single entry to the paper, else flag), **inflation** (flag, or lower in `align_to_paper`),
  **dropped** (inject a placeholder at paper marks + `key_parse_missing` for manual grading),
  **unknown** (flag). Writes `key_integrity.json` `{checked, adjusted, flagged, injected,
  qp_total, key_total}`. In `raise` mode it can lift a wrong total to the truth but never
  lower a correct one.

### Stage 6 — Grading & report (`skills/answer-evaluator-and-report-generation/scripts/evaluate.py`, 2260 ln)
Invoked `evaluate.py <student_name> <ocr_answers.json> <db_answers.json>
[diagram_evals.json]`; report dir, student details, diagram sentinel etc. via env. Also has
`--regenerate` (rebuild PDF from a working copy, no grading) and `--regrade-one` (single
question) modes.

**Per‑question grading** (`evaluate_all` `:639`, ids ordered `natural_sort_key`):
- **Blank/NA short‑circuit** — `[BLANK]`/empty → 0, Needs‑Review "No answer captured"; explicit
  NA → 0, no review. No API call.
- **MCQ / objective deterministic path** — `_mcq_confident_verdict` (`:364`) using
  `parse_option`/`mcq_match` (id match, or fuzzy label ≥0.85 with conflicting‑letter guard).
  Awards full/0 with **no LLM call** when exactly one clean option matches (injection‑proof).
  Ambiguous (0 or ≥2 option letters, or the key format isn't a clean single letter) → defers
  to the LLM.
- **LLM prose path** — `evaluate_single` (`:407`, async, `Semaphore(EVAL_MAX_CONCURRENCY=15)`):
  picks a rubric by **type + subject + marks** (`rubric_kind` → `get_rubric`, see *Grading
  calibration* below); injects the OR/inline‑OR/choice directive and the
  `EVAL_POINTWISE` / `EVAL_FLAG_MISPLACED` directives; wraps the student answer in
  `<untrusted_student_response>` tags with explicit **prompt‑injection** detection
  instructions; requests a strict JSON verdict (`Marks Awarded, Maximum Marks, Student Wrote,
  Correct Answer, Justification, Feedback, Confidence, Needs Review, Off‑Topic, Prompt
  Injection Detected, Injection Warning, Bad Handwriting Flag`). Retries with escalating
  temperature and robust JSON salvage; **the answer key defines the max** (LLM value echoed
  over, marks capped to `[0, db_max]`, over‑award forces review).

**MARK GRANULARITY — every mark is a multiple of 0.5** (`scripts/marks_policy.py`,
`quantize_mark` / `is_valid_mark` / `MARK_STEP`). Nearest half, exact ties (x.25/x.75) **UP**
in the student's favour — deliberately *not* `round()`, which is half‑to‑even and would push
marks DOWN on exactly the values a per‑point split produces. Returns an `int` when whole, so a
2‑mark MCQ still renders `2 / 2`, not `2.0 / 2`. Enforced in **two layers**:
- *Prompt* — an unconditional `MARK GRANULARITY` block in `evaluate_single` spells out the full
  ladder of legal values for any maximum ≤10 (`0, 0.5, 1, … , 3`), so the model emits a legal
  mark natively and its justification matches the number it reports. `EVAL_POINTWISE` now closes
  with a rounding step; it was the mechanism that made fractions reachable, since it splits a
  scheme into value‑points "each worth a share of the marks" (4 marks / 5 points = 0.8).
  `evaluate_diagrams.eval_single` carries the same rule and quantizes its own result.
- *Code backstop* — `quantize_mark` at **every** write site: the LLM clamp (`evaluate.py:636`),
  the deterministic MCQ award (`:896`), the diagram merge (`:2702`), both teacher‑override paths
  and the Accept baseline (`review_corrections.apply_corrections` / `apply_decisions`), and the
  marks editor (`upload_validation.apply_marks_corrections` — the teacher is *typing*, so here it
  snaps rather than warns). `snapHalfInput` in `index.html` mirrors the rule on the four mark
  inputs, bound to `change` (not `input`, which would fight the user mid‑keystroke) and
  re‑dispatching `input` so the page model picks up the corrected value — `step="0.5"` alone only
  constrains the spinner arrows, a typed/pasted `0.8` posts happily.
- Side effect: a **NaN** mark can no longer reach a report. `"NaN"` parses as a float and
  `float('nan')` defeats both sides of a `min`/`max` clamp, so it used to pass straight through.
- Verified: 14/14 mutations caught; replaying accept‑all over 18 archived run files / 682
  questions moved **0** marks — the rule constrains new values without disturbing graded work.

**GRADING CALIBRATION — `EVAL_GRADING_CALIBRATION=v2|legacy`** (default **v2**; predicate in
`scripts/grading_calibration.py`, imported by *both* `evaluate.py` and `evaluate_diagrams.py` so a
run is never half‑calibrated). Fixes five defects that together made the grader award 0 where
partial credit was due — measured over the archived corpus: **514 attempted answers → 18.7% zero**,
15.6% partial, 65.8% full (`Maths_OSD` 44.8% zero).

1. **Rubric routing was dead code for two files.** `get_rubric` matched the words
   `code`/`programming`/`math`/`equation`/`calculation` against the question **type**, but the key
   parser only ever emits `MCQ, Short Answer, Long Answer, Numerical, Fill in the Blank, True/False`
   — none of which contain any of those words. `code_rubric.md` and `equation_rubric.md` (70 KB of
   CBSE step‑marking, carry‑forward and syntax‑cap rules) therefore routed to **zero questions,
   ever**; all 342 Mathematics and 148 Computer Science answers got the *subjective* rubric.
   `rubric_kind(type, subject, marks)` now routes on **subject** (the type vocabulary is generic),
   objective wins outright, and a binary‑form type (`True/False`, `Fill in the Blank`) is only
   graded binary at ≤1 mark — a 2‑mark fill‑in has two blanks and must be able to score one.
   Corpus effect: `subjective 308 → 53`, `equation 0 → 168`, `code 0 → 87`.
2. **`Rubric: {rubric[:2000]}` head‑truncated every rubric** (files are 31–89 KB), discarding
   94–98%. Every partial‑credit mechanism sits *below* the cut; what survived was the preamble —
   "must behave as a **strict** … evaluator" and "reduce or **eliminate credit**" for a
   directionally‑correct answer, severed mid‑word at the leniency rule. Each rubric now carries a
   `<!-- GRADER-DIRECTIVES:BEGIN/END -->` block of distilled operative rules and **that** is what
   the model reads. Truncation lives in `get_rubric` only (the maths block exceeds 2000 chars — a
   slice at the prompt would silently re‑break it); `legacy` **strips** the block before
   head‑truncating, or the revert flag would revert nothing.
3. **The subjective rubric contradicted itself** — Golden Rule 4 "Never deduct" vs Rule 9 "must
   incur a ½ mark deduction"; §3.2 zeroed a "present but lacks depth" value point that §4.2
   half‑credited. Corrected permanently (not gated): the old text was two policies at once, so
   reverting it would restore an ambiguity rather than a behaviour.
4. **A diagram verdict overwrote the text mark** (`:2702`). Measured: `Maths_Class12 Q24` — 742
   chars deriving |d₁|=6, |d₂|=2√2, verbatim the answer key, recorded **0/2** because the sketch
   lacked axis labels, and the diagram grader was reading a crop **from Q23**. Now **best of the
   two**, both quantized before comparison, with `Marks Source` recording the winner; the loser's
   justification is kept separately so a report never explains a mark nobody received.
   `evaluate_diagrams.eval_single` also gained per‑feature partial credit (its prompt previously
   had no rubric at all).
5. **Off‑topic hard‑zeroed on question‑text mismatch alone.** The question text is OCR'd; when it
   garbles, every correct answer on the page looks off‑topic. False zeros: `Q26` (the key *is* the
   student's integration by parts), `Q23` (PQ vs QP — same vector, opposite convention), `Q28`.
   The directive now makes the **answer key** the deciding signal.

Also: `EVAL_POINTWISE` defaults **ON** under v2 (it was off in code and only enabled via `.env`, so
any run without the overlay graded with no partial‑credit directive), and `EVAL_CASCADE_MIN_MARKS`
defaults **1** (a 1‑mark zero was previously final on the fast instruct model with reasoning off).

*Measured* (replay, byte‑identical inputs, v2 vs legacy through the same entry point — the archived
mark is **not** the control, it may have come from the fast cascade model): substantive zeros
**12% → 32%** of max (12 up / 7 same / 2 down); genuine segmentation zeros **8/8 held at 0**;
partials 71% → 75%. The routing fix alone accounted for 20% → 32%. **27/27 mutations caught.**
Still open: run‑to‑run variance (the same question moved in opposite directions on different
sheets) and an accuracy check against teacher‑marked sheets — leniency is not accuracy.

**EVAL_CASCADE** (env `EVAL_CASCADE`; **ON in the deployed `.env`**, code default OFF):
`grade_cascade` grades first on the fast instruct model (`EVAL_CASCADE_FAST_MODEL`), and
`_cascade_escalation_reason` re‑grades on the thinking model only when the answer is
*in‑the‑balance* — low/blank confidence, off‑topic/injection flag, non‑finite mark, or a
substantive answer scored 0 on a ≥`EVAL_CASCADE_MIN_MARKS` question.
It **deliberately does not escalate** on Needs‑Review/Bad‑Handwriting (OCR legibility flags a
better model can't fix). MCQ that reaches the LLM skips the fast pass (instruct‑model
ambiguous‑MCQ regression). `Graded By = fast|thinking` is recorded, plus
`Escalated Because = <code>` and the overridden `Fast Marks` on every escalated result — so the
trigger mix and tier agreement are readable off real runs instead of needing a bespoke A/B.

**PARTIAL CREDIT NO LONGER ESCALATES** (`EVAL_CASCADE_ESCALATE_PARTIAL`, default **0**;
`1` or `EVAL_GRADING_CALIBRATION=legacy` restores it). The old trigger fired on any
`0 < mark < maximum`, which after the grading calibration meant **~40% of answers** — partial credit
became the common outcome, so the cascade's cheap path stopped being the common path (68% of
LLM‑graded answers escalated on one sheet).

*Measured against a teacher's own per‑question marks* — Computer Science, 22 LLM‑graded questions,
teacher subtotal 48.5, run through the real `grade_cascade`:

| policy | total | MAE | escalations | output tokens |
|---|---|---|---|---|
| **narrowed (default)** | **47.0** | **0.43** | **2** | **19,038** |
| old (escalate on all partial) | 46.0 | 0.48 | 11 (9 purely partial) | 58,591 |

Escalating moved marks **away** from the teacher, so this was never a speed‑versus‑accuracy trade:
the expensive path was also the less accurate one, and the thinking tier is 8.1× slower and 17.5×
dearer *per call*. Both marks that differed between policies (Q31, Q37) landed closer to the teacher
under the narrowed rule. Repeat passes scored 21/22 identically per tier (0.02 mean drift), so a fast
mark is stable rather than lucky — re‑grading it was buying variance reduction that wasn't needed.

Two implementation notes:
- **Attribution ordering.** The always‑on triggers are tested BEFORE partial credit, so
  `Escalated Because = "partial_credit"` means it was the SOLE reason — i.e. the trigger's true
  marginal cost. The original ordering credited it with every also‑low‑confidence answer, overstating
  it.
- **Evidence limit.** One sheet, one marker. Hence the revert flag and the per‑result instrumentation;
  the token saving (−68%) is the reliable signal, while wall‑clock in a 22‑answer test is noise
  because the provider isn't saturated — at full‑sheet scale the ~370 tok/s ceiling makes the token
  cut the binding one.

**Post‑grade passes:**
- `apply_internal_choices` — "answer any N of M" best‑of‑N: parse `_instructions_`, keep the
  top‑N sub‑parts, set dropped ones' `Marks Awarded=0 AND Maximum Marks=0` (effective marks,
  so the denominator isn't inflated); a sub‑part‑count sanity mismatch flags rather than zeros.
- Flag routers (all flag‑only, never change marks): mixed‑answer flags, orientation flags,
  boundary‑weld continuation, OCR ambiguous‑glyph markers, `is_choice_uncertain`, key‑integrity
  notes → Needs‑Review with an explanatory note.

**`is_bad_handwriting` MEANS LEGIBILITY AND NOTHING ELSE.** Its one legitimate source is the OCR
model's `[BAD_HANDWRITING]` marker (`run_ocr._clean_chunk`). Three other places used to set it purely
as a "route this to review" lever, which reported *illegible handwriting* about answers that were
perfectly legible — **66% of math‑bearing answers (86 of 131)**, 27 of 38 on one maths sheet.
- The root cause was in `_reconcile_math` (`run_ocr.py`): its agreement gate compared **this answer's**
  math lines against `_ocr_math_only()`, which transcribes **the whole page**, and demanded string
  **equality**. On a page holding two answers that can never pass — measured, *every* archived math
  flag sat on a shared page — so every math answer fell through to two *give‑up* branches
  ("multi‑page" 15, "arbiter line count differs" 23) that flagged without evidence. Only 1 of 38 came
  from the real per‑line check.
- **Containment, not equality**, is the correct relation against a page‑level superset. It also yields
  the exact unmatched‑line set, so a multi‑page answer is now **arbitrated per page** instead of
  abandoned, and a line‑count mismatch **applies nothing and says nothing**. `_reconcile_code` gets the
  same treatment (its old Layer‑2 compared a block's connector count with the whole page's — the same
  superset mistake).
- **Content guard** (new): `_WORD_RE` only matches runs of ≥2 letters, so `x = 5` has an *empty* word
  multiset — and so does `""`. An empty or truncated arbiter line therefore satisfied the word/tag
  invariant and would **wipe the student's working**. The code path had guarded this since it was
  written (`_accept_code_arbiter` rejects an empty reply); the math path never did.
- Genuine, unresolved disagreements travel on **`symbol_flags.json`** (`{base: note}`, written by
  `run_ocr.write_symbol_flags`) → `evaluate._apply_symbol_flags` → `OCR Symbol Warning` → the
  `ocr_symbol_uncertain` / `question_number_misread` review flags. A **sidecar** for the same reason
  `recovery_flags.json` is one: full_evaluator rebuilds OCR entries in 13 places and would drop a key
  set on the entry. `full_evaluator.reconcile_ocr_to_question_set` writes here too — it still forces
  review (a misread question *number* is real evidence) under an accurate name.
- Measured on real pages, replaying old vs new logic over byte‑identical captured second‑pass text:
  **Maths_Class12 22 → 0** reconciler flags, **Class_X_Ujjawal 13 → 0**. The 2 surviving notes on
  Maths_Class12 were checked against the page images and are true OCR errors — Q23's `|QP⃗|` really
  was transcribed `|PQ⃗|`. 21/21 mutations caught.
- A **stale duplicate `full_evaluator.py` used to sit at the repo root** (241 lines behind, a strict
  subset — 0 top‑level names of its own). Deleted 2026‑07‑23. It was live‑by‑luck: the repo root *is*
  on `sys.path` under pytest, so a bare `import full_evaluator` resolved to the STALE copy, and every
  caller only got the right file because it inserted `scripts/` at position 0 first. One missing
  `sys.path.insert` would have silently loaded 241‑line‑old logic. `import full_evaluator` now
  resolves only via `scripts/`.

**2‑D CHEMICAL STRUCTURES** (`evaluate.py`, display‑only). A hand‑drawn structural formula is ASCII
art whose meaning lives in the **column alignment**, so it renders as one monospace block —
`{"type": "structure"}`, `.report-structure` on the web, Courier in the PDF.
- *Why it broke:* the indented atom/bond rows match `_INDENT_CODE_RE` (`^ {4,}\S`) so each PAIR was
  boxed as **code**, while the backbone (`H - C - C - C = C - H`) starts at column 0, matches no code
  pattern and rendered as proportional **prose** — one structure split into three differently‑styled
  pieces with the bonds no longer over their atoms. `_split_structure_blocks` therefore runs **before**
  `_autotag_split`, which also keeps structures away from `humanize_math`/KaTeX.
- *Detection:* a run of ≥2 lines built only from **real element symbols** (an actual element list, not
  `[A-Z][a-z]?` — that is what excludes an MCQ row `A. B. C. D.`), bond glyphs, digits and brackets,
  containing at least one **bond‑only row** (`_STRUCT_BOND_ROW_RE`: nothing but `| / \` and spaces).
  That last requirement is load‑bearing: a first cut keyed on merely *containing* a bond claimed **8
  blocks of ordinary maths working** across the 544 archived answers (`S₃₀ = 30/2 [2(1000) + 29(100)]`
  — `S` is an element and `/` is division). Now exactly the 4 genuine structures match, 0 false.
- *Alignment:* `_align_structure_block` snaps satellite rows onto the backbone's atom columns. OCR
  transcribes row‑by‑row and drifts — on the real Q34 every satellite row sat **+2 columns** right, so
  each bond pointed at a bond rather than at its carbon. A row is shifted by ONE offset, only when
  **every** glyph lands on an atom column, offset 0 wins ties, and rings / ambiguous backbones are
  left exactly as written.
- *PDF:* `_to_latin1_monospace` (one glyph in, one glyph out). The normal `_to_latin1` widens 56
  characters (`≡`→`==`, `→`→`->`), which in a monospace block shifts every column to its right and
  would re‑break the alignment the feature exists to protect.

**REVIEW FLAGS — why a question was flagged** (`scripts/review_flags.py`). `derive_flags(res)` →
ordered, de‑duplicated `[{code, label, detail, severity, rank}]`; `summarise_flags(evaluations)` →
the same grouped by code for the report's top panel; `attach_flags()` stamps `Review Flags` onto a
`[[qid, res], …]` list. The reasons were always in the data and simply never rendered — `Capture
Status` is set on 73 archived questions and appeared **zero** times in `index.html`.
- *Two application sites, no duplicated logic.* `evaluate.py` stamps `Review Flags` after every pass
  that can raise a flag and before `generate_pdf_report`, so `review_state.json`, the returned
  `report_data["evaluations"]` and the PDF all agree. `review_corrections.load_working_state`
  **backfills on read** when the field is absent — the same read‑time pattern as
  `_backfill_question_fields` — which is what lets all 18 archived runs show reasons with **no
  re‑grade**. Derived, never written back to disk.
- *Notes are classified by phrase, never by "it was in brackets."* Key‑integrity and OCR‑ambiguity
  reasons were only ever appended into `Justification` prose as `[…]`. New runs also record them in
  `Review Notes`; archived runs are recovered by scanning for **distinctive machine‑generated
  phrases**. A bare `\[…\]` regex is not safe: measured on the corpus it also matches student working
  (`[10, 20, 10, 30]`, `[(1-tan2x)/(1+tan2x)]`), which would present a student's own answer as a
  system warning. De‑dup is by `code`, fields before notes — all 41 archived mixed‑answer cases carry
  **both** the field and the note.
- *`rank` travels on the flag* (severity × 1000 + code order) so `index.html` can group and order
  without re‑deciding policy; a node‑vs‑Python parity test pins the two groupings together.
- `low_confidence` is emitted only when nothing else explains the flag (measured: it co‑occurs with a
  real reason 61 times in 62). A last‑resort `needs_review` flag guarantees no badge is ever wordless.
- Two sites that used to flag and record nothing now write one: `Choice Uncertain` and
  `Incomplete Grader Reply`.
- Verified: 14/14 mutations caught; across the archived corpus **233** flagged questions all yield a
  non‑empty reason (93 illegible, 61 no‑answer‑captured, 41 mixed, 38 not‑in‑paper, 17 key‑integrity,
  12 misplaced, …), and the PDF's grouped order matches the web report's exactly.
- **Diagram merge** — busy‑waits on `DIAGRAM_EVALS_SENTINEL` (up to `DIAGRAM_WAIT_TIMEOUT`=300s;
  timeout degrades to "grade without diagrams"), remaps sub‑part diagram evals onto merged
  parents, caps to key max, preserves the student's written answer text, sets the expected‑
  diagram description, maps `confidence_score` → Low/Medium/High.

**Report rendering:**
- **Question‑text logic** — `_question_for` returns the real question text or `""` when it
  equals the answer (objective keys), so the report falls back to the question number rather
  than echoing the answer. Backfilled per question after grading.
- **Code boxes** — `_is_code_line` / `_autotag_split` / `_is_code_output_question` detect
  code and render it verbatim in a monospace box; **snake_case protection**
  (`_protect_ident_underscores`) shields `push_element` from the math subscript pass.
- **Math** — `humanize_math` converts LaTeX‑ish OCR markup to Unicode for the PDF;
  `latexify_for_web` wraps bare LaTeX in `\(…\)` for client‑side KaTeX, with a humanised
  Unicode fallback string carried alongside.
- **`format_answer`** produces a segment map `[{type:text|code|diagram, content, web?}]` used
  by both the PDF and the web view; diagram crop images optionally embedded
  (`PDF_INCLUDE_DIAGRAM_IMAGES`, default off in PDF).
- **PDF** via `fpdf2` with an embedded Unicode TTF (Greek/math/arrows), color‑coded
  per‑question cards, security/illegible/teacher‑review blocks → `{Name}_{RollNo}.pdf`.

**Outputs:** JSON report to **stdout** (`{status, report_path, tokens_used, api_cost_usd,
evaluations:[[qid,res]], student_details}`), captured by the orchestrator which persists the
**pristine** `review_state.json`, reads the cost ledger, and writes `stage_timings.json`.

---

## 6. Batch flow (multiple students in one PDF)

1. `POST /separate` saves one combined PDF under `output/batch_<uuid8>/`, spawns
   `_run_separation` → `separate_sheets.py`.
2. **`separate_sheets.py`** classifies each page by sending the **top 38%** of the page
   (`SEPARATOR_CROP_FRACTION`, at `SEPARATOR_DPI`=150) to the vision model, asking
   "is this a student‑details section (printed/handwritten header OR OMR block)?" — NOT QR
   codes, NOT blank separators. `build_sheets` turns per‑page `is_sheet_start` verdicts into
   contiguous 1‑indexed page ranges (page 0 force‑started), each `{id, name, subject,
   start_page, end_page, page_count, is_omr, needs_review, confidence}`. Writes
   `separation/manifest.json` + `separation/thumbs/page_N.png`. Runs **before** ingestion.
3. Teacher reviews/edits boundaries, names, merges/splits in the UI; autosaved via
   `POST /batch/<id>/manifest` (`_normalize_manifest` clamps ranges, re‑ids `sheet_1..N`).
   `POST /batch/<id>/rescan` re‑runs separation.
4. `POST /batch/<id>/approve` (same 4 gates as single) → `_run_batch_eval` (skip orientation)
   or `_run_batch_prepare_orientation` (orient gate) in a background thread.
5. **`batch_evaluate`** / **`batch_resume_orientation`** (`batch_evaluator.py`): for each sheet
   `slice_pdf` the page range to `separated/sheet_N_NAME.pdf`, then run the full per‑sheet
   pipeline via `full_evaluate(...)` / `resume_after_orientation(...)` with the **shared**
   answer key / paper / report dir / marks source. Each student gets its own top‑level
   `output/sheet_N_NAME/` run dir (siblings of, not nested under, the batch dir).
   `run_id = sheet_N_NAME` (the slice filename stem). Progress streams to `status.json` via a
   `status_cb`.
   - **Sheet‑level concurrency** (`BATCH_SHEET_CONCURRENCY`, default **1** = the original serial
     **in‑process** loop, byte‑identical). `>1` grades that many sheets in parallel, **each in its
     own `python` subprocess** (`--run-sheet` entrypoint → `_run_sheet_entry`), so every sheet
     owns a private `os.environ` / cost ledger / stage‑timings — required because
     `_evaluate_from_preprocessed` seeds per‑run config into the **global** `os.environ` for its
     in‑process glue‑matcher (`full_evaluator.py:1623`), which is only safe one‑run‑per‑process.
     Bounded `ThreadPoolExecutor(BATCH_SHEET_CONCURRENCY)` supervises the subprocesses; results are
     collected **in input order**, cost summed in input order (float‑identical to serial), progress
     reported on completion with a monotonic `done`. Per‑sheet outer timeout `BATCH_SHEET_TIMEOUT`
     (default 2400 s) so one hung sheet can't stall the batch.
   - **Why not faster‑than‑serial‑×N:** grading is throughput‑bound and a single sheet already
     saturates the grading endpoint (~370 tok/s), so concurrency is **latency‑hiding**, not extra
     grading throughput — it overlaps one sheet's grading with another's OCR/ingest (separate
     endpoints/CPU). To hold aggregate provider load ≈ one sheet's worth (no new 429s), `_scaled_caps`
     **splits** `EVAL_MAX_CONCURRENCY` / `OCR_MAX_WORKERS` across the in‑flight sheets and passes them
     as `env_overrides=` (applied **after** the `.env` overlay, so they win) to each subprocess.
   - **`ANSWER_CROP_MAX_WORKERS` is split on a higher floor (4, vs 4/6 for eval/OCR after division).**
     Crops run on the **instruct** model, not the thinking model that bounds grading throughput, so
     throttling them buys nothing on the real bottleneck; the split only matters against a *staggered*
     sheet's OCR, which does share the instruct pool. The old floor of 2 turned a 20‑page sheet's 14
     calls into 7 waves — measured **16.59 s at 2 workers vs 8.97 s at 4** (1.85×, identical 36/39 tight
     coverage) — eroding the margin that keeps cropping inside the grading window. At `n=3` the live
     caps are `EVAL=8, OCR=6, CROP=4`.
   - **A floor must never become a boost.** `_split_cap` clamps to `min(configured, max(floor, cap//n))`:
     a bare `max(floor, cap//n)` would hand a *concurrent* batch more workers per sheet than a serial
     run whenever an operator has deliberately configured a cap below the floor (e.g.
     `ANSWER_CROP_MAX_WORKERS=2`) — the exact inverse of this function's job. No effect at shipped
     defaults, where every cap exceeds its floor.
6. Results (each with `review_id=run_id`) land in `status.json` `{phase:done, results}`.

**Batch phase state machine** (in `status.json`): `separating → review → [approve] →
orienting → orient_review → [confirm] → evaluating → done` (or `separating → review →
evaluating → done` when orientation is skipped); `error` at any point.

The frontend polls `GET /batch/<id>/status`, renders a **student card grid** (score, %,
badges: injection ×N / N to review / Reviewed X/Y). Clicking a card opens
`GET /student-report/<review_id>` (the authoritative working copy).

---

## 7. Teacher review & correction

Two files per run encode the review model (`review_corrections.py`):
- **`review_state.json`** — write‑once **pristine** AI snapshot (`full_evaluator.py:2028`),
  never mutated. The Accept baseline and the truthful "original marks".
- **`review_render.json`** — the **one working copy**, created **lazily on the first teacher
  action** (`ensure_working_copy`), stamping each eval with `Machine Marks`. An untouched run
  has no working copy and is byte‑identical to grade time. `load_working_state` prefers the
  working copy (with question‑field backfill), else pristine.

**Decisions** (`apply_decisions`, operates on a deepcopy):
- **reject / change‑marks** → clamp corrected marks to `[0,max]`, stamp `Teacher Corrected`,
  original + corrected marks, remark, `Needs Review=No`, append a Postgres row.
- **accept** → revert to the `Machine Marks` baseline, clear override fields.
- Un‑decided questions are left exactly as they are (prior overrides/regrades survive).
- Idempotent and un‑reject‑safe (re‑accepting a rejected question reverts to the machine mark
  and drops its DB row).

**Orchestration** (`app.py`):
- `POST /submit-corrections` — builds `pristine_by_qid`, `ensure_working_copy` +
  `load_working_state` + `apply_decisions`, writes `review_render.json`, **regenerates the PDF
  in place** (`evaluate.py --regenerate`), persists rejected answers to Postgres
  (`store_rejected_answers`, DELETE‑then‑INSERT per `review_id`), returns
  `compute_review_progress`.
- `POST /re-evaluate-question` — edits ONE question's OCR text, stages it to
  `regrade_input.txt`, runs `evaluate.py --regrade-one` in a **background thread** under a
  **per‑review lock** (so two regrades on one sheet can't clobber the working copy); browser
  polls `GET /re-evaluate-status/<job_id>`. `--regrade-one` has a **skip‑if‑unchanged** fast
  path (whitespace‑normalised compare → no LLM call), re‑grades only the target question,
  preserves every other mark, stamps `Machine Marks`/`Pre-edit AI Marks`, regenerates the PDF.

`compute_review_progress` counts a question reviewed if accepted, rejected, or re‑evaluated;
the batch grid's "Reviewed X/Y" badge uses the same predicate on both server and client so
counts always agree.

---

## 8. LLM infrastructure

**Single chokepoint — `llm_client.generate(model, …) → (text, in_tok, out_tok)`** (`:250`).
Model‑agnostic (the per‑stage model slug is passed in by each caller). Highlights:
- OpenAI‑compatible client cached per `(base_url, api_key)`; `LLM_BASE_URL` (default
  OpenRouter), `LLM_API_KEY`/`OPENROUTER_API_KEY`, `LLM_TIMEOUT`=180s, `LLM_MAX_RETRIES`=2.
- Images → base64 `data:image/png` parts. Text‑only calls collapse to a string.
- `reasoning_effort` → OpenRouter `extra_body.reasoning.effort` (only effective on `-thinking`
  slugs). `json_mode` → `response_format=json_object` (best‑effort; robustness is downstream).
- `strip_reasoning` removes `<think>…</think>` (incl. truncated).
- **Retry/backoff**: SDK exponential backoff + one manual retry that drops the knobs strict
  servers reject (`response_format`, `reasoning`) while keeping provider pin + usage accounting.
- **Provider routing** (`_provider_directive`): `LLM_PROVIDER_SORT/ORDER/ONLY/IGNORE/
  QUANTIZATIONS/ALLOW_FALLBACKS/REQUIRE_PARAMETERS` — performance knobs, off unless configured.
  Quantisation pinning keeps grading numbers byte‑identical.
- **Provider PRIVACY** (same directive, **ON BY DEFAULT**): `data_collection: "deny"`
  (`LLM_PROVIDER_DATA_COLLECTION`) routes only to providers that will not **train** on the prompt;
  `zdr: true` (`LLM_PROVIDER_ZDR`) only to endpoints that do not **store** it at rest. These are
  *different* guarantees — a provider can have either without the other — so both are always sent.
  Rationale: this pipeline ships children's exam answers to third‑party inference, and OpenRouter's
  own default is `data_collection: "allow"`, which permits retention **and** training.
  Three deliberate behaviours: privacy alone emits a directive (it previously required a
  *performance* knob to be set, so a privacy‑only config would have been dropped); an invalid value
  falls back to `"deny"` rather than omitting the field (omission returns the decision to
  OpenRouter's permissive default — a `denny` typo would have leaked silently, a bug caught by
  mutation testing); and a blank value means *unset*, not false. Both settings RESTRICT the endpoint
  pool, so `scripts/check_provider_privacy.py` probes every configured model under the active policy
  — run it after any `LLM_PROVIDER_*` change (verified 2026‑07‑29: all three models resolve, incl.
  the two‑provider `-thinking` grader).
- **Cost**: `LLM_USAGE_ACCOUNTING` on → request OpenRouter's real `usage.cost`; a per‑process
  accumulator (`get_real_cost`) prefers real cost, else `llm_pricing.estimate_cost`.
- `diagram_llm_opts()` gives tighter timeout/retries for fast diagram calls.

**Concurrency lives at call sites**, not in the client: OCR `ThreadPoolExecutor(12)`, grading
`Semaphore(15)` under `asyncio.to_thread`, parse `ThreadPoolExecutor(8)`, diagram/separator
`ThreadPoolExecutor(10/12)`.

**Cost ledger** — each stage subprocess appends one JSON‑line to `$API_COST_LOG`
(`api_costs.jsonl`, set per‑run by the orchestrator) via `llm_pricing.log_cost`
(`{stage, model, input_tokens, output_tokens, cost_usd, cost_source, cost_estimate_usd}`);
`cost_source="openrouter"` only when every record for the stage was provider‑billed. The
orchestrator totals it for the per‑paper cost.

**Pricing** (`llm_pricing.py`): USD per 1M tokens `(in, out)` for every Qwen3‑VL tier +
legacy Gemini rows; self‑hosted `qwen-local` = `(0,0)`; unknown model → most‑expensive
default so the meter never under‑reports.

**Per‑stage model selection** — env‑driven, resolved at each call site. As deployed
(`.env`), overriding the code defaults (which are the smaller `30b-a3b` tiers):

| Stage | Env var | As deployed |
|---|---|---|
| OCR | `OCR_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |
| Answer‑sheet separator | `SEPARATOR_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |
| Key + question‑paper parse | `KEY_PARSER_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |
| Grading | `EVAL_MODEL` | `qwen/qwen3-vl-235b-a22b-thinking` |
| Cascade fast pass | `EVAL_CASCADE_FAST_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |
| Diagram features | `DIAGRAM_FEATURES_MODEL` | `qwen/qwen3-vl-30b-a3b-instruct` |
| Diagram grading | `DIAGRAM_EVAL_MODEL` | `qwen/qwen3-vl-30b-a3b-thinking` |
| Glue matcher / segment repair | `GLUE_MATCHER_MODEL` / `SEGMENT_REPAIR_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |

The 397B model was tried and reverted (runaway hidden reasoning → JSON parse failures). The
throughput lever is provider **aggregate token/s** — the grading semaphore saturates it, so
only reducing output‑token *volume* (e.g. the cascade) shortens the grading wall clock.

---

## 9. Data model (on disk)

**Per‑run `output/<run_id>/`:**
```
images/<stem>_page_N.png              raw 300-DPI rasters
preprocessed/preprocessed_<...>.png   conditioned pages
ocr_output/
  ocr_answers.json                    {qid:{answer,is_bad_handwriting}} + _instructions_
  page_mapping.json                   qid <-> page image
  mixed_answer_flags.json             glue/collision base numbers
  recovery_flags.json                 {base: why this answer was RESCUED} -> Needs Review + note
  orphan_pages.json                   pages that produced no question number (text held, not lost)
  orientation_flags.json              per-page orientation outcomes
  question_ids.json                   derived authoritative question-number set (OCR anchor)
  student_meta.json                   Name / Roll No / Date
  {Name}_{Date}.docx / .pdf           OCR transcript exports
diagram_crops.json                    detected diagram page list
student_features.json                 diagram features (transient)
diagram_evals.json (+ .done)          diagram grades + completion sentinel
db_answers.json                       answer key aligned+merged+reconciled to this student
db_answers_diagram.json               isolated un-merged key snapshot for the diagram thread
key_integrity.json                    marks-vs-paper reconciliation report
review_state.json                     pristine AI snapshot (write-once)
review_render.json                    teacher working copy (lazy; absent if untouched)
student_details.json                  name/roll/class/subject for report naming
regrade_input.txt                     staged edited OCR text for --regrade-one
orientation_review.json               orientation gate manifest (index->file map)
progress.json                         live 3-step UI checklist
stage_timings.json                    per-stage + wall-clock profiling
api_costs.jsonl                       per-stage cost ledger
```

**Per‑batch `output/batch_<id>/`:** the uploaded PDF, `separation/manifest.json`,
`separation/thumbs/page_N.png`, `separated/sheet_N_NAME.pdf`, `status.json`.

**Session state `evaluation_app/uploads/`:** raw uploaded files (prefixed `question_paper_`/
`answer_key_`), `current_question_paper.json`, `current_answer_key.json`
(+`_parsed`, `_choices`, `_choices_parsed`), `marks_source_state.json`,
`report_path_state.json`.

**`.parse_cache/`:** `key_<sha256>.json` content‑hash cache of parsed keys.

**PostgreSQL** (optional; app runs without it):
- `DB_TABLE` — legacy question/answer bank read by `fetch_answers.py` (**superseded** by the
  manual key; the `db_answers.json` name is a vestige of this origin).
- `REJECTED_ANSWERS_TABLE` (default `rejected_answers`) — teacher corrections audit trail,
  written idempotently per `review_id` by `review_corrections.store_rejected_answers`.

Final reports are written to the confirmed folder (`~/Desktop/{Class}/{Subject}` by default)
and served via `GET /reports/<filename>`.

---

## 10. Web layer (`evaluation_app/app.py`)

31 routes + a `before_request` auth hook. Grouped:
- **Setup**: `/parse-question-paper`, `/parse-answer-key`, `/paste-question-paper`,
  `/paste-answer-key`, `/report-path`, `/confirm-report-path`, `/marks-source`,
  `/confirm-marks-source`, `/marks-breakdown`, `/confirm-marks-breakdown`,
  `/reset-marks-breakdown`.
- **Single eval**: `/evaluate` (synchronous legacy), `/prepare-orientation`,
  `/orient-status/<run_id>`, `/orient-preview/<run_id>/<page>`, `/confirm-orientation/<run_id>`.
- **Review**: `/submit-corrections`, `/re-evaluate-question`, `/re-evaluate-status/<job_id>`,
  `/student-report/<run_id>`.
- **Batch**: `/separate`, `/batch/<id>/status`, `/thumbs/<id>/<page>`, `/sheets/<id>/<sid>.pdf`,
  `/batch/<id>/manifest`, `/batch/<id>/rescan`, `/batch/<id>/approve`,
  `/batch/<id>/confirm-orientation`.
- **Static/help**: `/`, `/reports/<filename>`, `/upload-guidelines`.

**Background‑job models (two):** (a) in‑memory `_REGRADE_JOBS` dict + per‑review locks for
single‑question regrade; (b) filesystem `status.json`/`orient_status.json` polled by the UI
for separation/batch/orientation. Both assume the single‑process `threaded=True` /
gunicorn‑threads model (they don't survive horizontal scaling).

**Auth**: HTTP Basic, env‑gated — `@before_request` is a no‑op unless `APP_AUTH_PASSWORD` is
set; compares user (`APP_AUTH_USERNAME`, default `teacher`) + password with
`hmac.compare_digest`. `run-public.sh` sets it for public exposure and forces `FLASK_DEBUG=0`.
CORS is wide‑open; there is no global upload‑size cap or extension allowlist (only `/separate`
requires `.pdf`); path‑traversal is blocked via `send_from_directory` + id validators.

---

## 11. Frontend (`evaluation_app/templates/index.html`)

A single Bootstrap page: a 3‑step wizard (`goToStep`, `refreshStepLocks`) that is
progressively unlocked, then replaced by result cards. No routing library — view state is
manual `display` toggling plus a `window.__reports[slug]` instance registry.

- **Live loading checklist** — polls `progress.json`; `renderProgressSteps` shows each step
  done/active/pending with a progress bar.
- **Shared report renderer** — one `renderReport`/`_reportRender` drives BOTH the single
  report and each batch student's full report; element ids are namespaced by a per‑report
  slug so nothing collides. `--accent` CSS card system; adaptive Student‑vs‑Expected layout
  (side‑by‑side only when both are short); Question field shows the *question* (never the
  answer); click‑to‑enlarge lightbox for diagrams.
- **Teacher review UX** — Accept / Change‑marks are **staged** on `inst.reviewItems` and saved
  only on Submit (`inst.dirty` guards unload); Re‑evaluate saves immediately. A snapshot/
  restore in `_reportRender` preserves staged decisions across a single‑question re‑grade.
  Reviewed‑count reconciles with the batch grid via `syncBatchStudent`.
- **Orientation gate UI** — per‑page cards with Left/Flip/Right controls; the preview is the
  un‑rotated image rotated by CSS so it matches OCR input; batch version groups pages by
  student in `<details>` blocks.
- **Marks‑breakdown editor** — five guided card renderers (conflict / OR‑suggestion / residual
  / extra / full table) with a live total, serialised to `{marks, added, removed,
  choice_groups}` on confirm.
- **Math/code** — self‑hosted KaTeX typesets `\(…\)`/`\[…\]` with a `data-mathfallback`
  Unicode revert if KaTeX fails; code renders in monospace boxes. (snake_case protection is
  server‑side.)

---

## 12. Deployment

- **Docker** — `python:3.12-slim-bookworm`; system deps `tesseract-ocr(+osd)`, `libglib2.0-0`,
  `libgomp1`, `fonts-dejavu-core` (Poppler deliberately omitted — PyMuPDF bundles its engine).
  Non‑root uid 1000, `pip install --user` layer‑cached. `EXPOSE 7860`. Entrypoint execs
  **gunicorn** (`gthread`, `WEB_WORKERS`=2, `WEB_THREADS`=8, `WEB_TIMEOUT`=600, no
  `--max-requests`). If `/data` is writable it symlinks `output/`, `uploads/`, reports onto it
  for persistence. `.dockerignore` excludes `.env*` (secrets come from the platform).
- **`run-public.sh`** — serves the Mac publicly on port 5055 behind a tunnel:
  **Cloudflare** quick‑tunnel (default), **ngrok** (`NGROK_DOMAIN`), or **Tailscale Funnel**
  (`TS_HOSTNAME`, permanent URL — the chosen host). Forces `FLASK_DEBUG=0`, auto‑generates +
  persists a 20‑char Basic‑Auth password (`~/.answer_evaluator_public_pw`, umask 177),
  `caffeinate`s the Mac, health‑checks `/`, tears everything down on exit. Config from the
  gitignored `.env.public` (`TUNNEL`, `APP_AUTH_USERNAME`, `APP_AUTH_PASSWORD`, `TS_HOSTNAME`,
  `NGROK_DOMAIN`).
- Deploy guides: `docs/DEPLOY-TAILSCALE.md` (chosen), `docs/DEPLOY-HUGGINGFACE.md` (HF Docker
  Space, now PRO‑gated for free persistence).

### 12.1 Platform support (macOS / Linux / Windows)

Spawning a stage and killing its tree are the **only** OS‑specific parts of the pipeline. Both are
defined once at the top of `full_evaluator.py` so no caller branches; POSIX behaviour is unchanged.

| Concern | POSIX | Windows |
|---|---|---|
| Interpreter | `PYTHON_EXE = sys.executable` | same |
| Group isolation | `_new_group_kwargs()` → `start_new_session=True` | `creationflags=CREATE_NEW_PROCESS_GROUP` |
| Tree kill | `kill_process_tree()` → `os.killpg(getpgid(pid), SIGKILL)` | `taskkill /F /T /PID` |
| Pipe encoding | `encoding="utf-8"` + `PYTHONIOENCODING=utf-8` (locale already UTF‑8 → no‑op) | same, and load‑bearing |
| `gunicorn` | installed | skipped (`sys_platform != "win32"`); run `python evaluation_app/app.py` |

Why each mattered — all three failed *silently* or crashed rather than erroring cleanly:
- Stages were spawned as the literal `"python3"`, which does not exist on Windows. Where Win10/11 ship
  the App Execution Alias of that name it opens the Microsoft Store and exits, so a stage produced
  nothing while appearing to run — the reported "separation logic is not working".
- `os.killpg` / `os.getpgid` / `signal.SIGKILL` **do not exist on Windows**, so touching them raises
  `AttributeError` — which the old `except (ProcessLookupError, PermissionError, OSError)` guard did not
  list, making the `proc.kill()` fallback directly beneath it unreachable. Every watchdog timeout and
  every teacher cancel would have taken the run down. `start_new_session` is separately a no‑op there:
  unlike `preexec_fn`, Popen accepts and **ignores** it (`unused_start_new_session`), so nothing raises.
- Child stdout was decoded with the locale encoding (cp1252 on Windows), mangling — or hard‑failing on —
  the maths/chemistry glyphs the OCR and parsers emit. Both ends are now pinned to UTF‑8.

**Text‑file encoding invariant.** Every text‑mode `open()` names `encoding="utf-8"`, with exactly one
exemption: a `json.dump` **write** (`ensure_ascii` defaults to True, so the bytes on disk are ASCII
whatever the locale). The rule is deliberately **asymmetric** — a *read* cannot know how the file was
produced, and two of this pipeline's `.json` files (`student_features.json`, `diagram_evals.json`) are a
stage's **raw stdout written straight through**, so they hold real non‑ASCII. Exempting `json.load`
reads is precisely the hole that let two encoding bugs survive a mutation pass.

Two findings worth recording:
- **The rubric loader was a hard blocker.** All four files in
  `skills/answer-evaluator-and-report-generation/references/*_rubric.md` contain characters cp1252
  cannot represent (`→`, `≤`, `₂`, `Σ`, box‑drawing `┌─┐`), so on Windows the read raised
  `UnicodeDecodeError` part‑way through — and the loader's `except OSError` does **not** catch it
  (`UnicodeDecodeError` is a `ValueError`). Grading died on every answer. The except is left narrow on
  purpose: a rubric that fails to decode must surface, because silently falling back to "Standard
  Evaluation Guidelines Apply." is the worse outcome (see the grading‑calibration work, where rubrics
  silently not reaching the grader was the single biggest defect).
- **The utf‑8 pipe fix made one site worse before fixing it.** Once the parent decodes a stage's stdout
  correctly, `out` holds real arrows/subscripts; writing that to `student_features.json` without an
  encoding raises `UnicodeEncodeError` where it previously produced silent mojibake. Caught by the AST
  audit, not by inspection.

The audit runs as a test (`test_raw_text_file_io_names_its_encoding`), so new unencoded IO fails CI
rather than waiting for a Windows run.

### 12.2 Preflight (`scripts/check_platform.py`)

One command to answer "can this machine run the app, and if not, why". Stdlib‑only at import time and
every check isolated, so it runs on a *broken* install — a missing dependency is reported, never raised.

```
python scripts/check_platform.py            # full report (spawns real stages)
python scripts/check_platform.py --quick    # imports + config only, no subprocesses
```

Checks: interpreter version against wheel availability (flags ≥3.14, where the pinned
numpy/opencv/PyMuPDF have no wheels and pip falls back to a source build needing MSVC); every pinned
dependency, with the *impact* of each optional one rather than a blanket FAIL; rubric utf‑8
decodability; `.env` and `output/` writability; artifact path length vs Windows' 260‑char `MAX_PATH`;
then the OS‑specific mechanics — interpreter resolution, process‑group isolation, a utf‑8 round‑trip
through a real stage, a **grandchild‑inclusive** tree kill, the stage watchdog, and finally a live
**ingestion + preprocessing** run on a generated 2‑page PDF.

That last probe is deliberate: preprocessing is the only stage using `ProcessPoolExecutor`, which is
**spawn** (not fork) on Windows, and it is the stage a tester reported failing. On failure the probe
prints the stage's own stderr, which is the text to report. Exit code 0 unless something FAILED;
warnings do not fail the run.

`sys.executable` additionally pins stages to the orchestrator's **virtualenv**, where the old PATH
lookup could resolve elsewhere. Verified identical to PATH `python3` on the POSIX dev machine.
Covered by `tests/test_platform_portability.py` (18 tests; Windows branches exercised by patching
`IS_WINDOWS`, so they run on any host), plus a real‑subprocess check that a **grandchild** dies with the
tree — the guarantee the cancel design rests on.

---

## 13. Configuration surface (env vars)

Grouped; `.env.example` documents only a subset — the full surface is read via
`os.environ.get` across `scripts/` + `skills/`.

- **LLM client**: `LLM_BASE_URL`, `LLM_API_KEY`/`OPENROUTER_API_KEY`, `LLM_JSON_MODE`,
  `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `LLM_USAGE_ACCOUNTING`, `LLM_LOG_PROVIDER`,
  `DIAGRAM_LLM_TIMEOUT`, `DIAGRAM_LLM_MAX_RETRIES`, `API_COST_LOG`.
- **Provider routing**: `LLM_PROVIDER_{SORT,ORDER,ONLY,IGNORE,QUANTIZATIONS,ALLOW_FALLBACKS,
  REQUIRE_PARAMETERS}`; **privacy (default‑on)** `LLM_PROVIDER_{DATA_COLLECTION,ZDR}`.
- **Student PII**: `OCR_EXTRACT_STUDENT_PII` (default 1). `HEADER_PROMPT` is the only call whose
  purpose is extracting a child's Name/Roll No; `full_evaluator` appends `--no-header-pii` whenever
  `_resolve_student_name` shows the name is already known (the OCR'd one would be discarded), and
  `process_header` then issues **no request at all**. Caveat: page‑1 pixels still contain the
  header, so this stops the system *asking* for identity, not the image carrying it — header‑band
  redaction would, but can clip answers, so it is not done implicitly.
- **Per‑stage models**: `OCR_MODEL`, `SEPARATOR_MODEL`, `KEY_PARSER_MODEL`, `EVAL_MODEL`,
  `EVAL_CASCADE_FAST_MODEL`, `DIAGRAM_FEATURES_MODEL`, `DIAGRAM_EVAL_MODEL`,
  `DIAGRAM_CROP_MODEL`, `ORIENT_MODEL`/`ORIENTATION_MODEL`, `SEGMENT_REPAIR_MODEL`,
  `GLUE_MATCHER_MODEL`.
- **Grading**: `EVAL_REASONING_EFFORT`, `EVAL_MAX_TOKENS`, `EVAL_PARSE_RETRIES`, `EVAL_VOTES`,
  `EVAL_MAX_CONCURRENCY`, `EVAL_POINTWISE`, `EVAL_FLAG_MISPLACED`, `EVAL_CASCADE`,
  `EVAL_CASCADE_MIN_MARKS`, `EVAL_GRADING_CALIBRATION` (`v2` default | `legacy`; read through
  `scripts/grading_calibration.py`, never re‑parsed per file).
- **Diagram**: `DIAGRAM_EVAL_REASONING_EFFORT`, `DIAGRAM_EVAL_MAX_TOKENS`,
  `MAX_DIAGRAM_PAGES_PER_Q`, `PDF_INCLUDE_DIAGRAM_IMAGES`, `DIAGRAM_CROPS_JSON`,
  `DIAGRAM_EVALS_SENTINEL`, `DIAGRAM_WAIT_TIMEOUT`, `PARALLEL_EVAL`.
- **OCR**: `OCR_MEDIA_RESOLUTION`, `OCR_THINKING_BUDGET`, `OCR_MAX_WORKERS`,
  `OCR_PAIR_CONTEXT(_FRAC)`, `OCR_VERIFY_CODE`, `OCR_VERIFY_MATH`, `OCR_ARBITRATE`,
  `OCR_AUTO_ORIENT`, and the orientation family `OCR_ORIENT_VOTE(_MIN_OOS)`,
  `OCR_ORIENT_CONTENT_FALLBACK`, `OCR_ORIENT_AUTOFIX*`, `OCR_ORIENT_USE_DETECTOR`,
  `OCR_ORIENT_LOG`.
- **Preprocess**: `PREPROCESS_FORCE_LANDSCAPE`, `PREPROCESS_LONG_EDGE_TARGET`.
- **Separator**: `SEPARATOR_CROP_FRACTION`, `SEPARATOR_DPI`, `SEPARATOR_THUMB_DPI`,
  `SEPARATOR_MAX_WORKERS`, `SEPARATOR_LOW_CONFIDENCE`.
- **Parsers**: `KEY_PARSER_MAX_TOKENS`, `KEY_PARSER_PAGE_MAX_TOKENS`,
  `KEY_PARSER_GLOBAL_MAX_TOKENS`, `PARSER_PARALLEL_PAGES`, `PARSER_MAX_WORKERS`.
- **Segmentation repair**: `GLUE_MAX_PROBES`, `GLUE_PROBE_NEIGHBORS`, `GLUE_PROBE_OFFTOPIC`,
  `GLUE_OFFTOPIC_MIN_CROSS`, `GLUE_OFFTOPIC_MARGIN`.
- **Marks reconcile**: `RECONCILE_KEY_MARKS_WITH_QP`.
- **Report**: `REPORT_OUTPUT_DIR`, `STUDENT_DETAILS_JSON`.
- **Answer screenshots**: `ANSWER_CROPS` (0=off; display‑only per‑answer crops), `ANSWER_CROP_MODEL`
  (defaults to `OCR_MODEL` — the small 30B VL model degenerates on this task), `ANSWER_CROP_MAX_WORKERS`,
  `ANSWER_CROP_MAX_WIDTH`/`_JPEG_QUALITY`, `_PAD_PX`, `_SNAP_WINDOW`, `_MIN_BAND_PX`, `_MIN_INK_ROWS`,
  `_MAX_STARTS_PER_PAGE`, `_MAX_PAGES_PER_Q`, `_MAX_TOKENS`, `ANSWER_CROPS_WAIT_TIMEOUT` (**default
  90 s** — how long `evaluate.py` will block on the sentinel *after* grading finishes before publishing
  the report without screenshots; deliberately far below the stage's own 300 s `_STAGE_TIMEOUTS` entry).
- **Batch**: `BATCH_SHEET_CONCURRENCY` (sheets graded in parallel, default 1 = serial in‑process;
  >1 = isolated subprocess per sheet with auto‑split caps), `BATCH_SHEET_TIMEOUT` (per‑sheet outer
  ceiling, default 2400 s).
- **Watchdog**: `STAGE_TIMEOUT`, `STAGE_TIMEOUT_<NAME>`.
- **Database**: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_TABLE`,
  `REJECTED_ANSWERS_TABLE`.
- **Web/deploy/auth**: `APP_HOST`, `APP_PORT`, `FLASK_DEBUG`, `APP_AUTH_USERNAME`,
  `APP_AUTH_PASSWORD`; entrypoint `PORT`, `DATA_DIR`, `WEB_WORKERS`, `WEB_THREADS`,
  `WEB_TIMEOUT`; `run-public.sh` `TUNNEL`, `NGROK_DOMAIN`, `TS_HOSTNAME`, `PYTHON`.

---

## 14. Testing

40 files, **358 test functions** (pytest; `monkeypatch.setenv` + canned fixtures; no network).
Clusters: orientation (~65 across 9 files), review workflow/report UX (~61), parsing/assembly/
segmentation‑repair (~56), marks (36), upload validation (37), OCR reconcile/pair‑context (30),
grading cascade/consistency/off‑topic (27), MCQ/choices (19), cost/provider/watchdog/cache
(31). A non‑pytest, zero‑cost harness `tests/harness/run_assembly.py` feeds canned OCR through
the pure segmentation‑repair functions.

---

## 15. Cross‑cutting concerns & known doc‑vs‑code discrepancies

- **Determinism & flag‑don't‑guess.** Every marks change is either deterministic (MCQ, best‑of‑N,
  raise‑to‑paper) or LLM‑graded against a key‑defined max; every uncertainty (mixed answer,
  orientation, glue, off‑topic, bad handwriting, key‑integrity) is *flagged* for a human, never
  silently altered. The reconciler can lift a wrong total to the truth but never lower a correct
  one (outside `align_to_paper`).
- **Byte‑identical fallbacks.** The orientation gate, segmentation repair, cascade, parallel
  diagrams, and provider routing are all gated so the "off" path is byte‑identical to before.
- **Single‑process assumption.** In‑memory regrade jobs + per‑review locks + file‑based status
  require one process; horizontal scaling would break them.
- **Discrepancies to be aware of** (surfaced during this mapping):
  - All `skills/**/SKILL.md` and `*.skill` manifests still name **Gemini** models; the live
    code defaults to **Qwen3‑VL** and `.env` pins the 235B tiers. Gemini support was removed.
  - `prepare_orientation`'s docstring still claims a Tesseract‑OSD *suggestion*; the gate is now
    **fully manual** (`suggested_rot=0` for every page).
  - `requirements.txt` comment says "235 tests"; the suite is **358**.
  - `.env.example` documents `EVAL_MAX_TOKENS=16000` / `DIAGRAM_EVAL_MAX_TOKENS=16000`; code
    defaults are `12288` / `8192` (and `.env` pins `EVAL_MAX_TOKENS=12288`).
  - `DB_TABLE` default differs (`ai_class10` in `.env.example`/`run_ocr` vs `questions` in the
    legacy `fetch_answers.py`).
  - `diagram_crops.json` stores **full page** paths despite the "crops" name.
  - `answer-retrieval` (DB answer bank) and several CV helpers (`crop_diagrams_cv.py`,
    `orientation_fix.py`, `crop_diagram_regions.py`) are **dormant/legacy**, retained for
    reference.

## 16. Report collection — local archive (`scripts/report_sync.py`)

A decoupled, **stdlib‑only** worker (launchd `deploy/com.methdai.report-sync.plist`, or
`--once`/`--dry-run`/`--loop`) that collects every completed report into a **local** archive on the Mac — no
cloud, no credentials. It scans `output/<run_id>/` (ready ⇔ `review_state.json` exists — covers single **and**
batch students), bundles each into `~/Evaluation Report Archive/bundles/<tester>/<subject>/<run_id>/vN-<hash>.zip`
(report JSONs + `ocr_output/` + the PDF + `manifest.json` with a sha256 per file; original scans excluded by
default since they already live in `output/` — `include_evidence` adds them), and upserts a row into
`~/Evaluation Report Archive/index.sqlite3` (`report_submissions`, PK `(run_id, content_hash)`) + an
`index.csv`. Idempotent + versioned via the content hash + a `~/.report_sync/state.json` ledger; a teacher
edit (`review_render.json`) → a new `vN` row. Config: `~/.report_sync/config` / `REPORT_SYNC_*` (paths +
toggles, no secrets). Attribution: the **Tester / School** field (`index.html`, localStorage) → `tester_id`
param → threaded like `exam_class` through `full_evaluate`/`resume_after_orientation`/
`_evaluate_from_preprocessed`/`_resume_tail` + `batch_evaluate`/`batch_prepare_orientation`/
`batch_resume_orientation` → `review_state["tester_id"]` + `student_details.json`. `full_evaluator` also
writes `run_meta.json` (grade‑time models/flags) which the worker prefers over `.env`. Full guide:
[REPORT-SYNC.md](REPORT-SYNC.md).
