---
name: ingestion-handler
description: Entry point for handling user input files. Use when the user uploads a single image, multiple images, or a PDF file containing multiple images. Converts PDFs to separate images per page in order, and assigns specific page numbers. Maximizes accuracy and minimizes API cost by pre-processing locally.
---

# Ingestion Handler

This skill normalizes user uploads (Single Image, Multiple Images, PDF File) into a standardized format of ordered, distinct images for downstream processing.

## Quick Start

When the user uploads files (images or PDFs), use the `process_input.py` script to normalize them. The script will automatically detect PDFs, extract each page in order as a high-resolution PNG, assign a page number, and return a summary of the extracted images.

```bash
# Process a single PDF
python3 scripts/process_input.py "path/to/upload.pdf" --output-dir "workspace/output_images"

# Process multiple images
python3 scripts/process_input.py "path/to/img1.jpg" "path/to/img2.png" --output-dir "workspace/output_images"

# Process a mix
python3 scripts/process_input.py "path/to/upload.pdf" "path/to/extra_page.jpg" --output-dir "workspace/output_images"
```

## How It Works

1. **Accuracy**: Uses `PyMuPDF` to render PDF pages at 300 DPI to maximize text and visual fidelity.
2. **Speed & Cost**: Processes documents locally via Python rather than sending large PDFs to external APIs, avoiding expensive token/image costs and long wait times.
3. **Ordering**: Guarantees PDF pages are extracted sequentially and assigned explicit 1-indexed page numbers.
4. **Diagram Recognition**: As pages are processed downstream by `vision-ocr`, any diagram drawn by the student will be transcribed as `[DIAGRAM: description]`. The images themselves are kept intact, allowing the `feature-extracter` to analyze the full page for the diagram features later.

## Dependencies

The script requires `PyMuPDF` and `Pillow`. They have already been installed, but if you encounter import errors, reinstall them:

```bash
python3 -m pip install PyMuPDF Pillow
```
