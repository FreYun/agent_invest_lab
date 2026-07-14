#!/bin/bash
# earnings-refresh — 早盘 08:40 拉最新业绩预告,重跑业绩共振.
# 供 scout 页面 业绩共振 tab 使用. 盘后一份沿用 stock-select-daily.sh.
set -uo pipefail

PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"
LOG_PREFIX="[earnings-refresh $(date +%Y-%m-%d_%H:%M:%S)]"

echo "$LOG_PREFIX ========== 开始 =========="

echo "$LOG_PREFIX 拉取业绩预告/快报 earnings_events ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/earnings_ingest.py
echo "$LOG_PREFIX earnings_ingest rc=$?"

echo "$LOG_PREFIX 业绩共振 earnings_resonance_daily ..."
"$PY" -u /home/rooot/agent_invest_lab/market_pipeline/openclaw/scout/earnings_resonance.py
echo "$LOG_PREFIX earnings_resonance rc=$?"

echo "$LOG_PREFIX ========== 完成 =========="
