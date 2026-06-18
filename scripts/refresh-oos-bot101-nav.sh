#!/usr/bin/env bash
set -euo pipefail

# OOS bot101 净值同步（每天 fund_nav 刷新之后跑）。
#
# 时序背景：
#   - lab-fund-daily-refresh cron 每天 06:17 把 T 日真实净值落进 fund_nav（T+1 早晨才有 T 日净值）。
#   - run-oos-bot101-daily.sh 在 T 日 08:00 跑 bot 并落 T 日快照，此刻 T 日净值还没出，
#     _compute_fund_snapshot 用 nav_date<=T 兜底 → 快照里的市值/净值是 T-1 的旧净值，
#     且之后没人回头重算；pending 订单也只在下一次 bot run 时才 T+1 收口。
#
# 本脚本在净值落库后补一遍「系统侧每日开盘+收盘」：
#   对 [窗口起点 .. fund_nav 最新日] 内的每个交易日 D（升序）：
#     1. settle_pending_orders --as-of-date D   T+1 收口（幂等，settled 单不再 pending）
#     2. close_my_day          --trade-date D   用现已可得的真实净值重算/补落 D 日快照
#        （bot 没跑的纯估值日——周一 skip、cron 漏跑——也会补出 mark-to-market 快照）
#     3. 重新镜像 D 日到 oos_* 表（看板 /api/oos/bot101 的数据源）
#
# 窗口起点 = max(本 run 首个活动日, 最新净值日往前 LOOKBACK_TRADING_DAYS 个交易日)，
# 全程幂等，重复跑收敛到同一结果。

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
RUN_ID="${OOS_BOT101_RUN_ID:-oos-bot101-daily}"
BOT_ID="bot101"
LOCK="${LOCK:-/tmp/refresh-oos-bot101-nav.lock}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
OOS_TABLES_SQL="${OOS_TABLES_SQL:-$ROOT_DIR/scripts/sql/oos_bot101_daily.sql}"
LOOKBACK_TRADING_DAYS="${LOOKBACK_TRADING_DAYS:-10}"
CLI="$ROOT_DIR/fund-portfolio-mcp/cli_tools.py"

# 与 world/src/run.ts runFundCli 同款解释器选择：裸 python3 在本机缺 mcp 包会秒挂。
VENV_PY="$ROOT_DIR/fund-portfolio-mcp/.venv/bin/python"
PYTHON="${FUND_MCP_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$VENV_PY" ]]; then PYTHON="$VENV_PY"; else PYTHON="python3"; fi
fi
# cli_tools.py → db.py 缺省指向 .openclaw 的另一套库；显式钉到本仓库 fund.db。
export OPENCLAW_ROOT="${OPENCLAW_ROOT:-$ROOT_DIR}"
export FUND_DB_PATH="$DB_PATH"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] previous nav refresh still active; skip" >&2
  exit 0
fi

mkdir -p "$LOG_DIR"

source "$ROOT_DIR/scripts/oos-bot101-mirror.lib.sh"
ensure_oos_tables

NAV_MAX="$("$SQLITE3_BIN" "$DB_PATH" "SELECT MAX(nav_date) FROM fund_nav;")"
if [[ ! "$NAV_MAX" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "[$(date '+%F %T')] fund_nav empty or bad MAX(nav_date)='$NAV_MAX'; abort" >&2
  exit 1
fi

# 本 run 首个活动日（快照/订单/动作里最早的日期）；run 还没任何活动则无事可做。
FIRST_DATE="$("$SQLITE3_BIN" "$DB_PATH" "
  SELECT MIN(d) FROM (
    SELECT MIN(trade_date) AS d FROM fund_bot_daily_snapshots WHERE bot_id='$BOT_ID' AND run_id='$RUN_ID'
    UNION ALL SELECT MIN(order_date) FROM fund_bot_orders WHERE bot_id='$BOT_ID' AND order_run_id='$RUN_ID'
    UNION ALL SELECT MIN(action_date) FROM fund_bot_actions WHERE bot_id='$BOT_ID' AND run_id='$RUN_ID'
  ) WHERE d IS NOT NULL;")"
if [[ -z "$FIRST_DATE" ]]; then
  echo "[$(date '+%F %T')] run $RUN_ID has no activity yet; nothing to refresh"
  exit 0
fi

# 窗口内交易日列表（升序）：直接用 fund_nav 的 nav_date 当交易日——净值已落库的日子
# 必然是交易日。不用 calendar.json：它由 09:30 的 world-calendar-refresh.timer 推进，
# 比 06:17 的净值落库晚一拍，按它取窗口会漏掉最新一个交易日。
mapfile -t DATES < <("$SQLITE3_BIN" "$DB_PATH" "
  SELECT nav_date FROM (
    SELECT DISTINCT nav_date FROM fund_nav
    WHERE nav_date <= '$NAV_MAX' AND nav_date >= '$FIRST_DATE'
    ORDER BY nav_date DESC LIMIT $LOOKBACK_TRADING_DAYS
  ) ORDER BY nav_date ASC;")

if [[ "${#DATES[@]}" -eq 0 ]]; then
  echo "[$(date '+%F %T')] no trading days in window (nav_max=$NAV_MAX first=$FIRST_DATE); nothing to refresh"
  exit 0
fi

echo "[$(date '+%F %T')] nav refresh start run_id=$RUN_ID nav_max=$NAV_MAX window=${DATES[0]}..${DATES[-1]} (${#DATES[@]} trading days)"

# 两遍走：先把整个窗口结算+重算完，再统一镜像。镜像不能跟在每天后面——
# D+1 的 settle 会改 D 日订单的 status/confirm_date，若 D 日已先镜像就会停在 pending。
for D in "${DATES[@]}"; do
  "$PYTHON" "$CLI" settle_pending_orders --bot-id "$BOT_ID" --run-id "$RUN_ID" --as-of-date "$D" >/dev/null
  "$PYTHON" "$CLI" close_my_day          --bot-id "$BOT_ID" --run-id "$RUN_ID" --trade-date "$D" >/dev/null
  echo "[$(date '+%F %T')]   $D settled + snapshot recomputed"
done
for D in "${DATES[@]}"; do
  mirror_oos_results_for_date "$D"
done
echo "[$(date '+%F %T')]   mirrored ${#DATES[@]} days into oos_* tables"

echo "[$(date '+%F %T')] nav refresh done; latest snapshot:"
"$SQLITE3_BIN" -header -column "$DB_PATH" "
  SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value,
         ROUND(cumulative_return_pct,4) AS cum_ret_pct, ROUND(cash_weight*100,2) AS cash_pct
  FROM oos_bot_daily_snapshots
  WHERE live_run_id='$RUN_ID' AND bot_id='$BOT_ID'
  ORDER BY trade_date DESC LIMIT 3;"
