import importlib.util
import sqlite3
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("repair_affected_fund_lots.py")
SPEC = importlib.util.spec_from_file_location("repair_affected_fund_lots", SCRIPT)
repair_mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = repair_mod
SPEC.loader.exec_module(repair_mod)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fund_bot_holdings (
          holding_id INTEGER PRIMARY KEY, bot_id TEXT, fund_code TEXT, run_id TEXT,
          entry_date TEXT, exit_date TEXT, entry_nav REAL, latest_nav REAL,
          shares REAL, pending_sell_shares REAL, amount_invested REAL,
          market_value REAL, unrealized_pnl REAL, unrealized_pnl_pct REAL,
          actual_weight REAL, status TEXT
        );
        CREATE TABLE fund_bot_holding_lots (
          lot_id INTEGER PRIMARY KEY, bot_id TEXT, fund_code TEXT, run_id TEXT,
          holding_id INTEGER, entry_date TEXT, entry_nav REAL,
          shares_initial REAL, shares_remaining REAL, cost_initial REAL,
          cost_remaining REAL, source_order_id INTEGER, status TEXT
        );
        CREATE TABLE fund_bot_actions (
          action_id INTEGER PRIMARY KEY, bot_id TEXT, fund_code TEXT, run_id TEXT,
          action_type TEXT, action_date TEXT, reason TEXT
        );
        CREATE TABLE fund_bot_orders (
          order_id INTEGER PRIMARY KEY, bot_id TEXT, fund_code TEXT, order_run_id TEXT,
          order_type TEXT, order_date TEXT, status TEXT, confirmed_amount REAL
        );
        """
    )
    return conn


def test_repair_rebuilds_only_open_lots_and_preserves_trades():
    conn = _db()
    conn.execute(
        "INSERT INTO fund_bot_holdings VALUES"
        " (1,'bot','000001','run','2026-01-01',NULL,2,2.5,100,100,200,250,50,25,1,'active')"
    )
    conn.execute(
        "INSERT INTO fund_bot_holding_lots VALUES"
        " (1,'bot','000001','run',1,'2026-01-01',2,40,40,80,80,10,'open')"
    )
    conn.execute(
        "INSERT INTO fund_bot_orders VALUES"
        " (10,'bot','000001','run','sell','2026-02-01','pending',NULL)"
    )
    conn.execute(
        "INSERT INTO fund_bot_actions VALUES"
        " (10,'bot','000001','run','REDUCE','2026-02-01','valid sell')"
    )
    conn.commit()

    result = repair_mod.repair(conn, apply=True)

    assert result["groups_rebuilt"] == 1
    lots = conn.execute(
        "SELECT status, shares_remaining, cost_remaining FROM fund_bot_holding_lots ORDER BY lot_id"
    ).fetchall()
    assert [tuple(r) for r in lots] == [("superseded", 40.0, 80.0), ("open", 100.0, 200.0)]
    assert conn.execute("SELECT status FROM fund_bot_orders WHERE order_id=10").fetchone()[0] == "pending"
    assert conn.execute("SELECT reason FROM fund_bot_actions WHERE action_id=10").fetchone()[0] == "valid sell"


def test_repair_closes_sell_evidenced_dust():
    conn = _db()
    conn.execute(
        "INSERT INTO fund_bot_holdings VALUES"
        " (1,'bot','000001','run','2025-03-06',NULL,1.2,1.3,0.002,0,0.01,0.01,0,0,0,'active')"
    )
    conn.execute(
        "INSERT INTO fund_bot_actions VALUES"
        " (1,'bot','000001','run','REDUCE','2025-03-07','ordinary full exit')"
    )
    conn.execute(
        "INSERT INTO fund_bot_orders VALUES"
        " (1,'bot','000001','run','sell','2025-03-07','confirmed',100)"
    )
    conn.commit()

    result = repair_mod.repair(conn, apply=True)

    assert result["residues_closed"] == 1
    row = conn.execute(
        "SELECT status, exit_date, shares, amount_invested FROM fund_bot_holdings"
    ).fetchone()
    assert tuple(row) == ("closed", "2025-03-07", 0.0, 0.0)
    assert conn.execute("SELECT status FROM fund_bot_orders").fetchone()[0] == "confirmed"


def test_repair_keeps_tiny_holding_when_last_action_is_buy():
    conn = _db()
    conn.execute(
        "INSERT INTO fund_bot_holdings VALUES"
        " (1,'bot','000001','run','2025-03-06',NULL,1.2,1.3,0.002,0,0.01,0.01,0,0,0,'active')"
    )
    conn.execute(
        "INSERT INTO fund_bot_actions VALUES"
        " (1,'bot','000001','run','ADD','2025-03-06','tiny test buy')"
    )
    conn.commit()

    result = repair_mod.repair(conn, apply=True)

    assert result["residues_closed"] == 0
    assert conn.execute("SELECT status FROM fund_bot_holdings").fetchone()[0] == "active"
