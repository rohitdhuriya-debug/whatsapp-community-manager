#!/bin/bash
# Start everything: WAHA container + the dashboard.
# Double-click this file in Finder, or run ./start_all.command
set -e

cd "$(dirname "$0")"
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"

echo "──────────────────────────────────────────────"
echo " WhatsApp Community Manager"
echo "──────────────────────────────────────────────"

if [ ! -f .env ]; then
  echo "ERROR: .env is missing. Copy .env.example to .env and fill it in."
  exit 1
fi

# 1. Docker daemon
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Starting Docker Desktop…"
  open -a Docker || { echo "Could not launch Docker Desktop. Start it manually."; exit 1; }
  printf "Waiting for Docker"
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then break; fi
    printf "."
    sleep 2
  done
  echo
  docker info >/dev/null 2>&1 || { echo "Docker did not start in time."; exit 1; }
fi
echo "✓ Docker is running"

# 2. WAHA
docker compose up -d
echo "✓ WAHA container up on http://localhost:3000"

# 3. Dashboard
if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3.12 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip --quiet
  .venv/bin/python -m pip install -r requirements.txt
fi

echo "✓ Starting dashboard on http://localhost:8080"
echo
echo "  Press Ctrl+C to stop the dashboard (WAHA keeps running)."
echo "  Run ./stop_all.command to stop everything."
echo

sleep 1
open http://localhost:8080 2>/dev/null || true

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
