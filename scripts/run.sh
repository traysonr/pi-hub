#!/usr/bin/env bash
# Run the Pi Hub FastAPI app on port 8000, listening on all interfaces so
# phones and laptops on the LAN can reach it.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

HOST="${PI_HUB_HOST:-0.0.0.0}"
PORT="${PI_HUB_PORT:-8000}"

exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
