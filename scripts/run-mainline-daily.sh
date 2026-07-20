#!/usr/bin/env bash
set -euo pipefail

# 日度主线（market_mainline_daily + mainline_rotation_daily，skill 日度纪律状态机）每日生成 + 补漏。
#
# 设计：盘后运行（board_trend_daily 当日行由 scout 盘中采集、15:37 prune 后定稿），
# 每次跑「最近 10 个交易日」窗口（幂等跳过已存在），漏跑/数据迟到自动补齐；
# 最后校验最新交易日已落库，缺失则退出非 0（cron 日志可见）。
# 与月度 market_mainline / prepass / oos-bot101 管线完全独立，互不影响。

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORLD_DIR="$ROOT_DIR/world"
DB_PATH="${FUND_DB_PATH:-$ROOT_DIR/data/fund.db}"
SCOUT_DB="${SCOUT_DB:-$ROOT_DIR/data/market.db}"
SQLITE3_BIN="${SQLITE3:-sqlite3}"
LOOKBACK_TDAYS="${LOOKBACK_TDAYS:-10}"

# 日期直接以引擎数据源（scout board_trend_daily）的最新交易日为准，不走 world calendar——
# calendar 由 refresh-calendar.ts 在上游出 HS300 收盘价后才推进，16:12 时常滞后一天；
# 而 board 数据 15:37 prune 后即定稿：数据就绪即生成，无滞后也无空转。
raw_dates="$("$SQLITE3_BIN" "file:$SCOUT_DB?mode=ro" "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM board_trend_daily ORDER BY trade_date DESC LIMIT $LOOKBACK_TDAYS) ORDER BY trade_date;")"
if [[ -z "$raw_dates" ]]; then
  echo "[mainline-daily] board_trend_daily 无数据" >&2
  exit 2
fi
dash() { echo "${1:0:4}-${1:4:2}-${1:6:2}"; }
TRADE_DATE="$(dash "$(echo "$raw_dates" | tail -1)")"
WINDOW_FROM="$(dash "$(echo "$raw_dates" | head -1)")"

echo "[mainline-daily] trade_date=$TRADE_DATE window=$WINDOW_FROM..$TRADE_DATE"

cd "$WORLD_DIR"
node --experimental-strip-types src/market-reports/backfill-mainline-daily.ts \
  --from "$WINDOW_FROM" \
  --to "$TRADE_DATE" \
  --fund-db "$DB_PATH" \
  --run-id "mainline-daily-cron-$TRADE_DATE"

exists="$("$SQLITE3_BIN" "$DB_PATH" "SELECT COUNT(*) FROM market_reports WHERE report_type IN ('market_mainline_daily','mainline_rotation_daily') AND scope='global' AND as_of_date='$TRADE_DATE';")"
if [[ "$exists" != "2" ]]; then
  echo "[mainline-daily] $TRADE_DATE 未落齐两份日度报告（board_trend_daily 当日数据可能未就绪，exists=$exists）" >&2
  exit 1
fi
echo "[mainline-daily] ready for $TRADE_DATE"
