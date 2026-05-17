"""runtime per-run 隔离测试（不是 schema 迁移测试）。

覆盖：
- 严格层 portfolio_get_my_history / portfolio_get_my_performance /
  portfolio_get_my_trades：空 run_id 必须返错；非空 run_id 必须只看本 run 行。
- 松散层 get_fund_holdings / get_fund_curve / get_fund_position_snapshots /
  get_fund_review_history：空 run_id 跨 run 全量；非空只看该 run。

种子数据：同 bot 两个 run（runA / runB），各 1 行 active 持仓 + 1 行已结算 BUY 单。
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
