#!/usr/bin/env python3
"""全量修复基金 bot 的历史持仓、现金与快照口径。

修复动作：
1. 所有 bot 统一按 fund_bot_actions + 复权单位净值回放当前账户状态
2. 删除 2026-05-12 基于旧份额口径生成的 pending orders / review
3. 重建所有 bot 的 daily / position snapshots
4. 删除基金 dashboard 会优先读取的旧 markdown，避免覆盖修复后的 DB
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

MCP_DIR = Path("/home/rooot/agent_invest_lab/fund-portfolio-mcp")
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from db import get_conn  # type: ignore
from server import _compute_fund_snapshot, _replay_and_repair_fund_holdings  # type: ignore

OPENCLAW_ROOT = Path("/home/rooot/agent_invest_lab")
BAD_REVIEW_DATE = "2026-05-12"
STALE_MD_FILENAMES = ("基金巡检记录.md", "当前基金持仓.md")


def _all_bot_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT bot_id FROM fund_bot_accounts ORDER BY bot_id"
    ).fetchall()
    return [r["bot_id"] for r in rows]


def _snapshot_dates(conn: sqlite3.Connection, bot_id: str) -> list[str]:
    row = conn.execute(
        "SELECT MIN(entry_date) AS start_date FROM fund_bot_holdings WHERE bot_id=?",
        (bot_id,),
    ).fetchone()
    start_date = row["start_date"] if row and row["start_date"] else None
    if not start_date:
        return []
    rows = conn.execute(
        "SELECT DISTINCT nav_date AS trade_date FROM fund_nav "
        "WHERE nav_date >= ? ORDER BY nav_date",
        (start_date,),
    ).fetchall()
    return [r["trade_date"] for r in rows if r["trade_date"]]


def _delete_bad_pending_review(conn: sqlite3.Connection, bot_id: str, trade_date: str) -> dict:
    deleted_review_ids: list[int] = []
    review_rows = conn.execute(
        "SELECT review_id FROM fund_bot_reviews WHERE bot_id=? AND review_date=?",
        (bot_id, trade_date),
    ).fetchall()
    for row in review_rows:
        review_id = row["review_id"]
        confirmed = conn.execute(
            "SELECT 1 FROM fund_bot_orders WHERE review_id=? AND status='confirmed' LIMIT 1",
            (review_id,),
        ).fetchone()
        if confirmed:
            continue
        conn.execute(
            "DELETE FROM fund_bot_orders WHERE review_id=? AND status='pending'",
            (review_id,),
        )
        conn.execute(
            "DELETE FROM fund_bot_reviews WHERE review_id=?",
            (review_id,),
        )
        deleted_review_ids.append(review_id)

    deleted_orders = conn.execute(
        "DELETE FROM fund_bot_orders WHERE bot_id=? AND order_date=? AND status='pending'",
        (bot_id, trade_date),
    ).rowcount
    return {"deleted_review_ids": deleted_review_ids, "deleted_orders": deleted_orders}


def _delete_stale_md(bot_id: str) -> list[str]:
    md_dir = OPENCLAW_ROOT / f"workspace-{bot_id}" / "memory" / "portfolio" / "fund"
    deleted = []
    for filename in STALE_MD_FILENAMES:
        path = md_dir / filename
        if path.exists():
            path.unlink()
            deleted.append(str(path))
    return deleted


def main() -> int:
    summary: list[dict] = []
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        bot_ids = _all_bot_ids(conn)

        for bot_id in bot_ids:
            repaired = _replay_and_repair_fund_holdings(conn, bot_id, BAD_REVIEW_DATE)
            cleanup = _delete_bad_pending_review(conn, bot_id, BAD_REVIEW_DATE)

            conn.execute("DELETE FROM fund_bot_position_snapshots WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM fund_bot_daily_snapshots WHERE bot_id=?", (bot_id,))

            rebuilt_dates = 0
            for trade_date in _snapshot_dates(conn, bot_id):
                _compute_fund_snapshot(conn, bot_id, trade_date)
                rebuilt_dates += 1

            deleted_md = _delete_stale_md(bot_id)
            summary.append(
                {
                    "bot_id": bot_id,
                    "holdings_repaired": repaired,
                    "rebuilt_dates": rebuilt_dates,
                    "deleted_orders": cleanup["deleted_orders"],
                    "deleted_review_ids": cleanup["deleted_review_ids"],
                    "deleted_md_count": len(deleted_md),
                }
            )

    for item in summary:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
