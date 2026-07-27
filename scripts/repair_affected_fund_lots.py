#!/usr/bin/env python3
"""Repair active fund lot ledgers without rewriting confirmed trades.

The repair is intentionally narrow:

* rebuild open lots only when their shares materially disagree with the active
  holding for the same (bot, fund, run);
* close sub-share residue only when the last action is a sell and no order is
  still pending;
* never update orders, actions, accounts, or confirmed cash amounts.

Dry-run is the default. Use --apply to write after creating a sibling backup.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass


def share_epsilon(shares: float) -> float:
    return max(0.05, abs(float(shares or 0.0)) * 1e-7)


@dataclass(frozen=True)
class Group:
    bot_id: str
    fund_code: str
    run_id: str
    holding_shares: float
    lot_shares: float


def find_mismatches(conn: sqlite3.Connection) -> list[Group]:
    rows = conn.execute(
        "WITH holdings AS ("
        " SELECT bot_id, fund_code, COALESCE(run_id, '') AS run_id,"
        "        SUM(shares) AS holding_shares"
        " FROM fund_bot_holdings WHERE status='active'"
        " GROUP BY bot_id, fund_code, COALESCE(run_id, '')"
        "), lots AS ("
        " SELECT bot_id, fund_code, run_id, SUM(shares_remaining) AS lot_shares"
        " FROM fund_bot_holding_lots WHERE status='open'"
        " GROUP BY bot_id, fund_code, run_id"
        ")"
        " SELECT h.bot_id, h.fund_code, h.run_id, h.holding_shares,"
        "        COALESCE(l.lot_shares, 0)"
        " FROM holdings h LEFT JOIN lots l"
        " ON l.bot_id=h.bot_id AND l.fund_code=h.fund_code AND l.run_id=h.run_id"
        " ORDER BY h.bot_id, h.run_id, h.fund_code"
    ).fetchall()
    return [
        Group(str(r[0]), str(r[1]), str(r[2]), float(r[3] or 0), float(r[4] or 0))
        for r in rows
        if abs(float(r[3] or 0) - float(r[4] or 0)) > share_epsilon(float(r[3] or 0))
    ]


def find_bug_residue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find legacy full-exit dust that should no longer be an active holding."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT h.*, ("
        " SELECT MAX(o.order_date) FROM fund_bot_orders o"
        " WHERE o.bot_id=h.bot_id AND o.fund_code=h.fund_code"
        "   AND COALESCE(o.order_run_id, '')=COALESCE(h.run_id, '')"
        "   AND o.order_type='sell' AND o.status='confirmed'"
        ") AS confirmed_sell_date, ("
        " SELECT a.action_date FROM fund_bot_actions a"
        " WHERE a.bot_id=h.bot_id AND a.fund_code=h.fund_code"
        "   AND COALESCE(a.run_id, '')=COALESCE(h.run_id, '')"
        " ORDER BY a.action_date DESC, a.action_id DESC LIMIT 1"
        ") AS last_action_date"
        " FROM fund_bot_holdings h"
        " WHERE h.status='active' AND h.shares>0"
        "   AND h.shares<=0.05"
        "   AND ("
        "     SELECT COALESCE(SUM(h2.shares), 0) FROM fund_bot_holdings h2"
        "     WHERE h2.bot_id=h.bot_id AND h2.fund_code=h.fund_code"
        "       AND COALESCE(h2.run_id, '')=COALESCE(h.run_id, '')"
        "       AND h2.status='active'"
        "   )<=0.05"
        "   AND COALESCE(("
        "     SELECT UPPER(a.action_type) FROM fund_bot_actions a"
        "     WHERE a.bot_id=h.bot_id AND a.fund_code=h.fund_code"
        "       AND COALESCE(a.run_id, '')=COALESCE(h.run_id, '')"
        "     ORDER BY a.action_date DESC, a.action_id DESC LIMIT 1"
        "   ), '') IN ('REDUCE','SELL','DECREASE','TAKE_PROFIT','STOP_LOSS','EXIT')"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM fund_bot_orders o"
        "     WHERE o.bot_id=h.bot_id AND o.fund_code=h.fund_code"
        "       AND COALESCE(o.order_run_id, '')=COALESCE(h.run_id, '')"
        "       AND o.status='pending'"
        "   )"
        " ORDER BY h.bot_id, h.run_id, h.fund_code, h.holding_id"
    ).fetchall()


def _rebuild_group(conn: sqlite3.Connection, group: Group) -> int:
    conn.execute(
        "UPDATE fund_bot_holding_lots SET status='superseded'"
        " WHERE bot_id=? AND fund_code=? AND run_id=? AND status='open'",
        (group.bot_id, group.fund_code, group.run_id),
    )
    holdings = conn.execute(
        "SELECT holding_id, entry_date, entry_nav, latest_nav, shares, amount_invested"
        " FROM fund_bot_holdings"
        " WHERE bot_id=? AND fund_code=? AND COALESCE(run_id, '')=? AND status='active'"
        " ORDER BY holding_id",
        (group.bot_id, group.fund_code, group.run_id),
    ).fetchall()
    inserted = 0
    for h in holdings:
        shares = float(h[4] or 0)
        if shares <= 0:
            continue
        entry_date = h[1]
        if not entry_date:
            raise ValueError(f"holding_id={h[0]} has no entry_date")
        entry_nav = float(h[2] or h[3] or 0)
        cost = max(0.0, float(h[5] or 0))
        conn.execute(
            "INSERT INTO fund_bot_holding_lots"
            " (bot_id, fund_code, run_id, holding_id, entry_date, entry_nav,"
            " shares_initial, shares_remaining, cost_initial, cost_remaining,"
            " source_order_id, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open')",
            (group.bot_id, group.fund_code, group.run_id, h[0], entry_date, entry_nav,
             shares, shares, cost, cost),
        )
        inserted += 1
    return inserted


def _close_residue(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    rid = str(row["run_id"] or "")
    conn.execute(
        "UPDATE fund_bot_holding_lots SET status='superseded'"
        " WHERE bot_id=? AND fund_code=? AND run_id=? AND status='open'",
        (row["bot_id"], row["fund_code"], rid),
    )
    exit_date = (row["last_action_date"] or row["confirmed_sell_date"]
                 or row["exit_date"] or row["entry_date"])
    conn.execute(
        "UPDATE fund_bot_holdings SET status='closed', exit_date=?, shares=0,"
        " pending_sell_shares=0, amount_invested=0, market_value=0,"
        " unrealized_pnl=0, unrealized_pnl_pct=0, actual_weight=0"
        " WHERE holding_id=?",
        (exit_date, row["holding_id"]),
    )


def repair(conn: sqlite3.Connection, *, apply: bool) -> dict:
    mismatches = find_mismatches(conn)
    residues = find_bug_residue(conn)
    result = {
        "mismatches": mismatches,
        "residues": residues,
        "groups_rebuilt": 0,
        "lots_inserted": 0,
        "residues_closed": 0,
    }
    if not apply:
        return result

    conn.execute("BEGIN IMMEDIATE")
    try:
        for group in mismatches:
            result["lots_inserted"] += _rebuild_group(conn, group)
            result["groups_rebuilt"] += 1
        for row in residues:
            _close_residue(conn, row)
            result["residues_closed"] += 1
        remaining = find_mismatches(conn)
        if remaining:
            raise RuntimeError(f"lot mismatches remain after repair: {remaining}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def _default_db_path() -> str:
    if os.environ.get("FUND_DB_PATH"):
        return os.environ["FUND_DB_PATH"]
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "fund-portfolio-mcp"))
    import db as db_mod
    return str(db_mod.DB_PATH)


def backup_database(db_path: str, backup_path: str) -> None:
    """Create a transactionally consistent backup while other runs are active."""
    if os.path.exists(backup_path):
        raise FileExistsError(f"backup already exists: {backup_path}")
    source_uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-suffix", default=None)
    args = parser.parse_args()
    db_path = os.path.abspath(args.db or _default_db_path())
    if not os.path.exists(db_path):
        parser.error(f"database does not exist: {db_path}")

    if args.apply:
        suffix = args.backup_suffix or f".bak-pre-reclear-{int(time.time())}"
        backup_path = db_path + suffix
        backup_database(db_path, backup_path)
        print(f"backup={backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = repair(conn, apply=args.apply)
    finally:
        conn.close()

    mode = "applied" if args.apply else "dry-run"
    print(f"mode={mode} mismatches={len(result['mismatches'])} "
          f"residues={len(result['residues'])} groups_rebuilt={result['groups_rebuilt']} "
          f"lots_inserted={result['lots_inserted']} residues_closed={result['residues_closed']}")
    for group in result["mismatches"]:
        print(f"mismatch bot={group.bot_id} run={group.run_id} fund={group.fund_code} "
              f"holding={group.holding_shares:.6f} lots={group.lot_shares:.6f}")
    for row in result["residues"]:
        print(f"residue bot={row['bot_id']} run={row['run_id']} fund={row['fund_code']} "
              f"holding_id={row['holding_id']} shares={float(row['shares']):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
