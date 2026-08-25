#!/usr/bin/env bash
# Nomos single-command startup:
#   1. Create the uv-managed venv if missing and install pinned dependencies
#   2. Run idempotent DB migrations
#   3. Start uvicorn (single worker — required: in-process pub/sub state)
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

if command -v uv >/dev/null 2>&1; then
    [ -d "$VENV" ] || uv venv --python 3.11 "$VENV" 2>/dev/null || uv venv "$VENV"
    uv pip install --python "$PY" -q -r requirements.txt
else
    echo "uv not found — falling back to python3 -m venv (install uv for faster setup)"
    [ -d "$VENV" ] || python3 -m venv "$VENV"
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q -r requirements.txt
fi

if [ -f .env ] && grep -q '^AGENTCOMMS_' .env; then
    echo "WARNING: .env contains legacy AGENTCOMMS_* variables — they are ignored." >&2
    echo "         Rename them to NOMOS_* (see .env.example)." >&2
fi
[ -f .env ] && set -a && source .env && set +a

"$PY" -m server.migrate

echo "Nomos starting on ${NOMOS_HOST:-127.0.0.1}:${NOMOS_PORT:-8484} (Ctrl-C for graceful shutdown)"
exec "$PY" -m uvicorn server.main:app \
    --host "${NOMOS_HOST:-127.0.0.1}" \
    --port "${NOMOS_PORT:-8484}" \
    --workers 1 \
    --no-access-log
