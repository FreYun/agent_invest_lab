#!/usr/bin/env bash
set -euo pipefail

PORT=18078
# 监听地址：默认 0.0.0.0 让远程回测机可直连；裸 HTTP 无鉴权，务必靠私网/隧道兜底。
# 想收回本机：TTJJ_MCP_HOST=127.0.0.1 ./restart.sh
HOST="${TTJJ_MCP_HOST:-0.0.0.0}"
DIR="$(cd "$(dirname "$0")" && pwd)"

lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

nohup python3 "$DIR/ttjj_data_pit_mcp.py" --port "$PORT" --host "$HOST" > /tmp/ttjj-data-pit-mcp.log 2>&1 &
echo "ttjj-data-pit-mcp started on :$PORT host $HOST (pid $!)"
