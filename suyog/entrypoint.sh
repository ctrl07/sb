#!/bin/bash
set -e
echo "[entrypoint] Starting uvicorn on port ${PORT:-8000}..."
exec uvicorn suyog.api:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1
