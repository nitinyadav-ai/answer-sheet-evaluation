---
name: diagram_evaluator
description: Multi-pass verification system to evaluate diagram features against the database ground-truth using Gemini 3.5 Flash. Ensures high accuracy through initial scoring and subsequent verification passes. Outputs formatted JSON for the report generator.
---

# Diagram Evaluator

This skill evaluates the raw diagram features (extracted by `feature-extracter`) against the expected features from the database. It enforces a strict multi-pass verification system using **Gemini 3.5 Flash** to achieve near 100% accuracy.

## Workflow

1. **Inputs:**
   - The cropped diagram image file.
   - `Student Diagram Features` (from `feature-extracter`).
   - `Expected Diagram Features` and maximum marks (from `answer-retrieval`).

2. **Pass 1 (Initial Scoring - Gemini 3.5 Flash):**
   - The LLM compares the "Student Diagram Features" against the "Expected Diagram Features".
   - It maps matches, notes missing features, calculates the marks (awarding points for presence, deducting/giving zero for absence).
   - Drafts an initial justification.

3. **Pass 2 (Verification Critique - Gemini 3.5 Flash):**
   - The vision model is given the original cropped image *and* the draft evaluation from Pass 1.
   - **Prompt:** "You are an auditor. Review this image against the proposed evaluation. Did the initial pass miss a feature that the student actually drew? Did it award a point for a feature that is illegible? Correct the evaluation."

4. **Final Output:**
   - A standardized JSON object containing exactly:
     - `marks_awarded`
     - `maximum_marks`
     - `student_diagram_features`
     - `correct_diagram_features`
     - `justification`
     - `feedback`
     - `confidence_score`
     - `needs_review` (boolean)
   - Pass this JSON output directly to the `answer-evaluator-and-report-generation` skill.
