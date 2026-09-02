# ---- AI Answer Evaluator — Hugging Face Docker Space image -----------------------------------------
# The AI models (Qwen) run remotely on OpenRouter; this image only runs the Flask app that calls them.
# Hugging Face Spaces run the container as a NON-root user (uid 1000) and route traffic to app_port
# (set to 7860 in README.md's front-matter), so the Dockerfile is written around those two rules.
FROM python:3.12-slim-bookworm

# --- System packages (installed as root, before we drop to the 'user' account) ---------------------
#   tesseract-ocr + -eng + -osd : pytesseract page-orientation detection (degrades gracefully if absent)
#   libglib2.0-0, libgomp1      : runtime libs for opencv-python-headless / numpy
#   fonts-dejavu-core           : broad Unicode TTF so PDF reports embed real glyphs (Greek/math/arrows)
#   PyMuPDF (fitz) bundles its own PDF engine -> Poppler is deliberately NOT installed.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd \
      libglib2.0-0 libgomp1 \
      fonts-dejavu-core \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Non-root user (Hugging Face Spaces requires uid 1000) ------------------------------------------
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    DATA_DIR=/data

WORKDIR /home/user/app

# Python deps first so this layer caches across code edits. --user installs into /home/user/.local.
COPY --chown=user requirements.txt .
RUN pip install --user --upgrade pip && pip install --user -r requirements.txt

# App code. output/, uploads/, .env, tests/ are excluded via .dockerignore and (re)created at runtime.
COPY --chown=user . .

EXPOSE 7860
ENTRYPOINT ["bash", "docker-entrypoint.sh"]
