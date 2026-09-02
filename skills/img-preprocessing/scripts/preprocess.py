import sys
import os
import argparse
from pathlib import Path
import concurrent.futures

try:
    import cv2
    import numpy as np
    from PIL import Image
except ImportError:
    print("Dependencies missing. Please run: pip install opencv-python-headless numpy Pillow")
    sys.exit(1)

def order_points(pts):
    """Order points in top-left, top-right, bottom-right, bottom-left format."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect

def apply_perspective_correction(image, gray):
    """Detects document contours and applies a perspective warp if a clean quad is found."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 75, 200)
    
    contours, _ = cv2.findContours(edged.copy(), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    
    doc_contour = None
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        
        if len(approx) == 4:
            # Check if area is large enough (at least 85% of image, so we don't accidentally crop inner answer boxes)
            if cv2.contourArea(c) > (image.shape[0] * image.shape[1] * 0.85):
                doc_contour = approx
                break
                
    if doc_contour is not None:
        pts = doc_contour.reshape(4, 2)
        rect = order_points(pts)
        (tl, tr, br, bl) = rect
        
        widthA = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
        widthB = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
        maxWidth = max(int(widthA), int(widthB))
        
        heightA = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
        heightB = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
        maxHeight = max(int(heightA), int(heightB))
        
        dst = np.array([
            [0, 0],
            [maxWidth - 1, 0],
            [maxWidth - 1, maxHeight - 1],
            [0, maxHeight - 1]], dtype="float32")
            
        M = cv2.getPerspectiveTransform(rect, dst)
        warped = cv2.warpPerspective(image, M, (maxWidth, maxHeight))
        return warped
    return image

def deskew(image):
    """Finds the text skew angle and rotates to align horizontally."""
    # Ensure image is grayscale
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
        
    # Invert the image (text becomes white, background black)
    thresh = cv2.bitwise_not(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1])
    
    # Get all non-zero pixel coordinates
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) == 0:
        return image
        
    # Estimate skew from the min-area box of the ink, then fold OpenCV's raw angle into a [-45, 45]
    # deviation from axis-aligned. OpenCV >= 4.5 returns the angle in (0, 90]; older versions return
    # [-90, 0). Without this fold a near-upright page (whose vertical ruled lines dominate the box)
    # comes back as ~89 deg, and the rotation below then warps the whole sheet -- cropping off the
    # question-number column and breaking OCR.
    angle = cv2.minAreaRect(coords)[-1]
    if angle > 45:
        angle -= 90
    elif angle < -45:
        angle += 90

    # Only correct genuine, small scan skew. Near-zero isn't worth the resampling blur; a large
    # residual means ruled lines / borders (not the text baseline) drove the estimate -- rotating by
    # it is exactly what mangled pages by ~89 deg, so leave those untouched.
    if abs(angle) < 0.5 or abs(angle) > 15:
        return image
        
    (h, w) = image.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    return rotated

def _maybe_force_landscape(img):
    """Legacy portrait->landscape force-rotate, OFF by default (PREPROCESS_FORCE_LANDSCAPE=1 restores).

    Orientation is now decided DOWNSTREAM by run_ocr's orientation-by-boundary-vote, which uses the
    exam's closed question set as a reliable signal. This blind "if portrait, rotate 90 deg CW" assumed
    every sheet should be landscape and instead CORRUPTED orientation: an upright portrait scan became
    sideways, and a sideways scan became fully upside-down -- the CS Class 12 failure where every 'Q7'
    header was misread as '87', collapsing the whole objective section. So it no longer runs by default."""
    if os.environ.get("PREPROCESS_FORCE_LANDSCAPE", "0").strip().lower() in ("0", "false", "no", "off", ""):
        return img
    h, w = img.shape[:2]
    if h > w:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    return img

def process_single_image(input_path, output_dir, index):
    try:
        # Load image via OpenCV
        img = cv2.imread(str(input_path))
        if img is None:
            return {"status": "error", "file": str(input_path), "error": "Unable to read image"}

        # 1. Orientation is handled DOWNSTREAM by the OCR-stage boundary-vote (run_ocr.py), so the old
        # blind portrait->landscape force-rotate is OFF by default (see _maybe_force_landscape).
        img = _maybe_force_landscape(img)

        # 2. Grayscale conversion
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # 3. Perspective correction
        img = apply_perspective_correction(img, gray)
        
        # Re-grayscale after perspective warp
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        
        # 4. Skew correction (Deskewing)
        deskewed = deskew(gray)
        
        # 5. Apply CLAHE contrast enhancement
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(deskewed)

        # 6. Keep an 8-bit GRAYSCALE image (no Otsu binarization).
        # A vision LLM needs stroke-weight and baseline cues to distinguish '_' (sits low, on the
        # baseline) from '-' (mid-height); 1-bit thresholding destroys them and causes symbol
        # misreads in code. Optionally upscale low-resolution scans so small symbols stay legible
        # after the model's internal image tiling.
        # Env-tunable (PREPROCESS_LONG_EDGE_TARGET): the higher-fidelity lever on Qwen, where
        # media_resolution is ignored -- raising it renders fine marks (a superscript '-1', 'Q' vs
        # '8') at more pixels, at the cost of a larger upload. Default keeps today's behaviour.
        try:
            LONG_EDGE_TARGET = int(os.environ.get("PREPROCESS_LONG_EDGE_TARGET", "3500"))
        except ValueError:
            LONG_EDGE_TARGET = 3500
        LONG_EDGE_TARGET = min(max(LONG_EDGE_TARGET, 1500), 6000)
        h, w = enhanced.shape[:2]
        scale = LONG_EDGE_TARGET / max(h, w)
        if scale > 1.05:
            enhanced = cv2.resize(enhanced, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

        # 7. Save as lossless PNG. No DPI tag: Gemini consumes pixels, not DPI metadata, so the
        # old dpi=(500,500) was cosmetic and misleading (it never changed the pixel count).
        out_filename = f"preprocessed_{Path(input_path).name}"
        out_filepath = os.path.join(output_dir, out_filename)

        pil_img = Image.fromarray(enhanced)
        pil_img.save(out_filepath)
        
        return {"status": "success", "file": str(input_path), "output": out_filepath, "index": index}
        
    except Exception as e:
        return {"status": "error", "file": str(input_path), "error": str(e), "index": index}

def main():
    parser = argparse.ArgumentParser(description="Image Pre-Processing handler for OCR")
    parser.add_argument("inputs", nargs="+", help="Paths to input images")
    parser.add_argument("--output-dir", default="./preprocessed_images", help="Directory to save preprocessed images")
    
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    
    print(f"Starting parallel processing of {len(args.inputs)} images...")
    
    # Execute in parallel to minimize processing time
    results = []
    with concurrent.futures.ProcessPoolExecutor() as executor:
        futures = {executor.submit(process_single_image, path, args.output_dir, idx): path for idx, path in enumerate(args.inputs)}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    # Sort results by original index to maintain order for downstream OCR
    results.sort(key=lambda x: x["index"])
            
    # Report
    success_count = 0
    print("\n--- Processing Complete ---")
    for res in results:
        if res["status"] == "success":
            print(f"[OK] {res['file']} -> {res['output']}")
            success_count += 1
        else:
            print(f"[FAIL] {res['file']}: {res['error']}")
            
    print(f"\nSuccessfully processed {success_count}/{len(args.inputs)} images.")

if __name__ == "__main__":
    main()
