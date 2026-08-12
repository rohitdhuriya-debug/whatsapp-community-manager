#!/bin/bash
# Start everything and expose the dashboard on a public HTTPS link.
#
# Only port 8080 (the dashboard) is tunnelled. Port 3000 is WAHA, which holds
# your live WhatsApp session - it stays on loopback and is never exposed.
#
# Double-click in Finder, or run ./start_public.command
set -e

cd "$(dirname "$0")"
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin"

echo "──────────────────────────────────────────────"
echo " Upsurge WhatsApp Manager — public link"
echo "──────────────────────────────────────────────"

[ -f .env ] || { echo "ERROR: .env missing. Copy .env.example to .env first."; exit 1; }
command -v cloudflared >/dev/null || { echo "ERROR: cloudflared not found. brew install cloudflared"; exit 1; }

# 1 · Docker + WAHA
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop…"
  open -a Docker || { echo "Could not launch Docker Desktop."; exit 1; }
  printf "Waiting for Docker"
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; printf "."; sleep 2; done
  echo
fi
docker compose up -d >/dev/null
echo "✓ WAHA running (loopback only)"

# 2 · venv
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip --quiet
  .venv/bin/python -m pip install -r requirements.txt
fi

# 3 · Tunnel first, so we know the URL before the app starts
echo "Opening the public tunnel…"
TUNNEL_LOG=$(mktemp /tmp/wa-tunnel.XXXXXX)
cloudflared tunnel --url http://localhost:8080 --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

PUBLIC_URL=""
for _ in $(seq 1 40); do
  PUBLIC_URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done

cleanup() {
  echo
  echo "Shutting the tunnel down…"
  kill "$TUNNEL_PID" 2>/dev/null || true
  rm -f "$TUNNEL_LOG"
}
trap cleanup EXIT INT TERM

if [ -z "$PUBLIC_URL" ]; then
  echo "Could not get a tunnel URL. Last lines:"
  tail -15 "$TUNNEL_LOG"
  exit 1
fi

echo "✓ Public link: $PUBLIC_URL"
echo
echo "  Anyone with this link can post to your communities — it has no login."
echo "  The link changes every time you run this."
echo
echo "  Keep this window open. Closing it takes the link down."
echo

# PUBLIC_URL is exported so absolute links (the Drive OAuth redirect) point at
# the tunnel rather than a localhost that only exists on this Mac.
export PUBLIC_URL

sleep 1
open "$PUBLIC_URL" 2>/dev/null || true

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
