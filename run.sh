#!/bin/bash
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
if ! [[ "${PORT}" =~ ^[0-9]+$ ]]; then
    echo "Error: PORT must be a number" >&2
    exit 1
fi
URL="http://localhost:${PORT}"

# Start the server in the background
.venv/bin/python main.py &
SERVER_PID=$!

# Wait for the server to be ready (up to 15 seconds)
echo "Waiting for server to start..."
for i in $(seq 1 30); do
    if curl -sf "${URL}" > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# Open the default browser
if command -v open > /dev/null 2>&1; then
    open "${URL}"
elif command -v xdg-open > /dev/null 2>&1; then
    xdg-open "${URL}"
fi

# Bring server to foreground
wait $SERVER_PID
