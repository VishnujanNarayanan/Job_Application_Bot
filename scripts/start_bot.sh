#!/usr/bin/env bash
#
# Local runner — one command to run the job bot while the laptop is awake.
#
#   1. brings up the always-on resume endpoint (Docker)
#   2. starts ngrok (public PDF/DOCX resume links) if it isn't already running
#   3. loops the pipeline every N minutes (LIVE run: real Telegram + Sheets)
#
# This is the laptop-local stand-in for Oracle cron (architecture Layer 1 /
# Iteration 5). The pipeline reads the current ngrok URL at runtime via
# resolve_endpoint_base_url(), so resume links always point at the live tunnel
# with no config edits.
#
# Usage:
#   ./scripts/start_bot.sh            # default: every 40 minutes
#   ./scripts/start_bot.sh 90         # every 90 minutes
#   ./scripts/start_bot.sh 0          # run once, then exit (no loop)
#
# Stop: Ctrl+C  — stops the loop and the ngrok it started; leaves the
#                 endpoint running (so existing resume links keep working).

set -uo pipefail
cd "$(dirname "$0")/.."

INTERVAL_MIN="${1:-40}"
NGROK_STARTED=0
NGROK_PID=""

# Reserved (static) ngrok domain — bind to it so every resume link, old and
# new, points at the same permanent URL across ngrok/laptop restarts. Must
# match config.yaml endpoint.base_url. Free tier includes one such domain.
NGROK_DOMAIN="murky-bonding-epileptic.ngrok-free.dev"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cleanup() {
  echo
  log "stopping runner loop"
  if [ "$NGROK_STARTED" = "1" ] && [ -n "$NGROK_PID" ]; then
    log "stopping ngrok (pid $NGROK_PID)"
    kill "$NGROK_PID" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup INT TERM

# --- 1. resume endpoint (idempotent) ---------------------------------------
log "starting resume endpoint"
docker compose up -d endpoint

# --- 2. ngrok --------------------------------------------------------------
if curl -s --max-time 2 http://localhost:4040/api/tunnels >/dev/null 2>&1; then
  log "ngrok already running — reusing it"
elif command -v ngrok >/dev/null 2>&1; then
  log "starting ngrok on static domain ${NGROK_DOMAIN}"
  ngrok http --url="https://${NGROK_DOMAIN}" 8000 >/dev/null 2>&1 &
  NGROK_PID=$!
  NGROK_STARTED=1
  sleep 3
else
  log "WARNING: ngrok not installed — resume links will fall back to config.yaml base_url"
fi

# best-effort: show the public URL the pipeline will embed
URL="$(curl -s --max-time 2 http://localhost:4040/api/tunnels 2>/dev/null \
  | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(next(t["public_url"] for t in d["tunnels"] if t["proto"]=="https"))
except Exception:
    pass' 2>/dev/null)"
[ -n "$URL" ] && log "public resume URL: $URL"

# --- 3. pipeline loop ------------------------------------------------------
run_pipeline() {
  log "pipeline run starting (live)"
  docker compose run --rm pipeline || log "pipeline run failed (continuing)"
}

if [ "$INTERVAL_MIN" -eq 0 ] 2>/dev/null; then
  run_pipeline
  log "single run complete (interval=0)"
  cleanup
fi

INTERVAL_SEC=$(( INTERVAL_MIN * 60 ))
log "pipeline loop every ${INTERVAL_MIN} min — Ctrl+C to stop"
while true; do
  run_pipeline
  log "sleeping ${INTERVAL_MIN} min"
  sleep "$INTERVAL_SEC"
done
