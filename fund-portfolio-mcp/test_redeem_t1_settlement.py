"""SELL T+1 结算（与 BUY 对称）机制的回归测试。

新机制（本次改造后，与 BUY 时序完全对称）：
  T 日 place_sell_order：
    - 若 T 日 NAV 已在库：锁 order.reference_nav / pricing_status='priced'
      若 T 日 NAV 未出：pricing_status='awaiting_nav'，settle 时按 order_date 定价
    - 立即 holding.pending_sell_shares += want（防重复卖）
    - 不动 holding.shares、不动 amount_invested、不写 REDUCE action、不动 cash_receivable/cash
    - order.status='pending', confirmed_shares/confirmed_amount/fee 均为 NULL
  T+1 settle_pending_fund_orders：
    - 按 reference_nav 与 order_date 的持有天数从阶梯表算赎回费
    - FIFO 消耗 lot、扣 holding.shares、close 或 update holding、释放 pending_sell_shares
    - 逐 lot 插 REDUCE action（action_date=order_date）
    - cash += (gross - fee)，订单翻 confirmed（confirm_date=as_of_date）
  老机制兼容：老 T+0 sell 代码遗留的 pending 单（confirmed_amount NOT NULL）在 settle 时
              仍走 cash_receivable → cash 的兼容分支。
"""
import asyncio
import importlib
import json
import os
import sqlite3
import tempfile

import pytest


# ---------- fixtures ----------

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
def reload_server(tmp_db):
    """server 模块在 import 时绑定 DB_PATH，monkeypatch 后必须 reload。"""
    import server
    importlib.reload(server)
    return server


# ---------- helpers (raw SQL seeding to stay independent of other tools) ----------

DEFAULT_REDEEM_TIERS = json.dumps(
    [
        {"max_days": 7,  "rate": 0.015},   # < 7 天 1.5%
        {"max_days": 30, "rate": 0.005},   # < 30 天 0.5%
        {"max_days": None, "rate": 0.0},   # >= 30 天 0%
    ],
    ensure_ascii=False,
)


def _seed_fund(conn, fund_code, name="测试基金", redeem_json=DEFAULT_REDEEM_TIERS):
    conn.execute(
        "INSERT OR REPLACE INTO fund_info "
        "(fund_code, fund_name, fund_type, share_class, purchase_status, redeem_status, "
        " mgmt_fee, custody_fee, purchase_fee, redeem_fee_json) "
        "VALUES (?, ?, '股票型', 'A', '开放', '开放', 0, 0, 0, ?)",
        (fund_code, name, redeem_json),
    )


def _seed_nav(conn, fund_code, nav_date, nav, daily_return_pct=0.0):
    conn.execute(
        "INSERT OR REPLACE INTO fund_nav (fund_code, nav_date, nav, acc_nav, daily_return_pct) "
        "VALUES (?, ?, ?, ?, ?)",
        (fund_code, nav_date, nav, nav, daily_return_pct),
    )


def _seed_account(conn, bot_id, cash, initial=None, run_id="seed"):
    initial = initial if initial is not None else cash
    conn.execute(
        "INSERT OR REPLACE INTO fund_bot_accounts "
        "(bot_id, initial_capital, cash, cash_in_transit, run_id) VALUES (?, ?, ?, 0, ?)",
        (bot_id, initial, cash, run_id),
    )


def _seed_holding(conn, bot_id, fund_code, shares, cost, entry_date, entry_nav, run_id="seed"):
    """seed holding + 对应单 lot（配合 FIFO settle 使用）。"""
    cur = conn.execute(
        "INSERT INTO fund_bot_holdings "
        "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
        " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
        "VALUES (?, ?, '测试基金', '股票类', 'core', ?, ?, ?, ?, 0, ?, ?, 'active', ?)",
        (bot_id, fund_code, entry_date, entry_nav, entry_nav, shares, shares * entry_nav,
         cost, run_id),
    )
    holding_id = cur.lastrowid
    conn.execute(
        "INSERT INTO fund_bot_holding_lots "
        "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
        " shares_initial, shares_remaining, cost_initial, cost_remaining, "
        " source_order_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open')",
        (bot_id, fund_code, run_id, holding_id, entry_date, entry_nav,
         shares, shares, cost, cost),
    )


def _row(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _seed_order(conn, *, bot_id, fund_code, order_type, order_date, run_id,
                status="confirmed"):
    conn.execute(
        "INSERT INTO fund_bot_orders "
        "(bot_id, fund_code, fund_name, order_type, order_date, order_amount, "
        " action_reason, status, order_run_id) "
        "VALUES (?, ?, '测试基金', ?, ?, 100, 'seed', ?, ?)",
        (bot_id, fund_code, order_type, order_date, status, run_id),
    )


def _setup_standard(reload_server, bot_id="botT", fund="000001",
                    entry_date="2026-04-10", entry_nav=1.0, shares=10_000.0,
                    cost=10_000.0, sell_date="2026-05-11", sell_nav=1.20,
                    initial_cash=10_000.0, account_cash=0.0,
                    settle_date="2026-05-12", run_id="run-T"):
    """造一个 bot 有一只持仓的标准场景。返回 (server, db_path)。

    持有 31 个自然日（4-10 → 5-11）→ 跨过 30 天阈值 → 赎回费 = 0%
    """
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, entry_date, entry_nav)
        _seed_nav(conn, fund, sell_date, sell_nav)
        _seed_nav(conn, fund, settle_date, sell_nav)
        _seed_account(conn, bot_id, account_cash, initial=initial_cash, run_id=run_id)
        _seed_holding(conn, bot_id, fund, shares, cost, entry_date, entry_nav, run_id=run_id)
        conn.commit()
    return s, db_mod.DB_PATH


# =============================================================================
#  T 日 place_sell_order 行为（新机制：只挂单 + 冻结 pending_sell_shares，不动实仓）
# =============================================================================

def test_sell_T_day_locks_pending_sell_shares_only(reload_server):
    """T 日 shares 不变、pending_sell_shares += want、status 仍 active。"""
    s, db_path = _setup_standard(reload_server)
    resp = asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=3000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    payload = json.loads(resp)
    assert payload["success"], payload
    assert payload["pricing_status"] == "priced"
    assert payload["reference_nav"] == 1.2
    assert abs(payload["pending_sell_shares_after"] - 3000.0) < 1e-6

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert abs(h["shares"] - 10000.0) < 1e-6, "T 日 shares 不动（等 T+1 settle 才扣）"
    assert abs(h["pending_sell_shares"] - 3000.0) < 1e-6
    assert h["status"] == "active"


def test_sell_T_day_amount_invested_unchanged(reload_server):
    """T 日 amount_invested 不动，settle 时才按 lot 消耗扣减。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=2500.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert abs(h["amount_invested"] - 10000.0) < 1e-3, h["amount_invested"]


def test_sell_T_day_cash_receivable_unchanged(reload_server):
    """T 日 cash_receivable 不动，也不动 cash。钱要到 T+1 settle 才落进 cash。"""
    s, db_path = _setup_standard(reload_server, account_cash=1234.0)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=4000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    assert abs((a["cash_receivable"] or 0.0)) < 1e-6
    assert abs(a["cash"] - 1234.0) < 1e-6


def test_sell_T_day_no_action_inserted(reload_server):
    """T 日不写 REDUCE action（等 T+1 settle 才逐 lot 写）。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=2000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT'")
    assert actions == []


def test_sell_T_day_order_status_and_fields(reload_server):
    """T 日 order.status=pending, confirmed_* 全 NULL, reference_nav 已锁 T 日 NAV。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=1000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        o = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT'")
    assert o["status"] == "pending"
    assert o["pricing_status"] == "priced"
    assert o["reference_nav"] == 1.2
    assert o["pricing_nav_date"] == "2026-05-11"
    assert o["confirmed_shares"] is None
    assert o["confirmed_amount"] is None
    assert o["fee"] is None
    assert o["confirm_date"] is None


def test_sell_T_day_multiple_partials_accumulate_pending(reload_server):
    """同日多次部分赎回累加：pending_sell_shares 累加，shares 仍不变，两条 pending 单。"""
    s, db_path = _setup_standard(reload_server)
    for amt in (1000.0, 2000.0):
        asyncio.run(s.portfolio_place_sell_order(
            bot_id="botT", fund_code="000001", shares=amt,
            trade_date="2026-05-11", reason="trim", run_id="run-T",
        ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        orders = _rows(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT' ORDER BY order_id")
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT'")
    assert abs(h["shares"] - 10000.0) < 1e-6
    assert abs(h["pending_sell_shares"] - 3000.0) < 1e-6
    assert len(orders) == 2
    assert all(o["status"] == "pending" for o in orders)
    assert actions == []


def test_sell_T_day_rejects_when_pending_exceeds_available(reload_server):
    """已冻结 8000，再卖 3000（合计 11000 > 持仓 10000）→ 应拒绝。"""
    s, _ = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=8000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    resp = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=3000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    )))
    assert not resp["success"], "超出可卖份额应拒绝"
    assert "可卖份额不足" in resp["message"]


# =============================================================================
#  T+1 settle：新机制 sell 走 legacy 路径完成扣仓 + 加现金 + 写 action
# =============================================================================

def test_settle_new_mechanism_extracts_shares_and_credits_cash(reload_server):
    """T+1 settle：扣 shares、cash += proceeds、订单 confirmed、pending_sell_shares 释放。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=5000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
        o = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT'")
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    # 持有 31 天 → fee=0% → proceeds = 5000 * 1.20 = 6000
    assert abs(h["shares"] - 5000.0) < 1e-6, "T+1 才扣 shares"
    assert abs(h["pending_sell_shares"]) < 1e-6, "pending 应释放"
    assert abs(a["cash"] - 6000.0) < 1e-3
    assert abs((a["cash_receivable"] or 0.0)) < 1e-6
    assert o["status"] == "confirmed"
    assert o["confirm_date"] == "2026-05-12"
    assert o["settle_run_id"] == "run-T1"
    assert abs(o["confirmed_shares"] - 5000.0) < 1e-3
    assert abs(o["confirmed_amount"] - 6000.0) < 1e-3


def test_settle_full_redeem_closes_holding(reload_server):
    """全部赎回：T+1 settle 后 status='closed', exit_date=as_of_date, shares=0。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=10000.0,
        trade_date="2026-05-11", reason="clear", run_id="run-T",
    ))
    # T 日 holding 仍 active（关键）
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert before["status"] == "active"
    assert abs(before["shares"] - 10000.0) < 1e-6

    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    assert h["status"] == "closed"
    # pricing_status=='priced' 分支 → exit_date=as_of_date
    assert h["exit_date"] == "2026-05-12"
    assert abs(h["shares"]) < 1e-6
    assert abs(a["cash"] - 12000.0) < 1e-3


def test_settle_uses_order_date_holding_days_for_fee(reload_server):
    """赎回费按 order_date 那天的持有天数（不是 settle 日）：持有 5 天 → 1.5%。"""
    s, db_path = _setup_standard(
        reload_server, entry_date="2026-05-06",
        sell_date="2026-05-11", sell_nav=1.00, settle_date="2026-05-12",
    )
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=1000.0,
        trade_date="2026-05-11", reason="quick sell", run_id="run-T",
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        order = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT'")
        acc = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    # gross=1000, fee=15 (1.5%), net=985
    assert abs(order["fee"] - 15.0) < 1e-3
    assert abs(order["confirmed_amount"] - 985.0) < 1e-3
    assert abs(order["confirmed_shares"] - 1000.0) < 1e-3
    assert order["status"] == "confirmed"
    assert abs(acc["cash"] - 985.0) < 1e-3


def test_settle_inserts_REDUCE_action_with_order_date(reload_server):
    """settle 时写 REDUCE action，action_date 用 order_date（T 日），非 settle 日。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=2000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT' ORDER BY action_id")
    assert len(actions) == 1
    a = actions[0]
    assert a["action_type"] == "REDUCE"
    assert a["action_date"] == "2026-05-11", "action_date 必须是 order_date（T 日）"
    # settle 时 action 归属 order_run_id（= run-T），不是 settle 的 run-T1
    assert a["run_id"] == "run-T"
    assert abs(float(a["shares"]) - 2000.0) < 1e-3


def test_multiple_partials_same_day_all_settle_at_t_plus_1(reload_server):
    """T 日两笔 pending sell，T+1 settle 后应逐个应用，两条 REDUCE action。"""
    s, db_path = _setup_standard(reload_server)
    for amt in (1000.0, 2000.0):
        asyncio.run(s.portfolio_place_sell_order(
            bot_id="botT", fund_code="000001", shares=amt,
            trade_date="2026-05-11", reason="trim", run_id="run-T",
        ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
        orders = _rows(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT' ORDER BY order_id")
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT' ORDER BY action_id")
    assert abs(h["shares"] - 7000.0) < 1e-6
    assert abs(h["pending_sell_shares"]) < 1e-6
    # 31 天 fee=0% → cash = 3000 * 1.20 = 3600
    assert abs(a["cash"] - 3600.0) < 1e-3
    assert len(orders) == 2
    assert all(o["status"] == "confirmed" for o in orders)
    assert len(actions) == 2


def test_settle_idempotent_no_double_extract(reload_server):
    """重复调用 settle：已 confirmed 的订单不会被再次处理，shares/cash 不动。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=3000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h1 = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a1 = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    # 再跑一遍 settle
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-13", run_id="run-T2",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h2 = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a2 = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT'")
    assert abs(h1["shares"] - h2["shares"]) < 1e-6
    assert abs(a1["cash"] - a2["cash"]) < 1e-3
    assert len(actions) == 1


# =============================================================================
#  存量老订单兼容路径（confirmed_amount NOT NULL）
# =============================================================================

def test_legacy_receivable_path_preserved(reload_server):
    """老机制遗留 pending SELL（confirmed_amount NOT NULL、shares 已在 T 日扣）：
    settle 只做 cash_receivable → cash 划账，不再动 holdings/actions。"""
    bot_id, fund = "botLegacy", "000001"
    import db as db_mod
    s = reload_server
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        # 账户：cash=0, cash_receivable=985（老代码 T 日已划入）
        conn.execute(
            "INSERT OR REPLACE INTO fund_bot_accounts "
            "(bot_id, initial_capital, cash, cash_in_transit, cash_receivable, run_id) "
            "VALUES (?, 10000, 0, 0, 985, 'legacy')",
            (bot_id,),
        )
        # 持仓：老代码 T 日已扣 shares（10000→9000）
        conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
            "VALUES (?, ?, '测试', '股票类', 'core', '2026-04-10', 1.00, 1.20, "
            " 9000, 0, 9000, 10800, 'active', 'legacy')",
            (bot_id, fund),
        )
        # 老 pending 卖单：confirmed_amount / confirmed_shares / fee 都已锁死
        conn.execute(
            "INSERT INTO fund_bot_orders "
            "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
            " order_amount, reference_nav, confirm_nav, confirmed_shares, confirmed_amount, "
            " fee, status, pricing_status, order_run_id) "
            "VALUES (?, ?, '测试', 'sell', '2026-05-11', NULL, 1000, 1.20, "
            " NULL, 1000, 985, 15, 'pending', 'priced', 'legacy')",
            (bot_id, fund),
        )
        conn.commit()

    asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
        o = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=?", (bot_id,))
    # 老路径不再动 holdings
    assert abs(h["shares"] - 9000.0) < 1e-3
    # cash_receivable → cash
    assert abs(a["cash"] - 985.0) < 1e-3
    assert abs((a["cash_receivable"] or 0.0)) < 1e-6
    assert o["status"] == "confirmed"
    # 老路径 settle 不插新 action
    assert actions == []


def test_legacy_awaiting_nav_still_settles_via_lot_fifo_path(reload_server):
    """老 awaiting_nav 兼容路径（无 confirmed_amount、pending_sell_shares 冻结）
    仍走 lot FIFO 消耗路径——本次改造保持向后兼容。"""
    bot_id, fund = "botL", "000001"
    import db as db_mod
    s = reload_server
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        _seed_account(conn, bot_id, 0.0, initial=10_000.0, run_id="legacy")
        cur = conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
            "VALUES (?, ?, '测试', '股票类', 'core', '2026-04-10', 1.00, 1.00, "
            " 10000, 3000, 10000, 10000, 'active', 'legacy')",
            (bot_id, fund),
        )
        holding_id = cur.lastrowid
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, "
            " source_order_id, status) "
            "VALUES (?, ?, 'legacy', ?, '2026-04-10', 1.00, 10000, 10000, 10000, 10000, NULL, 'open')",
            (bot_id, fund, holding_id),
        )
        conn.execute(
            "INSERT INTO fund_bot_orders "
            "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
            " order_amount, reference_nav, confirm_nav, confirmed_shares, confirmed_amount, "
            " fee, status, order_run_id) "
            "VALUES (?, ?, '测试', 'sell', '2026-05-11', NULL, 3000, 1.20, "
            " NULL, NULL, NULL, NULL, 'pending', 'legacy')",
            (bot_id, fund),
        )
        conn.commit()

    asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
        o = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=?", (bot_id,))
    assert abs(h["shares"] - 7000.0) < 1e-3
    assert abs(h["pending_sell_shares"]) < 1e-3
    assert abs(a["cash"] - 3600.0) < 1e-3
    assert o["status"] == "confirmed"
    assert len(actions) == 1


# =============================================================================
#  接口层与快照连续性
# =============================================================================

def test_get_my_history_shows_pending_sell_via_orders_and_holding(reload_server):
    """T 日下卖单后，portfolio_get_my_history 应能透过 orders 列表 + holding.pending_sell_shares
    让 bot 看到"卖单已挂但未生效"。"""
    s, _ = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=4000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    resp = json.loads(asyncio.run(s.portfolio_get_my_history(bot_id="botT", run_id="run-T")))
    assert resp["success"], resp
    # holding 的 pending_sell_shares 反映冻结
    hs = [h for h in resp["holdings"] if h["fund_code"] == "000001"]
    assert hs and abs(hs[0]["pending_sell_shares"] - 4000.0) < 1e-6
    # orders 里能看到这条 pending sell
    pending_sells = [o for o in resp["orders"] if o["order_type"] == "sell" and o["status"] == "pending"]
    assert len(pending_sells) == 1
    # summary.pending_sell_shares 汇总
    assert abs(resp["summary"]["pending_sell_shares"] - 4000.0) < 1e-6


def test_total_value_continuity_T_to_T_plus_1(reload_server):
    """T 日卖出与 T+1 settle 之间总资产连续（NAV 不变前提下）。
    直接查 fund_bot_accounts + fund_bot_holdings 校验，避开 replay/close_my_day 的复杂路径。
    """
    s, db_path = _setup_standard(reload_server, account_cash=2_000.0, initial_cash=12_000.0)

    def _snapshot():
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
            hs = _rows(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT' AND status='active'")
        cash = float(a["cash"] or 0.0)
        in_transit = float(a["cash_in_transit"] or 0.0)
        receivable = float(a["cash_receivable"] or 0.0)
        # 用当日 nav 1.20 计市值
        mv = sum(float(h["shares"] or 0.0) * 1.20 for h in hs)
        return cash, in_transit, receivable, mv, cash + in_transit + receivable + mv

    # T 日卖前基准：cash=2000 + mv=10000*1.2=12000 → 14000
    _, _, _, _, base = _snapshot()
    assert abs(base - 14000.0) < 1e-6

    # T 日卖 5000 → 新机制不改 shares/cash/receivable → 总值不变
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=5000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    cash1, it1, rec1, mv1, total1 = _snapshot()
    assert abs(cash1 - 2000.0) < 1e-6
    assert abs(rec1) < 1e-6
    assert abs(mv1 - 12000.0) < 1e-6  # shares 未减
    assert abs(total1 - 14000.0) < 1e-2

    # T+1 settle → shares 减 5000、cash += 6000 (fee=0) → 总值仍 14000
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    cash2, it2, rec2, mv2, total2 = _snapshot()
    assert abs(cash2 - 8000.0) < 1e-3
    assert abs(mv2 - 6000.0) < 1e-3  # 5000 shares × 1.20
    assert abs(rec2) < 1e-6
    assert abs(total2 - 14000.0) < 1e-2


def test_place_buy_cannot_use_pending_sell_proceeds(reload_server):
    """T 日下的卖单未 settle 前，钱不算 available cash——即使有 pending sell，超过 cash 的买也不能。

    用 place_buy_order + settle 建立真实 holding（否则 _bot_run_cash_view 的 replay 看不到直接 seed）。
    """
    bot_id, fund, run_id = "botT", "000001", "run-T"
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_nav(conn, fund, "2026-04-11", 1.00)
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        _seed_nav(conn, fund, "2026-05-12", 1.20)
        _seed_account(conn, bot_id, cash=10_000.0, initial=10_000.0, run_id=run_id)
        conn.commit()
    # 建仓：10000 全买
    asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date="2026-04-10", reason="init", run_id=run_id,
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-11", run_id=run_id,
    ))
    # 卖一部分制造 pending sell
    asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=4000.0,
        trade_date="2026-05-11", reason="trim", run_id=run_id,
    ))
    # 造 buyable
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, "000002", name="基金B")
        _seed_nav(conn, "000002", "2026-05-11", 1.00)
        conn.commit()
    # 尝试买 100 → cash=0（已全部投入建仓），pending sell 不算可用现金
    resp = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code="000002", amount=100.0,
        trade_date="2026-05-11", reason="test", run_id=run_id,
    )))
    assert not resp["success"], "cash=0 时即使有 pending sell 也不能买"


def test_sell_and_buy_at_same_t_day_both_defer_to_settle(reload_server):
    """同 T 日 buy + sell：两者都在 T 日只挂单、T+1 settle 才落到持仓。
    快照上 T 日：卖出基金 shares 不减、买入基金未出现，只 cash_in_transit / pending_sell_shares 变。
    """
    bot_id, fund, run_id = "botT", "000001", "run-T"
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_nav(conn, fund, "2026-04-11", 1.00)
        # 反向交易闸门用 fund_nav distinct nav_date 作全局交易日历；
        # 该场景实际跨一个月，测试库需显式补足至少 7 个交易日。
        for d in ["2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16",
                  "2026-04-17", "2026-04-20"]:
            _seed_nav(conn, fund, d, 1.00)
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        _seed_nav(conn, fund, "2026-05-12", 1.20)
        _seed_fund(conn, "000002", name="基金B")
        _seed_nav(conn, "000002", "2026-05-11", 2.00)
        _seed_nav(conn, "000002", "2026-05-12", 2.00)
        _seed_account(conn, bot_id, cash=13_000.0, initial=13_000.0, run_id=run_id)
        conn.commit()
    # 先建仓 A：10000 元
    asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date="2026-04-10", reason="init A", run_id=run_id,
    ))
    asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-04-11", run_id=run_id,
    ))
    # 同 T 日：卖 A 3000 shares + 买 B 1000 元
    asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=3000.0,
        trade_date="2026-05-11", reason="trim A", run_id=run_id,
    ))
    asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code="000002", amount=1000.0,
        trade_date="2026-05-11", reason="add B", run_id=run_id,
    ))
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        hs = _rows(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=? ORDER BY fund_code", (bot_id,))
        acc = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=? AND action_date='2026-05-11'", (bot_id,))
    # A 持仓 shares 未减（10000）、B 尚未出现在 holdings（buy T+1 才建仓）
    a_row = [h for h in hs if h["fund_code"] == fund][0]
    assert abs(a_row["shares"] - 10000.0) < 1e-6
    assert abs(a_row["pending_sell_shares"] - 3000.0) < 1e-6
    assert not any(h["fund_code"] == "000002" for h in hs)
    # cash 已扣 1000（buy 立即扣现金），cash_in_transit=1000
    assert abs(acc["cash"] - 2000.0) < 1e-3  # 建仓后 3000, buy B 又扣 1000
    assert abs(acc["cash_in_transit"] - 1000.0) < 1e-3
    assert abs((acc["cash_receivable"] or 0.0)) < 1e-6
    # 当日无 REDUCE/ADD action（都在 settle 时写）
    assert actions == []


# =============================================================================
#  盘中无 T 日 NAV：先接单，NAV 入库后按 T 日净值定价（原有兼容路径）
# =============================================================================

def test_intraday_buy_without_t_nav_prices_after_nav_arrives(reload_server):
    bot_id, fund, run_id = "botIB", "000888", "run-T"
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        _seed_account(conn, bot_id, cash=100_000.0, initial=100_000.0, run_id=run_id)
        conn.commit()

    resp = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date="2026-05-11", reason="intraday buy", run_id=run_id,
    )))
    assert resp["success"], resp
    assert resp["reference_nav"] is None
    assert resp["pricing_status"] == "awaiting_nav"

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        order = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        acc = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
    assert order["reference_nav"] is None
    assert order["pricing_status"] == "awaiting_nav"
    assert abs(acc["cash"] - 90_000.0) < 1e-6
    assert abs(acc["cash_in_transit"] - 10_000.0) < 1e-6

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_nav(conn, fund, "2026-05-11", 2.00)
        conn.commit()

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-12", run_id="run-T1",
    )))
    assert settled["success"], settled
    assert settled["settled"][0]["nav"] == 2.0

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        order = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        holding = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
        action = _row(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=?", (bot_id,))
        acc = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
    assert order["status"] == "confirmed"
    assert order["reference_nav"] == 2.0
    assert order["pricing_status"] == "priced"
    assert order["pricing_nav_date"] == "2026-05-11"
    assert order["settle_run_id"] == "run-T1"
    assert abs(order["confirmed_shares"] - 5000.0) < 1e-6
    assert holding["run_id"] == run_id
    assert abs(holding["shares"] - 5000.0) < 1e-6
    assert action["action_date"] == "2026-05-11"
    assert action["run_id"] == run_id
    assert abs(acc["cash_in_transit"]) < 1e-6


def test_intraday_sell_without_t_nav_freezes_then_prices_after_nav_arrives(reload_server):
    """盘中卖出（T 日 NAV 未出）：走 awaiting_nav 分支，与新机制"nav 已在库"分支行为一致
    （T 日只冻结 pending_sell_shares，settle 时按 order_date NAV 完成扣仓+加现金）。"""
    bot_id, fund, run_id = "botIS", "000889", "run-T"
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_account(conn, bot_id, cash=0.0, initial=10_000.0, run_id=run_id)
        cur = conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
            "VALUES (?, ?, '测试基金', '股票类', 'core', '2026-04-10', 1.00, 1.00, "
            " 10000, 0, 10000, 10000, 'active', ?)",
            (bot_id, fund, run_id),
        )
        holding_id = cur.lastrowid
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, "
            " source_order_id, status) "
            "VALUES (?, ?, ?, ?, '2026-04-10', 1.00, 10000, 10000, 10000, 10000, NULL, 'open')",
            (bot_id, fund, run_id, holding_id),
        )
        conn.commit()

    resp = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=3000.0,
        trade_date="2026-05-11", reason="intraday sell", run_id=run_id,
    )))
    assert resp["success"], resp
    assert resp["reference_nav"] is None
    assert resp["pricing_status"] == "awaiting_nav"

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        holding = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=?", (bot_id,))
    assert abs(holding["shares"] - 10000.0) < 1e-6
    assert abs(holding["pending_sell_shares"] - 3000.0) < 1e-6
    assert actions == []

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        conn.commit()

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-12", run_id="run-T1",
    )))
    assert settled["success"], settled
    assert settled["settled"][0]["settled_via"] == "t_plus_1"

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        holding = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=?", (bot_id,))
        order = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        action = _row(conn, "SELECT * FROM fund_bot_actions WHERE bot_id=?", (bot_id,))
        acc = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
    assert holding["run_id"] == run_id
    assert abs(holding["shares"] - 7000.0) < 1e-6
    assert abs(holding["pending_sell_shares"]) < 1e-6
    assert order["status"] == "confirmed"
    assert order["reference_nav"] == 1.2
    assert order["pricing_status"] == "priced"
    assert order["pricing_nav_date"] == "2026-05-11"
    assert abs(order["confirmed_amount"] - 3600.0) < 1e-3
    assert action["action_date"] == "2026-05-11"
    assert action["run_id"] == run_id
    assert abs(action["shares"] - 3000.0) < 1e-6
    assert abs(acc["cash"] - 3600.0) < 1e-3


def test_close_my_day_repairs_stale_active_holding_rows(reload_server, tmp_db):
    bot_id = "botGhost"
    run_id = "run-ghost"
    stale_fund = "000111"
    live_fund = "000222"
    s = reload_server
    import db as db_mod

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, stale_fund, name="旧仓基金")
        _seed_fund(conn, live_fund, name="现仓基金")
        for fund in (stale_fund, live_fund):
            _seed_nav(conn, fund, "2026-01-01", 1.0)
            _seed_nav(conn, fund, "2026-01-02", 1.0)
            _seed_nav(conn, fund, "2026-01-03", 1.0)
        _seed_account(conn, bot_id, cash=700.0, initial=1000.0, run_id=run_id)
        conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, unrealized_pnl, "
            " unrealized_pnl_pct, actual_weight, status, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 1.0, 1.0, 0, 0, 0, 200, 0, 0, 0.2, ?, ?)",
            (bot_id, stale_fund, "旧仓基金", "股票类", "satellite", "2026-01-01", "active", run_id),
        )
        conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, unrealized_pnl, "
            " unrealized_pnl_pct, actual_weight, status, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 1.0, 1.0, 300, 0, 300, 300, 0, 0, 0.3, ?, ?)",
            (bot_id, live_fund, "现仓基金", "股票类", "core", "2026-01-01", "active", run_id),
        )
        for fund, action_type, amount, shares, action_date in (
            (stale_fund, "ADD", 200.0, 200.0, "2026-01-01"),
            (live_fund, "ADD", 300.0, 300.0, "2026-01-01"),
            (stale_fund, "REDUCE", 200.0, 200.0, "2026-01-02"),
        ):
            conn.execute(
                "INSERT INTO fund_bot_actions "
                "(bot_id, fund_code, action_type, nav_used, amount, shares, fee, reason, action_date, run_id) "
                "VALUES (?, ?, ?, 1.0, ?, ?, 0, ?, ?, ?)",
                (bot_id, fund, action_type, amount, shares, "seed", action_date, run_id),
            )
        conn.commit()

    data = json.loads(asyncio.run(s.portfolio_close_my_day(
        bot_id=bot_id, trade_date="2026-01-03", run_id=run_id,
    )))
    assert data["success"], data
    active_codes = [h["fund_code"] for h in data["holdings"] if h["status"] == "active"]
    assert active_codes == [live_fund]
    assert abs(data["assets"]["cash_available"] - 700.0) < 1e-6
    assert abs(data["assets"]["market_value"] - 300.0) < 1e-6
    assert abs(data["assets"]["total_value"] - 1000.0) < 1e-6

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        stale = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND fund_code=? AND run_id=?", (bot_id, stale_fund, run_id))
    assert stale["status"] == "closed"
    assert abs(stale["shares"] or 0.0) < 1e-6
    assert abs(stale["market_value"] or 0.0) < 1e-6
    assert abs(stale["actual_weight"] or 0.0) < 1e-6


def test_settle_does_not_consume_future_lots(reload_server):
    """3/5 的卖单不能消耗 3/6 才买入的 lot；否则会产生负持有天数。"""
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _seed_fund(conn, "000777")
        _seed_nav(conn, "000777", "2026-05-10", 1.0)
        _seed_nav(conn, "000777", "2026-05-11", 1.2)
        _seed_nav(conn, "000777", "2026-05-12", 1.2)
        _seed_account(conn, "botFuture", cash=0.0, initial=1000.0, run_id="run-F")
        cur = conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
            "VALUES ('botFuture', '000777', '测试基金', '股票类', 'core', '2026-05-10', 1.0, 1.2, "
            " 150, 0, 150, 180, 'active', 'run-F')"
        )
        hid = cur.lastrowid
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, shares_initial, shares_remaining, "
            " cost_initial, cost_remaining, source_order_id, status) "
            "VALUES ('botFuture','000777','run-F',?,'2026-05-10',1.0,100,100,100,100,NULL,'open')",
            (hid,),
        )
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, shares_initial, shares_remaining, "
            " cost_initial, cost_remaining, source_order_id, status) "
            "VALUES ('botFuture','000777','run-F',?,'2026-05-12',1.0,50,50,50,50,NULL,'open')",
            (hid,),
        )
        conn.commit()

    placed = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id="botFuture", fund_code="000777", shares=150.0,
        trade_date="2026-05-11", reason="sell all", run_id="run-F",
    )))
    assert placed["success"], placed

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botFuture", as_of_date="2026-05-12", run_id="run-F",
    )))
    assert not settled["success"], settled
    assert "insufficient eligible open lots" in settled["blocking_skipped"][0]["reason"]

    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        lots = _rows(conn, "SELECT entry_date, shares_remaining, status FROM fund_bot_holding_lots WHERE bot_id='botFuture' ORDER BY entry_date")
        order = _row(conn, "SELECT status FROM fund_bot_orders WHERE bot_id='botFuture'")
    assert [(l["entry_date"], l["shares_remaining"], l["status"]) for l in lots] == [
        ("2026-05-10", 100.0, "open"),
        ("2026-05-12", 50.0, "open"),
    ]
    assert order["status"] == "pending"


def test_settle_allows_dust_gap_on_full_liquidation(reload_server):
    """全仓卖出时，order shares 与 lot shares 的亚份额舍入差不能让订单永久 pending。"""
    s, db_path = _setup_standard(
        reload_server, bot_id="botDust", fund="000778", shares=100.002,
        cost=100.002, entry_date="2026-05-01", sell_date="2026-05-11",
        sell_nav=1.2, settle_date="2026-05-12", run_id="run-D",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE fund_bot_holding_lots SET shares_initial=100, shares_remaining=100, "
            "cost_initial=100, cost_remaining=100 WHERE bot_id='botDust'"
        )
        conn.commit()

    placed = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id="botDust", fund_code="000778", shares=100.002,
        trade_date="2026-05-11", reason="sell all", run_id="run-D",
    )))
    assert placed["success"], placed

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botDust", as_of_date="2026-05-12", run_id="run-D",
    )))
    assert settled["success"], settled
    assert settled["settled"][0]["shares_sold"] == 100.0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT status, shares, pending_sell_shares FROM fund_bot_holdings WHERE bot_id='botDust'")
        o = _row(conn, "SELECT status, confirmed_shares FROM fund_bot_orders WHERE bot_id='botDust'")
    assert h["status"] == "closed"
    assert abs(h["shares"] or 0.0) < 1e-9
    assert abs(h["pending_sell_shares"] or 0.0) < 1e-9
    assert o["status"] == "confirmed"
    assert abs(o["confirmed_shares"] - 100.0) < 1e-9


def test_settle_closes_remaining_lot_dust_on_full_liquidation(reload_server):
    """请求份额略小于持仓时，全仓容差不能留下无 holding 的 open lot。"""
    s, db_path = _setup_standard(
        reload_server, bot_id="botLotDust", fund="000779", shares=100.002,
        cost=100.002, entry_date="2026-05-01", sell_date="2026-05-11",
        sell_nav=1.2, settle_date="2026-05-12", run_id="run-LD",
    )

    placed = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id="botLotDust", fund_code="000779", shares=100.0,
        trade_date="2026-05-11", reason="sell all within dust tolerance", run_id="run-LD",
    )))
    assert placed["success"], placed

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botLotDust", as_of_date="2026-05-12", run_id="run-LD",
    )))
    assert settled["success"], settled
    assert settled["settled"][0]["shares_sold"] == 100.0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT status, shares FROM fund_bot_holdings WHERE bot_id='botLotDust'")
        lots = _rows(conn, "SELECT status, shares_remaining, cost_remaining FROM fund_bot_holding_lots WHERE bot_id='botLotDust'")
    assert h["status"] == "closed"
    assert h["shares"] == 0
    assert all(l["status"] == "closed" for l in lots)
    assert all(l["shares_remaining"] == 0 and l["cost_remaining"] == 0 for l in lots)


# =============================================================================
#  清仓残留按「值不值 ¥1」判，不按固定份额容差判（bot105d 019633 回归）
# =============================================================================

def _seed_extra_lot(conn, bot_id, fund_code, run_id, holding_id, entry_date,
                    entry_nav, shares, cost):
    conn.execute(
        "INSERT INTO fund_bot_holding_lots "
        "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
        " shares_initial, shares_remaining, cost_initial, cost_remaining, "
        " source_order_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open')",
        (bot_id, fund_code, run_id, holding_id, entry_date, entry_nav,
         shares, shares, cost, cost),
    )


def test_settle_full_exit_when_residue_worth_under_one_yuan(reload_server):
    """bot 把份额抄成一位小数：残留 0.0696 份越过 0.05 容差，但只值 ¥0.19 → 仍判全清。

    旧口径判成部分卖出，holding 侧后来按 ¥1 口径置 closed，lot 侧留下 open 零头，
    这只基金以后任何一次真清仓都会被 full-exit 守卫永久挡住。
    """
    s, db_path = _setup_standard(
        reload_server, bot_id="botCopy1", fund="019633", shares=41622.269579,
        cost=83244.5, entry_date="2026-04-10", entry_nav=2.0,
        sell_date="2026-05-11", sell_nav=2.7871, settle_date="2026-05-12",
        run_id="run-C1",
    )

    placed = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id="botCopy1", fund_code="019633", shares=41622.2,
        trade_date="2026-05-11", reason="clear", run_id="run-C1",
    )))
    assert placed["success"], placed

    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botCopy1", as_of_date="2026-05-12", run_id="run-C1",
    )))
    assert settled["success"], settled

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT status, shares FROM fund_bot_holdings WHERE bot_id='botCopy1'")
        lots = _rows(conn, "SELECT status, shares_remaining FROM fund_bot_holding_lots WHERE bot_id='botCopy1'")
    assert h["status"] == "closed", "残留只值 ¥0.19，应判全清"
    assert abs(h["shares"] or 0.0) < 1e-9
    assert all(l["status"] == "closed" and l["shares_remaining"] == 0 for l in lots), lots


def test_settle_keeps_partial_when_residue_worth_over_one_yuan(reload_server):
    """真实剩仓（值 ≥ ¥1）不能被零头口径误吞：holding 保持 active、lot 保持 open。"""
    s, db_path = _setup_standard(
        reload_server, bot_id="botCopy2", fund="019633", shares=41622.269579,
        cost=83244.5, entry_date="2026-04-10", entry_nav=2.0,
        sell_date="2026-05-11", sell_nav=2.7871, settle_date="2026-05-12",
        run_id="run-C2",
    )

    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botCopy2", fund_code="019633", shares=41621.0,
        trade_date="2026-05-11", reason="trim", run_id="run-C2",
    ))
    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botCopy2", as_of_date="2026-05-12", run_id="run-C2",
    )))
    assert settled["success"], settled

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT status, shares FROM fund_bot_holdings WHERE bot_id='botCopy2'")
        lots = _rows(conn, "SELECT status, shares_remaining FROM fund_bot_holding_lots WHERE bot_id='botCopy2'")
    # 残留 1.269579 份 × 2.7871 ≈ ¥3.54 ≥ ¥1
    assert h["status"] == "active"
    assert abs(h["shares"] - 1.269579) < 1e-6
    assert any(l["status"] == "open" for l in lots), lots


def test_settle_full_exit_tolerates_legacy_dust_lot(reload_server):
    """库里已有的孤儿零头 lot 不该永久挡住这只基金的清仓——放行并顺手 sweep 掉。"""
    s, db_path = _setup_standard(
        reload_server, bot_id="botCopy3", fund="019633", shares=10_000.0,
        cost=20_000.0, entry_date="2026-04-10", entry_nav=2.0,
        sell_date="2026-05-11", sell_nav=2.7871, settle_date="2026-05-12",
        run_id="run-C3",
    )
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        hid = _row(conn, "SELECT holding_id FROM fund_bot_holdings WHERE bot_id='botCopy3'")["holding_id"]
        # entry_date 更晚 → FIFO 先吃主 lot，这条零头留到最后
        _seed_extra_lot(conn, "botCopy3", "019633", "run-C3", hid,
                        "2026-04-20", 2.0, 0.069579, 0.139)
        conn.commit()

    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botCopy3", fund_code="019633", shares=10_000.0,
        trade_date="2026-05-11", reason="clear", run_id="run-C3",
    ))
    settled = json.loads(asyncio.run(s.settle_pending_fund_orders(
        bot_id="botCopy3", as_of_date="2026-05-12", run_id="run-C3",
    )))
    assert settled["success"], settled

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT status FROM fund_bot_holdings WHERE bot_id='botCopy3'")
        lots = _rows(conn, "SELECT status, shares_remaining FROM fund_bot_holding_lots WHERE bot_id='botCopy3'")
    assert h["status"] == "closed"
    assert all(l["status"] == "closed" and l["shares_remaining"] == 0 for l in lots), lots


def test_settle_full_exit_still_rejects_material_lot_residue(reload_server):
    """守卫不能被削弱：残留值 ≥ ¥1 的 lot 缺口仍要抛错，不能默默抹平。"""
    s, db_path = _setup_standard(
        reload_server, bot_id="botCopy4", fund="019633", shares=10_000.0,
        cost=20_000.0, entry_date="2026-04-10", entry_nav=2.0,
        sell_date="2026-05-11", sell_nav=2.7871, settle_date="2026-05-12",
        run_id="run-C4",
    )
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        hid = _row(conn, "SELECT holding_id FROM fund_bot_holdings WHERE bot_id='botCopy4'")["holding_id"]
        _seed_extra_lot(conn, "botCopy4", "019633", "run-C4", hid,
                        "2026-04-20", 2.0, 5.0, 10.0)  # 5 份 ≈ ¥13.9
        conn.commit()

    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botCopy4", fund_code="019633", shares=10_000.0,
        trade_date="2026-05-11", reason="clear", run_id="run-C4",
    ))
    with pytest.raises(ValueError, match="full-exit lot residue too large"):
        asyncio.run(s.settle_pending_fund_orders(
            bot_id="botCopy4", as_of_date="2026-05-12", run_id="run-C4",
        ))


# =============================================================================
#  同一标的反向交易 7 交易日硬冷却
# =============================================================================

def test_reverse_order_gate_rejects_buy_within_7_trading_days_after_sell(reload_server):
    s = reload_server
    bot_id, fund, run_id = "botRGbuy", "009901", "run-RG"
    calendar = [
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
    ]
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        for d in calendar:
            _seed_nav(conn, fund, d, 1.0)
        _seed_account(conn, bot_id, cash=100_000.0, initial=100_000.0, run_id=run_id)
        _seed_order(
            conn, bot_id=bot_id, fund_code=fund, order_type="sell",
            order_date=calendar[0], run_id=run_id,
        )
        conn.commit()

    blocked = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date=calendar[6], reason="reverse too early", run_id=run_id,
    )))
    assert blocked["success"] is False
    assert blocked["gate"] == "reverse_order_cooldown"
    assert blocked["elapsed_trading_days"] == 6
    assert blocked["remaining_trading_days"] == 1

    allowed = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id=bot_id, fund_code=fund, amount=10_000.0,
        trade_date=calendar[7], reason="cooldown complete", run_id=run_id,
    )))
    assert allowed["success"] is True, allowed


def test_reverse_order_gate_rejects_sell_within_7_trading_days_after_buy(reload_server):
    s = reload_server
    bot_id, fund, run_id = "botRGsell", "009902", "run-RG"
    calendar = [
        "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
        "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
    ]
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        for d in calendar:
            _seed_nav(conn, fund, d, 1.0)
        _seed_account(conn, bot_id, cash=0.0, initial=10_000.0, run_id=run_id)
        _seed_holding(
            conn, bot_id, fund, shares=10_000.0, cost=10_000.0,
            entry_date=calendar[0], entry_nav=1.0, run_id=run_id,
        )
        _seed_order(
            conn, bot_id=bot_id, fund_code=fund, order_type="buy",
            order_date=calendar[0], run_id=run_id,
        )
        conn.commit()

    blocked = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=1_000.0,
        trade_date=calendar[6], reason="reverse too early", run_id=run_id,
    )))
    assert blocked["success"] is False
    assert blocked["gate"] == "reverse_order_cooldown"
    assert blocked["previous_order_type"] == "buy"

    allowed = json.loads(asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=1_000.0,
        trade_date=calendar[7], reason="cooldown complete", run_id=run_id,
    )))
    assert allowed["success"] is True, allowed
