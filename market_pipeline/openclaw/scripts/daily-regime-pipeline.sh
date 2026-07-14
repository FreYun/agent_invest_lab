#!/bin/bash
# 每日 regime 数据管道 (工作日 16:00 cron)
#
# 流程:
#   1. daily_review.py  → 采集行情, 写入 daily/stk_limit/regime_raw_daily/index_daily
#   2. replay.py        → 从 2015 起完整回放, 直接 INSERT OR REPLACE 写 regime_classify_daily(v2)
#                          (保证 3 日确认链完整一致, ~40 秒)
#   3. s5-prewarm.py    → 预热 S5 战法所需缓存: limit_up_pool + klines_cache
#                          (让 s5 select.py 运行时只读 DB, 不触外部 API)
#
# 为什么用 replay 而不是 classify.py:
#   classify.py 读 regime-log.md 做 3 日确认, 历史链可能跟 DB 不一致
#   replay.py 从 regime_raw_daily 完整回放, 链条永远一致
#
# crontab:
#   0 16 * * 1-5 /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/daily-regime-pipeline.sh >> /home/rooot/agent_invest_lab/logs/daily-regime-pipeline.log 2>&1

set -euo pipefail

LOG_PREFIX="[regime-pipeline $(date +%Y-%m-%d_%H:%M:%S)]"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVIEW_DIR="/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts"
BACKFILL_DIR="$SCRIPT_DIR/market-regime/scripts/backfill"
PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"

TODAY=$(date +%Y-%m-%d)
TODAY_FMT=$(date +%Y%m%d)

echo ""
echo "$LOG_PREFIX ========== 开始 =========="

# Step 1: daily_review.py — 采集 + 落库
echo "$LOG_PREFIX [1/3] daily_review.py"
cd "$REVIEW_DIR"
"$PY" review/daily_review.py "$TODAY" 2>&1 || {
    echo "$LOG_PREFIX daily_review.py 失败 (rc=$?), 但继续尝试 replay" >&2
}

# Step 2: replay.py — 完整回放 + 直接写 regime_classify_daily
echo "$LOG_PREFIX [2/3] replay.py (全量回放 → DB)"
cd "$BACKFILL_DIR"
"$PY" replay.py --rules v2 --start 20150105 --end "$TODAY_FMT" 2>&1

# Step 3: S5 cache 预热 (涨停池 + 行业成分股 + 近 35 日 K 线)
echo "$LOG_PREFIX [3/3] s5-prewarm.py"
"$PY" "$SCRIPT_DIR/s5-prewarm.py" --date "$TODAY" 2>&1 || {
    echo "$LOG_PREFIX s5-prewarm 失败 (rc=$?), 不影响 regime 数据" >&2
}

echo "$LOG_PREFIX 今日结果:"
"$PY" -c "
import sqlite3
conn = sqlite3.connect('${MARKET_DB_PATH:-/home/rooot/agent_invest_lab/data/market.db}')
conn.row_factory = sqlite3.Row
r = conn.execute('''
    SELECT trade_date, total_score, regime_name, switched, emergency_switch
    FROM regime_classify_daily WHERE rules_version=\"v2\"
    ORDER BY trade_date DESC LIMIT 1
''').fetchone()
if r:
    sw = 'switched=1 ' if r['switched'] else ''
    emg = 'emergency! ' if r['emergency_switch'] else ''
    regime = r['regime_name']
    boosted = r['switched'] and regime in ('强牛','强势震荡')
    pos = '90% ⚡' if boosted else '50%'
    print(f'  {r[\"trade_date\"]} | 总分={r[\"total_score\"]} | {regime} | {sw}{emg}| 仓位={pos}')
conn.close()
"

# Step 4: mkt_facts_fetch (V2 fund-cron 改造预留, 失败不影响前序步骤)
# 拉 GVIX / 估值 / 国债收益率 写 mkt_*_daily 表, fact_pack 读取这些表
echo "$LOG_PREFIX [4/4] mkt_facts_fetch.py (可选, 失败不阻塞)"
"$PY" "$SCRIPT_DIR/mkt_facts_fetch.py" --date "$TODAY" 2>&1 || {
    echo "$LOG_PREFIX mkt_facts_fetch 失败 (rc=$?), 不影响 regime pipeline" >&2
}

echo "$LOG_PREFIX ========== 完成 =========="
