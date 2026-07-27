"""Allocation Charter（配置宪章）机制测试。

覆盖：表结构、declare/amend 工具、卫星复评工具、买单结构闸门矩阵。
fixture 模式与 test_run_id_isolation.py 一致：tmp_db + reload_server。
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
    import server
    importlib.reload(server)
    return server


def test_charter_tables_exist(tmp_db):
    conn = sqlite3.connect(tmp_db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fund_bot_charters" in names
    assert "fund_bot_satellite_reviews" in names
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fund_bot_charters)")}
    assert {"bot_id", "run_id", "declared_date", "core_fund_codes",
            "single_fund_max_ratio", "satellite_min_ratio",
            "min_equity_threshold", "satellite_review_cadence_days",
            "status", "reason"} <= cols
    conn.close()


def _seed_nav_days(db_path, dates, code="000051", nav=1.0):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO fund_info (fund_code, fund_name) VALUES (?, ?)",
                 (code, "华夏沪深300ETF联接A"))
    for d in dates:
        conn.execute("INSERT OR IGNORE INTO fund_nav (fund_code, nav_date, nav) VALUES (?, ?, ?)",
                     (code, d, nav))
    conn.commit(); conn.close()


def _seed_account(db_path, bot_id="bot105d", cash=1_000_000.0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, cash_in_transit, run_id) "
        "VALUES (?, ?, ?, 0, 'runT')", (bot_id, cash, cash))
    conn.commit(); conn.close()


VALID_CHARTER = {
    "core_fund_codes": ["000051"],
    "single_fund_max_ratio": 0.60,
    "satellite_min_ratio": 0.25,
    "min_equity_threshold": 0.30,
    "satellite_review_cadence_days": 5,
}


def test_declare_charter_ok(reload_server, tmp_db):
    _seed_nav_days(tmp_db, ["2025-01-02"])
    _seed_account(tmp_db)
    r = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(VALID_CHARTER),
        trade_date="2025-01-02", reason="按 METHODOLOGY 核心60+卫星25 架构声明", run_id="runT")))
    assert r["success"] is True
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT status, satellite_min_ratio FROM fund_bot_charters "
                       "WHERE bot_id='bot105d' AND run_id='runT'").fetchone()
    assert row == ("active", 0.25)


def test_declare_charter_rejects_class_bounds(reload_server, tmp_db):
    _seed_nav_days(tmp_db, ["2025-01-02"])
    _seed_account(tmp_db)
    bad = dict(VALID_CHARTER, satellite_min_ratio=0.05)   # < 0.15 类别底线
    r = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(bad),
        trade_date="2025-01-02", reason="x", run_id="runT")))
    assert r["success"] is False
    assert "satellite_min_ratio" in r["message"]


def test_declare_charter_rejects_non_integer_cadence(reload_server, tmp_db):
    """cadence=5.7 被拒，cadence=5.0 可成功。"""
    _seed_nav_days(tmp_db, ["2025-01-02"])
    _seed_account(tmp_db)
    # 非整数浮点应被拒
    bad_cadence = dict(VALID_CHARTER, satellite_review_cadence_days=5.7)
    r = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(bad_cadence),
        trade_date="2025-01-02", reason="x", run_id="runT")))
    assert r["success"] is False
    assert "satellite_review_cadence_days" in r["message"]

    # 整数值的浮点应可成功
    good_cadence = dict(VALID_CHARTER, satellite_review_cadence_days=5.0)
    r = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(good_cadence),
        trade_date="2025-01-02", reason="整数浮点声明", run_id="runT")))
    assert r["success"] is True


def test_amend_charter_cooldown(reload_server, tmp_db):
    # 22 个交易日：01-02 声明；第 10 个交易日修订被拒；第 21 个交易日修订成功
    days = [f"2025-01-{d:02d}" for d in range(2, 24)]     # 22 天连续当交易日用
    _seed_nav_days(tmp_db, days)
    _seed_account(tmp_db)
    ok = asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(VALID_CHARTER),
        trade_date="2025-01-02", reason="init", run_id="runT"))
    assert json.loads(ok)["success"]
    amended = dict(VALID_CHARTER, satellite_min_ratio=0.20)
    r1 = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(amended),
        trade_date="2025-01-12", reason="降低卫星下限", run_id="runT")))
    assert r1["success"] is False and "冷却" in r1["message"]
    r2 = json.loads(asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(amended),
        trade_date="2025-01-23", reason="降低卫星下限", run_id="runT")))
    assert r2["success"] is True
    conn = sqlite3.connect(tmp_db)
    n_superseded = conn.execute("SELECT COUNT(*) FROM fund_bot_charters "
                                "WHERE status='superseded'").fetchone()[0]
    assert n_superseded == 1
