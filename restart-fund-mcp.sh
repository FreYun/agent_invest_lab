#!/usr/bin/env bash
# Start fund-portfolio-mcp in READONLY mode.
# Bots in the lab read holdings/orders/snapshots via this MCP; writes are
# performed post-bot by scripts/fund_md_to_db.py (which uses admin DB access
# directly, no admin-port MCP needed in lab mode).
set -euo pipefail

PORT="${FUND_MCP_PORT:-28071}"
LAB="$(cd "$(dirname "$0")" && pwd)"

# Tell the MCP where the lab root + DB live (overrides /home/rooot/.openclaw default).
export OPENCLAW_ROOT="$LAB"
export FUND_DB_PATH="$LAB/data/fund.db"
export FUND_MCP_READONLY=1

# Kill any prior instance on this port.
lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

cd "$LAB/fund-portfolio-mcp"
nohup python3 server.py --transport streamable-http --port "$PORT" \
  > /tmp/fund-portfolio-mcp-readonly.log 2>&1 &
PID=$!
echo "fund-portfolio-mcp (readonly) started on :$PORT (pid $PID)"
echo "  OPENCLAW_ROOT  = $OPENCLAW_ROOT"
echo "  FUND_DB_PATH   = $FUND_DB_PATH"
echo "  log            = /tmp/fund-portfolio-mcp-readonly.log"
