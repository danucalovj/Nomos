#!/usr/bin/env bash
# Gracefully stop the Nomos server (SIGTERM -> uvicorn graceful shutdown,
# which WAL-checkpoints SQLite on exit). Ctrl-C in the start.sh terminal does
# the same thing.
set -euo pipefail
cd "$(dirname "$0")"

# Port resolution: env var wins; else .env's NOMOS_PORT if present (cut after
# the FIRST '=' only, so values containing '=' survive); else the default.
# The old logic blanked PORT whenever .env existed without the key.
PORT="${NOMOS_PORT:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(grep -E '^NOMOS_PORT=' .env | head -1 | cut -d= -f2- || true)"
fi
PORT="${PORT:-8484}"

if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof is required to find the server process; install it or stop the server manually." >&2
    exit 1
fi

PIDS="$(lsof -ti tcp:"$PORT" -s tcp:LISTEN 2>/dev/null || true)"
if [ -z "$PIDS" ]; then
    echo "No Nomos server listening on port $PORT."
    exit 0
fi
kill -TERM $PIDS
echo "Sent SIGTERM to PID(s): $PIDS — server will checkpoint and exit."
