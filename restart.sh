#!/usr/bin/env bash
set -euo pipefail

PORT=18078
DIR="$(cd "$(dirname "$0")" && pwd)"

lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

nohup python3 "$DIR/ttjj_data_pit_mcp.py" --port "$PORT" > /tmp/ttjj-data-pit-mcp.log 2>&1 &
echo "ttjj-data-pit-mcp started on :$PORT (pid $!)"
