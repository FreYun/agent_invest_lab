#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
CALENDAR_PATH="${CALENDAR_PATH:-$WORLD_DIR/runtime/calendar.json}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
RUN_ID="${OOS_BOT101_RUN_ID:-oos-bot101-daily}"
LOCK="${LOCK:-/tmp/oos-bot101-daily.lock}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
OOS_TABLES_SQL="${OOS_TABLES_SQL:-$ROOT_DIR/scripts/sql/oos_bot101_daily.sql}"
REQUIRE_PREV_TRADING="${REQUIRE_PREV_TRADING:-1}"
REQUIRE_TARGET_TRADING="${REQUIRE_TARGET_TRADING:-1}"

today_date() {
  date '+%F'
}

# calendar 里 <= 给定日 的最新交易日。simworld 报价"15:00 收盘后可见"→ calendar 最新只到
# 昨天收盘，系统当天通常不在其中。oos 在 T+1 早晨用此函数取"数据已就绪的最新交易日 L"作为
# 要决策的 T 日（而非系统当天——当天数据还没出、也不在 calendar，driver 会直接报错）。
latest_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<=t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

# calendar 里严格早于给定日的最新交易日（前一交易日）。周一→上周五，正确跨过周末/节假日。
prev_trade_date() {
  node -e 'const fs=require("fs");const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days;const t=process.argv[2];let r="";for(let i=c.length-1;i>=0;i--){if(c[i]<t){r=c[i];break}}if(r)console.log(r);else process.exit(1)' "$CALENDAR_PATH" "$1"
}

is_trading_day() {
  node -e 'const fs=require("fs"); const cal=new Set(JSON.parse(fs.readFileSync(process.argv[1],"utf8")).trading_days); process.exit(cal.has(process.argv[2]) ? 0 : 1)' "$CALENDAR_PATH" "$1"
}

# 无显式日期时取 calendar 最新交易日（数据已就绪的 T 日），而不是系统当天。
# 系统当天 08:00 盘前既无数据也不在 calendar，driver 必报错——这是旧版每天 skip 的根因。
TRADE_DATE="${1:-${TRADE_DATE:-}}"
if [[ -z "$TRADE_DATE" ]]; then
  TRADE_DATE="$(latest_trade_date "$(today_date)")" || { echo "no trading day <= $(today_date) in calendar $CALENDAR_PATH" >&2; exit 2; }
fi
if [[ ! "$TRADE_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "bad TRADE_DATE: $TRADE_DATE" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "bad OOS_BOT101_RUN_ID: $RUN_ID" >&2
  exit 2
fi

# 需要存在"前一交易日"作为净值基线。用 calendar 找前一交易日，周一会正确跨到上周五，
# 而不是旧逻辑里"日历前一天=周日→每周一误 skip"。
if [[ "$REQUIRE_PREV_TRADING" == "1" ]] && ! prev_trade_date "$TRADE_DATE" >/dev/null; then
  echo "[$(date '+%F %T')] skip: no prior trading day before $TRADE_DATE in calendar"
  exit 0
fi
if [[ "$REQUIRE_TARGET_TRADING" == "1" ]] && ! is_trading_day "$TRADE_DATE"; then
  echo "[$(date '+%F %T')] skip: target day $TRADE_DATE is not a trading day"
  exit 0
fi

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-$LOG_DIR/oos-bot101-daily-${TRADE_DATE}.log}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] previous oos bot101 daily run is still active; skip" >&2
  exit 0
fi

exec > >(tee -a "$LOG_FILE") 2>&1

# ensure_oos_tables / mirror_oos_results_for_date 共享实现（refresh-oos-bot101-nav.sh 同源）。
source "$ROOT_DIR/scripts/oos-bot101-mirror.lib.sh"

mirror_oos_results() {
  ensure_oos_tables
  mirror_oos_results_for_date "$TRADE_DATE"
}

summary() {
  local status="$1"
  echo
  echo "===== OOS bot101 daily health summary ====="
  echo "finished_at: $(date '+%F %T %z')"
  echo "status: $status"
  echo "prepass_market_reports_rc: ${prepass_rc:-NA}"
  echo "report_date: $TRADE_DATE"
  echo "bot_id: bot101"
  echo "live_run_id: $RUN_ID"
  echo "tables: oos_market_report_status, oos_bot_daily_snapshots, oos_bot_position_snapshots, oos_bot_orders, oos_bot_actions"
  echo
  echo "-- reports"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT report_type, as_of_date, generated_at, chars FROM oos_market_report_status WHERE live_run_id='$RUN_ID' AND as_of_date='$TRADE_DATE' ORDER BY report_type;" || true
  echo
  echo "-- bot101 orders"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT order_date, order_type, fund_code, ROUND(order_amount,2) AS amount, ROUND(reference_nav,4) AS nav, status, substr(coalesce(action_reason,''),1,80) AS reason FROM oos_bot_orders WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND order_date='$TRADE_DATE' ORDER BY source_order_id;" || true
  echo
  echo "-- bot101 settled actions"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT action_date, action_type, fund_code, ROUND(amount,2) AS amount, ROUND(shares,4) AS shares, ROUND(nav_used,4) AS nav, substr(coalesce(reason,''),1,80) AS reason FROM oos_bot_actions WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND action_date='$TRADE_DATE' ORDER BY source_action_id;" || true
  echo
  echo "-- bot101 positions"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, fund_code, ROUND(weight * 100, 2) AS weight_pct, ROUND(market_value, 2) AS market_value, ROUND(shares, 4) AS shares FROM oos_bot_position_snapshots WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND trade_date='$TRADE_DATE' ORDER BY weight DESC, fund_code;" || true
  echo
  echo "-- bot101 nav"
  "$SQLITE3_BIN" -header -column "$DB_PATH" "SELECT trade_date, ROUND(total_value,2) AS total_value, ROUND(net_value,6) AS net_value, ROUND(cumulative_return_pct,4) AS cumulative_return_pct, ROUND(cash_weight * 100, 2) AS cash_weight_pct FROM oos_bot_daily_snapshots WHERE bot_id='bot101' AND live_run_id='$RUN_ID' AND trade_date='$TRADE_DATE';" || true
  echo "===== end summary ====="
}

status=failed
trap 'summary "$status"' EXIT

ensure_oos_tables

echo "[$(date '+%F %T')] OOS bot101 daily start date=$TRADE_DATE live_run_id=$RUN_ID log=$LOG_FILE"
# prepass(市场研报) 非致命：某份研报上游(LLM/数据)瞬时抖动失败时，只告警不 abort。
# 研报有午夜批预生成兜底在库，且 bot 走 get_market_report 的 PIT 提取（取 as_of<=世界日的最新一期），
# 缺当日某份最多回退到前一日；bot 的「当日决策」才是难复算的核心产物，绝不能被一份研报连坐掐掉。
prepass_rc=0
RUN_ID="market-reports-daily-${TRADE_DATE}" "$ROOT_DIR/scripts/run-oos-market-reports-daily.sh" "$TRADE_DATE" || prepass_rc=$?
if [[ "$prepass_rc" != "0" ]]; then
  echo "[$(date '+%F %T')] WARN: prepass(market-reports) rc=$prepass_rc —— 某份研报可能缺/未更新；继续跑 bot 决策（bot 走 PIT 读 DB，午夜批兜底）" >&2
fi

# 判"bot 当天是否真决策过"用 driver 跑痕迹（runtime/runs/<run>/<date> 目录），而不是
# fund_bot_daily_snapshots 有没有快照——nav-sync 会给未决策日造 mark-to-market 快照，
# 用快照判会让 daily 永远以为"已决策"而 skip，bot 决策永远跑不起来。driver 跑过才有该目录，
# nav-sync 不碰 runtime → 干净区分"真决策" vs "mark-to-market 估值"。
if [[ "${FORCE_BOT:-0}" != "1" ]] && [[ -d "$WORLD_DIR/runtime/runs/$RUN_ID/$TRADE_DATE" ]]; then
  mirror_oos_results
  echo "[$(date '+%F %T')] bot101 already ran a real decision for $TRADE_DATE (run dir exists); skip bot stage (set FORCE_BOT=1 to rerun)"
  status=ok
  exit 0
fi

(
  cd "$WORLD_DIR"
  OOS_BOT101_RUN_ID="$RUN_ID" TRADE_DATE="$TRADE_DATE" node --experimental-strip-types src/oos-daily-driver.ts \
    --date "$TRADE_DATE" \
    --run-id "$RUN_ID" \
    --config config/world-multi-fund-backtest.yaml \
    --world-dir runtime
)
mirror_oos_results
status=ok
echo "[$(date '+%F %T')] OOS bot101 daily done date=$TRADE_DATE live_run_id=$RUN_ID"
