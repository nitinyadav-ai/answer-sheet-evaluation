---
name: feature-extracter
description: Automates diagram detection and extracts visual features using Gemini 3.5 Flash. Analyzes the image without knowing the answer key, returning a raw list of student diagram features. Use right after vision-ocr.
---

# Feature Extracter

This skill detects which questions contain diagrams (by parsing OCR results) and uses **Gemini 2.5 Pro** to thoroughly analyze the corresponding page images. It does not know the answer key. Its sole purpose is to list every label, shape, axis, arrow, and structural relationship present.

## Workflow

1. **Detect Diagrams:** Run `detect_diagrams.py` to parse `ocr_answers.json` and generate `diagram_crops.json`.
   ```bash
   python3 scripts/detect_diagrams.py <ocr_answers.json> <page_mapping.json> <preprocessed_dir> <output_diagram_crops.json>
   ```

2. **Extract Features:** Run `extract_features.py` using **Gemini 3.5 Flash** to analyze the visual content.
   ```bash
   python3 scripts/extract_features.py <diagram_crops.json> > student_features.json
   ```

3. **Output:** A structured JSON format capturing the `Student Diagram Features` (what the student actually drew/labeled). Pass this list forward to the `diagram_evaluator` skill.
