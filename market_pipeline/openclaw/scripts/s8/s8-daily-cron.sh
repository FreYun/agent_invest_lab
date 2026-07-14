#!/usr/bin/env bash
# S8 — 主线分歧低吸 每日选股 (cron 入口)
#
# 依赖链 (都必须先跑完):
#   21:00 daily-regime-pipeline.sh  → daily/stk_limit/regime_classify_daily
#   21:35 stock-select-daily.sh      → daily_basic/moneyflow_daily/kpl_theme_daily/...
# 本脚本只读市场数据, 写 s8_candidates / s8_select_runs。S8 无 verify.py。
#
# crontab 示例 (排在 stock-select-daily 之后):
#   40 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh s8-daily-cron /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s8/s8-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/s8-daily.log 2>&1

set -uo pipefail
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1

TODAY=$(date +%Y-%m-%d)
TODAY_FMT=$(date +%Y%m%d)
LOG_PREFIX="[s8-daily $(date +%Y-%m-%d_%H:%M:%S)]"
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

HAS_THEME=$(sqlite3 "$DB" "SELECT COUNT(*) FROM kpl_theme_daily WHERE trade_date='$TODAY_FMT'" 2>/dev/null || echo "0")
if [ "$HAS_THEME" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 无 kpl_theme_daily 数据 (stock-select-daily 未跑完?), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi
echo "$LOG_PREFIX 数据就绪: daily=$HAS_DAILY 行, kpl_theme=$HAS_THEME 行"

echo "$LOG_PREFIX select.py --date=$TODAY"
if "$PY" "$SCRIPT_DIR/select.py" --date="$TODAY" 2>&1; then
    echo "$LOG_PREFIX S8 select OK"
else
    RC=$?
    echo "$LOG_PREFIX S8 select 失败 (rc=$RC)" >&2
fi

echo "$LOG_PREFIX 今日结果:"
sqlite3 "$DB" <<SQL 2>/dev/null
.mode column
.headers on
SELECT '$TODAY' AS date, path, tone, regime_name, is_divergence, kept_count, universe_size
FROM s8_select_runs WHERE date='$TODAY';
SQL

echo "$LOG_PREFIX ========== 结束 =========="
