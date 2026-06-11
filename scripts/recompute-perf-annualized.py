#!/usr/bin/env python3
"""一次性全量重算 fund_bot_performance / fund_nav_performance（统一年化口径）。

背景：2026-06-11 起两张派生业绩表统一为年化口径（vol/sharpe ×√252、
calmar=年化收益/|MDD|、新增 annualized_return_pct；return/MDD 仍为区间原值），
与 backtest-dashboard 对齐。历史行是旧口径（不年化），必须按新口径全量重算，
否则新旧口径混杂。

做法：直接复用 fund-portfolio-mcp/server.py 的 _compute_bot_performance /
_compute_fund_nav_performance（INSERT OR REPLACE 幂等覆盖），对两张表现有的
全部 (bot_id, trade_date, run_id) / (fund_code, trade_date) 键重算一遍。

用法（必须用 /usr/bin/python3.12，裸 python3 缺 mcp 包）：
    /usr/bin/python3.12 scripts/recompute-perf-annualized.py
"""
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "fund-portfolio-mcp"))

import db  # noqa: E402
import server  # noqa: E402

COMMIT_EVERY = 2000


def main() -> None:
    db.init_db()  # 跑迁移：加 annualized_return_pct 列
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    t0 = time.time()
    bot_keys = conn.execute(
        "SELECT DISTINCT bot_id, trade_date, run_id FROM fund_bot_performance"
    ).fetchall()
    print(f"fund_bot_performance: {len(bot_keys)} 个 (bot, date, run) 键待重算")
    for i, k in enumerate(bot_keys, 1):
        server._compute_bot_performance(conn, k["bot_id"], k["trade_date"], run_id=k["run_id"] or "")
        if i % COMMIT_EVERY == 0:
            conn.commit()
            print(f"  bot perf {i}/{len(bot_keys)} ({time.time()-t0:.0f}s)")
    conn.commit()
    print(f"fund_bot_performance 重算完成（{time.time()-t0:.0f}s）")

    t1 = time.time()
    nav_keys = conn.execute(
        "SELECT DISTINCT fund_code, trade_date FROM fund_nav_performance"
    ).fetchall()
    print(f"fund_nav_performance: {len(nav_keys)} 个 (fund, date) 键待重算")
    for i, k in enumerate(nav_keys, 1):
        server._compute_fund_nav_performance(conn, k["fund_code"], k["trade_date"])
        if i % COMMIT_EVERY == 0:
            conn.commit()
            print(f"  nav perf {i}/{len(nav_keys)} ({time.time()-t1:.0f}s)")
    conn.commit()
    print(f"fund_nav_performance 重算完成（{time.time()-t1:.0f}s）")

    # 抽样自检：年化列已填、calmar = ann/|mdd|
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN annualized_return_pct IS NULL THEN 1 ELSE 0 END) AS nul "
        "FROM fund_bot_performance"
    ).fetchone()
    print(f"自检 bot perf: {row['n']} 行，annualized_return_pct 为空 {row['nul']} 行")
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN annualized_return_pct IS NULL THEN 1 ELSE 0 END) AS nul "
        "FROM fund_nav_performance"
    ).fetchone()
    print(f"自检 nav perf: {row['n']} 行，annualized_return_pct 为空 {row['nul']} 行")
    conn.close()


if __name__ == "__main__":
    main()
