#!/usr/bin/env bash
# S1/S2/S3/S4/S6/S7 — 每日选股 + T+1 验证 (cron 入口)
#
# 依赖: daily-regime-pipeline.sh 必须先跑完, 产出 daily/stk_limit/regime_classify_daily。
# 本脚本只读市场数据、写 s1_* / s2_* / s3_* / s4_* / s6_* / s7_* 策略表。
# (脚本文件名保留 s1s2s3s4,因为已有 crontab 入口引用;s5 仍由 s5-daily-cron.sh 单独跑)
#
# crontab 示例:
#   20 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/s1s2s3s4-daily-cron.sh >> /home/rooot/agent_invest_lab/logs/s1s2s3s4-daily.log 2>&1

set -uo pipefail

TODAY=$(date +%Y-%m-%d)
TODAY_FMT=$(date +%Y%m%d)
LOG_PREFIX="[s1s2s3s4-daily $(date +%Y-%m-%d_%H:%M:%S)]"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${MARKET_DB_PATH:-/home/rooot/agent_invest_lab/data/market.db}"
PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"

echo ""
echo "$LOG_PREFIX ========== 开始 (date=$TODAY) =========="

# 等 daily 就绪: regime-pipeline(21:00) 偶发跑 >20min(如 2026-06-16 跑到 21:22:50),
# 本脚本 21:20 启动会撞上"数据未就绪"而整批空跳过(S2/S3/S6/S7 当日全丢)。
# 改为有界轮询: 每 30s 查一次 daily, 最多等 15min, 数据一到立刻继续。
#   - 正常交易日: 21:20 时数据早就绪, 查一次即过, 0 等待。
#   - 偶发慢日(如 06-16): 多睡几次后赶上, 不再丢数据。
#   - 非交易日(工作日休市): 等满上限后跳过; 仅 sleep 休眠, 不占 CPU/不常驻。
WAIT_MAX=30   # 30 次 × 30s = 15 分钟上限
HAS_DAILY=0
for i in $(seq 1 "$WAIT_MAX"); do
    HAS_DAILY=$(sqlite3 "$DB" "SELECT COUNT(*) FROM daily WHERE trade_date='$TODAY_FMT'" 2>/dev/null || echo "0")
    [ "$HAS_DAILY" != "0" ] && break
    [ "$i" = "1" ] && echo "$LOG_PREFIX daily 未就绪, 等 regime-pipeline 完成 (每30s 查一次, 上限 15min)..."
    sleep 30
done
if [ "$HAS_DAILY" = "0" ]; then
    echo "$LOG_PREFIX $TODAY 等满 15min 仍无 daily 数据 (非交易日 or pipeline 异常), 跳过"
    echo "$LOG_PREFIX ========== 结束 =========="
    exit 0
fi
echo "$LOG_PREFIX daily 已就绪 ($HAS_DAILY 行)"

# 2026-06-02 按结构择时: S4 回踩战法已下线(深史每个 regime 都亏, 见 final_report), 从生成循环移除。
# S1/S5/S6 保留生成但 regime 软门关闭(降为 observe/研究); S5 仍由 s5-daily-cron.sh 单独跑。
for STRATEGY in s1 s2 s3 s6 s7; do
    DIR="$SCRIPT_DIR/$STRATEGY"
    UPPER=$(printf '%s' "$STRATEGY" | tr '[:lower:]' '[:upper:]')

    echo "$LOG_PREFIX [$UPPER 1/2] verify.py --date=$TODAY --mode=backtest"
    if "$PY" "$DIR/verify.py" --date="$TODAY" --mode=backtest 2>&1; then
        echo "$LOG_PREFIX $UPPER verify OK"
    else
        RC=$?
        echo "$LOG_PREFIX $UPPER verify 失败 (rc=$RC, 常见原因: 昨日无 candidate, 非致命)"
    fi

    SELECT_ARGS=(--date="$TODAY")
    if [ "$STRATEGY" = "s2" ]; then
        SELECT_ARGS+=(--write-db --production)
    fi

    echo "$LOG_PREFIX [$UPPER 2/2] select.py ${SELECT_ARGS[*]}"
    if "$PY" "$DIR/select.py" "${SELECT_ARGS[@]}" 2>&1; then
        echo "$LOG_PREFIX $UPPER select OK"
    else
        RC=$?
        echo "$LOG_PREFIX $UPPER select 失败 (rc=$RC)" >&2
    fi
done

echo "$LOG_PREFIX 今日结果:"
sqlite3 "$DB" <<SQL 2>/dev/null
.mode column
.headers on
SELECT '$TODAY' AS date,
    (SELECT passed_count FROM s1_select_runs WHERE date='$TODAY') AS s1_picked,
    (SELECT COUNT(*) FROM s1_verifications WHERE t1_date='$TODAY') AS s1_verified_prev_day,
    (SELECT passed_count FROM s2_select_runs WHERE date='$TODAY') AS s2_picked,
    (SELECT COUNT(*) FROM s2_verifications WHERE t1_date='$TODAY') AS s2_verified_prev_day,
    (SELECT passed_count FROM s3_select_runs WHERE date='$TODAY') AS s3_picked,
    (SELECT COUNT(*) FROM s3_verifications WHERE t1_date='$TODAY') AS s3_verified_prev_day,
    (SELECT passed_count FROM s4_select_runs WHERE date='$TODAY') AS s4_picked,
    (SELECT COUNT(*) FROM s4_verifications WHERE t1_date='$TODAY') AS s4_verified_prev_day,
    (SELECT passed_count FROM s6_select_runs WHERE date='$TODAY') AS s6_picked,
    (SELECT COUNT(*) FROM s6_verifications WHERE t1_date='$TODAY') AS s6_verified_prev_day,
    (SELECT passed_count FROM s7_select_runs WHERE date='$TODAY') AS s7_picked,
    (SELECT COUNT(*) FROM s7_verifications WHERE t1_date='$TODAY') AS s7_verified_prev_day;
SQL

echo "$LOG_PREFIX ========== 结束 =========="
