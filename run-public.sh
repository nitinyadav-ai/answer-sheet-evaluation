#!/usr/bin/env bash
# ---------------------------------------------------------------------------------------------------
# Serve the AI Answer Evaluator on a public link from this Mac. Usage:
#
#     ./run-public.sh
#
# Login + which tunnel to use are read from .env.public (gitignored). Tunnels supported:
#   cloudflare : free, random URL each run, no account          (default)
#   tailscale  : free, PERMANENT url  https://<TS_HOSTNAME>.<your-tailnet>.ts.net   (free account)
#   ngrok      : free, PERMANENT url  https://<NGROK_DOMAIN>                          (free account)
# It keeps the Mac awake, starts the app with the password gate ON and Flask's debugger OFF, then
# opens the tunnel. Press Ctrl-C to take the site offline.
# ---------------------------------------------------------------------------------------------------
set -u   # error on unset vars, but NOT -e/pipefail: a stray non-zero must never kill us silently.
cd "$(dirname "$0")"

# Load local-only settings (login + tunnel choice) if present.
if [ -f "./.env.public" ]; then set -a; . "./.env.public"; set +a; fi

PY="${PYTHON:-python3}"
PORT="${APP_PORT:-5055}"            # dedicated port (avoids clashing with a normal :5005 dev instance)
export APP_PORT="$PORT"
export FLASK_DEBUG=0                # NEVER expose Flask's interactive debugger publicly
export APP_AUTH_USERNAME="${APP_AUTH_USERNAME:-teacher}"
TUNNEL="${TUNNEL:-cloudflare}"

# --- common prerequisites ------------------------------------------------------------------------
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: '$PY' not found. Set PYTHON=/full/path/to/python and re-run." >&2; exit 1
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $PORT is already in use. Stop it, or run:  APP_PORT=5056 ./run-public.sh" >&2; exit 1
fi

# --- login password: .env.public/env, else saved file, else generate + save --------------------
PW_FILE="$HOME/.answer_evaluator_public_pw"
if [ -z "${APP_AUTH_PASSWORD:-}" ]; then
  if [ -f "$PW_FILE" ]; then
    APP_AUTH_PASSWORD="$(cat "$PW_FILE")"
  else
    APP_AUTH_PASSWORD="$("$PY" -c 'import secrets,string; print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(20)))')"
    ( umask 177; printf '%s' "$APP_AUTH_PASSWORD" > "$PW_FILE" )
  fi
fi
export APP_AUTH_PASSWORD

# --- resolve + validate the tunnel command up front (fail early with guidance) --------------------
TUN_CMD=()
case "$TUNNEL" in
  cloudflare)
    command -v cloudflared >/dev/null 2>&1 || { echo "ERROR: cloudflared missing.  brew install cloudflared" >&2; exit 1; }
    TUN_CMD=(cloudflared tunnel --url "http://localhost:$PORT") ;;
  ngrok)
    command -v ngrok >/dev/null 2>&1 || { echo "ERROR: ngrok missing.  brew install ngrok  (then: ngrok config add-authtoken <token>)" >&2; exit 1; }
    [ -n "${NGROK_DOMAIN:-}" ] || { echo "ERROR: set NGROK_DOMAIN in .env.public (your reserved *.ngrok-free.dev domain)." >&2; exit 1; }
    TUN_CMD=(ngrok http "$PORT" --url="$NGROK_DOMAIN" --log=stdout) ;;
  tailscale)
    TS="$(command -v tailscale || true)"
    [ -z "$TS" ] && [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ] && TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    [ -n "$TS" ] || { echo "ERROR: tailscale missing.  brew install --cask tailscale  (then log in)." >&2; exit 1; }
    "$TS" status >/dev/null 2>&1 || { echo "ERROR: Tailscale isn't logged in.  Run:  '$TS' up --hostname=${TS_HOSTNAME:-ai-answer-evaluator}" >&2; exit 1; }
    TUN_CMD=("$TS" funnel "$PORT") ;;
  *)
    echo "ERROR: unknown TUNNEL='$TUNNEL' in .env.public (use cloudflare | tailscale | ngrok)." >&2; exit 1 ;;
esac

# --- keep the Mac awake (no idle/system sleep) for as long as this script runs -------------------
caffeinate -is -w "$$" &
CAFF_PID=$!

# --- start the app (password gate ON) in the background; clean everything up on exit --------------
APP_LOG="${TMPDIR:-/tmp}/answereval_app_$$.log"
TUN_PID=""
cleanup() { kill "$APP_PID" "$CAFF_PID" ${TUN_PID:-} 2>/dev/null; }
echo "Starting the evaluator (password gate ON, tunnel=$TUNNEL) on http://localhost:$PORT ..."
"$PY" evaluation_app/app.py > "$APP_LOG" 2>&1 &
APP_PID=$!
trap cleanup EXIT INT TERM

# --- wait up to ~45s for it to accept connections (a 401 still means it's up) ---------------------
up=""
for _ in $(seq 1 45); do
  if curl -s -o /dev/null "http://localhost:$PORT/"; then up=1; break; fi
  kill -0 "$APP_PID" 2>/dev/null || break        # app process died -> stop waiting
  sleep 1
done
if [ -z "$up" ]; then
  echo "" >&2; echo "ERROR: the app did not start. Its last log lines were:" >&2
  tail -n 20 "$APP_LOG" >&2; exit 1
fi

echo ""
echo "=================================================================="
echo "  LOGIN for the public link:"
echo "     username: $APP_AUTH_USERNAME"
echo "     password: $APP_AUTH_PASSWORD"
echo "=================================================================="
echo "  The public link appears below. Press Ctrl-C to take it offline."
echo "=================================================================="
echo ""

# --- open the tunnel (backgrounded so cleanup can kill it; wait blocks until Ctrl-C) --------------
"${TUN_CMD[@]}" &
TUN_PID=$!
wait "$TUN_PID"
