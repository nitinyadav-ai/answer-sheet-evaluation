# Uploading Question Papers & Answer Keys — Teacher Guidelines

Follow these when uploading the **question paper** and the **answer key**. They prevent the parsing
errors that would otherwise mis-score a paper. The app also checks your files automatically on upload
and will warn you (or block) if something here is missed — but getting it right up front is fastest.

> **Why it matters:** the app reads the *text* of your files (it does **not** photograph or OCR them).
> If the text is missing, garbled, or ambiguous, the marks can come out wrong. A clean, text-based
> file with clear numbering and marks parses perfectly.

---

## A. File format — the biggest lever

1. **Upload a digital, text-based file — DOCX or a text PDF. Never a photo or a scan.**
   The parser reads the embedded text layer, not the image. A scanned/photographed document has *no
   text* and will be rejected. *Test:* if you can select/highlight the text in the file, it's fine.
   *If you only have a scan,* run OCR / "Make searchable PDF" first (e.g. in Acrobat, Preview, or
   Google Docs), then upload.
2. **Use a simple, single-column layout.** Avoid text embedded inside images, multi-column pages,
   watermarks over text, and unusual fonts — these garble text extraction.
3. **Upload BOTH the question paper and the answer key.** The automatic cross-check that catches any
   remaining error compares the two as independent sources. With only the key, that safety net can't run.

## B. Numbering & marks

4. **Number every question uniquely, and use the same numbers in the paper and the key.**
   `Q1` / `1.` / `Q1(a)` are all fine — just be consistent between the two documents. This is what lets
   the app line them up.
5. **Put the marks next to each question and each sub-part** — not only in a section header
   ("Section D — 5 marks each") or in a far-away right-hand column. Marks that are visually detached
   from their question are the exact thing that made the app drop parts of a question before.
6. **State the total marks** somewhere (e.g. "Maximum Marks: 80"). It's a free extra sanity check.
7. **Marks must be whole or half numbers** (1, 1.5, 2, 2.5 …). Grading awards marks in half-mark
   steps, so a question that parses as e.g. `0.8` is flagged as a likely misread — you can correct
   it in the marks editor.

## C. Choices & multi-part questions

7. **Make "answer any one / OR" choices explicit.** Label the alternatives clearly —
   `31. (a) … OR (b) …`, or `Answer any ONE of Q31 / Q32`. Ambiguous choices get mis-counted
   (dropped, or double-counted so the total inflates).
8. **Label multi-part questions, and show whether the parts add up or are alternatives.**
   Use `(a)(b)(c)` or `(i)(ii)(iii)`; keep each part's marks with that part; and don't split one
   question's parts across pages or tables in a confusing way. Example of a 4-mark case study:
   `37. (a) … [1]  (b) … [1]  (c)(i) … OR (c)(ii) … [2]`.

## D. Objective / MCQ answer keys

9. **Give BOTH the option letter and the option text, with a plain separator:**
   `1. (B) 5` or `1. B) 5`. Don't reduce the answer to only the letter (`B`) or only the value (`5`) —
   the app matches on either, so keep both.

## E. Math & special content

10. **Prefer plain Unicode or simple LaTeX for equations, kept clean.** Deeply nested / complex LaTeX
    is more error-prone.

## F. Pasting JSON directly (advanced)

Instead of uploading a PDF/Word file, you can **paste the question paper or answer key as JSON** — use
the **Paste JSON** tab on each step. This skips parsing entirely: useful when you already have the
structured data, or want to hand-fix a few fields. Pasted JSON goes through the **same validation,
marks cross-check and choices handling** as a parsed PDF.

**Tip — start from a parse.** Upload a PDF once, then click **Copy** above the extracted-JSON preview,
edit what you need, and paste it back next time. That gives you a correct template to work from.

### Question paper — format

A single object with a `questions` map (question id → question). There is **no `answer`** in the paper.

```json
{
  "questions": {
    "Q1": { "question_id": "Q1", "question": "The LCM of 960 and 240 is: (A) 960 (B) 240 (C) 60 (D) 15", "marks": 1, "type": "MCQ" },
    "Q2": { "question_id": "Q2", "question": "Prove that root 5 is irrational.", "marks": 3, "type": "Short Answer" }
  }
}
```

- `type` is one of `MCQ`, `Short Answer`, `Long Answer`, `Numerical`.
- A whole-question **choice** (`(a) … OR (b) …`) stays **one** entry worth its single printed total.

### Answer key — format

An object with `metadata` (class, subject, and the choices structure) plus a `questions` map. Each
question carries the **correct answer** and its **marks**.

```json
{
  "metadata": {
    "class": "Class X",
    "subject": "Mathematics",
    "choice_groups": [ { "parent": "Q22", "members": ["Q22(a)", "Q22(b)"], "required": 1 } ],
    "inline_choice_ids": []
  },
  "questions": {
    "Q1":     { "question_id": "Q1",     "question": "...", "answer": "(A) 960",    "type": "MCQ",          "subject": "Mathematics", "marks": 1 },
    "Q22(a)": { "question_id": "Q22(a)", "question": "...", "answer": "AC = 5 cm",  "type": "Short Answer", "subject": "Mathematics", "marks": 2 },
    "Q22(b)": { "question_id": "Q22(b)", "question": "...", "answer": "YR = 2.7 cm","type": "Short Answer", "subject": "Mathematics", "marks": 2 }
  }
}
```

### Rules that matter (these are what the checks enforce)

- **Every question needs `marks` greater than 0.** A question with `0`/missing marks is flagged (and
  blocks if most questions lack marks).
- **Objective answers keep BOTH the option letter and text** — `"(B) 5"`, not `"B"` or `"5"`.
- **Question ids** may be bare (`"12"`) or prefixed (`"Q12"`) — either is accepted; use the **same base
  numbers** the question paper uses so the cross-check can line them up.
- **"Answer any one / OR" choices:** put each alternative as its **own** entry (e.g. `Q22(a)` and
  `Q22(b)`), **each carrying the FULL marks** (never summed), tied together by one `choice_groups` entry
  whose `members` list **exactly matches those entry keys** (`required` = how many the student must
  answer, almost always `1`).
- **Additive multi-part questions** (must answer all; marks add up, e.g. `(i)(ii)(iii)`) are separate
  entries and are **not** listed in `choice_groups`.
- If choices don't apply, use `"choice_groups": []` and `"inline_choice_ids": []`.

You may also paste the **flat** form — a bare `{ "Q1": { … } }` map with no `metadata` — it's accepted,
but then the key carries no choice structure, so prefer the `metadata` form whenever the paper has "OR"
choices.

---

## What the automatic check does on upload

| Signal | What you'll see |
| --- | --- |
| File is a scan / has no text layer | **Blocked** — "no text layer (looks like a scan); upload a text-based PDF or DOCX" |
| Parser found no questions | **Blocked** — "no questions were extracted" |
| Some questions have no marks | **Warning** — lists the questions to check |
| A question's marks aren't a whole or half number (e.g. `0.8`) | **Warning** — names the questions; almost always a misread |
| No question paper uploaded | **Warning / blocked at evaluation** |
| Key total ≠ paper total (e.g. 74 vs 80) | **Warning** — shows the mismatch and the affected questions |
| A question is in the paper but missing from the key (or vice-versa) | **Warning** — names the question |

Warnings don't stop you — the grader corrects the marks total against the question paper and flags the
affected answers for your review. But a warning almost always means the upload can be improved; fixing
the file is the reliable fix.
