#!/usr/bin/env bash
# Refresh usage data + open dashboard in browser.
# Usage: ./open.sh [days]   (default: 30)
set -e
cd "$(dirname "$0")"

DAYS="${1:-30}"
PORT=8765

echo "→ Generating usage data for last ${DAYS} days..."
python3 usage_breakdown.py --json --days "$DAYS" --out data.json

# Kill any prior server on this port
if lsof -ti:$PORT >/dev/null 2>&1; then
  lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
fi

echo "→ Serving on http://localhost:${PORT}/"
python3 -m http.server $PORT >/tmp/cc-usage-dashboard.log 2>&1 &
SRV_PID=$!

# Wait until server is actually listening (max 5s)
for i in {1..50}; do
  if nc -z localhost $PORT 2>/dev/null; then break; fi
  sleep 0.1
done
if ! nc -z localhost $PORT 2>/dev/null; then
  echo "ERROR: server failed to start. See /tmp/cc-usage-dashboard.log"
  exit 1
fi

# Open browser (macOS: open, Linux: xdg-open)
if command -v open >/dev/null 2>&1; then
  open "http://localhost:${PORT}/"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:${PORT}/"
fi

echo "Dashboard running (PID $SRV_PID). Logs: /tmp/cc-usage-dashboard.log"
echo "To stop: kill $SRV_PID"
