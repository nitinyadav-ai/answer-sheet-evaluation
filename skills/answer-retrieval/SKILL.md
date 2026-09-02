---
name: answer-retrieval
description: Retrieves exam question and answer metadata (question, answer, type, subject, Marks) from a PostgreSQL database using question_ids. Use this immediately after the 'qr-scanner-and-id-retrieval' skill to fetch the ground truth required for evaluation. Does not save to disk; returns structured JSON directly to the context.
---

# Answer Retrieval

This skill queries a PostgreSQL database to fetch the ground truth answer and metadata for a given set of `question_id`s.

## Workflow

1. **Prerequisites**:
   - You must have a list of `question_id` strings (usually obtained via `qr-scanner-and-id-retrieval`).
   - The target PostgreSQL database must be accessible.
   - The required Python package `psycopg2-binary` must be installed.

2. **Execution**:
   - Run `scripts/fetch_answers.py` passing the `question_id`s as arguments.
   - You must provide the necessary database connection details as environment variables.
   - **Example Command:**
     ```bash
     DB_HOST=localhost DB_NAME=exams DB_USER=admin DB_TABLE=question_bank DB_PASSWORD=secret python3 scripts/fetch_answers.py "AI10_Q1.i" "AI10_Q2.ii"
     ```
   - **Variables:** `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_TABLE`.

3. **Output**:
   - The script outputs a JSON object mapping each `question_id` to its details (`question`, `answer`, `type`, `subject`, `marks`).
   - Keep this JSON in your conversation context (memory). It will be passed to the subsequent evaluation skill. **Do not write this output to a file.**

## Performance Optimization
- **Speed:** Minimizes time by using a single bulk query (`WHERE question_id = ANY(%s)`) instead of executing individual queries in a loop.
- **Cost:** $0.00 (Standard SQL query executes locally, no LLM API cost incurred for retrieval).
- **Accuracy:** 100% deterministic exact-match retrieval relying on Postgres indexing.