#!/usr/bin/env bash
# lab 库 fund.db 每日数据追新（独立于生产 Phase A）。
#
# 直连 research-mcp，按库内 fund_info 清单刷新 nav + performance：
#   - fund_nav         最近 LOOKBACK_DAYS 天复权净值（覆盖周末/节假日/漏跑缺口，UPSERT 幂等）
#   - fund_performance 分区间业绩，as_of_date = 今天
# 不刷 fund_info（基本信息变化慢，按需手动 / 月度跑全量）。
#
# 由 crontab 每天调用；flock 防止与上一轮重叠。
set -euo pipefail

LAB="/home/rooot/agent_invest_lab"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-10}"
LOCK="/tmp/fund-lab-daily-refresh.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 上一轮仍在运行，跳过本次" >&2
  exit 0
fi

cd "$LAB"
exec python3 scripts/fund-pool-ingest.py \
  --from-db \
  --lookback-days "$LOOKBACK_DAYS" \
  --skip-info \
  "$@"
