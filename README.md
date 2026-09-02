---
title: AI Answer Evaluator
emoji: 📝
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

<!-- The YAML block above is required by Hugging Face Spaces (sdk: docker + app_port). It is
     harmless anywhere else. See docs/DEPLOY-HUGGINGFACE.md for the full deploy walkthrough. -->

# AI Answer Evaluator

This is a standalone automated grading pipeline. It extracts text from student answer sheets, preprocesses the images, utilizes Qwen3 (via OpenRouter or any OpenAI-compatible endpoint) for high-accuracy OCR and evaluation, and generates structured PDF reports.

## Prerequisites
Ensure you have the required Python modules installed:
```bash
python3 -m pip install flask flask_cors psycopg2-binary openai fpdf2 opencv-python-headless numpy Pillow pymupdf python-docx python-dotenv
```

## Setup
Ensure that your `.env` file is present in this root directory. It must contain your OpenRouter API key (`LLM_API_KEY`) and Database credentials (`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_TABLE`). See `.env.example` for the full set of options.

## Models (OpenRouter / OpenAI-compatible Qwen3)
All AI stages call models through a single client (`scripts/llm_client.py`) that targets an **OpenAI-compatible** endpoint — **OpenRouter** for hosted Qwen3, or a **local vLLM / SGLang** server — configured via `.env` (no code changes needed). Set `LLM_BASE_URL` + `LLM_API_KEY`, and each stage's model via its own env var so you can A/B Qwen3 sizes:

```bash
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
OCR_MODEL=qwen/qwen3-vl-30b-a3b-instruct           # 32B A/B: qwen/qwen3-vl-32b-instruct
SEPARATOR_MODEL=qwen/qwen3-vl-30b-a3b-instruct
KEY_PARSER_MODEL=qwen/qwen3-vl-30b-a3b-instruct
EVAL_MODEL=z-ai/glm-5.3-flash
DIAGRAM_EVAL_MODEL=qwen/qwen3-vl-30b-a3b-instruct  # must be a vision model (Pass 2 sends the image)
DIAGRAM_FEATURES_MODEL=z-ai/glm-5.3-flash
```

Add new model prices to `scripts/llm_pricing.py` so the cost meter stays accurate (local self-hosted = `(0.0, 0.0)`).

## Running the Web App Locally (VSCode or Terminal)
You do not need OpenClaw running to use this. You can run this entirely standalone.

1. Open this folder (`Answer_Evaluator_OpenClaw`) in VSCode.
2. Open the built-in terminal.
3. Run the Flask application:
   ```bash
   python3 evaluation_app/app.py
   ```
4. Open your browser and go to: `http://localhost:5005`

The pipeline will run perfectly, generate the PDF reports in your `~/Evaluation Reports` folder, and display the interactive results on the web interface.