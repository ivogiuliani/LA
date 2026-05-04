#!/bin/bash
# My Villa — Review Server
# Double-click this file to start the review dashboard.
#
# What it does:
#  1. Stops any server already running on port 8787
#  2. Starts approve.py in the background
#  3. Opens http://localhost:8787 in your default browser
#
# To stop the server later: close this Terminal window, or run:
#   lsof -ti :8787 | xargs kill -9

set -e
cd "$(dirname "$0")"

PORT=8787

echo ""
echo "════════════════════════════════════════════════"
echo "  My Villa — Review Dashboard"
echo "════════════════════════════════════════════════"
echo ""

# Stop any existing instance
EXISTING=$(lsof -ti :$PORT 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
    echo "  → Stopping previous server (PID: $EXISTING)..."
    echo "$EXISTING" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

echo "  → Starting review server on port $PORT..."
echo "  → Opening http://localhost:$PORT in your browser..."
echo ""

# Open browser shortly after starting the server
(sleep 2 && open "http://localhost:$PORT/") &

# Start the server (keeps running until Terminal is closed)
exec python3 _system/scripts/approve.py --no-browser --port $PORT
