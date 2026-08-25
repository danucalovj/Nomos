#!/usr/bin/env bash
# Gracefully stop the Nomos server (SIGTERM -> uvicorn graceful shutdown,
# which WAL-checkpoints SQLite on exit). Ctrl-C in the start.sh terminal does
# the same thing.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${NOMOS_PORT:-8484}"
[ -f .env ] && PORT="$(grep -E '^NOMOS_PORT=' .env | cut -d= -f2 || true)"
PORT="${PORT:-8484}"

PIDS="$(lsof -ti tcp:"$PORT" -s tcp:LISTEN 2>/dev/null || true)"
if [ -z "$PIDS" ]; then
    echo "No Nomos server listening on port $PORT."
    exit 0
fi
kill -TERM $PIDS
echo "Sent SIGTERM to PID(s): $PIDS — server will checkpoint and exit."
