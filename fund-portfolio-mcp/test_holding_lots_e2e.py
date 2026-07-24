"""End-to-end test for lot-based BUY → BUY → SELL flow through real MCP tools.

Verifies that:
  - Each BUY creates a new lot row.
  - SELL across two lots produces per-lot fee at each lot's own holding_days tier.
  - Total fee in fund_bot_orders matches sum of consumption fees.
  - fund_bot_actions has one REDUCE row per consumed lot.
  - Holdings invariants hold after sell.

Spec: docs/superpowers/specs/2026-05-19-fund-holding-lots-design.md
"""
import asyncio
import importlib
import json
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


DEFAULT_REDEEM_TIERS = json.dumps(
    [
        {"max_days": 7,  "rate": 0.015},
        {"max_days": 30, "rate": 0.005},
        {"max_days": None, "rate": 0.0},
    ],
    ensure_ascii=False,
)


def _seed_fund(conn, fund_code, redeem_json=DEFAULT_REDEEM_TIERS, purchase_fee=0.0):
    conn.execute(
        "INSERT OR REPLACE INTO fund_info "
        "(fund_code, fund_name, fund_type, share_class, purchase_status, redeem_status, "
        " mgmt_fee, custody_fee, purchase_fee, redeem_fee_json) "
        "VALUES (?, ?, '股票型', 'A', '开放', '开放', 0, 0, ?, ?)",
        (fund_code, "测试基金", purchase_fee, redeem_json),
    )


def _seed_nav(conn, fund_code, nav_date, nav):
    conn.execute(
        "INSERT OR REPLACE INTO fund_nav (fund_code, nav_date, nav, acc_nav, daily_return_pct) "
        "VALUES (?, ?, ?, ?, 0)",
        (fund_code, nav_date, nav, nav),
    )


def _seed_account(conn, bot_id, cash, run_id):
    conn.execute(
        "INSERT OR REPLACE INTO fund_bot_accounts "
        "(bot_id, initial_capital, cash, cash_in_transit, cash_receivable, run_id) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (bot_id, cash, cash, run_id),
    )


def _rows(path, sql, args=()):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def test_buy_creates_lot_at_settle(srv, tmp_db):
    fund = "000001"
    run_id = "run-1"
    bot_id = "botE"
    with sqlite3.connect(tmp_db) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-01", 2.0)
        _seed_nav(conn, fund, "2026-04-02", 2.0)
        _seed_account(conn, bot_id, cash=100_000.0, run_id=run_id)
        conn.commit()

    # T 日下单：lot 还没建（T+1 settle 才建）
    r1 = asyncio.run(srv.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date="2026-04-01", reason="t1", run_id=run_id))
    assert json.loads(r1)["success"], r1
    lots_pre = _rows(tmp_db, "SELECT * FROM fund_bot_holding_lots")
    assert lots_pre == [], "T 日下单时不应该有 lot"

    # T+1 settle：lot 建好
    r2 = asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-02", run_id=run_id))
    payload = json.loads(r2)
    assert payload["success"], r2

    lots = _rows(tmp_db, "SELECT * FROM fund_bot_holding_lots")
    assert len(lots) == 1
    lot = lots[0]
    assert lot["bot_id"] == bot_id
    assert lot["fund_code"] == fund
    assert lot["run_id"] == run_id
    assert lot["entry_date"] == "2026-04-01"
    assert abs(lot["entry_nav"] - 2.0) < 1e-6
    assert abs(lot["shares_initial"] - 5000.0) < 1e-3  # 10000/2.0 (purchase_fee=0)
    assert abs(lot["shares_remaining"] - 5000.0) < 1e-3
    assert abs(lot["cost_initial"] - 10_000.0) < 1e-3
    assert lot["status"] == "open"
    # holding_id must point to the active holding
    holdings = _rows(tmp_db, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
    assert holdings[0]["holding_id"] == lot["holding_id"]


def test_two_buys_two_lots_sell_fifo_crosses_tiers(srv, tmp_db):
    """The headline scenario from user spec:
       buy 10000 → 20 days later buy 20000 → 10 days later sell ALL.
       Old lot: held 30 days → 0% (tier boundary: <30 fails, so exactly 30 → 0).
       New lot: held 10 days  → 0.5%.
       Expected: per-lot fee, sum matches orders.fee, two REDUCE actions.
    """
    fund = "000002"
    run_id = "run-2"
    bot_id = "botF"
    with sqlite3.connect(tmp_db) as conn:
        _seed_fund(conn, fund)
        # First buy: 2026-04-01 nav 2.0, T+1 settle on 04-02
        _seed_nav(conn, fund, "2026-04-01", 2.0)
        _seed_nav(conn, fund, "2026-04-02", 2.0)
        # Second buy: 2026-04-21 nav 2.5, T+1 settle on 04-22
        _seed_nav(conn, fund, "2026-04-21", 2.5)
        _seed_nav(conn, fund, "2026-04-22", 2.5)
        # Sell: 2026-05-01 nav 3.0 (10 days after second buy, 30 days after first buy)
        _seed_nav(conn, fund, "2026-05-01", 3.0)
        _seed_account(conn, bot_id, cash=100_000.0, run_id=run_id)
        conn.commit()

    # Buy 1: 10,000 → 5000 shares
    asyncio.run(srv.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date="2026-04-01", reason="buy1", run_id=run_id))
    asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-02", run_id=run_id))

    # Buy 2: 20,000 → 8000 shares
    asyncio.run(srv.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=20_000.0,
        trade_date="2026-04-21", reason="buy2", run_id=run_id))
    asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-22", run_id=run_id))

    lots_before = _rows(tmp_db,
        "SELECT * FROM fund_bot_holding_lots WHERE bot_id=? ORDER BY entry_date", (bot_id,))
    assert len(lots_before) == 2
    assert lots_before[0]["entry_date"] == "2026-04-01"
    assert lots_before[1]["entry_date"] == "2026-04-21"
    total_shares = sum(l["shares_remaining"] for l in lots_before)
    assert abs(total_shares - 13_000.0) < 1e-3

    # Sell ALL 13000 on 2026-05-01 — 新机制：T 日只挂单，T+1 settle 才落 lot/action/cash
    r = asyncio.run(srv.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=13_000.0,
        trade_date="2026-05-01", reason="sell-all", run_id=run_id))
    payload = json.loads(r)
    assert payload["success"], r
    assert payload["pricing_status"] == "priced"
    assert abs(payload["reference_nav"] - 3.0) < 1e-6
    # T 日 lot 未消耗
    lots_at_t = _rows(tmp_db,
        "SELECT * FROM fund_bot_holding_lots WHERE bot_id=? ORDER BY entry_date", (bot_id,))
    assert all(l["status"] == "open" for l in lots_at_t)

    # T+1 settle：seed NAV for settle 日（settle 通过 order.reference_nav 已锁定，无需再查）
    with sqlite3.connect(tmp_db) as conn:
        _seed_nav(conn, fund, "2026-05-02", 3.0)
        conn.commit()
    r2 = asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-02", run_id=run_id))
    settled = json.loads(r2)["settled"]
    assert any(s["type"] == "sell" and s["settled_via"] == "t_plus_1" for s in settled)

    # Compute expected fees:
    #   Lot A: 5000 shares × nav 3.0 = 15000 gross, held 30 days → tier=0% → fee=0
    #   Lot B: 8000 shares × nav 3.0 = 24000 gross, held 10 days → tier=0.5% → fee=120
    #   Total: gross=39000, fee=120, proceeds=38880

    # Verify lots state
    lots_after = _rows(tmp_db,
        "SELECT * FROM fund_bot_holding_lots WHERE bot_id=? ORDER BY entry_date", (bot_id,))
    assert all(l["status"] == "closed" for l in lots_after)
    assert all(abs(l["shares_remaining"]) < 1e-6 for l in lots_after)

    # Verify actions: two REDUCE rows on 2026-05-01
    actions = _rows(tmp_db,
        "SELECT * FROM fund_bot_actions WHERE bot_id=? AND action_type='REDUCE' "
        "ORDER BY action_id", (bot_id,))
    assert len(actions) == 2
    assert abs(actions[0]["fee"] - 0.0) < 1e-3
    assert abs(actions[1]["fee"] - 120.0) < 1e-2

    # Verify orders.fee = sum of action fees
    orders = _rows(tmp_db,
        "SELECT * FROM fund_bot_orders WHERE bot_id=? AND order_type='sell'", (bot_id,))
    assert len(orders) == 1
    assert abs(orders[0]["fee"] - 120.0) < 1e-2
    assert abs(orders[0]["confirmed_amount"] - 38_880.0) < 1e-2

    # Verify holding is closed
    holdings = _rows(tmp_db, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
    assert holdings[0]["status"] == "closed"
    assert abs(holdings[0]["shares"]) < 1e-6


def test_partial_sell_preserves_newer_lot_and_invariants(srv, tmp_db):
    """Sell only the older lot worth; new lot stays fully open, holding invariants hold."""
    fund = "000003"
    run_id = "run-3"
    bot_id = "botG"
    with sqlite3.connect(tmp_db) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-03-01", 1.0)
        _seed_nav(conn, fund, "2026-03-02", 1.0)
        _seed_nav(conn, fund, "2026-04-20", 1.5)
        _seed_nav(conn, fund, "2026-04-21", 1.5)
        _seed_nav(conn, fund, "2026-05-01", 2.0)
        _seed_account(conn, bot_id, cash=100_000.0, run_id=run_id)
        conn.commit()

    # Buy A: 1000 amount → 1000 shares at nav 1.0
    asyncio.run(srv.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=1000.0,
        trade_date="2026-03-01", reason="A", run_id=run_id))
    asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-03-02", run_id=run_id))
    # Buy B: 1500 amount → 1000 shares at nav 1.5
    asyncio.run(srv.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=1500.0,
        trade_date="2026-04-20", reason="B", run_id=run_id))
    asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-21", run_id=run_id))

    # Sell 1000 shares: 新机制 T 日只挂单 + 冻结 pending_sell_shares
    r = asyncio.run(srv.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=1000.0,
        trade_date="2026-05-01", reason="partial", run_id=run_id))
    payload = json.loads(r)
    assert payload["success"]
    assert payload["pricing_status"] == "priced"
    # T+1 settle 才真正消耗 lot
    with sqlite3.connect(tmp_db) as conn:
        _seed_nav(conn, fund, "2026-05-02", 2.0)
        conn.commit()
    asyncio.run(srv.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-02", run_id=run_id))

    # Lot A closed, B still open with 1000 shares
    lots = _rows(tmp_db, "SELECT * FROM fund_bot_holding_lots WHERE bot_id=? ORDER BY entry_date",
                 (bot_id,))
    assert lots[0]["status"] == "closed"
    assert lots[1]["status"] == "open"
    assert abs(lots[1]["shares_remaining"] - 1000.0) < 1e-3
    assert abs(lots[1]["cost_remaining"] - 1500.0) < 1e-3

    # Holding invariants: sum(open lot.shares) == holdings.shares,
    #                     sum(open lot.cost_remaining) == holdings.amount_invested
    #                     min(open lot.entry_date) == holdings.entry_date
    h = _rows(tmp_db, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))[0]
    assert abs(h["shares"] - 1000.0) < 1e-3
    assert abs(h["amount_invested"] - 1500.0) < 1e-3
    assert h["entry_date"] == "2026-04-20"
    assert h["status"] == "active"
