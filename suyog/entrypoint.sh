#!/bin/bash
set -e

# Start virtual display (required for UC mode — headless=True is detectable)
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
export DISPLAY=:99
sleep 1

exec uvicorn suyog.api:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1
