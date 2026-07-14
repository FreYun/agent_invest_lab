"""market-regime 的离线数据回填工具。

数据层:
- /home/rooot/agent_invest_lab/data/market.db (SQLite, 不进 git)
- 三张表: daily / stk_limit / index_daily
- 由 db.py 定义 schema 和连接

流水线:
- backfill_raw_data.py — 采集层 (tushare → SQLite), 增量回填
- derive_raw_data.py    — 派生层 (SQLite → 六维原始数据 CSV)
"""
