#!/usr/bin/env python3
"""Rebuild fund curves from actions while preserving live account state.

This maintenance tool rewrites only daily snapshots, position snapshots, and
performance rows for explicitly selected (bot, run) pairs. Current accounts,
holdings, orders, actions, and lots are restored before commit.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FUND_MCP = ROOT / "fund-portfolio-mcp"


def backup_database(db_path: str, backup_path: str) -> None:
    if os.path.exists(backup_path):
        raise FileExistsError(f"backup already exists: {backup_path}")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source, \
            sqlite3.connect(backup_path) as target:
        source.backup(target)


def _load_server(db_path: str):
    os.environ["FUND_DB_PATH"] = db_path
    sys.path.insert(0, str(FUND_MCP))
    import db as db_mod
    db_mod.DB_PATH = db_path
    import server
    return server


def _save_operational_state(conn: sqlite3.Connection, bot_id: str) -> dict:
    account = conn.execute(
        "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,)
    ).fetchone()
    holdings = conn.execute(
        "SELECT * FROM fund_bot_holdings WHERE bot_id=? ORDER BY holding_id", (bot_id,)
    ).fetchall()
    return {
        "account": dict(account) if account else None,
        "holdings": [dict(row) for row in holdings],
    }


def _restore_row(conn: sqlite3.Connection, table: str, pk: str, row: dict) -> None:
    columns = [c for c in row if c != pk]
    assignments = ",".join(f"{c}=?" for c in columns)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {pk}=?",
        [row[c] for c in columns] + [row[pk]],
    )


def _restore_operational_state(conn: sqlite3.Connection, bot_id: str, state: dict) -> None:
    account = state["account"]
    if account:
        _restore_row(conn, "fund_bot_accounts", "bot_id", account)

    holdings = state["holdings"]
    original_ids = {int(row["holding_id"]) for row in holdings}
    current_ids = {
        int(row[0]) for row in conn.execute(
            "SELECT holding_id FROM fund_bot_holdings WHERE bot_id=?", (bot_id,)
        ).fetchall()
    }
    inserted_ids = current_ids - original_ids
    if inserted_ids:
        placeholders = ",".join("?" for _ in inserted_ids)
        conn.execute(
            f"DELETE FROM fund_bot_holdings WHERE holding_id IN ({placeholders})",
            sorted(inserted_ids),
        )
    for row in holdings:
        _restore_row(conn, "fund_bot_holdings", "holding_id", row)


def _changed(before: dict, after: dict) -> bool:
    numeric = ("cash", "invested_value", "total_value", "net_value",
               "daily_return_pct", "cumulative_return_pct", "max_drawdown_pct")
    for key in numeric:
        if abs(float(before.get(key) or 0) - float(after.get(key) or 0)) > 1e-6:
            return True
    return before.get("holdings_json") != after.get("holdings_json")


def recompute_target(conn: sqlite3.Connection, server, bot_id: str, run_id: str) -> dict:
    old_rows = conn.execute(
        "SELECT * FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? ORDER BY trade_date",
        (bot_id, run_id),
    ).fetchall()
    if not old_rows:
        raise ValueError(f"no daily snapshots for {bot_id}/{run_id}")
    before = {row["trade_date"]: dict(row) for row in old_rows}
    dates = list(before)
    old_positions = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_position_snapshots WHERE bot_id=? AND run_id=?",
        (bot_id, run_id),
    ).fetchone()[0]
    operational = _save_operational_state(conn, bot_id)

    try:
        conn.execute(
            "DELETE FROM fund_bot_position_snapshots WHERE bot_id=? AND run_id=?",
            (bot_id, run_id),
        )
        conn.execute(
            "DELETE FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=?",
            (bot_id, run_id),
        )
        conn.execute(
            "DELETE FROM fund_bot_performance WHERE bot_id=? AND run_id=?",
            (bot_id, run_id),
        )
        for trade_date in dates:
            result = server._compute_fund_snapshot(
                conn, bot_id, trade_date, run_id=run_id
            )
            if not result.get("success", True):
                raise RuntimeError(f"snapshot failed for {bot_id}/{run_id}/{trade_date}: {result}")

        new_rows = conn.execute(
            "SELECT * FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? ORDER BY trade_date",
            (bot_id, run_id),
        ).fetchall()
        after = {row["trade_date"]: dict(row) for row in new_rows}
        if list(after) != dates:
            raise RuntimeError(f"snapshot date set changed for {bot_id}/{run_id}")
        new_positions = conn.execute(
            "SELECT COUNT(*) FROM fund_bot_position_snapshots WHERE bot_id=? AND run_id=?",
            (bot_id, run_id),
        ).fetchone()[0]
    finally:
        _restore_operational_state(conn, bot_id, operational)
    if _save_operational_state(conn, bot_id) != operational:
        raise RuntimeError(f"operational state changed for {bot_id}/{run_id}")

    changed_dates = [d for d in dates if _changed(before[d], after[d])]
    max_total_delta = max(
        abs(float(after[d]["total_value"] or 0) - float(before[d]["total_value"] or 0))
        for d in dates
    )
    max_nav_delta = max(
        abs(float(after[d]["net_value"] or 0) - float(before[d]["net_value"] or 0))
        for d in dates
    )
    last = dates[-1]
    return {
        "bot_id": bot_id,
        "run_id": run_id,
        "dates": len(dates),
        "changed": len(changed_dates),
        "first_changed": changed_dates[0] if changed_dates else None,
        "last_changed": changed_dates[-1] if changed_dates else None,
        "max_total_delta": max_total_delta,
        "max_nav_delta": max_nav_delta,
        "old_positions": old_positions,
        "new_positions": new_positions,
        "old_last_nav": float(before[last]["net_value"] or 0),
        "new_last_nav": float(after[last]["net_value"] or 0),
    }


def _parse_target(raw: str) -> tuple[str, str]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError("target must be BOT:RUN_ID")
    bot_id, run_id = raw.split(":", 1)
    if not bot_id or not run_id:
        raise argparse.ArgumentTypeError("target must be BOT:RUN_ID")
    return bot_id, run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "fund.db"))
    parser.add_argument("--target", action="append", required=True, type=_parse_target)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = os.path.abspath(args.db)
    if args.apply:
        backup_path = f"{db_path}.bak-pre-curve-recompute-{int(time.time())}"
        backup_database(db_path, backup_path)
        print(f"backup={backup_path}")

    server = _load_server(db_path)
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        reports = [recompute_target(conn, server, *target) for target in args.target]
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"mode={'applied' if args.apply else 'dry-run'} targets={len(reports)}")
    for r in reports:
        print(
            f"{r['bot_id']}/{r['run_id']} dates={r['dates']} changed={r['changed']} "
            f"range={r['first_changed']}..{r['last_changed']} "
            f"max_total_delta={r['max_total_delta']:.2f} max_nav_delta={r['max_nav_delta']:.6f} "
            f"positions={r['old_positions']}->{r['new_positions']} "
            f"last_nav={r['old_last_nav']:.6f}->{r['new_last_nav']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
