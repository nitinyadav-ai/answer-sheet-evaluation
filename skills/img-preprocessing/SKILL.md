---
name: img-preprocessing
description: Pre-processes images before passing them to the vision_ocr skill. Executes geometric transformations (deskewing, perspective correction, portrait-to-landscape rotation) and image quality enhancements (grayscale conversion, CLAHE contrast, adaptive upscaling) using parallel processing. Use this skill right after the ingestion-handler to maximize OCR accuracy and minimize API cost by doing the heavy lifting locally.
---

# Image Pre-Processing Handler

This skill optimizes raw document images output by the `ingestion-handler` to prepare them for High-Accuracy Handwritten Text Detection via `vision-ocr`. 

## Quick Start

Pass the image paths to the `preprocess.py` script. The script uses parallel processing via `ProcessPoolExecutor` to drastically reduce processing time.

```bash
# Process a batch of images
python3 scripts/preprocess.py "path/to/img1.png" "path/to/img2.png" --output-dir "workspace/preprocessed"
```

## Supported Transformations

The script automatically performs the following steps on every image in parallel:

**Geometric Transformations:**
1. **Portrait to Landscape:** Detects dimensions and applies a 90-degree clockwise rotation if the height exceeds the width.
2. **Perspective Correction:** Identifies document edges (the largest quad-contour bounding box) and warps the perspective to lay flat. (Falls back gracefully if no clean edges are found).
3. **Deskewing:** Detects text angle using `cv2.minAreaRect` bounding boxes over text pixels and rotates the image to align horizontally.

**Image Quality Enhancement:**
1. **Grayscale Conversion:** Converts BGR to Grayscale for uniform analysis.
2. **Contrast Enhancement:** Applies CLAHE (Contrast Limited Adaptive Histogram Equalization) to balance lighting and bring out faded text.
3. **Grayscale Output (no binarization):** Saves an 8-bit grayscale PNG rather than 1-bit black & white. The vision LLM relies on stroke-weight and baseline cues to distinguish similar symbols (e.g. `_` vs `-`); Otsu thresholding would destroy them and cause symbol misreads in code.
4. **Adaptive Upscaling:** Low-resolution scans are upscaled (cubic interpolation) so small symbols stay legible after the model's internal image tiling; pages already above the target resolution are left unchanged. No DPI metadata is written — the model consumes pixels, not DPI tags.

## Dependencies

The script relies on native computer vision tools installed locally to ensure you pay zero API fees for image manipulations. Ensure the following are installed:

```bash
python3 -m pip install opencv-python-headless numpy Pillow
```