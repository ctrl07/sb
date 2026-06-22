#!/bin/bash
set -e

echo "[entrypoint] Starting Xvfb..."
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:99
sleep 2

if kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[entrypoint] Xvfb running (pid $XVFB_PID)"
else
    echo "[entrypoint] WARNING: Xvfb failed to start — continuing anyway"
fi

echo "[entrypoint] Starting uvicorn on port ${PORT:-8000}..."
exec uvicorn suyog.api:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1
