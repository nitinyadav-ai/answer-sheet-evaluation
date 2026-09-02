#!/usr/bin/env bash
# Start the AI Answer Evaluator (Flask + CV/OCR pipeline) under gunicorn.
#
# Storage model:
#   * Hugging Face FREE Spaces have an EPHEMERAL filesystem — wiped on every rebuild/restart/sleep.
#     In that case we run in-place: the app writes output/, uploads/ and ~/Evaluation Reports to the
#     container's temporary disk (fine for "grade -> download the PDF now").
#   * If a PERSISTENT disk is mounted and writable at $DATA_DIR (HF's paid "persistent storage"
#     upgrade, or a Render/Fly volume), we redirect every write path onto it via symlinks so reports
#     survive restarts — no app code change needed. The output dir is computed as <repo>/output in
#     app.py, full_evaluator.py and batch_evaluator.py, so one symlink redirects all three.
set -e

PORT="${PORT:-7860}"
APP="$(cd "$(dirname "$0")" && pwd)"        # repo root (HF: /home/user/app, Render/Fly: /app)
DATA="${DATA_DIR:-/data}"

if [ -d "$DATA" ] && [ -w "$DATA" ]; then
  echo "[entrypoint] persistent storage at $DATA — reports will survive restarts"
  mkdir -p "$DATA/output" "$DATA/uploads" "$DATA/reports" "$DATA/evaluation_reports"
  rm -rf "$APP/output" "$APP/evaluation_app/uploads"
  ln -sfn "$DATA/output"             "$APP/output"
  ln -sfn "$DATA/uploads"            "$APP/evaluation_app/uploads"
  rm -rf "$HOME/Evaluation Reports" "$HOME/Desktop"
  ln -sfn "$DATA/evaluation_reports" "$HOME/Evaluation Reports"
  ln -sfn "$DATA/reports"            "$HOME/Desktop"
else
  echo "[entrypoint] no persistent storage — EPHEMERAL filesystem (free Space): data resets on restart"
  mkdir -p "$APP/output" "$APP/evaluation_app/uploads" "$HOME/Evaluation Reports"
fi

# Background batch/orientation/grading jobs run as in-process daemon threads that coordinate via files,
# so DO NOT add --max-requests (it would recycle a worker mid-job and orphan the work). gthread handles
# the I/O-bound LLM calls; --timeout is only the request cap for the legacy synchronous /evaluate path.
exec gunicorn \
  --chdir "$APP/evaluation_app" \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_WORKERS:-2}" \
  --threads "${WEB_THREADS:-8}" \
  --worker-class gthread \
  --timeout "${WEB_TIMEOUT:-600}" \
  --graceful-timeout 30 \
  --access-logfile - --error-logfile - \
  app:app
