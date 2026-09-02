import sys
import os
import json
import PIL.Image
import concurrent.futures
from dotenv import load_dotenv

load_dotenv()

# Provider-agnostic LLM client (Qwen3-VL via OpenRouter). Lives in scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "scripts"))
from llm_client import generate, strip_reasoning

# Vision model for orientation detection; env-overridable, falls back to the OCR model.
# (Dormant helper -- not wired into the active preprocess.py path, which handles geometric
# orientation via OpenCV. Kept for opt-in upside-down / rotated-page detection.)
ORIENTATION_MODEL = os.environ.get("ORIENTATION_MODEL",
                                   os.environ.get("OCR_MODEL", "qwen/qwen3-vl-30b-a3b-instruct"))

PROMPT = "Look at this exam page. What degree of clockwise rotation is needed to make the text perfectly upright and readable? Reply with ONLY one of these numbers: 0, 90, 180, 270."


def fix_orientation(image_path):
    try:
        img = PIL.Image.open(image_path)
        text, _, _ = generate(model=ORIENTATION_MODEL, prompt=PROMPT, images=[image_path],
                              temperature=0.0, max_tokens=8)
        angle = int(strip_reasoning(text).strip())

        if angle != 0:
            # PIL rotate is counter-clockwise, so we map clockwise angles to the ROTATE_* transposes.
            if angle == 90:
                img = img.transpose(PIL.Image.ROTATE_270)
            elif angle == 180:
                img = img.transpose(PIL.Image.ROTATE_180)
            elif angle == 270:
                img = img.transpose(PIL.Image.ROTATE_90)

            img.save(image_path)

        return {"file": os.path.basename(image_path), "angle_applied": angle}
    except Exception as e:
        return {"file": os.path.basename(image_path), "error": str(e)}


if __name__ == "__main__":
    images = sys.argv[1:]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for res in executor.map(fix_orientation, images):
            results.append(res)
    print(json.dumps(results, indent=2))
