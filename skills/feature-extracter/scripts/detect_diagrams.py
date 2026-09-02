import json
import sys
import re
import os

def main():
    if len(sys.argv) < 5:
        print("Usage: python3 detect_diagrams.py <ocr_answers_json> <page_mapping_json> <preprocessed_dir> <output_crops_json>")
        sys.exit(1)
        
    ocr_path = sys.argv[1]
    qr_path = sys.argv[2]
    pre_dir = sys.argv[3]
    out_path = sys.argv[4]
    
    try:
        with open(ocr_path, 'r') as f:
            ocr_data = json.load(f)
    except FileNotFoundError:
        print(f"File not found: {ocr_path}")
        sys.exit(1)
        
    try:
        with open(qr_path, 'r') as f:
            qr_data = json.load(f)
    except FileNotFoundError:
        print(f"File not found: {qr_path}")
        sys.exit(1)
        
    # Build mapping from question_id -> list of full preprocessed image paths
    q_to_images = {}
    for filepath, items in qr_data.items():
        for item in items:
            if "question_id" in item and "image" in item:
                q_id = item["question_id"]
                basename = item["image"]
                # The preprocessed images usually have a prefix or just the same name
                # Let's search the preprocessed_dir for a file ending with this basename
                match_path = None
                for fname in os.listdir(pre_dir):
                    if fname.endswith(basename):
                        match_path = os.path.join(pre_dir, fname)
                        break
                
                if match_path:
                    if q_id not in q_to_images:
                        q_to_images[q_id] = []
                    if match_path not in q_to_images[q_id]:
                        q_to_images[q_id].append(match_path)
                
    # A student's diagram for one question lives on a page or two; if a question maps to far more
    # pages than that, the OCR/question-separation over-assigned pages to it (e.g. a bad OCR run
    # lumping the sheet under one id). Cap pages-per-question so a single question can't explode the
    # downstream vision stages (feature extraction + evaluation each send these page images per call).
    # Env-tunable; the cap only ever trims pathological over-mappings, never normal 1-2 page diagrams.
    max_pages = int(os.environ.get("MAX_DIAGRAM_PAGES_PER_Q", "4"))

    diagram_crops = []

    for q_id, q_content in ocr_data.items():
        if q_id == "_instructions_":
            continue

        answer = q_content.get("answer", "")
        # Check if Gemini transcribed a diagram block
        if re.search(r'\[DIAGRAM:', answer, re.IGNORECASE):
            image_paths = q_to_images.get(q_id, [])
            if image_paths:
                if len(image_paths) > max_pages:
                    print(f"Note: {q_id} maps to {len(image_paths)} pages (likely OCR over-assignment); "
                          f"capping diagram crops to the first {max_pages}.")
                    image_paths = image_paths[:max_pages]
                for img_path in image_paths:
                    diagram_crops.append({
                        "question_id": q_id,
                        "image": img_path
                    })
            else:
                print(f"Warning: Diagram detected for {q_id} but no corresponding image found in {pre_dir}")
                
    with open(out_path, 'w') as f:
        json.dump(diagram_crops, f, indent=2)
        
    print(f"Detected {len(diagram_crops)} diagrams. Saved to {out_path}")

if __name__ == "__main__":
    main()
