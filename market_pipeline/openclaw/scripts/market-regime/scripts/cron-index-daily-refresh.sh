#!/usr/bin/env bash
# 每日 EOD 刷新 10 大宽基 index_daily(最近窗口, 幂等)。系统 crontab 调。
# 路径从脚本自身推导, 不写死 /home(云端前缀不同)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# repo 根 = scripts/market-regime/scripts 的上三级
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/index-daily-refresh.log"

{
  echo "===== $(date '+%F %T') index_daily refresh 开始 ====="
  rc=0
  /usr/bin/python3.12 "$SCRIPT_DIR/backfill_index_daily.py" --all --days 20 || rc=$?
  echo "===== $(date '+%F %T') 结束 rc=$rc ====="
  exit $rc
} >> "$LOG" 2>&1
