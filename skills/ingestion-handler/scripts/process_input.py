import sys
import os
import argparse
from pathlib import Path
import concurrent.futures

try:
    import fitz  # PyMuPDF
    from PIL import Image
except ImportError:
    print("Error: PyMuPDF or Pillow not installed. Please run: pip install PyMuPDF Pillow")
    sys.exit(1)

def render_pdf_page(pdf_path, page_num, output_dir):
    """Worker function to render a single PDF page in parallel."""
    doc = fitz.open(pdf_path)
    zoom = 300 / 72
    mat = fitz.Matrix(zoom, zoom)
    page = doc.load_page(page_num)
    pix = page.get_pixmap(matrix=mat)
    
    base_name = Path(pdf_path).stem
    output_filename = f"{base_name}_page_{page_num + 1}.png"
    output_filepath = os.path.join(output_dir, output_filename)
    
    pix.save(output_filepath)
    doc.close()
    
    return {
        "original_file": str(pdf_path),
        "page_number": page_num + 1,
        "image_path": output_filepath
    }

def verify_image(img_path, index):
    """Worker function to verify an image in parallel."""
    try:
        with Image.open(img_path) as img:
            img.verify()
        return {
            "original_file": str(img_path),
            "page_number": index,
            "image_path": str(img_path)
        }
    except Exception as e:
        print(f"Error reading image {img_path}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Ingestion handler for Single/Multiple Images or PDF.")
    parser.add_argument("inputs", nargs="+", help="Paths to input files (Images or PDFs)")
    parser.add_argument("--output-dir", default="./output_images", help="Directory to save extracted PDF pages")
    
    args = parser.parse_args()
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    results = []
    tasks = []
    
    # Use ProcessPoolExecutor to heavily parallelize CPU-bound tasks (rendering pages and verifying images)
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for idx, file_path in enumerate(args.inputs):
            path = Path(file_path)
            if not path.exists():
                print(f"Warning: File not found: {file_path}")
                continue
                
            ext = path.suffix.lower()
            
            if ext == ".pdf":
                print(f"Queueing PDF for parallel processing: {path.name}")
                doc = fitz.open(file_path)
                num_pages = len(doc)
                doc.close()
                for page_num in range(num_pages):
                    future = executor.submit(render_pdf_page, file_path, page_num, args.output_dir)
                    tasks.append((idx, page_num, future))
            elif ext in [".png", ".jpg", ".jpeg", ".webp"]:
                print(f"Queueing Image for parallel verification: {path.name}")
                future = executor.submit(verify_image, file_path, idx + 1)
                tasks.append((idx, 0, future))
            else:
                print(f"Warning: Unsupported file type: {ext}")
                
        # Wait for all tasks to complete and gather results in the exact original order
        for idx, page_num, future in tasks:
            res = future.result()
            if res is not None:
                results.append(res)
            
    print("\n--- Processing Complete ---")
    for res in results:
        print(f"File: {res['original_file']} | Page: {res['page_number']} -> Image: {res['image_path']}")

if __name__ == "__main__":
    main()