#!/usr/bin/env python3
"""Rewind a bot run to a given cutoff date (dry-run + apply).

Usage:
    python3 rewind_run.py --run-id <RUN_ID> --cutoff-date YYYY-MM-DD [--apply]

Behavior:
    - Backs up fund.db / run log / state.json to timestamped copies
    - Deletes DB rows on/after cutoff for the run's bot(s)
    - Reverts holdings that exited on/after cutoff (exit_date=NULL, status='active')
    - Deletes per-day dirs under the run dir on/after cutoff
    - Deletes workspace research notes on/after cutoff (keeps earlier ones)
    - Prunes run memory/store.jsonl entries whose created_at date is on/after
      cutoff (malformed or undated entries are conservatively kept)
    - Prunes prediction tracker sections dated on/after cutoff
    - Cleans rl-openclaw sessions dir: removes sessions.json entries with
      trailing YYYY-MM-DD >= cutoff and deletes the matching {UUID}.jsonl /
      {UUID}.state.json files
    - Rebuilds fund_bot_holdings / fund_bot_holding_lots / fund_bot_position_snapshots
      / fund_bot_accounts.cash by replaying fund_bot_actions up to LAST_KEEP
      (uses fund-portfolio-mcp's _replay_and_repair_fund_holdings — the only
      source of truth that reconciles cash & shares from action history)
    - Rewinds state.json cursor/current_date to cutoff; drops pid/aborted_reason
    - Sets last_deep_research_date to the latest research note file still kept

Assumes the run's state.json exposes: run_id, bots, trading_dates, cursor.

Default is dry-run — pass --apply to actually execute.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BASE = Path("/home/rooot/agent_invest_lab")
DB_PATH = BASE / "data" / "fund.db"
WORLD_ROOT = BASE / "world"
FPMCP_DIR = BASE / "fund-portfolio-mcp"

# Tables scoped by (bot_id, run_id) with a date column
DATE_FILTERED = [
    ("fund_bot_reviews", "review_date"),
    ("fund_bot_actions", "action_date"),
    ("fund_bot_daily_snapshots", "trade_date"),
    ("fund_bot_position_snapshots", "trade_date"),
    ("fund_allocation_runs", "trade_date"),
    ("fund_selection_runs", "trade_date"),
    ("fund_paradigm_runs", "trade_date"),
    ("fund_bot_performance", "trade_date"),
    ("fund_bot_charters", "declared_date"),
    ("fund_bot_satellite_reviews", "review_date"),
]
# fund_bot_orders has no run_id column; use order_run_id OR settle_run_id
ORDERS_WHERE_TMPL = ("bot_id=? AND order_date>=? AND "
                     "(order_run_id=? OR settle_run_id=?)")

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_state(state_file: Path) -> dict:
    with open(state_file) as f:
        return json.load(f)


def compute_cursor(trading_dates: list[str], cutoff: str) -> int:
    if cutoff in trading_dates:
        return trading_dates.index(cutoff)
    # cutoff not a trading day → use the first trading day >= cutoff
    for i, d in enumerate(trading_dates):
        if d >= cutoff:
            return i
    raise SystemExit(f"cutoff {cutoff} is beyond trading_dates range")


def prev_trading_day(trading_dates: list[str], cutoff_index: int) -> str:
    if cutoff_index == 0:
        raise SystemExit(f"cutoff is the very first trading day; nothing to keep")
    return trading_dates[cutoff_index - 1]


def latest_research_before(research_dir: Path, cutoff: str) -> str | None:
    """Return YYYY-MM-DD of the latest research note strictly before cutoff."""
    if not research_dir.exists():
        return None
    dates = set()
    for f in research_dir.iterdir():
        if not f.is_file():
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", f.name)
        if m and m.group(1) < cutoff:
            dates.add(m.group(1))
    return max(dates) if dates else None


def backup(paths: dict, apply: bool) -> int:
    ts = int(time.time())
    db_backup = DB_PATH.parent / f"fund-backup-pre-rewind-{paths['run_id']}-{paths['cutoff']}-{ts}.db"
    log_backup = paths['log_file'].parent / f"{paths['log_file'].name}.pre-rewind-{ts}"
    state_backup = paths['state_file'].parent / f"state.json.pre-rewind-{ts}"
    memory_backup = paths['memory_store'].parent / f"store.jsonl.pre-rewind-{ts}"
    tracker_backup = paths['prediction_tracker'].parent / f"tracker.md.pre-rewind-{ts}"
    print(f"\n=== backup ===")
    print(f"  fund.db  -> {db_backup}")
    print(f"  log      -> {log_backup}  (exists={paths['log_file'].exists()})")
    print(f"  state    -> {state_backup}")
    print(f"  memory   -> {memory_backup}  (exists={paths['memory_store'].exists()})")
    print(f"  tracker  -> {tracker_backup}  (exists={paths['prediction_tracker'].exists()})")
    if apply:
        shutil.copy2(DB_PATH, db_backup)
        if paths['log_file'].exists():
            shutil.copy2(paths['log_file'], log_backup)
        shutil.copy2(paths['state_file'], state_backup)
        if paths['memory_store'].exists():
            shutil.copy2(paths['memory_store'], memory_backup)
        if paths['prediction_tracker'].exists():
            shutil.copy2(paths['prediction_tracker'], tracker_backup)
    return ts


def prune_memory_store(memory_store: Path, cutoff: str, apply: bool):
    """Remove JSONL memories dated on/after cutoff, preserving unsafe lines."""
    print(f"\n=== memory store cleanup ===")
    if not memory_store.exists():
        print(f"  no store.jsonl at {memory_store} — skipping")
        return

    lines = memory_store.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = []
    remove_count = 0
    unsafe_count = 0
    date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ])")

    for line in lines:
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("memory record is not a JSON object")
            created_at = record.get("created_at")
            match = date_re.match(created_at) if isinstance(created_at, str) else None
            if not match:
                raise ValueError("missing or unsupported created_at")
            # Reject impossible calendar dates instead of comparing them as strings.
            time.strptime(match.group(1), "%Y-%m-%d")
        except (json.JSONDecodeError, TypeError, ValueError):
            kept.append(line)
            unsafe_count += 1
            continue

        if match.group(1) >= cutoff:
            remove_count += 1
        else:
            kept.append(line)

    print(f"  entries to remove: {remove_count}  (kept: {len(kept)})")
    if unsafe_count:
        print(f"  WARNING: kept {unsafe_count} malformed/undated entries")
    if not apply:
        return

    # Write beside the store and atomically replace it so readers never see a
    # partially-written JSONL file.
    mode = memory_store.stat().st_mode
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=memory_store.parent,
                prefix=".store.jsonl.rewind-", delete=False) as temp:
            temp_name = temp.name
            temp.writelines(kept)
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, memory_store)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    print("  memory store cleanup applied")


def prune_prediction_tracker(tracker: Path, cutoff: str, apply: bool):
    """Remove markdown prediction sections whose heading date is >= cutoff."""
    print(f"\n=== prediction tracker cleanup ===")
    if not tracker.exists():
        print(f"  no tracker at {tracker} — skipping")
        return

    kept = []
    keep_section = True
    removed_sections = 0
    heading_re = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:\s|$)")
    for line in tracker.read_text(encoding="utf-8").splitlines(keepends=True):
        match = heading_re.match(line)
        if match:
            keep_section = match.group(1) < cutoff
            if not keep_section:
                removed_sections += 1
        if keep_section:
            kept.append(line)

    print(f"  prediction sections to remove: {removed_sections}")
    if not apply:
        return

    mode = tracker.stat().st_mode
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=tracker.parent,
                prefix=".tracker.md.rewind-", delete=False) as temp:
            temp_name = temp.name
            temp.writelines(kept)
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, tracker)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    print("  prediction tracker cleanup applied")


def db_cleanup(bot_id: str, run_id: str, cutoff: str, last_keep: str, apply: bool):
    print(f"\n=== db cleanup: run_id={run_id}, bot_id={bot_id}, cutoff>={cutoff} ===")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    plan = []

    # 1. simple date-filtered tables
    for tbl, dcol in DATE_FILTERED:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE bot_id=? AND run_id=? AND {dcol}>=?",
            (bot_id, run_id, cutoff)).fetchone()[0]
        plan.append((tbl, dcol, n))

    # 1b. orders (no run_id column)
    orders_params = (bot_id, cutoff, run_id, run_id)
    n_orders = conn.execute(
        f"SELECT COUNT(*) FROM fund_bot_orders WHERE {ORDERS_WHERE_TMPL}",
        orders_params).fetchone()[0]
    plan.append(("fund_bot_orders", "order_date+run_id", n_orders))

    # 2. holdings — delete entered >= cutoff; revert exit >= cutoff
    hold_del = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND entry_date>=?",
        (bot_id, run_id, cutoff)).fetchone()[0]
    hold_revert = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_holdings WHERE bot_id=? AND run_id=? "
        "AND exit_date IS NOT NULL AND exit_date>=?",
        (bot_id, run_id, cutoff)).fetchone()[0]
    plan.append(("fund_bot_holdings", "entry_date DEL", hold_del))
    plan.append(("fund_bot_holdings", "exit_date REVERT", hold_revert))

    # 3. holding_lots — delete entered >= cutoff
    lots_del = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_holding_lots WHERE bot_id=? AND run_id=? AND entry_date>=?",
        (bot_id, run_id, cutoff)).fetchone()[0]
    plan.append(("fund_bot_holding_lots", "entry_date DEL", lots_del))

    # 4. bot_research_reports
    br_del = conn.execute(
        "SELECT COUNT(*) FROM bot_research_reports WHERE bot_id=? AND run_id=? AND as_of_date>=?",
        (bot_id, run_id, cutoff)).fetchone()[0]
    plan.append(("bot_research_reports", "as_of_date", br_del))

    # 5. research_run / research_note — by bot_id + timestamps
    rr_del = conn.execute(
        "SELECT COUNT(*) FROM research_run WHERE bot_id=? AND (finished_at>=? OR started_at>=?)",
        (bot_id, cutoff, cutoff)).fetchone()[0]
    plan.append(("research_run", "finished_at/started_at", rr_del))
    rn_del = conn.execute(
        "SELECT COUNT(*) FROM research_note WHERE bot_id=? AND created_at>=?",
        (bot_id, cutoff)).fetchone()[0]
    plan.append(("research_note", "created_at", rn_del))

    print(f"{'table':<32} {'field':<28} {'rows to touch':>12}")
    for tbl, fld, n in plan:
        print(f"{tbl:<32} {fld:<28} {n:>12}")

    if not apply:
        conn.close()
        return

    with conn:  # transaction
        for tbl, dcol in DATE_FILTERED:
            conn.execute(
                f"DELETE FROM {tbl} WHERE bot_id=? AND run_id=? AND {dcol}>=?",
                (bot_id, run_id, cutoff))
        conn.execute(f"DELETE FROM fund_bot_orders WHERE {ORDERS_WHERE_TMPL}", orders_params)
        conn.execute(
            "DELETE FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND entry_date>=?",
            (bot_id, run_id, cutoff))
        conn.execute(
            "UPDATE fund_bot_holdings SET exit_date=NULL, status='active' "
            "WHERE bot_id=? AND run_id=? AND exit_date IS NOT NULL AND exit_date>=?",
            (bot_id, run_id, cutoff))
        conn.execute(
            "DELETE FROM fund_bot_holding_lots WHERE bot_id=? AND run_id=? AND entry_date>=?",
            (bot_id, run_id, cutoff))
        conn.execute(
            "DELETE FROM bot_research_reports WHERE bot_id=? AND run_id=? AND as_of_date>=?",
            (bot_id, run_id, cutoff))
        conn.execute(
            "DELETE FROM research_run WHERE bot_id=? AND (finished_at>=? OR started_at>=?)",
            (bot_id, cutoff, cutoff))
        conn.execute(
            "DELETE FROM research_note WHERE bot_id=? AND created_at>=?",
            (bot_id, cutoff))
    print("db cleanup applied — cash/shares will be reconciled by replay step below")
    conn.close()


def _rebuild_lots(conn, bot_id: str, run_id: str, as_of_date: str, srv) -> None:
    """Rebuild fund_bot_holding_lots for every active holding from action replay.

    Walks fund_bot_actions (ADD/REDUCE, ordered by date/id) for each active
    fund and simulates FIFO consumption in memory, then wipes the holding's
    existing lot rows and re-inserts the reconstructed set. Called after
    _replay_and_repair_fund_holdings has settled holdings.shares — this
    guarantees sum(open lots.shares_remaining) == holdings.shares so that
    subsequent sell orders don't get stuck in pending because lots FIFO can't
    find inventory.
    """
    holdings = conn.execute(
        "SELECT holding_id, fund_code, shares FROM fund_bot_holdings "
        "WHERE bot_id=? AND run_id=? AND status='active' ORDER BY fund_code",
        (bot_id, run_id)).fetchall()
    print(f"  rebuilding lots for {len(holdings)} active holdings")
    print(f"    {'fund':<10} {'holding':<10} {'h.shares':>14} {'lots_sum':>14} {'open':>5} {'total':>5}")

    for h in holdings:
        actions = conn.execute(
            "SELECT action_id, action_type, action_date, nav_used, amount, shares, fee "
            "FROM fund_bot_actions WHERE bot_id=? AND run_id=? AND fund_code=? "
            "AND action_type IN ('ADD','REDUCE') ORDER BY action_date, action_id",
            (bot_id, run_id, h["fund_code"])).fetchall()

        lots: list[dict] = []
        for a in actions:
            # Use the exact same point-in-time NAV reconstruction as the canonical
            # holdings replay.  Using action.shares directly here used to diverge
            # whenever a historical REDUCE stored rounded shares but replay derived
            # amount / real_nav (e.g. 019633: 39702 vs 39701.996959).  That made
            # sum(open lots) larger than holdings.shares and blocked the next full
            # exit with "full-exit lot residue too large".
            effective_shares, effective_nav = srv._effective_action_shares(
                conn,
                h["fund_code"],
                a["amount"],
                a["shares"],
                a["nav_used"],
                a["fee"],
                a["action_date"],
                as_of_date,
                a["action_type"],
            )
            effective_shares = float(effective_shares or 0.0)
            effective_nav = float(effective_nav or a["nav_used"] or 0.0)
            if a["action_type"] == "ADD":
                lots.append({
                    "entry_date": a["action_date"],
                    "entry_nav": effective_nav,
                    "shares_initial": effective_shares,
                    "shares_remaining": effective_shares,
                    "cost_initial": float(a["amount"] or 0),
                    "cost_remaining": float(a["amount"] or 0),
                })
            else:  # REDUCE — FIFO consume open lots
                want = effective_shares
                for lot in lots:
                    if want <= 1e-6:
                        break
                    if lot["shares_remaining"] <= 1e-6:
                        continue
                    take = min(want, lot["shares_remaining"])
                    ratio = take / lot["shares_initial"] if lot["shares_initial"] > 0 else 0
                    lot["shares_remaining"] -= take
                    lot["cost_remaining"] -= ratio * lot["cost_initial"]
                    want -= take
                # Canonical holdings replay closes a position cycle when the
                # post-REDUCE remainder is worth less than ¥1.  Mirror that
                # here immediately; otherwise an old dust lot survives and is
                # incorrectly merged into a later re-entry in the same fund.
                remaining = sum(
                    lot["shares_remaining"] for lot in lots
                    if lot["shares_remaining"] > 1e-6
                )
                if remaining > 0 and srv._is_dust_shares(remaining, effective_nav):
                    for lot in lots:
                        if lot["shares_remaining"] > 1e-6:
                            lot["shares_remaining"] = 0.0
                            lot["cost_remaining"] = 0.0

        open_lots = [l for l in lots if l["shares_remaining"] > 1e-6]
        open_sum = sum(l["shares_remaining"] for l in open_lots)
        target_shares = float(h["shares"] or 0.0)
        delta = target_shares - open_sum
        invariant_eps = max(1e-5, abs(target_shares) * 1e-10)
        if abs(delta) > invariant_eps:
            current_nav, _ = srv._get_nav(conn, h["fund_code"], as_of_date)
            # Match settlement's economic dust rule: a sub-¥1 discrepancy is
            # harmless historical rounding and may be reconciled; a material
            # discrepancy must stop the rewind instead of corrupting inventory.
            if not srv._is_dust_shares(abs(delta), float(current_nav or 0.0)):
                raise RuntimeError(
                    f"lot rebuild invariant failed: bot={bot_id} fund={h['fund_code']} "
                    f"holding={target_shares:.9f} lots={open_sum:.9f} delta={delta:.9f}"
                )
        # holdings.shares is persisted at 6 decimals while replay math is full
        # precision.  Put the sub-epsilon rounding delta into the final open lot
        # so the persisted rows sum to the persisted holding exactly.
        if open_lots and abs(delta) > 0:
            lot = open_lots[-1]
            unit_cost = (lot["cost_initial"] / lot["shares_initial"]
                         if lot["shares_initial"] > 0 else 0.0)
            lot["shares_remaining"] += delta
            lot["cost_remaining"] = max(0.0, lot["cost_remaining"] + delta * unit_cost)
            open_sum = target_shares
        n_open = sum(1 for l in lots if l["shares_remaining"] > 1e-6)
        # If open_sum < 1 share, treat as float residue (e.g. 100000/1.1578 -
        # 86370.7 = 0.003058) and auto-close the holding + all its lots. Real
        # positions are always ≥1 share; anything smaller is rounding drift
        # that would otherwise leave a zombie active holding.
        residue = 0 < open_sum < 1.0
        if residue:
            open_sum = 0.0
            n_open = 0
            for l in lots:
                l["shares_remaining"] = 0.0
                l["cost_remaining"] = 0.0
        tag = " [RESIDUE→CLOSED]" if residue else ""
        print(f"    {h['fund_code']:<10} {h['holding_id']:<10} "
              f"{float(h['shares'] or 0):>14.6f} {open_sum:>14.6f} {n_open:>5} {len(lots):>5}{tag}")

        conn.execute("DELETE FROM fund_bot_holding_lots WHERE holding_id=?", (h["holding_id"],))
        for lot in lots:
            status = "open" if lot["shares_remaining"] > 1e-6 else "closed"
            conn.execute(
                "INSERT INTO fund_bot_holding_lots "
                "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
                "shares_initial, shares_remaining, cost_initial, cost_remaining, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (bot_id, h["fund_code"], run_id, h["holding_id"],
                 lot["entry_date"], round(lot["entry_nav"], 6),
                 round(lot["shares_initial"], 6),
                 round(max(0.0, lot["shares_remaining"]), 6),
                 round(lot["cost_initial"], 2),
                 round(max(0.0, lot["cost_remaining"]), 2),
                 status))
        if residue:
            # Use the last action's date as exit if available, else today.
            exit_dt = actions[-1]["action_date"] if actions else time.strftime("%Y-%m-%d")
            conn.execute(
                "UPDATE fund_bot_holdings SET status='closed', exit_date=?, shares=0, "
                "amount_invested=0, market_value=0, unrealized_pnl=0, unrealized_pnl_pct=0 "
                "WHERE holding_id=?",
                (exit_dt, h["holding_id"]))

    # Close orphan open lots — those attached to closed/missing holdings will
    # otherwise be consumed by _consume_lots_fifo (which filters by
    # bot_id+fund_code+run_id, NOT holding_id) and let sells drain more shares
    # than actually held.
    active_ids = [row[0] for row in conn.execute(
        "SELECT holding_id FROM fund_bot_holdings WHERE bot_id=? AND run_id=? AND status='active'",
        (bot_id, run_id)).fetchall()]
    if active_ids:
        placeholders = ",".join("?" * len(active_ids))
        orphan = conn.execute(
            f"UPDATE fund_bot_holding_lots SET status='closed', shares_remaining=0, "
            f"cost_remaining=0 WHERE bot_id=? AND run_id=? AND status='open' "
            f"AND holding_id NOT IN ({placeholders})",
            [bot_id, run_id] + active_ids).rowcount
    else:
        orphan = conn.execute(
            "UPDATE fund_bot_holding_lots SET status='closed', shares_remaining=0, "
            "cost_remaining=0 WHERE bot_id=? AND run_id=? AND status='open'",
            (bot_id, run_id)).rowcount
    print(f"  closed {orphan} orphan open lots (belonging to closed/missing holdings)")


def replay_repair(bot_id: str, run_id: str, last_keep: str, apply: bool):
    """Rebuild holdings + accounts.cash from fund_bot_actions replay.

    fund_bot_holdings.shares/amount_invested/market_value can be stale after
    previous rewinds or run-id migrations; the only correct way to restore them
    is to replay the action log through fund-portfolio-mcp's canonical function.
    Also rebuilds fund_bot_position_snapshots for the kept trade dates.
    """
    print(f"\n=== replay-and-repair holdings via fund-portfolio-mcp ===")
    print(f"  bot={bot_id}  run={run_id}  as_of={last_keep}")
    if not apply:
        print("  (skipped in dry-run)")
        return

    sys.path.insert(0, str(FPMCP_DIR))
    os.environ["FUND_DB_PATH"] = str(DB_PATH)
    import db as fmdb   # type: ignore
    fmdb.DB_PATH = str(DB_PATH)
    fmdb.init_db()
    import server as srv  # type: ignore

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    dates = [r[0] for r in conn.execute(
        "SELECT trade_date FROM fund_bot_daily_snapshots WHERE bot_id=? AND run_id=? "
        "ORDER BY trade_date ASC",
        (bot_id, run_id)).fetchall()]
    if not dates:
        print("  no daily_snapshots left — nothing to replay")
        conn.close()
        return

    pos_del = conn.execute(
        "DELETE FROM fund_bot_position_snapshots WHERE bot_id=? AND run_id=?",
        (bot_id, run_id)).rowcount
    print(f"  cleared {pos_del} stale position_snapshots, recomputing {len(dates)} dates "
          f"({dates[0]} → {dates[-1]})")
    for td in dates:
        result = srv._compute_fund_snapshot(conn, bot_id, td, run_id=run_id)
        if not result.get("success", True):
            raise RuntimeError(f"recompute failed for {td}: {result}")
    repaired = srv._replay_and_repair_fund_holdings(conn, bot_id, dates[-1], run_id=run_id)
    # _replay_and_repair_fund_holdings only touches accounts.cash. Zero the
    # T+1 in-transit / receivable buckets — any stale value here would leak
    # into the resumed run's "money I still expect to arrive" view.
    conn.execute(
        "UPDATE fund_bot_accounts SET cash_in_transit=0, cash_receivable=0, "
        "updated_at=datetime('now') WHERE bot_id=? AND run_id=?",
        (bot_id, run_id))
    conn.commit()
    print(f"  repaired {repaired} active holdings rows; zeroed cash_in_transit + cash_receivable")

    # Rebuild fund_bot_holding_lots from fund_bot_actions ADD/REDUCE.
    # _replay_and_repair_fund_holdings only fixes fund_bot_holdings; lots can
    # accumulate stale rows (shares_remaining=0 or missing entirely) from prior
    # corruption. sell orders route through _consume_lots_fifo which reads lots,
    # so if lots disagree with holdings.shares, sells will stay stuck in pending
    # forever. Rebuild ensures sum(open lots.shares_remaining) == holdings.shares.
    _rebuild_lots(conn, bot_id, run_id, dates[-1], srv)
    conn.commit()

    acc = conn.execute(
        "SELECT cash FROM fund_bot_accounts WHERE bot_id=? AND run_id=?",
        (bot_id, run_id)).fetchone()
    snap = conn.execute(
        "SELECT cash, invested_value, total_value FROM fund_bot_daily_snapshots "
        "WHERE bot_id=? AND run_id=? AND trade_date=?",
        (bot_id, run_id, dates[-1])).fetchone()
    print(f"  accounts.cash={acc['cash'] if acc else None}, "
          f"snapshot[{dates[-1]}] cash={snap['cash']} inv={snap['invested_value']} tot={snap['total_value']}")
    conn.close()


def rl_openclaw_cleanup(run_dir: Path, bot_id: str, cutoff: str, apply: bool):
    """Clean rl-openclaw session records dated >= cutoff.

    The dashboard at /world reads:
      <run>/rl-openclaw/agents/<bot>/sessions/sessions.json (keys end with -YYYY-MM-DD)
      <run>/rl-openclaw/agents/<bot>/sessions/{UUID}.jsonl + {UUID}.state.json
    """
    sess_dir = run_dir / "rl-openclaw" / "agents" / bot_id / "sessions"
    sess_json = sess_dir / "sessions.json"
    print(f"\n=== rl-openclaw sessions cleanup ===")
    if not sess_json.exists():
        print(f"  no sessions.json at {sess_json} — skipping")
        return
    data = json.load(open(sess_json))
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})$")
    to_remove_keys, to_delete_uuids = [], set()
    for key, entry in data.items():
        m = pat.search(key)
        if m and m.group(1) >= cutoff:
            to_remove_keys.append(key)
            sid = entry.get("sessionId")
            if sid:
                to_delete_uuids.add(sid)
    print(f"  sessions.json entries to remove: {len(to_remove_keys)}  "
          f"(kept: {len(data) - len(to_remove_keys)})")
    file_count = 0
    deep_run_ids = set()
    for sid in to_delete_uuids:
        for ext in ("jsonl", "state.json"):
            session_file = sess_dir / f"{sid}.{ext}"
            if session_file.exists():
                file_count += 1
                if ext == "jsonl":
                    raw = session_file.read_text(encoding="utf-8", errors="replace")
                    deep_run_ids.update(re.findall(
                        r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", raw))
    deep_session_files = []
    deep_tmp_dirs = []
    deep_tmp_root = run_dir / "workspaces" / bot_id / "tmp" / "research-loop" / "runs"
    for rid in sorted(deep_run_ids):
        for ext in ("jsonl", "state.json"):
            p = sess_dir / f"{rid}.{ext}"
            if p.exists():
                deep_session_files.append(p)
        p = deep_tmp_root / rid
        if p.exists():
            deep_tmp_dirs.append(p)
    # Remove stale deep-research runs not referenced by any session that will
    # remain after the rewind. These are commonly left by an earlier rewind and
    # can otherwise resurface through run ids cached in old artifacts.
    kept_session_ids = {
        entry.get("sessionId") for key, entry in data.items()
        if key not in to_remove_keys and entry.get("sessionId")
    }
    kept_deep_ids = set()
    for sid in kept_session_ids:
        p = sess_dir / f"{sid}.jsonl"
        if p.exists():
            raw = p.read_text(encoding="utf-8", errors="replace")
            kept_deep_ids.update(re.findall(
                r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", raw))
    orphan_deep_session_files = [
        p for p in sess_dir.glob("*.jsonl")
        if re.fullmatch(r"[0-9a-f]{12}\.jsonl", p.name)
        and p.stem not in kept_deep_ids
        and p not in deep_session_files
    ]
    orphan_deep_tmp_dirs = [
        p for p in deep_tmp_root.iterdir()
        if p.is_dir() and p.name not in kept_deep_ids and p not in deep_tmp_dirs
    ] if deep_tmp_root.exists() else []
    print(f"  session files to delete: {file_count}")
    print(f"  linked deep-research session files to delete: {len(deep_session_files)}")
    print(f"  linked deep-research tmp dirs to delete: {len(deep_tmp_dirs)}")
    print(f"  orphan deep-research session files to delete: {len(orphan_deep_session_files)}")
    print(f"  orphan deep-research tmp dirs to delete: {len(orphan_deep_tmp_dirs)}")

    if not apply:
        return
    for k in to_remove_keys:
        del data[k]
    with open(sess_json, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    for sid in to_delete_uuids:
        for ext in ("jsonl", "state.json"):
            p = sess_dir / f"{sid}.{ext}"
            if p.exists():
                p.unlink()
    for p in deep_session_files:
        p.unlink()
    for p in deep_tmp_dirs:
        shutil.rmtree(p)
    for p in orphan_deep_session_files:
        p.unlink()
    for p in orphan_deep_tmp_dirs:
        shutil.rmtree(p)
    print("  rl-openclaw sessions cleanup applied")


def fs_cleanup(run_dir: Path, workspace_research: Path, cutoff: str, apply: bool):
    print(f"\n=== fs cleanup ===")
    to_del = []
    for p in sorted(run_dir.iterdir()):
        if p.is_dir() and DATE_DIR_RE.match(p.name) and p.name >= cutoff:
            to_del.append(p)
    print(f"per-day dirs to delete: {len(to_del)}  "
          f"(first: {to_del[0].name if to_del else '-'}, "
          f"last: {to_del[-1].name if to_del else '-'})")

    notes_del = []
    if workspace_research.exists():
        for f in sorted(workspace_research.iterdir()):
            if f.is_file() and f.name >= cutoff:
                notes_del.append(f)
    print(f"research note files to delete: {len(notes_del)}")
    for f in notes_del[:5]:
        print(f"  {f.name}")
    if len(notes_del) > 5:
        print(f"  ... and {len(notes_del) - 5} more")

    if not apply:
        return
    for p in to_del:
        shutil.rmtree(p)
    for f in notes_del:
        f.unlink()
    print("fs cleanup applied")


def state_update(state_file: Path, new_cursor: int, cutoff: str,
                 last_deep_research_by_bot: dict[str, str | None], apply: bool):
    print(f"\n=== state.json update ===")
    with open(state_file) as f:
        s = json.load(f)
    print(f"  cursor        : {s['cursor']} -> {new_cursor}")
    print(f"  current_date  : {s['current_date']} -> {cutoff}")
    print(f"  status        : {s['status']} -> paused")
    kept_deep_dates = [d for d in last_deep_research_by_bot.values() if d]
    last_deep_research = max(kept_deep_dates) if kept_deep_dates else None
    print(f"  last_deep_research_date: {s.get('last_deep_research_date')} -> {last_deep_research}")
    print(f"  last_deep_research_dates: {s.get('last_deep_research_dates')} -> {last_deep_research_by_bot}")
    print(f"  pid / aborted_reason   : drop if present")
    if not apply:
        return
    s['cursor'] = new_cursor
    s['current_date'] = cutoff
    s['status'] = 'paused'
    if last_deep_research is not None:
        s['last_deep_research_date'] = last_deep_research
    else:
        s.pop('last_deep_research_date', None)
    s['last_deep_research_dates'] = {
        bot_id: deep_date
        for bot_id, deep_date in last_deep_research_by_bot.items()
        if deep_date is not None
    }
    s['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
    s.pop('pid', None)
    s.pop('aborted_reason', None)
    with open(state_file, 'w') as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    print("state.json updated")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True, help="e.g. bot105d-daily-agenticdeep-20260723")
    ap.add_argument("--cutoff-date", required=True,
                    help="First date to REMOVE (inclusive), YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true",
                    help="actually execute (default: dry-run)")
    args = ap.parse_args()

    run_id = args.run_id
    cutoff = args.cutoff_date
    apply = args.apply

    run_dir = WORLD_ROOT / "runtime" / "runs" / run_id
    log_file = WORLD_ROOT / "runtime" / "runs" / f"{run_id}.log"
    state_file = run_dir / "state.json"
    memory_store = run_dir / "memory" / "store.jsonl"
    if not state_file.exists():
        raise SystemExit(f"state.json not found: {state_file}")

    state = load_state(state_file)
    bots = state.get("bots") or []
    if not bots:
        raise SystemExit(f"state.json has no 'bots' entries")
    prediction_tracker = run_dir / "workspaces" / bots[0] / "memory" / "predictions" / "tracker.md"
    trading_dates = state.get("trading_dates") or []
    if not trading_dates:
        raise SystemExit(f"state.json has no 'trading_dates'")

    cutoff_index = compute_cursor(trading_dates, cutoff)
    normalized_cutoff = trading_dates[cutoff_index]
    if normalized_cutoff != cutoff:
        print(f"NOTE: cutoff {cutoff} snapped to next trading day {normalized_cutoff}")
        cutoff = normalized_cutoff
    last_keep = prev_trading_day(trading_dates, cutoff_index)

    print(f"mode        : {'APPLY' if apply else 'DRY-RUN'}")
    print(f"run_id      : {run_id}")
    print(f"bots        : {bots}")
    print(f"cutoff      : {cutoff}  (index {cutoff_index}, first date REMOVED)")
    print(f"last_keep   : {last_keep}  (last date KEPT)")

    paths = {
        "run_id": run_id,
        "cutoff": cutoff,
        "log_file": log_file,
        "state_file": state_file,
        "memory_store": memory_store,
        "prediction_tracker": prediction_tracker,
    }
    backup(paths, apply)
    prune_memory_store(memory_store, cutoff, apply)
    prune_prediction_tracker(prediction_tracker, cutoff, apply)

    # process each bot in the run (usually one)
    last_deep_research_by_bot: dict[str, str | None] = {}
    for bot_id in bots:
        workspace_research = run_dir / "workspaces" / bot_id / "memory" / "research"
        db_cleanup(bot_id, run_id, cutoff, last_keep, apply)
        fs_cleanup(run_dir, workspace_research, cutoff, apply)
        rl_openclaw_cleanup(run_dir, bot_id, cutoff, apply)
        replay_repair(bot_id, run_id, last_keep, apply)
        last_deep_research_by_bot[bot_id] = latest_research_before(workspace_research, cutoff)

    state_update(state_file, cutoff_index, cutoff, last_deep_research_by_bot, apply)
    print("\n=== done ===")


if __name__ == "__main__":
    main()
