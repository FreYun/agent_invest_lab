"""一次性修复因 run_id 串味而腐败的持仓 / daily / position / performance 快照。

使用方法：
    uv run --project fund-portfolio-mcp python scripts/repair_run_holdings.py

会针对硬编码列表中每个 (bot_id, run_id)：
  1. 按该 run 自己的 actions 逐日重算 daily / position snapshots 与 performance
  2. 修正 fund_bot_holdings 的当前 active 行 (shares / amount_invested / market_value / weight)
  3. 修正 fund_bot_accounts.cash 为按 run 隔离的回放结果

修复前会先做一次状态打印，便于人工校对修复前后。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fund-portfolio-mcp"))

DB_PATH = ROOT / "data" / "fund.db"
os.environ["FUND_DB_PATH"] = str(DB_PATH)

import db as fmdb  # noqa: E402

fmdb.DB_PATH = str(DB_PATH)
fmdb.init_db()  # 应用任何尚未跑过的 ALTER TABLE 迁移（cash_receivable 等）

import server as srv  # noqa: E402


def _discover_corrupted(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """扫描所有 (bot_id, run_id)：只要在 fund_bot_daily_snapshots 里有行，就重算一遍。

    旧 run 的 snapshot 即使数据看起来正常，重算后仍是按 run-isolated replay 的结果，与新代码口径一致。
    """
    rows = conn.execute(
        "SELECT DISTINCT bot_id, run_id FROM fund_bot_daily_snapshots "
        "WHERE run_id IS NOT NULL AND run_id <> '' "
        "ORDER BY bot_id, run_id"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _print_state(conn: sqlite3.Connection, bot_id: str, run_id: str, label: str) -> None:
    print(f"--- {label}: {bot_id} / {run_id} ---")
    row = conn.execute(
        "SELECT cash, initial_capital, run_id FROM fund_bot_accounts WHERE bot_id=?",
        (bot_id,),
    ).fetchone()
    if row:
        print(f"  accounts: cash={row[0]!r}  initial={row[1]!r}  account.run_id={row[2]!r}")
    holdings = conn.execute(
        "SELECT holding_id, fund_code, shares, amount_invested, market_value, actual_weight, status, run_id "
        "FROM fund_bot_holdings WHERE bot_id=? AND run_id=?",
        (bot_id, run_id),
    ).fetchall()
    for h in holdings:
        print(f"  holding id={h[0]} fund={h[1]} shares={h[2]!r} invested={h[3]!r} mv={h[4]!r} weight={h[5]!r} status={h[6]!r}")
    last_daily = conn.execute(
        "SELECT trade_date, cash, invested_value, total_value, net_value, equity_weight, cash_weight "
        "FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? "
        "ORDER BY trade_date DESC LIMIT 1",
        (bot_id, run_id),
    ).fetchone()
    if last_daily:
        td, cash, inv, tot, nv, eqw, chw = last_daily
        print(f"  daily[{td}]: cash={cash!r} inv={inv!r} total={tot!r} net={nv!r} eq_w={eqw!r} cash_w={chw!r}")


def repair_one(conn: sqlite3.Connection, bot_id: str, run_id: str) -> None:
    _print_state(conn, bot_id, run_id, "BEFORE")
    dates = [r[0] for r in conn.execute(
        "SELECT trade_date FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? "
        "ORDER BY trade_date ASC",
        (bot_id, run_id),
    ).fetchall()]
    if not dates:
        print(f"  no daily snapshots — nothing to recompute")
        return
    # _compute_fund_snapshot 用 INSERT OR REPLACE 写 position_snapshots，但只覆盖"当天有持仓的 fund_code"。
    # 老污染版本可能在某些日子写过幽灵持仓行（如 shares > replay 应有值），新 replay 当天算出 0 持仓时
    # 不会写不会删 → 残留。所以先 DELETE 整个 (bot, run) 的 position_snapshots，让 _compute_fund_snapshot
    # 全量重建。daily_snapshots 也用 INSERT OR REPLACE，但每个 trade_date 一定有一行，所以不需要预删。
    pos_deleted = conn.execute(
        "DELETE FROM fund_bot_position_snapshots WHERE bot_id=? AND run_id=?",
        (bot_id, run_id),
    ).rowcount
    print(f"  pre-deleted {pos_deleted} stale position_snapshot rows")
    print(f"  recomputing {len(dates)} trade-dates from {dates[0]} to {dates[-1]} ...")
    for td in dates:
        result = srv._compute_fund_snapshot(conn, bot_id, td, run_id=run_id)
        if not result.get("success", True):
            raise RuntimeError(f"recompute failed for {bot_id}/{run_id}/{td}: {result}")
    repaired = srv._replay_and_repair_fund_holdings(conn, bot_id, dates[-1], run_id=run_id)
    print(f"  _replay_and_repair_fund_holdings → {repaired} active rows")
    _print_state(conn, bot_id, run_id, "AFTER")


def main() -> int:
    print(f"db = {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        corrupted = _discover_corrupted(conn)
        print(f"discovered {len(corrupted)} (bot, run) combinations to repair:")
        for bot_id, run_id in corrupted:
            print(f"  - {bot_id} / {run_id}")
        print()
        for bot_id, run_id in corrupted:
            repair_one(conn, bot_id, run_id)
            conn.commit()
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
