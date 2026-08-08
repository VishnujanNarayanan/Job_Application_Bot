#!/usr/bin/env bash
#
# Local runner — brings up the endpoint + dashboard and makes it reachable
# from the operator's phone.
#
#   1. starts the always-on resume endpoint / dashboard (Docker, loopback)
#   2. checks that `tailscale serve` is publishing it to the tailnet
#   3. optionally loops the pipeline every N minutes
#
# Pipeline runs are normally started from the dashboard's Run button or the
# GitHub Actions workflow (which works with the laptop shut), so the loop is
# off by default — pass an interval to re-enable it.
#
# Usage:
#   ./scripts/start_bot.sh          # endpoint only, no pipeline loop
#   ./scripts/start_bot.sh 90       # ...and run the pipeline every 90 minutes
#   ./scripts/start_bot.sh once     # ...and run the pipeline once, then exit
#
# Stop: Ctrl+C — stops the loop and leaves the endpoint running, so existing
#                resume links keep working.

set -uo pipefail
cd "$(dirname "$0")/.."

INTERVAL_MIN="${1:-0}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cleanup() {
  echo
  log "stopping runner loop (endpoint left running)"
  exit 0
}
trap cleanup INT TERM

# --- 1. endpoint + dashboard (idempotent) ----------------------------------
log "starting endpoint + dashboard"
docker compose up -d endpoint

# --- 2. tailscale ----------------------------------------------------------
# The container binds 127.0.0.1 only (see docker-compose.yml). `tailscale
# serve` is what makes it reachable from the phone, and only from tailnet
# devices — the endpoint has no authentication of its own.
if ! command -v tailscale >/dev/null 2>&1; then
  log "WARNING: tailscale not installed — dashboard is localhost-only."
  log "         install it, then: tailscale serve --bg 8000"
elif ! tailscale status >/dev/null 2>&1; then
  log "WARNING: tailscale is installed but not connected — run: tailscale up"
elif tailscale serve status 2>/dev/null | grep -q "8000"; then
  DNSNAME="$(tailscale status --json 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)"
  [ -n "$DNSNAME" ] && log "dashboard: https://${DNSNAME}/dashboard" \
                    || log "dashboard published over tailscale"
else
  log "tailscale is up but not serving port 8000 — publishing it now"
  tailscale serve --bg 8000 \
    && log "published; set endpoint.base_url in config/config.yaml to this host's .ts.net name" \
    || log "WARNING: could not publish — run manually: tailscale serve --bg 8000"
fi

# --- 3. optional pipeline loop ---------------------------------------------
run_pipeline() {
  log "pipeline run starting (live)"
  docker compose run --rm pipeline || log "pipeline run failed (continuing)"
}

if [ "$INTERVAL_MIN" = "once" ]; then
  run_pipeline
  log "single run complete"
  cleanup
fi

if [ "$INTERVAL_MIN" -eq 0 ] 2>/dev/null; then
  log "endpoint is up; no pipeline loop requested"
  log "run the pipeline from the dashboard, or: ./scripts/start_bot.sh 90"
  exit 0
fi

INTERVAL_SEC=$(( INTERVAL_MIN * 60 ))
log "pipeline loop every ${INTERVAL_MIN} min — Ctrl+C to stop"
while true; do
  run_pipeline
  log "sleeping ${INTERVAL_MIN} min"
  sleep "$INTERVAL_SEC"
done
