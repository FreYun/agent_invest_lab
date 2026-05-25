#!/usr/bin/env python3
"""One-shot, idempotent migration: rebuild fund_bot_holding_lots from fund_bot_actions.

Spec: docs/superpowers/specs/2026-05-19-fund-holding-lots-design.md (§5)

Per (bot_id, fund_code, run_id) the migration walks ADD/REDUCE actions in
chronological order, treating each ADD as a new lot and each REDUCE as a FIFO
consumption from the oldest open lot. Remaining open lots become rows in
fund_bot_holding_lots; closed lots are also inserted with status='closed' so
the per-lot history is preserved for audit.

Usage:
    python3 scripts/migrate_holding_lots.py            # apply, write to lab DB
    python3 scripts/migrate_holding_lots.py --dry-run  # just report, no writes
    python3 scripts/migrate_holding_lots.py --db PATH  # override DB path
    python3 scripts/migrate_holding_lots.py --backup-suffix SUFFIX  # custom backup name

Default DB is read from $FUND_DB_PATH or db.py's default; before any DELETE we
make a sibling .bak-pre-lots-<ts> copy (skipped on --dry-run).

The migration is **idempotent**: it DELETEs all rows in fund_bot_holding_lots
first, then rebuilds. Safe to re-run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict

# Make `import db` work whether script is run from repo root or from scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "fund-portfolio-mcp"))


TOLERANCE_SHARES = 1e-3
TOLERANCE_COST = 0.05  # holdings.amount_invested stored 2-decimal; small rounding ok


def _default_db_path() -> str:
    if os.environ.get("FUND_DB_PATH"):
        return os.environ["FUND_DB_PATH"]
    import db as db_mod
    return db_mod.DB_PATH


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Make sure the table + indexes exist. Safe to call regardless of state."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fund_bot_holding_lots (
            lot_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id              TEXT NOT NULL,
            fund_code           TEXT NOT NULL,
            run_id              TEXT NOT NULL,
            holding_id          INTEGER,
            entry_date          TEXT NOT NULL,
            entry_nav           REAL NOT NULL,
            shares_initial      REAL NOT NULL,
            shares_remaining    REAL NOT NULL,
            cost_initial        REAL NOT NULL,
            cost_remaining      REAL NOT NULL,
            source_order_id     INTEGER,
            status              TEXT DEFAULT 'open',
            created_at          TEXT DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fund_lots_fifo "
        "ON fund_bot_holding_lots(bot_id, fund_code, run_id, status, entry_date, lot_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fund_lots_holding "
        "ON fund_bot_holding_lots(holding_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fund_lots_source_order "
        "ON fund_bot_holding_lots(source_order_id)"
    )


def _resolve_holding_id(conn: sqlite3.Connection, bot_id: str, fund_code: str,
                        run_id: str, on_date: str) -> int | None:
    """Find the holdings row whose [entry_date, exit_date] window contains on_date.

    NULL exit_date = still active. If multiple match (shouldn't happen) take min id.
    """
    row = conn.execute(
        "SELECT holding_id FROM fund_bot_holdings "
        "WHERE bot_id=? AND fund_code=? AND COALESCE(run_id,'')=? "
        "AND entry_date <= ? AND (exit_date IS NULL OR exit_date >= ?) "
        "ORDER BY holding_id ASC LIMIT 1",
        (bot_id, fund_code, run_id or "", on_date, on_date),
    ).fetchone()
    return row[0] if row else None


def _resolve_buy_order(conn: sqlite3.Connection, bot_id: str, fund_code: str,
                       run_id: str, on_date: str) -> int | None:
    row = conn.execute(
        "SELECT order_id FROM fund_bot_orders "
        "WHERE bot_id=? AND fund_code=? AND COALESCE(order_run_id,'')=? "
        "AND order_date=? AND order_type='buy' "
        "ORDER BY order_id ASC LIMIT 1",
        (bot_id, fund_code, run_id or "", on_date),
    ).fetchone()
    return row[0] if row else None


def replay_group(conn: sqlite3.Connection, *, bot_id: str, fund_code: str,
                 run_id: str | None) -> dict:
    """Replay ADD/REDUCE actions for one (bot, fund, run) and return summary.

    Returns dict:
      {
        "open_lots":    [{entry_date, shares_remaining, cost_remaining,
                          entry_nav, source_order_id, holding_id}, ...],
        "closed_lots":  [{...same shape with shares_remaining=0, ...}, ...],
        "warnings":     ["reduce drained queue", ...],
      }
    """
    rid = run_id if run_id is not None else ""
    rows = conn.execute(
        "SELECT action_id, action_type, action_date, amount, shares, nav_used "
        "FROM fund_bot_actions "
        "WHERE bot_id=? AND fund_code=? AND COALESCE(run_id,'')=? "
        "ORDER BY action_date ASC, action_id ASC",
        (bot_id, fund_code, rid),
    ).fetchall()

    queue: list[dict] = []   # FIFO of open lots
    closed: list[dict] = []
    warnings: list[str] = []

    for r in rows:
        atype = r[1]
        adate = r[2]
        amount = float(r[3] or 0.0)
        shares = float(r[4] or 0.0)
        nav = float(r[5] or 0.0)

        if atype == "ADD":
            if shares <= 1e-6:
                warnings.append(f"ADD with 0 shares at {adate} action_id={r[0]} skipped")
                continue
            queue.append({
                "entry_date": adate,
                "entry_nav": nav if nav > 0 else (amount / shares if shares else 0.0),
                "shares_initial": shares,
                "shares_remaining": shares,
                "cost_initial": amount,
                "cost_remaining": amount,
                "source_order_id": _resolve_buy_order(conn, bot_id, fund_code, rid, adate),
                "holding_id": _resolve_holding_id(conn, bot_id, fund_code, rid, adate),
            })
        elif atype == "REDUCE":
            remaining = shares
            while remaining > 1e-6 and queue:
                head = queue[0]
                lot_shares = head["shares_remaining"]
                take = min(lot_shares, remaining)
                cost_consumed = head["cost_remaining"] * (take / lot_shares) if lot_shares else 0.0
                head["shares_remaining"] = lot_shares - take
                head["cost_remaining"] -= cost_consumed
                remaining -= take
                if head["shares_remaining"] <= 1e-6:
                    closed.append(queue.pop(0))
            if remaining > 1e-3:
                warnings.append(
                    f"REDUCE at {adate} action_id={r[0]} short by {remaining:.4f} shares "
                    f"(action ledger inconsistent)"
                )
        # other action types ignored
    return {"open_lots": queue, "closed_lots": closed, "warnings": warnings}


def insert_lots(conn: sqlite3.Connection, *, bot_id: str, fund_code: str,
                run_id: str, summary: dict) -> int:
    inserted = 0
    rid = run_id or ""
    for lot, status in [(l, "open") for l in summary["open_lots"]] + \
                       [(l, "closed") for l in summary["closed_lots"]]:
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, "
            " source_order_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, fund_code, rid, lot.get("holding_id"),
             lot["entry_date"], round(lot["entry_nav"], 6),
             round(lot["shares_initial"], 6), round(lot["shares_remaining"], 6),
             round(lot["cost_initial"], 2), round(lot["cost_remaining"], 2),
             lot.get("source_order_id"), status),
        )
        inserted += 1
    return inserted


def backfill_synthetic_lots(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """For each active holding whose rebuilt-from-actions lot_sum < holdings.shares,
    synthesize one 'open' lot covering the difference. Uses holdings.entry_date as
    the lot's entry_date (the best signal we have for fee-tier purposes) and
    holdings.entry_nav for the lot's entry_nav.

    Reason: some legacy holdings were created/modified outside the action ledger
    (e.g., admin scripts, init_fund_account variants). Without this backfill,
    future SELLs against those holdings would fail with 'insufficient open lots'.

    Returns (count_inserted, warnings).
    """
    inserted = 0
    warnings: list[str] = []
    rows = conn.execute(
        "SELECT h.holding_id, h.bot_id, h.fund_code, COALESCE(h.run_id,'') AS run_id, "
        "       h.entry_date, h.entry_nav, h.shares, h.amount_invested, "
        "       (SELECT COALESCE(SUM(shares_remaining), 0) "
        "          FROM fund_bot_holding_lots l "
        "         WHERE l.holding_id=h.holding_id AND l.status='open') AS lot_shares, "
        "       (SELECT COALESCE(SUM(cost_remaining), 0) "
        "          FROM fund_bot_holding_lots l "
        "         WHERE l.holding_id=h.holding_id AND l.status='open') AS lot_cost "
        "FROM fund_bot_holdings h WHERE h.status='active'"
    ).fetchall()
    for r in rows:
        hid, bot, fund, rid, entry_date, entry_nav, h_shares, h_cost, lot_shares, lot_cost = r
        h_shares = float(h_shares or 0.0)
        h_cost = float(h_cost or 0.0)
        lot_shares = float(lot_shares or 0.0)
        lot_cost = float(lot_cost or 0.0)

        diff_shares = h_shares - lot_shares
        diff_cost = h_cost - lot_cost
        if diff_shares <= TOLERANCE_SHARES:
            continue   # already covered (or lots overshoot — separate problem)
        if not entry_date:
            warnings.append(
                f"holding_id={hid} ({bot}/{fund}/run={rid}): need backfill {diff_shares:.4f} "
                f"shares but holding has no entry_date — skipped"
            )
            continue
        nav = float(entry_nav) if entry_nav else (
            diff_cost / diff_shares if diff_shares > 0 else 0.0
        )
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, "
            " source_order_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open')",
            (bot, fund, rid, hid, entry_date, round(nav, 6),
             round(diff_shares, 6), round(diff_shares, 6),
             round(max(diff_cost, 0.0), 2), round(max(diff_cost, 0.0), 2)),
        )
        inserted += 1
        warnings.append(
            f"backfilled synthetic lot for holding_id={hid} ({bot}/{fund}/run={rid}): "
            f"+{diff_shares:.4f} shares cost +{diff_cost:.2f} entry={entry_date}"
        )
    return inserted, warnings


def check_invariants(conn: sqlite3.Connection) -> list[str]:
    """Compare open-lot sums to active-holding sums at (bot, fund, run) granularity.

    The lots runtime (_consume_lots_fifo) consumes by (bot, fund, run), NOT per
    holding_id, so the invariant that must hold is the GROUP aggregate:
        SUM(open lot.shares) for (bot,fund,run) == SUM(active holdings.shares)
    Comparing per individual holding gives false positives when a (bot,fund,run)
    group legitimately has more than one active holdings row.

    Tolerances are relative: float-stored shares drift by ~1e-3, and the holdings
    cost column is 2-decimal, so we scale the threshold with the magnitude.
    """
    warnings: list[str] = []

    # Aggregate active holdings by (bot, fund, run).
    hold_rows = conn.execute(
        "SELECT bot_id, fund_code, COALESCE(run_id,'') AS run_id, "
        "       COUNT(*) AS n_active, "
        "       COALESCE(SUM(shares), 0) AS h_shares, "
        "       COALESCE(SUM(amount_invested), 0) AS h_cost, "
        "       MIN(entry_date) AS h_entry "
        "FROM fund_bot_holdings WHERE status='active' "
        "GROUP BY bot_id, fund_code, COALESCE(run_id,'')"
    ).fetchall()

    # Aggregate open lots by (bot, fund, run) into a lookup.
    lot_rows = conn.execute(
        "SELECT bot_id, fund_code, run_id, "
        "       COALESCE(SUM(shares_remaining), 0) AS lot_shares, "
        "       COALESCE(SUM(cost_remaining), 0) AS lot_cost, "
        "       MIN(entry_date) AS lot_entry "
        "FROM fund_bot_holding_lots WHERE status='open' "
        "GROUP BY bot_id, fund_code, run_id"
    ).fetchall()
    lots_by_key = {
        (r[0], r[1], r[2]): (float(r[3] or 0.0), float(r[4] or 0.0), r[5])
        for r in lot_rows
    }

    for bot, fund, rid, n_active, h_shares, h_cost, h_entry in hold_rows:
        h_shares = float(h_shares or 0.0)
        h_cost = float(h_cost or 0.0)
        lot_shares, lot_cost, lot_entry = lots_by_key.get((bot, fund, rid), (0.0, 0.0, None))

        tag = f"({bot}/{fund}/run={rid})"
        if n_active > 1:
            tag += f" [{n_active} active holdings rows — pre-existing duplicate]"

        share_tol = max(TOLERANCE_SHARES, 1e-6 * h_shares)
        cost_tol = max(1.0, 1e-3 * h_cost)
        if abs(h_shares - lot_shares) > share_tol:
            warnings.append(
                f"{tag} shares mismatch: holdings_sum={h_shares:.4f} "
                f"lots_sum={lot_shares:.4f} (diff {h_shares - lot_shares:+.4f})"
            )
        if abs(h_cost - lot_cost) > cost_tol:
            warnings.append(
                f"{tag} cost mismatch: holdings_sum={h_cost:.2f} "
                f"lots_sum={lot_cost:.2f} (diff {h_cost - lot_cost:+.2f})"
            )
        if lot_entry and h_entry and lot_entry != h_entry:
            warnings.append(
                f"{tag} entry_date drift: holdings_min={h_entry} "
                f"oldest_open_lot={lot_entry} (cosmetic — lots carry own entry_date)"
            )
    return warnings


def close_orphan_lots(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Mark open lots as 'orphan' when their (bot, fund, run) has no active holding.

    Action replay groups by actions.run_id, but the holdings table sometimes keys the
    SAME logical position under a different run_id (the order_run_id / settle_run_id
    split documented in server.py). That leaves open lots under run_ids that own no
    active holding — stale residue the SELL path can never reach (no holding → no sell).

    Active holdings are already fully covered by lots under their own run_id (the
    invariant check passes), so these orphan lots are not needed by any live position.
    We flip them to status='orphan' (excluded from FIFO, which only selects 'open')
    rather than DELETE, so the replay record stays auditable.

    Returns (count, summary_warnings).
    """
    rows = conn.execute(
        "SELECT l.bot_id, l.fund_code, l.run_id, "
        "       COUNT(*) AS n_lots, COALESCE(SUM(l.shares_remaining), 0) AS sh "
        "FROM fund_bot_holding_lots l "
        "WHERE l.status='open' AND NOT EXISTS ("
        "   SELECT 1 FROM fund_bot_holdings h "
        "    WHERE h.bot_id=l.bot_id AND h.fund_code=l.fund_code "
        "      AND COALESCE(h.run_id,'')=l.run_id AND h.status='active') "
        "GROUP BY l.bot_id, l.fund_code, l.run_id"
    ).fetchall()
    notes: list[str] = []
    total = 0
    for bot, fund, rid, n_lots, sh in rows:
        conn.execute(
            "UPDATE fund_bot_holding_lots SET status='orphan' "
            "WHERE status='open' AND bot_id=? AND fund_code=? AND run_id=?",
            (bot, fund, rid),
        )
        total += n_lots
        notes.append(
            f"orphaned {n_lots} open lots ({float(sh):.4f} shares) for "
            f"{bot}/{fund}/run={rid or '<null>'} — no active holding under this run_id"
        )
    return total, notes


def reconcile_holdings_to_fifo(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Overwrite active holdings' amount_invested / entry_date / unrealized_pnl to match
    the FIFO open-lot sums (the new cost convention the user adopted).

    Single-holding (bot,fund,run) groups reconcile at group granularity — this also
    covers open lots whose holding_id is NULL. The lone multi-holding group reconciles
    per holding_id (its lots all carry a holding_id, so the split is well-defined).

    market_value / latest_nav are left untouched; unrealized_pnl is recomputed from the
    existing market_value and the new cost so the row stays internally consistent.
    """
    notes: list[str] = []
    updated = 0
    groups = conn.execute(
        "SELECT bot_id, fund_code, COALESCE(run_id,'') AS rid, COUNT(*) AS n, "
        "       GROUP_CONCAT(holding_id) AS hids "
        "FROM fund_bot_holdings WHERE status='active' "
        "GROUP BY bot_id, fund_code, COALESCE(run_id,'')"
    ).fetchall()

    for bot, fund, rid, n, hids in groups:
        if n == 1:
            hid = int(hids)
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_remaining), 0), MIN(entry_date) "
                "FROM fund_bot_holding_lots "
                "WHERE bot_id=? AND fund_code=? AND run_id=? AND status='open'",
                (bot, fund, rid),
            ).fetchone()
            if _apply_reconcile(conn, hid, float(row[0] or 0.0), row[1], notes,
                                bot, fund, rid):
                updated += 1
        else:
            for hid_str in str(hids).split(","):
                hid = int(hid_str)
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_remaining), 0), MIN(entry_date) "
                    "FROM fund_bot_holding_lots WHERE holding_id=? AND status='open'",
                    (hid,),
                ).fetchone()
                if _apply_reconcile(conn, hid, float(row[0] or 0.0), row[1], notes,
                                    bot, fund, rid):
                    updated += 1
    return updated, notes


def _apply_reconcile(conn: sqlite3.Connection, holding_id: int, lot_cost: float,
                     lot_entry: str | None, notes: list[str],
                     bot: str, fund: str, rid: str) -> bool:
    cur = conn.execute(
        "SELECT amount_invested, entry_date, market_value "
        "FROM fund_bot_holdings WHERE holding_id=?",
        (holding_id,),
    ).fetchone()
    if cur is None:
        return False
    old_cost = float(cur[0] or 0.0)
    old_entry = cur[1]
    mv = float(cur[2] or 0.0)
    new_cost = round(lot_cost, 2)
    new_entry = lot_entry or old_entry
    new_pnl = round(mv - new_cost, 2)
    new_pnl_pct = round((mv - new_cost) / new_cost * 100, 4) if new_cost else 0.0
    conn.execute(
        "UPDATE fund_bot_holdings SET amount_invested=?, entry_date=?, "
        "unrealized_pnl=?, unrealized_pnl_pct=? WHERE holding_id=?",
        (new_cost, new_entry, new_pnl, new_pnl_pct, holding_id),
    )
    changed = abs(old_cost - new_cost) > 0.01 or (old_entry != new_entry)
    if changed:
        notes.append(
            f"reconciled holding_id={holding_id} ({bot}/{fund}/run={rid}): "
            f"cost {old_cost:.2f}->{new_cost:.2f} entry {old_entry}->{new_entry}"
        )
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="Override DB path (defaults to $FUND_DB_PATH or db.py default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen but make no DB writes")
    ap.add_argument("--backup-suffix", default=None,
                    help="Override backup file suffix (default: .bak-pre-lots-<unix-ts>)")
    args = ap.parse_args()

    db_path = args.db or _default_db_path()
    print(f"DB: {db_path}")
    if not os.path.exists(db_path):
        print(f"FATAL: DB does not exist: {db_path}", file=sys.stderr)
        sys.exit(2)

    if not args.dry_run:
        suffix = args.backup_suffix or f".bak-pre-lots-{int(time.time())}"
        backup = db_path + suffix
        shutil.copy2(db_path, backup)
        print(f"Backup: {backup}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _ensure_table(conn)
        # Always insert into a transaction; commit at end (unless --dry-run).
        conn.execute("BEGIN")
        conn.execute("DELETE FROM fund_bot_holding_lots")

        triples = conn.execute(
            "SELECT DISTINCT bot_id, fund_code, COALESCE(run_id,'') AS run_id "
            "FROM fund_bot_actions "
            "WHERE bot_id IS NOT NULL AND fund_code IS NOT NULL"
        ).fetchall()
        print(f"Scanning {len(triples)} (bot, fund, run) groups from fund_bot_actions ...")

        stats = defaultdict(int)
        all_warnings: list[str] = []
        for bot_id, fund_code, rid in triples:
            summary = replay_group(conn, bot_id=bot_id, fund_code=fund_code, run_id=rid)
            stats["groups"] += 1
            stats["open"] += len(summary["open_lots"])
            stats["closed"] += len(summary["closed_lots"])
            for w in summary["warnings"]:
                all_warnings.append(f"[{bot_id}/{fund_code}/run={rid or '<null>'}] {w}")
            inserted = insert_lots(conn, bot_id=bot_id, fund_code=fund_code,
                                   run_id=rid, summary=summary)
            stats["inserted"] += inserted

        print(f"Replayed: {stats['groups']} groups, "
              f"{stats['open']} open lots, {stats['closed']} closed lots, "
              f"{stats['inserted']} rows {'would be ' if args.dry_run else ''}inserted from actions.")

        # Backfill synthetic lots for active holdings whose action-derived lot_sum
        # is short of holdings.shares (legacy holdings created outside the action ledger).
        backfilled, backfill_warnings = backfill_synthetic_lots(conn)
        all_warnings.extend(backfill_warnings)
        stats["inserted"] += backfilled
        print(f"Backfilled synthetic lots for {backfilled} holdings "
              f"(action ledger gaps).")

        # Neutralize open lots that belong to a (bot,fund,run) with no active holding
        # (replay residue from the order_run_id/settle_run_id split). They become
        # status='orphan' so FIFO never touches them.
        orphaned, orphan_notes = close_orphan_lots(conn)
        all_warnings.extend(orphan_notes)
        print(f"Orphaned {orphaned} open lots with no active holding (run_id residue).")

        # Reconcile active holdings' cost basis + entry_date to the FIFO open-lot sums
        # (user chose 方案A: holdings adopt the FIFO convention immediately).
        reconciled, reconcile_notes = reconcile_holdings_to_fifo(conn)
        all_warnings.extend(reconcile_notes)
        print(f"Reconciled {reconciled} active holdings to FIFO cost/entry_date.")

        # Invariants run AFTER inserts + backfill + reconcile so check sees final state.
        inv_warnings = check_invariants(conn)
        all_warnings.extend(inv_warnings)
        if all_warnings:
            print()
            print(f"WARNINGS ({len(all_warnings)}):")
            for w in all_warnings[:50]:
                print(f"  - {w}")
            if len(all_warnings) > 50:
                print(f"  ... ({len(all_warnings) - 50} more)")
        else:
            print("Invariants pass: all active holdings match their lot sums.")

        if args.dry_run:
            conn.execute("ROLLBACK")
            print()
            print("[dry-run] no writes committed")
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
