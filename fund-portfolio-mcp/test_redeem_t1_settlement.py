"""SELL T+0 份额变动 / T+1 资金到账机制的回归测试。

新机制（与旧 settle-时扣 shares 的实现对比）：
  T 日 place_sell_order：
    - 用 T 日 NAV
    - 立即扣 holding.shares、按比例扣 amount_invested
    - 立即在 fund_bot_accounts.cash_receivable += (gross - fee)
    - 立即插入 fund_bot_actions（REDUCE，action_date=order_date）
    - order.confirmed_shares / confirmed_amount / fee 在 T 日就已锁定
    - account.cash 不变
    - holding.pending_sell_shares 仍保持 0（新机制不再用份额冻结）
  T+1 settle_pending_fund_orders：
    - 仅做 cash_receivable -> cash 的转账 + order 状态收口
    - 不再动 holdings、不再插 actions
  老机制兼容：confirmed_amount IS NULL 的存量 pending 卖单仍走旧 settle 路径
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
    conn.execute(
        "INSERT INTO fund_bot_holdings "
        "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
        " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
        "VALUES (?, ?, '测试基金', '股票类', 'core', ?, ?, ?, ?, 0, ?, ?, 'active', ?)",
        (bot_id, fund_code, entry_date, entry_nav, entry_nav, shares, shares * entry_nav,
         cost, run_id),
    )


def _row(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _setup_standard(reload_server, bot_id="botT", fund="000001",
                    entry_date="2026-04-10", entry_nav=1.0, shares=10_000.0,
                    cost=10_000.0, sell_date="2026-05-11", sell_nav=1.20,
                    initial_cash=10_000.0, account_cash=0.0):
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
        _seed_account(conn, bot_id, account_cash, initial=initial_cash)
        _seed_holding(conn, bot_id, fund, shares, cost, entry_date, entry_nav)
        conn.commit()
    return s, db_mod.DB_PATH


# =============================================================================
#  T 日 place_sell_order 行为
# =============================================================================

def test_sell_T_day_extracts_shares_immediately_for_partial(reload_server):
    """部分赎回：T 日 holding.shares 立刻减少，pending_sell_shares 保持 0。"""
    s, db_path = _setup_standard(reload_server)
    resp = asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=3000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    payload = json.loads(resp)
    assert payload["success"], payload

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert abs(h["shares"] - 7000.0) < 1e-6, f"shares 应在 T 日就扣到 7000，得到 {h['shares']}"
    assert abs(h["pending_sell_shares"]) < 1e-6, "新机制不再使用 pending_sell_shares 冻结"
    assert h["status"] == "active", "部分卖出后持仓应仍 active"


def test_sell_T_day_extracts_amount_invested_proportionally(reload_server):
    """部分赎回：amount_invested 按 sell/shares_before 比例扣减。"""
    s, db_path = _setup_standard(reload_server)  # cost=10000, shares=10000
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=2500.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    # 卖 25% → 成本保留 75%
    assert abs(h["amount_invested"] - 7500.0) < 1e-3, h["amount_invested"]


def test_sell_T_day_full_redeem_closes_holding(reload_server):
    """全部赎回：T 日 status='closed', exit_date=order_date, shares=0."""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=10000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert h["status"] == "closed"
    assert h["exit_date"] == "2026-05-11"
    assert abs(h["shares"]) < 1e-6


def test_sell_T_day_credits_cash_receivable_net_amount(reload_server):
    """T 日 cash_receivable += (gross - fee)。持有 31 天 → fee 为 0 → receivable = shares * nav。"""
    s, db_path = _setup_standard(reload_server)  # nav=1.20, fee=0%
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=4000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    assert abs(a["cash_receivable"] - 4800.0) < 1e-3, a["cash_receivable"]
    assert abs(a["cash"]) < 1e-6, "T 日 cash 不动"


def test_sell_T_day_cash_remains_untouched(reload_server):
    """T 日 cash 不变 — 钱要到 T+1 才到账。"""
    s, db_path = _setup_standard(reload_server, account_cash=1234.0)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=1000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = _row(conn, "SELECT cash FROM fund_bot_accounts WHERE bot_id='botT'")
    assert abs(a["cash"] - 1234.0) < 1e-6, "T 日 SELL 不能动 cash"


def test_sell_T_day_locks_fee_at_order_date_holding_days(reload_server):
    """赎回费按 order_date 那天的真实持有天数锁定，并写入 order.fee/confirmed_amount。

    持有 5 天（< 7 天）→ 1.5% 赎回费。
    """
    bot_id, fund = "botT", "000001"
    # 持有 5 天：2026-05-06 → 2026-05-11
    s, db_path = _setup_standard(
        reload_server, entry_date="2026-05-06", sell_date="2026-05-11",
        sell_nav=1.00,
    )
    asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=1000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        order = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id=?", (bot_id,))
        acc = _row(conn, "SELECT cash_receivable FROM fund_bot_accounts WHERE bot_id=?", (bot_id,))
    # gross=1000, fee=15 (1.5%), net=985
    assert abs(order["fee"] - 15.0) < 1e-3, order["fee"]
    assert abs(order["confirmed_amount"] - 985.0) < 1e-3, order["confirmed_amount"]
    assert abs(order["confirmed_shares"] - 1000.0) < 1e-3
    assert order["status"] == "pending"
    assert order["confirm_date"] is None, "T 日 confirm_date 还是 NULL"
    assert abs(acc["cash_receivable"] - 985.0) < 1e-3


def test_sell_T_day_inserts_REDUCE_action_at_order_date(reload_server):
    """T 日就要插入 fund_bot_actions 一条 REDUCE，action_date = order_date。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=2000.0,
        trade_date="2026-05-11", reason="trim", run_id="run-T",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT' ORDER BY action_id")
    assert len(actions) == 1, "应在 T 日插入一条 REDUCE action"
    a = actions[0]
    assert a["action_type"] == "REDUCE"
    assert a["action_date"] == "2026-05-11"
    assert a["run_id"] == "run-T"
    assert abs(float(a["shares"]) - 2000.0) < 1e-3


def test_sell_T_day_multiple_partials_same_day(reload_server):
    """同日多次部分赎回累加：每次基于当前 shares 算可卖、cash_receivable 累加。"""
    s, db_path = _setup_standard(reload_server)
    for amt in (1000.0, 2000.0):
        asyncio.run(s.portfolio_place_sell_order(
            bot_id="botT", fund_code="000001", shares=amt,
            trade_date="2026-05-11", reason="", run_id="run-T",
        ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
        orders = _rows(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT' ORDER BY order_id")
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT' ORDER BY action_id")
    assert abs(h["shares"] - 7000.0) < 1e-6
    # 持有 31 天 fee=0% → receivable = 3000*1.20 = 3600
    assert abs(a["cash_receivable"] - 3600.0) < 1e-3
    assert len(orders) == 2
    assert len(actions) == 2


# =============================================================================
#  T+1 settle 行为
# =============================================================================

def test_settle_converts_receivable_to_cash_for_new_sell(reload_server):
    """T+1 settle 把新机制 SELL 订单的 cash_receivable 转成 cash，不动 holdings。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=5000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    # T+1 settle
    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
        o = _row(conn, "SELECT * FROM fund_bot_orders WHERE bot_id='botT'")
        h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        actions = _rows(conn, "SELECT * FROM fund_bot_actions WHERE bot_id='botT'")
    # holding 在 T 日就动了，settle 不应再动 shares
    assert abs(h["shares"] - 5000.0) < 1e-6
    assert abs(a["cash_receivable"]) < 1e-6
    assert abs(a["cash"] - 6000.0) < 1e-3, f"5000*1.20*(1-0)=6000, got {a['cash']}"
    assert o["status"] == "confirmed"
    assert o["confirm_date"] == "2026-05-12"
    assert o["settle_run_id"] == "run-T1"
    # actions 只有 T 日那一条
    assert len(actions) == 1, "settle 不应再插 action"


def test_settle_for_new_sell_does_not_double_extract_shares(reload_server):
    """settle 处理新机制 SELL 时绝对不能再次扣 shares。"""
    s, db_path = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=10000.0,  # 全部
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    # T 日已 closed，shares=0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before_h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
    assert before_h["status"] == "closed"

    asyncio.run(s.settle_pending_fund_orders(
        bot_id="botT", as_of_date="2026-05-12", run_id="run-T1",
    ))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        after_h = _row(conn, "SELECT * FROM fund_bot_holdings WHERE bot_id='botT'")
        a = _row(conn, "SELECT * FROM fund_bot_accounts WHERE bot_id='botT'")
    assert after_h["status"] == "closed"
    assert abs(after_h["shares"]) < 1e-6
    # cash receivable -> cash
    assert abs(a["cash"] - 12000.0) < 1e-3, a["cash"]
    assert abs(a["cash_receivable"]) < 1e-6


# =============================================================================
#  存量老订单兼容
# =============================================================================

def test_legacy_pending_sell_still_settles_via_old_path(reload_server):
    """旧机制 pending SELL（confirmed_amount IS NULL、pending_sell_shares 冻结）在 settle 时仍走老路径。

    NOTE: 现在 legacy 路径也走 FIFO lot 消耗——前提是 migration 已为持仓建好 lot 行。
    本测试模拟"迁移完成后存留的老订单"的状态：seed holding + seed 对应 lot。
    """
    bot_id, fund = "botL", "000001"
    import db as db_mod
    s = reload_server
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        _seed_nav(conn, fund, "2026-05-11", 1.20)
        _seed_account(conn, bot_id, 0.0, initial=10_000.0)
        # 老机制持仓：pending_sell_shares 冻结
        cur = conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, entry_date, entry_nav, latest_nav, "
            " shares, pending_sell_shares, amount_invested, market_value, status, run_id) "
            "VALUES (?, ?, '测试', '股票类', 'core', '2026-04-10', 1.00, 1.00, "
            " 10000, 3000, 10000, 10000, 'active', 'legacy')",
            (bot_id, fund),
        )
        holding_id = cur.lastrowid
        # 模拟 migration 产物：单 lot 对应整个 holding（10000 份，成本 10000）
        conn.execute(
            "INSERT INTO fund_bot_holding_lots "
            "(bot_id, fund_code, run_id, holding_id, entry_date, entry_nav, "
            " shares_initial, shares_remaining, cost_initial, cost_remaining, "
            " source_order_id, status) "
            "VALUES (?, ?, 'legacy', ?, '2026-04-10', 1.00, 10000, 10000, 10000, 10000, NULL, 'open')",
            (bot_id, fund, holding_id),
        )
        # 老 pending 卖单：confirmed_amount IS NULL，order_amount 是申报份额
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
    # 老路径在 settle 时扣 shares + 释放冻结
    assert abs(h["shares"] - 7000.0) < 1e-3, h["shares"]
    assert abs(h["pending_sell_shares"]) < 1e-3
    # 31 天持有 → fee=0% → cash += 3000*1.20 = 3600
    assert abs(a["cash"] - 3600.0) < 1e-3, a["cash"]
    assert o["status"] == "confirmed"
    assert len(actions) == 1, "老路径在 settle 时插 action"


# =============================================================================
#  接口层：portfolio_get_my_history 暴露 cash_receivable
# =============================================================================

def test_get_my_history_exposes_cash_receivable(reload_server):
    """portfolio_get_my_history 的 account 块新增 cash_receivable 字段。"""
    s, _ = _setup_standard(reload_server)
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=4000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    resp = json.loads(asyncio.run(s.portfolio_get_my_history(bot_id="botT")))
    assert "cash_receivable" in resp["account"]
    assert abs(float(resp["account"]["cash_receivable"]) - 4800.0) < 1e-3
    # total = cash + in_transit + receivable + market_value
    total = float(resp["account"]["total_value"])
    expected = 0.0 + 0.0 + 4800.0 + 6000.0 * 1.20  # cash=0, in_transit=0, rec=4800, mv=6000*1.20=7200
    assert abs(total - expected) < 1e-3, f"expected {expected}, got {total}"


def test_total_value_continuity_T_to_T_plus_1(reload_server):
    """T 日卖出与 T+1 settle 之间，bot 视角的总资产必须连续（NAV 不变前提下）。
    通过 portfolio_close_my_day.assets.total_value（直接用 account 字段拼）验证，
    避开 _replay_fund_account_state 的 run-isolation 行为（既有设计）。

    场景：initial=12000，已建仓 10000 元（10000 shares @1.0），cash=2000。
         T 日 NAV=1.20，卖前总资产 = 2000 + 12000 = 14000。
    """
    bot_id, fund = "botC", "000001"
    s = reload_server
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, fund)
        _seed_nav(conn, fund, "2026-04-10", 1.00)
        for d in ("2026-05-11", "2026-05-12"):
            _seed_nav(conn, fund, d, 1.20)
        _seed_account(conn, bot_id, cash=2_000.0, initial=12_000.0)
        _seed_holding(conn, bot_id, fund, shares=10_000.0, cost=10_000.0,
                      entry_date="2026-04-10", entry_nav=1.0)
        conn.commit()

    # 注：_compute_fund_snapshot 的 replay 走 run-isolation，看不到我们直接 seed 的 holding。
    # 但本测试关心的是 place_sell_order 已用 nav=1.20 更新了 holding.market_value，
    # close_my_day.assets.total_value 从 account/holdings 直接拼，与 NAV 同步。

    # T 日卖一半 → T 日 shares 已扣，cash_receivable=6000
    asyncio.run(s.portfolio_place_sell_order(
        bot_id=bot_id, fund_code=fund, shares=5000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    r1 = json.loads(asyncio.run(s.portfolio_close_my_day(
        bot_id=bot_id, trade_date="2026-05-11", run_id="run-T",
    )))
    # cash=2000 + in_transit=0 + receivable=6000 + mv=6000 → 14000 ✓
    assert abs(r1["assets"]["total_value"] - 14000.0) < 1e-2, r1["assets"]
    assert abs(r1["assets"]["cash_available"] - 2000.0) < 1e-2
    assert abs(r1["assets"]["cash_receivable"] - 6000.0) < 1e-2
    assert abs(r1["assets"]["market_value"] - 6000.0) < 1e-2

    # T+1 settle → cash_receivable 清零、cash += 6000
    asyncio.run(s.settle_pending_fund_orders(
        bot_id=bot_id, as_of_date="2026-05-12", run_id="run-T1",
    ))
    r2 = json.loads(asyncio.run(s.portfolio_close_my_day(
        bot_id=bot_id, trade_date="2026-05-12", run_id="run-T1",
    )))
    # cash=8000 + receivable=0 + mv=6000 → 14000 ✓
    assert abs(r2["assets"]["total_value"] - 14000.0) < 1e-2, r2["assets"]
    assert abs(r2["assets"]["cash_available"] - 8000.0) < 1e-2
    assert abs(r2["assets"]["cash_receivable"]) < 1e-6


def test_place_buy_cannot_use_cash_receivable(reload_server):
    """receivable 不算可用现金 — 即使 cash=0、receivable 充足，买单也应失败。"""
    s, _ = _setup_standard(reload_server, account_cash=0.0)  # cash=0
    # 卖一部分制造 receivable
    asyncio.run(s.portfolio_place_sell_order(
        bot_id="botT", fund_code="000001", shares=4000.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    ))
    # 再造一只 buyable 基金
    import db as db_mod
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        _seed_fund(conn, "000002", name="基金B")
        _seed_nav(conn, "000002", "2026-05-11", 1.00)
        conn.commit()
    # 尝试买 — 应失败，cash=0
    resp = json.loads(asyncio.run(s.portfolio_place_buy_order(
        bot_id="botT", fund_code="000002", amount=100.0,
        trade_date="2026-05-11", reason="", run_id="run-T",
    )))
    assert not resp["success"], "cash=0 时即使有 receivable 也不能买"


# =============================================================================
#  盘中无 T 日 NAV：先接单，NAV 入库后按 T 日净值定价
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
    assert settled["settled"][0]["settled_via"] == "legacy"

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
