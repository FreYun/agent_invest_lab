#!/bin/bash
# cron-wrapper.sh — 通用 cron 任务防孤儿包装(thin shim)
#
# 这只是一个 thin wrapper,真正的逻辑在 cron_wrapper.py(它用 prctl PR_SET_CHILD_SUBREAPER,
# 这样不管子进程怎么 setsid / fork 出孙子进程,孤儿都会 reparent 到 wrapper 而不是 init,
# wrapper 退出时一次性清干净)。
PY="${PY:-/home/rooot/agent_invest_lab/.venv/bin/python}"
exec "$PY" /home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/cron_wrapper.py "$@"
