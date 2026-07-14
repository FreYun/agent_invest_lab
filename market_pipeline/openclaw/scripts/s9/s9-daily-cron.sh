#!/usr/bin/env bash
# S9 — 大市值 regime 自适应选股 每日 cron 入口
#
# 依赖链:
#   21:00 daily-regime-pipeline.sh  → daily / index_daily
#   21:35 stock-select-daily.sh      → daily_basic (含 total_mv)
# 只读市场数据, 写 s9_candidates / s9_select_runs.
#
# crontab 示例 (排在 stock-select-daily 之后, 与 s8 并列):
#   45 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh s9-daily-cron /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s9/s9-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/s9-daily.log 2>&1

set -uo pipefail
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1

TODAY=$(date +%Y-%m-%d)
TODAY_FMT=$(date +%Y%m%d)
LOG_PREFIX="[s9-daily $(date +%Y-%m-%d_%H:%M:%S)]"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${MARKET_DB_PATH:-/home/rooot/agent_invest_lab/data/market.db}"
PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"

echo ""
echo "$LOG_PREFIX ========== 开始 (date=$TODAY) =========="

HAS_DAILY=$(sqlite3 "$DB" "SELECT COUNT(*) FROM daily WHERE trade_date='$TODAY_FMT'" 2>/dev/null || echo "0")
if [ "$HAS_DAILY" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 无 daily 数据 (非交易日 or pipeline 未跑完), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi

HAS_BASIC=$(sqlite3 "$DB" "SELECT COUNT(*) FROM daily_basic WHERE trade_date='$TODAY_FMT'" 2>/dev/null || echo "0")
if [ "$HAS_BASIC" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 无 daily_basic 数据 (stock-select-daily 未跑完?), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi

HAS_HS300=$(sqlite3 "$DB" "SELECT COUNT(*) FROM index_daily WHERE ts_code='000300.SH' AND trade_date='$TODAY_FMT'" 2>/dev/null || echo "0")
if [ "$HAS_HS300" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 无沪深300 index_daily 数据 (S9 regime 开关依赖), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi
echo "$LOG_PREFIX 数据就绪: daily=$HAS_DAILY 行, daily_basic=$HAS_BASIC 行, 沪深300 ok"

echo "$LOG_PREFIX select.py --date=$TODAY"
if "$PY" "$SCRIPT_DIR/select.py" --date="$TODAY" 2>&1; then
    echo "$LOG_PREFIX S9 select OK"
else
    RC=$?
    echo "$LOG_PREFIX S9 select 失败 (rc=$RC)" >&2
fi

echo "$LOG_PREFIX 今日结果:"
sqlite3 "$DB" <<SQL 2>/dev/null
.mode column
.headers on
SELECT '$TODAY' AS date, regime, sleeve,
       ROUND(regime_signal*100,2) AS signal_pct,
       ma_window, kept_count, universe_size
FROM s9_select_runs WHERE date='$TODAY';
SQL

echo "$LOG_PREFIX ========== 结束 =========="
