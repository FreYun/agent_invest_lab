#!/usr/bin/env bash
# Start fund-portfolio-mcp in READONLY mode.
# Bots in the lab read holdings/orders/snapshots via this MCP; writes are
# performed post-bot by scripts/fund_md_to_db.py (which uses admin DB access
# directly, no admin-port MCP needed in lab mode).
set -euo pipefail

PORT="${FUND_MCP_PORT:-28071}"
LAB="$(cd "$(dirname "$0")" && pwd)"

# 用哪个 Python：系统 python3 没装 mcp 包，本地 .venv 又是为 GLIBC 2.38 编译的(本机更旧,跑不起来)。
# 唯一可用的是 /opt/MCP 共享 venv(装了 mcp + requests)。允许用 FUND_MCP_PYTHON 覆盖。
PYTHON="${FUND_MCP_PYTHON:-/opt/MCP/.venv/bin/python}"
if ! "$PYTHON" -c "import mcp" 2>/dev/null; then
    echo "ERROR: $PYTHON 缺少 mcp 包，无法启动 fund-portfolio-mcp" >&2
    echo "       请设置 FUND_MCP_PYTHON 指向装有 mcp[cli] 的 Python 解释器" >&2
    exit 1
fi

# Tell the MCP where the lab root + DB live (overrides /home/rooot/.openclaw default).
export OPENCLAW_ROOT="$LAB"
export FUND_DB_PATH="$LAB/data/fund.db"
export FUND_MCP_READONLY=1

# Kill any prior instance on this port.
lsof -ti:"$PORT" | xargs -r kill 2>/dev/null || true
sleep 0.5

cd "$LAB/fund-portfolio-mcp"
nohup "$PYTHON" server.py --transport streamable-http --port "$PORT" \
  > /tmp/fund-portfolio-mcp-readonly.log 2>&1 &
PID=$!
echo "fund-portfolio-mcp (readonly) started on :$PORT (pid $PID)"
echo "  PYTHON         = $PYTHON"
echo "  OPENCLAW_ROOT  = $OPENCLAW_ROOT"
echo "  FUND_DB_PATH   = $FUND_DB_PATH"
echo "  log            = /tmp/fund-portfolio-mcp-readonly.log"
