#!/usr/bin/env python3
"""
orient_pages.py - build the MANUAL orientation-review manifest over a run's preprocessed pages.

Lists each page for the teacher to review, presented EXACTLY as uploaded (suggested_rot=0) with no
automatic rotation and no verify flag -- the teacher rotates only the pages that are actually wrong.
The confirmed rotations are applied to the pristine images just before OCR (resume_after_orientation).
(No detector runs: the old Tesseract-OSD first pass was ~2/3 reliable and flipped already-upright pages.)

The page order (index 1..N) matches full_evaluator's OCR order: preprocessed/*.png sorted by the
same natural-sort key, so index -> file is stable across prepare, review, and resume.

usage: python3 orient_pages.py <preprocessed_dir> <output_json>
"""
import os
import re
import sys
import json
import glob


def _natural_key(s):
    """page_2 before page_10 (filenames are not zero-padded) -- mirrors full_evaluator."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(s))]


def build_review(preprocessed_dir):
    """Return the review manifest dict {"pages": [...]} for a preprocessed/ directory.

    Fully MANUAL: every page is listed exactly as uploaded (suggested_rot=0) with a neutral,
    non-flagging state (confidence="ok", method="manual"). No detector runs -- the teacher decides
    each page's orientation in the review gate; confirmed rotations are applied by
    resume_after_orientation. deg==0 there is a true no-op, so an all-upright sheet is OCR'd unchanged."""
    files = sorted(glob.glob(os.path.join(preprocessed_dir, "*.png")), key=_natural_key)
    pages = []
    for i, path in enumerate(files, start=1):
        pages.append({"index": i, "file": os.path.basename(path),
                      "suggested_rot": 0, "confidence": "ok", "method": "manual"})
    return {"pages": pages}


def main():
    if len(sys.argv) < 3:
        print("usage: python3 orient_pages.py <preprocessed_dir> <output_json>", file=sys.stderr)
        return 1
    preprocessed_dir, out_json = sys.argv[1], sys.argv[2]
    review = build_review(preprocessed_dir)
    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(review, f, indent=2)
    print(json.dumps({"status": "ok", "count": len(review["pages"]), "output": out_json}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
