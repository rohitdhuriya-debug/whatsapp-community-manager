#!/bin/bash
# Stop the dashboard and the WAHA container.
# Your WhatsApp pairing survives - it lives in ./waha-sessions.

cd "$(dirname "$0")"
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"

echo "Stopping dashboard…"
pkill -f "uvicorn app.main:app" 2>/dev/null && echo "✓ Dashboard stopped" || echo "· Dashboard was not running"

echo "Stopping WAHA…"
if docker info >/dev/null 2>&1; then
  docker compose down
  echo "✓ WAHA stopped (your WhatsApp pairing is saved in ./waha-sessions)"
else
  echo "· Docker is not running, nothing to stop"
fi

echo
echo "Done. Run ./start_all.command to bring everything back."
