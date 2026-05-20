"""Unit tests for the lot-based holdings model: _insert_lot + _consume_lots_fifo.

Spec: docs/superpowers/specs/2026-05-19-fund-holding-lots-design.md

Tests target server.py's helper functions directly, with a fresh DB per test.
The end-to-end BUY/SELL integration is covered in test_holding_lots_e2e.py.
"""
import importlib
import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr("db.DB_PATH", path)
    import db as db_mod
    db_mod.init_db()
    yield path
    os.unlink(path)


@pytest.fixture
def srv(tmp_db):
    import server
    importlib.reload(server)
    return server


DEFAULT_TIERS = [
    {"max_days": 7, "rate": 0.015},
    {"max_days": 30, "rate": 0.005},
    {"max_days": None, "rate": 0.0},
]


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _lots(path, **filters):
    c = _conn(path)
    where = " AND ".join(f"{k}=?" for k in filters)
    sql = "SELECT * FROM fund_bot_holding_lots"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY lot_id"
    return [dict(r) for r in c.execute(sql, tuple(filters.values())).fetchall()]


# =============================================================================
# _insert_lot
# =============================================================================

def test_insert_lot_creates_row(srv, tmp_db):
    with sqlite3.connect(tmp_db) as conn:
        lot_id = srv._insert_lot(
            conn, bot_id="botA", fund_code="000001", run_id="run1",
            holding_id=42, entry_date="2026-05-01", entry_nav=1.2345,
            shares=1000.0, cost=1234.50, source_order_id=99,
        )
    assert isinstance(lot_id, int) and lot_id > 0

    rows = _lots(tmp_db, lot_id=lot_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["bot_id"] == "botA"
    assert r["fund_code"] == "000001"
    assert r["run_id"] == "run1"
    assert r["holding_id"] == 42
    assert r["entry_date"] == "2026-05-01"
    assert abs(r["entry_nav"] - 1.2345) < 1e-9
    assert abs(r["shares_initial"] - 1000.0) < 1e-9
    assert abs(r["shares_remaining"] - 1000.0) < 1e-9
    assert abs(r["cost_initial"] - 1234.50) < 1e-9
    assert abs(r["cost_remaining"] - 1234.50) < 1e-9
    assert r["source_order_id"] == 99
    assert r["status"] == "open"
    assert r["created_at"]


def test_insert_lot_two_lots_independent_ids(srv, tmp_db):
    with sqlite3.connect(tmp_db) as conn:
        a = srv._insert_lot(conn, bot_id="b", fund_code="f", run_id="r",
                            holding_id=None, entry_date="2026-01-01",
                            entry_nav=1.0, shares=100.0, cost=100.0,
                            source_order_id=None)
        b = srv._insert_lot(conn, bot_id="b", fund_code="f", run_id="r",
                            holding_id=None, entry_date="2026-01-02",
                            entry_nav=1.1, shares=200.0, cost=220.0,
                            source_order_id=None)
    assert a != b
    rows = _lots(tmp_db, bot_id="b")
    assert len(rows) == 2


# =============================================================================
# _consume_lots_fifo — single-lot cases
# =============================================================================

def _seed_one_lot(srv, tmp_db, *, entry_date, shares, cost=None, entry_nav=2.0,
                  bot_id="bot1", fund_code="000001", run_id="run1"):
    cost = cost if cost is not None else shares * entry_nav
    with sqlite3.connect(tmp_db) as conn:
        return srv._insert_lot(conn, bot_id=bot_id, fund_code=fund_code,
                               run_id=run_id, holding_id=None,
                               entry_date=entry_date, entry_nav=entry_nav,
                               shares=shares, cost=cost, source_order_id=None)


def test_consume_partial_single_lot_reduces_shares_and_cost(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-04-01", shares=1000.0, cost=2000.0)
    # Hold 40 days → tier = 0%
    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=300.0, as_of_date="2026-05-11", nav=2.5,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 1
    c = consumptions[0]
    assert c["holding_days"] == 40
    assert abs(c["rate"] - 0.0) < 1e-9
    assert abs(c["shares"] - 300.0) < 1e-9
    assert abs(c["gross"] - 300.0 * 2.5) < 1e-6
    assert abs(c["fee"] - 0.0) < 1e-9
    # cost_consumed = 2000 * (300/1000) = 600
    assert abs(c["cost_consumed"] - 600.0) < 1e-6

    rows = _lots(tmp_db)
    assert len(rows) == 1
    assert abs(rows[0]["shares_remaining"] - 700.0) < 1e-6
    assert abs(rows[0]["cost_remaining"] - 1400.0) < 1e-6
    assert rows[0]["status"] == "open"


def test_consume_exact_single_lot_closes_it(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-01", shares=500.0, cost=1000.0)
    # 10 days → tier = 0.5%
    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=500.0, as_of_date="2026-05-11", nav=3.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 1
    c = consumptions[0]
    assert c["holding_days"] == 10
    assert abs(c["rate"] - 0.005) < 1e-9
    assert abs(c["gross"] - 1500.0) < 1e-6
    assert abs(c["fee"] - 7.5) < 1e-6  # 1500 * 0.005

    rows = _lots(tmp_db)
    assert rows[0]["status"] == "closed"
    assert abs(rows[0]["shares_remaining"]) < 1e-6
    assert abs(rows[0]["cost_remaining"]) < 1e-6


# =============================================================================
# _consume_lots_fifo — multi-lot FIFO
# =============================================================================

def test_consume_fifo_crosses_two_lots_with_different_rates(srv, tmp_db):
    # Lot A: entered 2026-01-01, sold 2026-05-11 → 130 days → 0%
    _seed_one_lot(srv, tmp_db, entry_date="2026-01-01", shares=200.0, cost=400.0)
    # Lot B: entered 2026-05-06, sold 2026-05-11 → 5 days → 1.5%
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-06", shares=300.0, cost=750.0)

    with sqlite3.connect(tmp_db) as conn:
        # Sell 350 shares: drain all of A (200) + 150 of B
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=350.0, as_of_date="2026-05-11", nav=2.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 2

    # First consumed = oldest lot A
    a = consumptions[0]
    assert a["entry_date"] == "2026-01-01"
    assert a["holding_days"] == 130
    assert abs(a["rate"] - 0.0) < 1e-9
    assert abs(a["shares"] - 200.0) < 1e-9
    assert abs(a["gross"] - 400.0) < 1e-6
    assert abs(a["fee"] - 0.0) < 1e-9
    assert abs(a["cost_consumed"] - 400.0) < 1e-6  # whole lot

    # Second = newer lot B, partial
    b = consumptions[1]
    assert b["entry_date"] == "2026-05-06"
    assert b["holding_days"] == 5
    assert abs(b["rate"] - 0.015) < 1e-9
    assert abs(b["shares"] - 150.0) < 1e-9
    assert abs(b["gross"] - 300.0) < 1e-6
    assert abs(b["fee"] - 300.0 * 0.015) < 1e-6
    # cost_consumed = 750 * (150/300) = 375
    assert abs(b["cost_consumed"] - 375.0) < 1e-6

    # Verify lot state
    rows = _lots(tmp_db)
    assert rows[0]["status"] == "closed"  # A drained
    assert rows[1]["status"] == "open"
    assert abs(rows[1]["shares_remaining"] - 150.0) < 1e-6
    assert abs(rows[1]["cost_remaining"] - 375.0) < 1e-6


def test_consume_fifo_exactly_oldest_lot_boundary(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-04-01", shares=100.0, cost=200.0)  # 40d → 0%
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-10", shares=100.0, cost=210.0)  # 1d → 1.5%

    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=100.0, as_of_date="2026-05-11", nav=2.5,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 1
    assert consumptions[0]["entry_date"] == "2026-04-01"
    assert abs(consumptions[0]["fee"]) < 1e-9

    rows = _lots(tmp_db)
    assert rows[0]["status"] == "closed"
    assert rows[1]["status"] == "open"
    assert abs(rows[1]["shares_remaining"] - 100.0) < 1e-6


def test_consume_drains_all_lots(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-04-01", shares=100.0, cost=200.0)
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-08", shares=50.0, cost=110.0)

    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=150.0, as_of_date="2026-05-11", nav=2.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 2
    rows = _lots(tmp_db)
    assert all(r["status"] == "closed" for r in rows)
    assert all(abs(r["shares_remaining"]) < 1e-6 for r in rows)


def test_consume_more_than_available_raises_and_no_writes(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-01", shares=100.0, cost=200.0)
    with sqlite3.connect(tmp_db) as conn:
        with pytest.raises(ValueError, match="insufficient open lots"):
            srv._consume_lots_fifo(
                conn, bot_id="bot1", fund_code="000001", run_id="run1",
                sell_shares=200.0, as_of_date="2026-05-11", nav=2.0,
                redeem_tiers=DEFAULT_TIERS,
            )
    # No partial writes: lot should be untouched
    rows = _lots(tmp_db)
    assert abs(rows[0]["shares_remaining"] - 100.0) < 1e-6
    assert rows[0]["status"] == "open"


def test_consume_only_open_lots(srv, tmp_db):
    """Closed lots must be ignored by FIFO scan."""
    # Manually create one closed lot and one open lot
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(
            "INSERT INTO fund_bot_holding_lots (bot_id, fund_code, run_id, holding_id, "
            "entry_date, entry_nav, shares_initial, shares_remaining, cost_initial, "
            "cost_remaining, source_order_id, status) VALUES "
            "('bot1','000001','run1',NULL,'2025-01-01',1.0,500,0,500,0,NULL,'closed')"
        )
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-01", shares=100.0, cost=200.0)

    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=100.0, as_of_date="2026-05-11", nav=2.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 1
    assert consumptions[0]["entry_date"] == "2026-05-01"  # not the closed one


def test_consume_isolated_by_run_id(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-01", shares=100.0, cost=200.0,
                  run_id="run-A")
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-01", shares=100.0, cost=200.0,
                  run_id="run-B")
    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run-A",
            sell_shares=100.0, as_of_date="2026-05-11", nav=2.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert len(consumptions) == 1
    rows = _lots(tmp_db, run_id="run-A")
    assert rows[0]["status"] == "closed"
    rows_b = _lots(tmp_db, run_id="run-B")
    assert rows_b[0]["status"] == "open"  # other run untouched


# =============================================================================
# Boundary cases on holding_days tiers
# =============================================================================

@pytest.mark.parametrize("days,expected_rate", [
    (0, 0.015),    # same day
    (1, 0.015),    # < 7
    (6, 0.015),
    (7, 0.005),    # boundary: < 7 fails → next tier
    (29, 0.005),
    (30, 0.0),     # boundary: < 30 fails → next tier
    (100, 0.0),
])
def test_redeem_fee_rate_boundaries(srv, days, expected_rate):
    assert srv._redeem_fee_rate(DEFAULT_TIERS, days) == expected_rate


def test_consume_holding_days_zero_when_same_day(srv, tmp_db):
    _seed_one_lot(srv, tmp_db, entry_date="2026-05-11", shares=100.0, cost=200.0)
    with sqlite3.connect(tmp_db) as conn:
        consumptions = srv._consume_lots_fifo(
            conn, bot_id="bot1", fund_code="000001", run_id="run1",
            sell_shares=50.0, as_of_date="2026-05-11", nav=2.0,
            redeem_tiers=DEFAULT_TIERS,
        )
    assert consumptions[0]["holding_days"] == 0
    assert abs(consumptions[0]["rate"] - 0.015) < 1e-9
