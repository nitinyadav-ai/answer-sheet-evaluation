import os
import sys
import json
import cv2
import numpy as np

def process_image(image_path, qr_data):
    if not qr_data: return []
    
    # We will map the largest hand-drawn box to the first ID, 
    # since these exams typically have 1 diagram per assigned page.
    q_ids = [q['question_id'] for q in qr_data]
    if not q_ids: return []
    target_qid = q_ids[0]

    img = cv2.imread(image_path)
    if img is None:
        return []
        
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Adaptive thresholding to isolate handwriting/drawings from background
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2)
    
    # Morphological operations to connect components of a drawing
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    dilated = cv2.dilate(thresh, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Find the largest contour that isn't the whole page, and isn't a tiny QR code
    max_area = 0
    best_box = None
    page_area = img.shape[0] * img.shape[1]
    
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        
        # Exclude tiny boxes (QR codes) and massive boxes (page borders)
        if area > 50000 and area < page_area * 0.8:
            # Aspect ratio check to avoid long thin lines of text
            aspect_ratio = float(w)/h
            if 0.2 < aspect_ratio < 5.0:
                if area > max_area:
                    max_area = area
                    best_box = (x, y, w, h)
                    
    results = []
    if best_box:
        x, y, w, h = best_box
        
        # Add padding
        pad = 20
        xmin = max(0, x - pad)
        ymin = max(0, y - pad)
        xmax = min(img.shape[1], x + w + pad)
        ymax = min(img.shape[0], y + h + pad)
        
        crop = img[ymin:ymax, xmin:xmax]
        
        crops_dir = os.path.join(os.path.dirname(image_path), "crops")
        os.makedirs(crops_dir, exist_ok=True)
        
        crop_path = os.path.join(crops_dir, f"{target_qid}_diagram.png")
        cv2.imwrite(crop_path, crop)
        results.append({"image": crop_path, "question_id": target_qid})
        
    return results

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 crop_diagrams.py <qr_results.json>")
        sys.exit(1)
        
    qr_results_path = sys.argv[1]
    with open(qr_results_path, 'r') as f:
        qr_results = json.load(f)
        
    all_crops = []
    for img_path, qr_data in qr_results.items():
        if qr_data:
            crops = process_image(img_path, qr_data)
            all_crops.extend(crops)
            
    print(json.dumps(all_crops, indent=2))

if __name__ == "__main__":
    main()
