#!/bin/bash
# stock-select-daily.sh — 每日增量更新"看懂龙头股选股框架"依赖的选股层数据 (工作日 21:35 cron)
#
# 依赖顺序: 必须在 daily-regime-pipeline.sh (21:00) 之后跑。
#   该 pipeline 第 1 步 daily_review.py 会把全市场今日行情写入 market.db 的 daily 表,
#   本脚本 backfill 的 trade_dates() 靠 daily 表发现"今天这个交易日", daily 没今日数据则本脚本 no-op。
#   非交易日(周末/节假日)daily 表无今日行→自然 no-op, 不报错。
#
# 增量更新 market.db 5 张选股表(幂等 INSERT OR REPLACE + 断点续传 backfill_progress):
#   daily_basic / moneyflow_daily / concept_board_daily / kpl_theme_daily / stock_concept_map
#
# 市场层数据(daily / regime / limit_up_pool)已由 21:00 regime pipeline + 21:15 S5 自动维护,
# 本脚本只补"选股层"。
#
# crontab:
#   35 21 * * 1-5 /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron-wrapper.sh stock-select-daily \
#     /bin/bash /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/stock-select-daily.sh \
#     >> /home/rooot/agent_invest_lab/logs/stock-select-daily.log 2>&1

set -uo pipefail

PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"
BF_DIR="/home/rooot/agent_invest_lab/market_pipeline/openclaw/workspace-bot11/scripts"
DB="${MARKET_DB_PATH:-/home/rooot/agent_invest_lab/data/market.db}"
LOG_PREFIX="[stock-select $(date +%Y-%m-%d_%H:%M:%S)]"
TODAY=$(date +%Y%m%d)

echo ""
echo "$LOG_PREFIX ========== 开始 =========="

cd "$BF_DIR"
"$PY" -u stock_select_backfill.py all
rc=$?
echo "$LOG_PREFIX backfill all rc=$rc"

echo "$LOG_PREFIX 今日($TODAY)各表入库行数:"
"$PY" - "$DB" "$TODAY" <<'PY'
import sqlite3, sys
db, today = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
for t in ["daily_basic", "moneyflow_daily", "concept_board_daily", "kpl_theme_daily"]:
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE trade_date=?", (today,)).fetchone()[0]
    except Exception as e:
        n = f"ERR {e}"
    print(f"  {t}: {n}")
snap = c.execute("SELECT MAX(snapshot_date), COUNT(*) FROM stock_concept_map").fetchone()
print(f"  stock_concept_map: snapshot={snap[0]} rows={snap[1]}")
c.close()
PY

echo "$LOG_PREFIX 计算板块龙头 board_leader_daily (先行, board_trend 依赖) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/board_leader.py
echo "$LOG_PREFIX board_leader rc=$?"

echo "$LOG_PREFIX 计算趋势主线 board_trend_daily ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/board_trend.py
echo "$LOG_PREFIX board_trend rc=$?"

echo "$LOG_PREFIX 萌芽主线探测 emerging_signal_daily (依赖 board_trend + regime) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/emerging.py
echo "$LOG_PREFIX emerging rc=$?"

echo "$LOG_PREFIX 计算板块对标 ETF board_etf_daily (依赖 board_leader + board_trend) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/board_etf.py
echo "$LOG_PREFIX board_etf rc=$?"

echo "$LOG_PREFIX 融资融券聚合 margin_flow (依赖 board_trend + stock_concept_map) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/margin_flow.py
echo "$LOG_PREFIX margin_flow rc=$?"

echo "$LOG_PREFIX 主力资金行业流向 board_moneyflow (依赖 moneyflow_daily + sw_industry_member) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/board_moneyflow.py
echo "$LOG_PREFIX board_moneyflow rc=$?"

echo "$LOG_PREFIX 拉取业绩预告/快报 earnings_events (tushare forecast+express) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/earnings_ingest.py
echo "$LOG_PREFIX earnings_ingest rc=$?"

echo "$LOG_PREFIX 业绩共振 earnings_resonance_daily (依赖 daily + daily_basic + board_trend + earnings_events) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/earnings_resonance.py
echo "$LOG_PREFIX earnings_resonance rc=$?"

echo "$LOG_PREFIX 周期股估值带 valuation (依赖 daily_basic + earnings_events; agent 触发器另走 22:20 crontab) ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/valuation.py
echo "$LOG_PREFIX valuation rc=$?"

echo "$LOG_PREFIX ========== 完成 (rc=$rc) =========="
exit $rc
