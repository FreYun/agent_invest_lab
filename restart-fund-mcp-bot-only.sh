#!/usr/bin/env bash
# Start fund-portfolio-mcp in BOT_ONLY mode for world backtest bots.
set -euo pipefail

PORT="${FUND_MCP_PORT:-28172}"
LAB="$(cd "$(dirname "$0")" && pwd)"

# System python and fund-portfolio-mcp/.venv may not have the mcp package on
# this host. Use the shared MCP venv by default, but allow explicit override.
PYTHON="${FUND_MCP_PYTHON:-/opt/MCP/.venv/bin/python}"
if ! "$PYTHON" -c "import mcp" 2>/dev/null; then
    echo "ERROR: $PYTHON 缺少 mcp 包，无法启动 fund-portfolio-mcp bot-only 端" >&2
    echo "       请设置 FUND_MCP_PYTHON 指向装有 mcp[cli] 的 Python 解释器" >&2
    exit 1
fi

export OPENCLAW_ROOT="$LAB"
export FUND_DB_PATH="$LAB/data/fund.db"
export FUND_BUYABLE_CODES_DIR="$LAB/data/buyable"
export FUND_MCP_BOT_ONLY=1
unset FUND_MCP_READONLY

# Kill any prior instance on this port.
lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

cd "$LAB/fund-portfolio-mcp"
nohup "$PYTHON" server.py --transport streamable-http --port "$PORT" \
  > /tmp/fund-portfolio-mcp-bot-only.log 2>&1 &
PID=$!
echo "fund-portfolio-mcp (bot-only) started on :$PORT (pid $PID)"
echo "  PYTHON         = $PYTHON"
echo "  OPENCLAW_ROOT  = $OPENCLAW_ROOT"
echo "  FUND_DB_PATH   = $FUND_DB_PATH"
echo "  log            = /tmp/fund-portfolio-mcp-bot-only.log"
