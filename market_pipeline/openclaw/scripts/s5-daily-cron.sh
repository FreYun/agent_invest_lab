#!/usr/bin/env bash
# S5 龙回头 — 每日选股 + T+1 验证 (cron 入口)
#
# 依赖: daily-regime-pipeline.sh (21:00) 必须先跑完, 产出 s5_daily_universe / limit_up_pool / klines_cache
# 本脚本建议 21:15 启动 (给 pipeline 15 分钟缓冲)
#
# 流程:
#   1. verify.py --mode=backtest  → 用 T+1 日线 OHLC 回放昨日候选, 写 s5_verifications
#   2. select.py                  → 选今日 T 日候选, 写 s5_select_runs/s5_candidates/s5_candidate_rejects
#
# 非交易日跳过: 通过检查 s5_daily_universe 是否有今日数据判断
#   (pipeline 已经处理非交易日 -> universe 没今日数据 -> 本脚本直接退出)
#
# crontab:
#   15 21 * * 1-5 /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/s5-daily.log 2>&1

set -uo pipefail

TODAY=$(date +%Y-%m-%d)
LOG_PREFIX="[s5-daily $(date +%Y-%m-%d_%H:%M:%S)]"
S5_DIR="/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s5"
DB="${MARKET_DB_PATH:-/home/rooot/agent_invest_lab/data/market.db}"
PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"

echo ""
echo "$LOG_PREFIX ========== 开始 (date=$TODAY) =========="

# 等 universe 就绪: regime-pipeline(21:00) 偶发跑 >15min(如 2026-06-16 跑到 21:22:50),
# 本脚本 21:15 启动会撞上"数据未就绪"而空跳过(当日 S5 候选全丢)。
# 改为有界轮询: 每 30s 查一次 s5_daily_universe, 最多等 15min, 数据一到立刻继续。
#   - 正常交易日: 查一次即过, 0 等待。  - 偶发慢日: 多睡几次赶上, 不丢数据。
#   - 非交易日(工作日休市): 等满上限后跳过; 仅 sleep 休眠, 不占 CPU/不常驻。
WAIT_MAX=30   # 30 次 × 30s = 15 分钟上限
HAS_UNIVERSE=0
for i in $(seq 1 "$WAIT_MAX"); do
    HAS_UNIVERSE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM s5_daily_universe WHERE date='$TODAY'" 2>/dev/null || echo "0")
    [ "$HAS_UNIVERSE" != "0" ] && break
    [ "$i" = "1" ] && echo "$LOG_PREFIX universe 未就绪, 等 regime-pipeline 完成 (每30s 查一次, 上限 15min)..."
    sleep 30
done
if [ "$HAS_UNIVERSE" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 等满 15min 仍无 universe 数据 (非交易日 or pipeline 异常), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi
echo "$LOG_PREFIX universe 已就绪 ($HAS_UNIVERSE 只)"

# Step 1: 验证昨日 T 日的 candidate (T+1 backtest)
echo "$LOG_PREFIX [1/2] verify.py --date=$TODAY --mode=backtest"
if "$PY" "$S5_DIR/verify.py" --date="$TODAY" --mode=backtest 2>&1; then
    echo "$LOG_PREFIX verify OK"
else
    RC=$?
    echo "$LOG_PREFIX verify 失败 (rc=$RC, 常见原因: 昨日无 candidate, 非致命)"
fi

# Step 2: 选今日 T 日的 candidate
echo "$LOG_PREFIX [2/2] select.py --date=$TODAY"
if "$PY" "$S5_DIR/select.py" --date="$TODAY" 2>&1; then
    echo "$LOG_PREFIX select OK"
else
    RC=$?
    echo "$LOG_PREFIX select 失败 (rc=$RC)" >&2
fi

# 汇总今日结果
echo "$LOG_PREFIX 今日结果:"
sqlite3 "$DB" <<SQL 2>/dev/null
.mode column
.headers on
SELECT '$TODAY' AS date,
    (SELECT passed_count FROM s5_select_runs WHERE date='$TODAY') AS picked,
    (SELECT COUNT(*) FROM s5_candidate_rejects WHERE date='$TODAY') AS rejected,
    (SELECT COUNT(*) FROM s5_verifications WHERE t1_date='$TODAY') AS verified_prev_day;
SQL

echo "$LOG_PREFIX ========== 结束 =========="
