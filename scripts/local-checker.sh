#!/usr/bin/env bash
# Runs the price checker on a fixed interval against the local Worker (wrangler dev).
# Usage: scripts/local-checker.sh [interval-seconds]   (default 300 = 5 minutes)
#
# Reads .env when present. Until the SMTP values are filled in, each run is a
# --dry-run (prices are checked and logged, nothing is written or sent).
set -u
cd "$(dirname "$0")/.."

INTERVAL="${1:-300}"
if [ -f .env ]; then set -a; source .env; set +a; fi
export ALERTS_API_URL="${LOCAL_ALERTS_API_URL:-http://127.0.0.1:8788}"
export CHECKER_TOKEN="${LOCAL_CHECKER_TOKEN:-local-checker-token-123456}"

while true; do
  if [ -n "${SMTP_USERNAME:-}" ] && [ -n "${SMTP_PASSWORD:-}" ] && [ -n "${EMAIL_TO:-}" ]; then
    .venv/bin/python -m backend.check_prices
  else
    echo "$(date '+%F %T') Email not configured in .env: running --dry-run (no email)"
    .venv/bin/python -m backend.check_prices --dry-run
  fi
  echo "$(date '+%F %T') exit=$?; next check in ${INTERVAL}s"
  sleep "$INTERVAL"
done
