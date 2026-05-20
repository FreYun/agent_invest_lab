"""runtime per-run 隔离测试（不是 schema 迁移测试）。

覆盖：
- 严格层 portfolio_get_my_history / portfolio_get_my_performance /
  portfolio_get_my_trades：空 run_id 必须返错；非空 run_id 必须只看本 run 行。
- 松散层 get_fund_holdings / get_fund_curve / get_fund_position_snapshots /
  get_fund_review_history：空 run_id 跨 run 全量；非空只看该 run。

种子数据：同 bot 两个 run（runA / runB），各 1 行 active 持仓 + 1 行已结算 BUY 单。
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
def reload_server(tmp_db):
    """server.py import 时绑定 DB_PATH，monkeypatch 后必须 reload。"""
    import server
    importlib.reload(server)
    return server


def _seed_two_runs(db_path: str, bot_id: str = "botX"):
    """同 bot 在 runA / runB 各落 1 行 active 持仓、1 行已结算 BUY 单、1 行日快照。
    每天 trade_date 一致，模拟 run 重跑同一天的场景。"""
    conn = sqlite3.connect(db_path)
    # 账户：accounts 表 PK 是 bot_id，只能一行——按 spec 设计取最近 run 视角。
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, cash_in_transit, run_id) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, 1_000_000.0, 400_000.0, "runB"),  # 最近一次 init 是 runB
    )
    # holdings：runA 持有 510300 (mv=300k)；runB 持有 008528 (mv=600k)
    conn.execute(
        "INSERT INTO fund_bot_holdings "
        "(bot_id, fund_code, fund_name, share_class, asset_class, role, entry_date, "
        " entry_nav, latest_nav, shares, amount_invested, market_value, "
        " unrealized_pnl, unrealized_pnl_pct, status, run_id) "
        "VALUES (?, '510300', '沪深300ETF', 'A', '股票类', '核心底仓', '2026-01-02', "
        " 2.0, 2.5, 120000, 240000, 300000, 60000, 25.0, 'active', 'runA')",
        (bot_id,),
    )
    conn.execute(
        "INSERT INTO fund_bot_holdings "
        "(bot_id, fund_code, fund_name, share_class, asset_class, role, entry_date, "
        " entry_nav, latest_nav, shares, amount_invested, market_value, "
        " unrealized_pnl, unrealized_pnl_pct, status, run_id) "
        "VALUES (?, '008528', '华泰柏瑞质量成长A', 'A', '股票类', '核心底仓', '2026-01-05', "
        " 1.5, 2.0, 300000, 450000, 600000, 150000, 33.3, 'active', 'runB')",
        (bot_id,),
    )
    # orders：runA 的 BUY 已 confirmed；runB 的 BUY 也已 confirmed
    conn.execute(
        "INSERT INTO fund_bot_orders "
        "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
        " order_amount, reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, "
        " action_reason, status, order_run_id, settle_run_id) "
        "VALUES (?, '510300', '沪深300ETF', 'buy', '2026-01-02', '2026-01-03', "
        " 240000, 2.0, 2.0, 120000, 240000, 0, '建仓', 'confirmed', 'runA', 'runA')",
        (bot_id,),
    )
    conn.execute(
        "INSERT INTO fund_bot_orders "
        "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, "
        " order_amount, reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, "
        " action_reason, status, order_run_id, settle_run_id) "
        "VALUES (?, '008528', '华泰柏瑞质量成长A', 'buy', '2026-01-05', '2026-01-06', "
        " 450000, 1.5, 1.5, 300000, 450000, 0, '建仓', 'confirmed', 'runB', 'runB')",
        (bot_id,),
    )
    # daily_snapshots：runA / runB 各落 1 行 trade_date=2026-01-10
    for run, total in (("runA", 1_050_000.0), ("runB", 1_100_000.0)):
        conn.execute(
            "INSERT INTO fund_bot_daily_snapshots "
            "(bot_id, trade_date, run_id, initial_capital, cash, invested_value, total_value, "
            " net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct, "
            " equity_weight, bond_weight, gold_weight, cash_weight, holdings_json) "
            "VALUES (?, '2026-01-10', ?, 1000000, 200000, ?, ?, ?, 0.5, ?, -1.0, "
            " 1.0, 0.0, 0.0, 0.0, '[]')",
            (bot_id, run, total - 200000, total, total / 1_000_000.0, (total - 1_000_000.0) / 10_000.0),
        )
    conn.commit()
    conn.close()


def test_seed_data_inserts_both_runs(tmp_db):
    """烟雾测试：种子函数能把数据塞进 fixture 准备的 db。"""
    _seed_two_runs(tmp_db)
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT run_id, fund_code FROM fund_bot_holdings WHERE bot_id='botX' ORDER BY run_id"
    ).fetchall()
    assert rows == [("runA", "510300"), ("runB", "008528")]
    # 同时确认 orders / daily_snapshots 也都进库了——三张表任一漏插都让后续 strict 测试误报。
    orders_count = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_orders WHERE bot_id='botX'"
    ).fetchone()[0]
    assert orders_count == 2
    snapshots_count = conn.execute(
        "SELECT COUNT(*) FROM fund_bot_daily_snapshots WHERE bot_id='botX'"
    ).fetchone()[0]
    assert snapshots_count == 2
    conn.close()


# === Strict layer: portfolio_get_my_history ===

def test_get_my_history_empty_run_id_returns_error(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_history("botX"))  # run_id 缺省 = ""
    data = json.loads(payload)
    assert data["success"] is False, data
    assert "run_id 缺失" in data["message"], data["message"]


def test_get_my_history_filters_by_run_id(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_history("botX", run_id="runA"))
    data = json.loads(payload)
    assert data["success"], data
    codes = [h["fund_code"] for h in data["holdings"]]
    assert codes == ["510300"], f"runA 应只看到 510300, 实际: {codes}"
    orders_codes = [o["fund_code"] for o in data["orders"]]
    assert orders_codes == ["510300"], f"runA 应只看到 510300 订单, 实际: {orders_codes}"


# === Strict layer: portfolio_get_my_performance ===

def _seed_perf_rows(db_path: str, bot_id: str = "botX"):
    """除 _seed_two_runs 之外再补 fund_bot_performance 两行（runA / runB），
    用来测 holdings_performance / interval_metrics 的 run_id 过滤。"""
    conn = sqlite3.connect(db_path)
    for run in ("runA", "runB"):
        conn.execute(
            "INSERT INTO fund_bot_performance "
            "(bot_id, trade_date, run_id, period, return_pct, max_drawdown_pct, "
            " volatility_pct, sharpe_ratio, calmar_ratio, data_points, window_target_days, fallback) "
            "VALUES (?, '2026-01-09', ?, 'since_inception', 5.0, -1.0, 0.5, 1.0, 5.0, 5, NULL, 0)",
            (bot_id, run),
        )
    conn.commit()
    conn.close()


def test_get_my_performance_empty_run_id_returns_error(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    _seed_perf_rows(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_performance("botX", "2026-01-11"))
    data = json.loads(payload)
    assert data["success"] is False, data
    assert "run_id 缺失" in data["message"]


def test_get_my_performance_filters_by_run_id(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    _seed_perf_rows(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_performance("botX", "2026-01-11", run_id="runA"))
    data = json.loads(payload)
    assert data["success"], data
    # daily_series 只有 runA 那一行
    assert len(data["daily_series"]) == 1, data["daily_series"]
    assert data["daily_series"][0]["total_value"] == 1_050_000.0
    # holdings_performance 只看到 runA 的 510300，不含 runB 的 008528
    hp = data["interval_metrics"]["holdings_performance"]
    assert set(hp.keys()) == {"510300"}, f"runA 的 hp keys 应只有 510300, 实际: {list(hp.keys())}"


# === Strict layer: portfolio_get_my_trades ===

def test_get_my_trades_empty_run_id_returns_error(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_trades("botX", "2026-01-11"))
    data = json.loads(payload)
    assert data["success"] is False
    assert "run_id 缺失" in data["message"]


def test_get_my_trades_filters_by_run_id(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_trades("botX", "2026-01-11", run_id="runB"))
    data = json.loads(payload)
    assert data["success"], data
    codes = [o["fund_code"] for o in data["orders"]]
    assert codes == ["008528"], f"runB 应只看到 008528 订单, 实际: {codes}"


# === Soft layer: get_fund_holdings (admin) ===

def test_get_fund_holdings_no_run_id_returns_all(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.get_fund_holdings("botX"))
    data = json.loads(payload)
    assert data["success"], data
    codes = sorted(h["fund_code"] for h in data["holdings"])
    assert codes == ["008528", "510300"], f"admin 默认应跨 run, 实际: {codes}"


def test_get_fund_holdings_with_run_id_filters(reload_server, tmp_db):
    _seed_two_runs(tmp_db)
    s = reload_server
    payload = asyncio.run(s.get_fund_holdings("botX", run_id="runB"))
    data = json.loads(payload)
    assert data["success"], data
    codes = [h["fund_code"] for h in data["holdings"]]
    assert codes == ["008528"], f"指定 runB 应只看到 008528, 实际: {codes}"


# === Followup: completed_positions buys/sells filter by run_id ===

def test_get_my_performance_completed_positions_filtered_by_run_id(reload_server, tmp_db):
    """两个 run 在同一基金各自有一次完整 round-trip：runA 买 1000 卖 1200，
    runB 买 2000 卖 2500。runA 的 completed_positions 应只算 runA 自己的 buys/sells，
    总 invested=1000、proceeds=1200，不能把 runB 的混进去。"""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    bot_id = "botRT"

    # 账户行（accounts PK 是 bot_id，所以只能一行；取最近 run）
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
        "VALUES (?, 1_000_000.0, 800_000.0, 'runB')",
        (bot_id,),
    )

    # 同 fund 510888，每个 run 各一次完整 round-trip（buy 在 d0, sell 在 d1）
    for run, d0, d1, buy_amt, sell_amt in (
        ("runA", "2026-01-02", "2026-01-09", 1000.0, 1200.0),
        ("runB", "2026-01-03", "2026-01-09", 2000.0, 2500.0),
    ):
        # 已平仓持仓
        conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, share_class, asset_class, role, entry_date, exit_date, "
            " entry_nav, latest_nav, shares, amount_invested, market_value, status, run_id) "
            "VALUES (?, '510888', 'TestFund', 'A', '股票类', '核心', ?, ?, "
            " 1.0, 1.0, 0, 0, 0, 'closed', ?)",
            (bot_id, d0, d1, run),
        )
        # buy 单
        conn.execute(
            "INSERT INTO fund_bot_orders "
            "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, order_amount, "
            " reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, action_reason, "
            " status, order_run_id, settle_run_id) "
            "VALUES (?, '510888', 'TestFund', 'buy', ?, ?, ?, 1.0, 1.0, ?, ?, 0, '建仓', "
            " 'confirmed', ?, ?)",
            (bot_id, d0, d0, buy_amt, buy_amt, buy_amt, run, run),
        )
        # sell 单（confirmed_amount 是 proceeds）
        conn.execute(
            "INSERT INTO fund_bot_orders "
            "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, order_amount, "
            " reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, action_reason, "
            " status, order_run_id, settle_run_id) "
            "VALUES (?, '510888', 'TestFund', 'sell', ?, ?, ?, 1.2, 1.2, ?, ?, 0, '清仓', "
            " 'confirmed', ?, ?)",
            (bot_id, d1, d1, sell_amt, sell_amt, sell_amt, run, run),
        )

    # 至少一行 daily_snapshot, 避免 portfolio_get_my_performance 早 return
    conn.execute(
        "INSERT INTO fund_bot_daily_snapshots "
        "(bot_id, trade_date, run_id, initial_capital, cash, invested_value, total_value, "
        " net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct, "
        " equity_weight, bond_weight, gold_weight, cash_weight, holdings_json) "
        "VALUES (?, '2026-01-10', 'runA', 1000000, 1000000, 0, 1000000, 1.0, 0, 0, 0, "
        " 1, 0, 0, 0, '[]')",
        (bot_id,),
    )
    conn.commit()
    conn.close()

    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_performance(bot_id, "2026-01-11", run_id="runA"))
    data = json.loads(payload)
    assert data["success"], data
    cp = data["completed_positions"]
    assert len(cp) == 1, f"runA 应只看到 1 个 round-trip, 实际: {len(cp)}"
    rt = cp[0]
    assert rt["total_invested"] == 1000.0, f"runA 投入应是 1000, 实际: {rt['total_invested']}"
    assert rt["total_proceeds"] == 1200.0, f"runA 回收应是 1200, 实际: {rt['total_proceeds']}"
    # 如果 buys/sells SELECT 没按 run_id 过滤，total_invested 会变成 3000 (1000+2000)


# === Cross-run phantom-order leak ===
# 真实场景：先前一次 backtest（runOld）跑崩留下一笔 pending sell，新 run（runNew）启动后
# 的 settle 流程把它确认了，从而 order_run_id=runOld、settle_run_id=runNew 的"幽灵单"
# 会出现在 fund_bot_orders 里。它的 cash 影响不该（也确实没有）落进 runNew 的资金视图，
# 但下面三处用户面查询如果用 `OR settle_run_id=?` 就会把它误算进 runNew 的统计/订单列表。

def _seed_phantom_order(db_path: str, bot_id: str = "botPhantom") -> None:
    conn = sqlite3.connect(db_path)
    # runNew 在用：账户 + 一笔正常买单（runNew 自己下、runNew 自己结算）
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, cash_in_transit, run_id) "
        "VALUES (?, 1_000_000.0, 600_000.0, 0, 'runNew')",
        (bot_id,),
    )
    conn.execute(
        "INSERT INTO fund_bot_holdings "
        "(bot_id, fund_code, fund_name, share_class, asset_class, role, entry_date, "
        " entry_nav, latest_nav, shares, amount_invested, market_value, status, run_id) "
        "VALUES (?, '016729', 'TestFund', 'A', '股票类', '核心', '2026-01-05', "
        " 1.0, 1.0, 400000, 400000, 400000, 'active', 'runNew')",
        (bot_id,),
    )
    conn.execute(
        "INSERT INTO fund_bot_orders "
        "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, order_amount, "
        " reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, action_reason, "
        " status, order_run_id, settle_run_id) "
        "VALUES (?, '016729', 'TestFund', 'buy', '2026-01-05', '2026-01-06', "
        " 400000, 1.0, 1.0, 400000, 400000, 480, '建仓', 'confirmed', 'runNew', 'runNew')",
        (bot_id,),
    )
    # 幽灵卖单：runOld 下的、runNew settle 时收口的——非本 run 的决策，必须从 runNew 的
    # 用户面查询里隔离掉。
    conn.execute(
        "INSERT INTO fund_bot_orders "
        "(bot_id, fund_code, fund_name, order_type, order_date, confirm_date, order_amount, "
        " reference_nav, confirm_nav, confirmed_shares, confirmed_amount, fee, action_reason, "
        " status, order_run_id, settle_run_id) "
        "VALUES (?, '510300', 'OtherFund', 'sell', '2026-01-04', '2026-01-05', "
        " 500000, 2.0, 2.0, 500000, 999999, 4900, '幽灵', 'confirmed', 'runOld', 'runNew')",
        (bot_id,),
    )
    # daily snapshot：让 portfolio_get_my_performance 走完整路径
    conn.execute(
        "INSERT INTO fund_bot_daily_snapshots "
        "(bot_id, trade_date, run_id, initial_capital, cash, invested_value, total_value, "
        " net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct, "
        " equity_weight, bond_weight, gold_weight, cash_weight, holdings_json) "
        "VALUES (?, '2026-01-06', 'runNew', 1000000, 600000, 400000, 1000000, 1.0, 0, 0, 0, "
        " 0.4, 0, 0, 0.6, '[]')",
        (bot_id,),
    )
    conn.commit()
    conn.close()


def test_get_my_trades_excludes_phantom_settled_only_order(reload_server, tmp_db):
    """runOld 下的、runNew settle 的 sell 单不能出现在 runNew 的 my_trades 输出。"""
    _seed_phantom_order(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_trades("botPhantom", "2026-01-07", run_id="runNew"))
    data = json.loads(payload)
    assert data["success"], data
    codes = [o["fund_code"] for o in data["orders"]]
    assert "510300" not in codes, f"runNew 不应看到 runOld 下的幽灵 510300 单, 实际: {codes}"
    summary = data["summary"]
    assert summary["sell_count"] == 0, f"runNew 没下过 sell, 实际 sell_count={summary['sell_count']}"
    assert summary["total_sell_proceeds"] == 0.0, summary


def test_get_my_performance_excludes_phantom_in_trades_summary(reload_server, tmp_db):
    """trades_summary 不能把 runOld 下、runNew settle 的 sell 单算进 runNew 的卖出统计。"""
    _seed_phantom_order(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_performance("botPhantom", "2026-01-07", run_id="runNew"))
    data = json.loads(payload)
    assert data["success"], data
    ts = data["trades_summary"]
    assert ts is not None, "trades_summary 不应为 None"
    assert ts["sell_count"] == 0, f"runNew 实际没 sell, 但 trades_summary.sell_count={ts['sell_count']}"
    assert ts["total_sell_proceeds"] == 0.0, ts


def test_get_my_history_excludes_phantom_settled_only_order(reload_server, tmp_db):
    """get_my_history 的 orders 列表也按 order_run_id 过滤，不带 settle_run_id 漏入。"""
    _seed_phantom_order(tmp_db)
    s = reload_server
    payload = asyncio.run(s.portfolio_get_my_history("botPhantom", run_id="runNew"))
    data = json.loads(payload)
    assert data["success"], data
    codes = [o["fund_code"] for o in data["orders"]]
    assert "510300" not in codes, f"runNew history 不应看到幽灵 510300 单, 实际: {codes}"
