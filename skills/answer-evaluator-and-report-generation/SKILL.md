---
name: answer-evaluator-and-report-generation
description: Evaluates OCR student answers against the database ground-truth using a specified rubric and Gemini 3.5 Flash. Generates a structured PDF report in the Downloads/Evaluation Reports directory. Use this immediately after fetching the ground truth answers. Ensures parallel evaluation to minimize time.
---

# Answer Evaluator and Report Generation

This skill takes the student's handwritten answers (extracted via OCR) and the correct answers (fetched from the database), evaluates them using either a Subjective or Objective rubric, and generates a structured, visually appealing PDF report.

## Workflow

1. **Prerequisites**:
   - `ocr_answers.json`: The text answers extracted by `vision-ocr`.
   - `db_answers.json`: The ground truth answers extracted by `answer-retrieval`.
   - `diagram_evaluations.json` (optional): Processed diagram evaluations strictly formatted by `diagram_evaluator`.
   - Python packages `openai` (Qwen3 / OpenAI-compatible) and `fpdf2`.
   - Ensure the `GEMINI_API_KEY` is present in the environment or `.env` file for API requests.

2. **Execution**:
   - The script `evaluate.py` will read the references `subjective_rubric.md` and `objective_rubric.md`.
   - **Parallelization Control:** Spawns parallel API calls to `gemini-3.5-flash` for high-speed evaluation.
   - **Pre-Evaluation Logic:** Automatically detects unattempted or "NA" questions in Python and awards 0 marks, skipping the API call to save cost and prevent hallucination/prompt injection.
   - **Scoring Enforcement:** Enforces strict binary scoring (0 or full marks) for objective questions as per the provided rubrics.
   - For diagrams, it seamlessly appends the `diagram_evaluator` results into the report.
   - It calculates marks, justifications, and flags reviews.
   - **Command:**
     ```bash
     python3 scripts/evaluate.py <student_name> <path_to_ocr_answers.json> <path_to_db_answers.json> [path_to_diagram_evaluations.json]
     ```
   
3. **Output**:
   - Saves a PDF named `StudentName_Date.pdf` in `~/Downloads/Evaluation Reports/`.
   - Prints the total API cost calculated from token usage.

## Resources

- [subjective_rubric.md](references/subjective_rubric.md): Rubric for descriptive answers.
- [objective_rubric.md](references/objective_rubric.md): Rubric for objective answers.
