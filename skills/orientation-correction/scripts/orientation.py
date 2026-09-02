"""
orientation.py - Page orientation detection & correction for answer-sheet OCR.

Vendored into the evaluation pipeline from the standalone assisted-orientation tool. Used as a
best-effort FIRST PASS that a teacher confirms before OCR (Qwen3-VL) runs -- never as a blind
auto-rotate, because on faint handwriting the automatic cardinal call is only ~2/3 reliable and a
wrong flip degrades OCR. The human confirmation is what makes the stage "improve-only".

Single-cascade method (deterministic image processing + one small LOCAL model):

  1. Coarse cardinal orientation {0,90,180,270} via Tesseract OSD
     (purpose-built, text-based, returns a confidence). No VLM is used for orientation.
  2. Aspect-ratio prior restricts the answer to the two rotations that yield a landscape page,
     and cross-checks OSD.
  3. Fallback (OSD low-confidence or contradicts the landscape prior): rotate to each landscape
     candidate, keep the one Tesseract OCRs with the highest total word confidence - upright text
     always scores higher than flipped.
  4. Fine deskew for residual tilt via projection-profile variance maximisation.

Public API:
    corrected_bgr, meta = correct_orientation(image_bgr)          # rotate + deskew, returns image
    info               = suggest_rotation(image_bgr)              # cardinal suggestion ONLY (cheap)

`image_bgr` is an OpenCV BGR ndarray, so the function is agnostic to source (PyMuPDF page raster,
scan, or phone photo).

Dependencies: opencv-python(-headless), numpy, pytesseract + the tesseract-ocr binary and the `osd`
traineddata. When pytesseract/tesseract is unavailable the module degrades to the aspect prior.
"""

from __future__ import annotations
import cv2
import numpy as np

try:
    import pytesseract
    from pytesseract import Output
    _HAS_TESS = True
except Exception:
    _HAS_TESS = False

# ---- tunables (safe defaults; tune on your own sheets) ----------------------
OSD_MIN_CONF   = 2.0    # trust OSD cardinal call at/above this orientation_conf
OSD_MIN_DIM    = 900    # upscale so the shorter side >= this before OSD (px)
DESKEW_LIMIT   = 12.0   # max residual tilt searched, degrees (cardinal is done)
DESKEW_STEP    = 0.4    # angular resolution of the deskew search, degrees
DESKEW_WIDTH   = 1000   # downscale width for the deskew search (speed)
MIN_WORD_CONF  = 40     # word conf floor when scoring uprightness (0..100)


def correct_orientation(image, target="landscape"):
    """Return (corrected_bgr, meta). Applies cardinal rotation then residual deskew.

    target : "landscape" (default), "portrait", or "auto" (trust OSD).
    """
    if image is None or image.size == 0:
        raise ValueError("empty image")

    gray = _to_gray(image)
    cardinal, conf, method = _decide_cardinal(image, gray, target)
    coarse = _rotate_cardinal(image, cardinal)

    skew = _deskew_angle(_to_gray(coarse))
    corrected = _rotate_fine(coarse, skew)

    meta = {
        "cardinal_deg": cardinal,     # {0,90,180,270} clockwise applied
        "skew_deg": round(skew, 3),   # fine tilt applied (deg, += CCW)
        "osd_conf": round(float(conf), 3),
        "method": method,             # 'osd' | 'ocr_fallback' | 'osd_unavailable'
        "out_size": (corrected.shape[1], corrected.shape[0]),
    }
    return corrected, meta


def suggest_rotation(image, target="landscape"):
    """Cheap cardinal-only suggestion for the assisted-review first pass.

    Returns {"suggested_rot": <0|90|180|270 CW>, "osd_conf": float, "method": str} WITHOUT applying
    the rotation and WITHOUT the fine-deskew search (the pipeline's preprocess step already deskews;
    the teacher-confirmed cardinal is all this stage needs). `method == "osd"` means a confident
    OSD call; anything else ("ocr_fallback" / "osd_unavailable") is low-confidence and should be
    surfaced for human verification.
    """
    if image is None or image.size == 0:
        raise ValueError("empty image")
    gray = _to_gray(image)
    cardinal, conf, method = _decide_cardinal(image, gray, target)
    return {"suggested_rot": int(cardinal) % 360, "osd_conf": round(float(conf), 3), "method": method}


# ---- cardinal orientation ---------------------------------------------------

def _decide_cardinal(image, gray, target):
    h, w = gray.shape[:2]
    landscape_now = w >= h

    if target == "landscape":
        landscape_rots = {0, 180} if landscape_now else {90, 270}
    elif target == "portrait":
        landscape_rots = {90, 270} if landscape_now else {0, 180}
    else:  # auto
        landscape_rots = {0, 90, 180, 270}

    osd_deg, osd_conf = _osd_orientation(gray)

    if osd_deg is not None and osd_conf >= OSD_MIN_CONF and osd_deg in landscape_rots:
        return osd_deg, osd_conf, "osd"

    if not _HAS_TESS:
        # no model available: fall back to aspect prior only
        deg = 0 if 0 in landscape_rots else min(landscape_rots)
        return deg, 0.0, "osd_unavailable"

    # fallback: choose the candidate whose OCR reads "most upright"
    best_deg, best_score = None, -1.0
    for deg in sorted(landscape_rots):
        score = _score_upright(_rotate_cardinal(image, deg))
        if score > best_score:
            best_score, best_deg = score, deg
    return best_deg, float(osd_conf or 0.0), "ocr_fallback"


def _osd_orientation(gray):
    """(rotate_deg, orientation_conf) from Tesseract OSD, or (None, 0.0)."""
    if not _HAS_TESS:
        return None, 0.0
    img = _upscale_min(gray, OSD_MIN_DIM)
    try:
        osd = pytesseract.image_to_osd(img, output_type=Output.DICT)
        return int(osd["rotate"]) % 360, float(osd["orientation_conf"])
    except Exception:
        return None, 0.0  # too few chars / tesseract error -> use fallback


def _score_upright(image):
    """Sum of confidences of confidently-read words. Higher = more upright."""
    if not _HAS_TESS:
        return 0.0
    try:
        data = pytesseract.image_to_data(
            _upscale_min(_to_gray(image), OSD_MIN_DIM), output_type=Output.DICT)
    except Exception:
        return 0.0
    return float(sum(c for c in (int(x) for x in data["conf"]) if c >= MIN_WORD_CONF))


# ---- deskew (residual tilt) -------------------------------------------------

def _deskew_angle(gray, limit=DESKEW_LIMIT, step=DESKEW_STEP):
    """Angle (deg, +=CCW) that best aligns text rows, via projection variance."""
    g = _downscale_width(gray, DESKEW_WIDTH)
    binimg = cv2.threshold(g, 0, 255,
                           cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    h, w = binimg.shape
    center = (w / 2.0, h / 2.0)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-limit, limit + step, step):
        M = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        rot = cv2.warpAffine(binimg, M, (w, h), flags=cv2.INTER_NEAREST,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        proj = np.sum(rot, axis=1, dtype=np.float64)
        score = float(np.sum(np.diff(proj) ** 2))  # sharp row edges -> high
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return best_angle


# ---- rotation primitives ----------------------------------------------------

def _rotate_cardinal(image, deg):
    deg %= 360
    if deg == 0:
        return image
    if deg == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError(f"non-cardinal degree: {deg}")


def _rotate_fine(image, angle):
    if abs(angle) < 0.1:
        return image
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), float(angle), 1.0)
    return cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


# ---- helpers ----------------------------------------------------------------

def _to_gray(image):
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _upscale_min(gray, min_dim):
    h, w = gray.shape[:2]
    s = min_dim / min(h, w)
    if s <= 1.0:
        return gray
    return cv2.resize(gray, (int(w * s), int(h * s)), interpolation=cv2.INTER_CUBIC)


def _downscale_width(gray, width):
    h, w = gray.shape[:2]
    if w <= width:
        return gray
    s = width / w
    return cv2.resize(gray, (width, int(h * s)), interpolation=cv2.INTER_AREA)


# ---- CLI: process a file or a folder ---------------------------------------

if __name__ == "__main__":
    import sys, os, glob, json

    if len(sys.argv) < 2:
        print("usage: python orientation.py <image|dir> [out_dir]")
        raise SystemExit(1)

    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "oriented"
    os.makedirs(out_dir, exist_ok=True)

    paths = ([src] if os.path.isfile(src)
             else sorted(glob.glob(os.path.join(src, "*"))))
    for p in paths:
        img = cv2.imread(p)
        if img is None:
            continue
        fixed, meta = correct_orientation(img)
        dst = os.path.join(out_dir, os.path.basename(p))
        cv2.imwrite(dst, fixed)
        print(os.path.basename(p), json.dumps(meta))
