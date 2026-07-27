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


def _seed_holding_actions(db_path, bot_id, run_id, orders):
    """orders: [(fund_code, fund_name, action_type, amount, shares, nav, date)] 直接写 actions 表，
    让 _replay_fund_account_state 能回放出持仓。"""
    conn = sqlite3.connect(db_path)
    for code, name, at, amount, shares, nav, d in orders:
        conn.execute("INSERT OR IGNORE INTO fund_info (fund_code, fund_name) VALUES (?, ?)", (code, name))
        conn.execute(
            "INSERT INTO fund_bot_actions (bot_id, fund_code, action_type, amount, shares, "
            " nav_used, fee, action_date, run_id) VALUES (?,?,?,?,?,?,0,?,?)",
            (bot_id, code, at, amount, shares, nav, d, run_id))
    conn.commit(); conn.close()


VALID_REVIEW = {
    "mainline_thesis": "通信设备主线受算力资本开支支撑，机器人板块跟随政策催化，维持双卫星结构。",
    "holdings": [
        {"fund_code": "007818", "verdict": "keep", "rationale": "通信设备近3月+0.98%跑赢主题均值，主线未破位"},
        {"fund_code": "014881", "verdict": "keep", "rationale": "机器人政策催化在途，浮盈+1.69%，持有成本低"},
    ],
    "candidates": [
        {"fund_code": "018135", "comparison": "大数据主题近1月弱于通信设备2.1pp，暂不切换"},
        {"fund_code": "001617", "comparison": "电子主题波动更大且与通信设备相关性高，不增强分散"},
    ],
}


def _setup_review_env(server, db_path):
    days = ["2025-01-02", "2025-01-03"]
    for code in ("000051", "007818", "014881", "018135", "001617"):
        _seed_nav_days(db_path, days, code=code)
    _seed_account(db_path)
    _seed_holding_actions(db_path, "bot105d", "runT", [
        ("000051", "华夏沪深300ETF联接A", "BUY", 400000, 400000, 1.0, "2025-01-02"),
        ("007818", "国泰通信设备C", "BUY", 60000, 60000, 1.0, "2025-01-02"),
        ("014881", "天弘机器人C", "BUY", 50000, 50000, 1.0, "2025-01-02"),
    ])
    ok = asyncio.run(server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(VALID_CHARTER),
        trade_date="2025-01-02", reason="init", run_id="runT"))
    assert json.loads(ok)["success"]


def test_submit_satellite_review_ok(reload_server, tmp_db):
    _setup_review_env(reload_server, tmp_db)
    r = json.loads(asyncio.run(reload_server.portfolio_submit_satellite_review(
        bot_id="bot105d", review_json=json.dumps(VALID_REVIEW),
        trade_date="2025-01-03", run_id="runT")))
    assert r["success"] is True
    conn = sqlite3.connect(tmp_db)
    n = conn.execute("SELECT COUNT(*) FROM fund_bot_satellite_reviews").fetchone()[0]
    assert n == 1


def test_submit_review_rejects_missing_holding(reload_server, tmp_db):
    _setup_review_env(reload_server, tmp_db)
    partial = dict(VALID_REVIEW, holdings=[VALID_REVIEW["holdings"][0]])  # 漏掉 014881
    r = json.loads(asyncio.run(reload_server.portfolio_submit_satellite_review(
        bot_id="bot105d", review_json=json.dumps(partial),
        trade_date="2025-01-03", run_id="runT")))
    assert r["success"] is False and "014881" in r["message"]


def test_submit_review_rejects_few_candidates(reload_server, tmp_db):
    _setup_review_env(reload_server, tmp_db)
    bad = dict(VALID_REVIEW, candidates=[VALID_REVIEW["candidates"][0]])  # 只有 1 个候选
    r = json.loads(asyncio.run(reload_server.portfolio_submit_satellite_review(
        bot_id="bot105d", review_json=json.dumps(bad),
        trade_date="2025-01-03", run_id="runT")))
    assert r["success"] is False and "候选" in r["message"]


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


# ── Task 4: 买单结构闸门测试矩阵 ──────────────────────────────────────────────

def _buy(server, code, amount, date="2025-01-03", bot="bot105d", run="runT"):
    return json.loads(asyncio.run(server.portfolio_place_buy_order(
        bot_id=bot, fund_code=code, amount=amount, trade_date=date,
        reason="test buy", run_id=run)))


def test_gate_no_charter_row_passes(reload_server, tmp_db):
    """表中无任何行（宪章未启用）→ 行为不变，买单照常受理。"""
    _seed_nav_days(tmp_db, ["2025-01-02", "2025-01-03"])
    _seed_account(tmp_db)
    r = _buy(reload_server, "000051", 100000)
    assert r["success"] is True


def test_gate_required_undeclared_rejects(reload_server, tmp_db):
    """world 已写 required 占位但 bot 未声明 → 拒买。"""
    _seed_nav_days(tmp_db, ["2025-01-02", "2025-01-03"])
    _seed_account(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO fund_bot_charters (bot_id, run_id, status) "
                 "VALUES ('bot105d', 'runT', 'required')")
    conn.commit(); conn.close()
    r = _buy(reload_server, "000051", 100000)
    assert r["success"] is False and "portfolio_declare_charter" in r["message"]


def test_gate_single_fund_cap(reload_server, tmp_db):
    """已持核心 40w+卫星 11w（核心占 78%>60%+5pp 容忍带），继续买核心 → 拒。"""
    _setup_review_env(reload_server, tmp_db)   # 核心40w 卫星11w，含宪章声明
    r = _buy(reload_server, "000051", 200000)
    assert r["success"] is False and "single_fund_max_ratio" in r["message"]


def test_gate_satellite_floor_blocks_core_buy(reload_server, tmp_db):
    """卫星占比 21.6%，但一笔大额核心买单会把卫星稀释到 <25%-5pp → 拒；买卫星放行。"""
    _setup_review_env(reload_server, tmp_db)
    r_sat = _buy(reload_server, "018135", 50000)
    assert r_sat["success"] is True            # 买卫星永远放行结构闸（仍受单基上限约束）
    r_core = _buy(reload_server, "000051", 100000)
    assert r_core["success"] is False and "satellite_min_ratio" in r_core["message"]


def test_gate_exemption_below_equity_threshold(reload_server, tmp_db):
    """权益/总资产 < min_equity_threshold（防御态）→ 结构闸豁免。"""
    days = ["2025-01-02", "2025-01-03"]
    for code in ("000051",):
        _seed_nav_days(tmp_db, days, code=code)
    _seed_account(tmp_db)   # 100w 现金
    _seed_holding_actions(tmp_db, "bot105d", "runT",
        [("000051", "华夏沪深300ETF联接A", "BUY", 100000, 100000, 1.0, "2025-01-02")])
    ok = asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(VALID_CHARTER),
        trade_date="2025-01-02", reason="init", run_id="runT"))
    assert json.loads(ok)["success"]
    # 权益 10w / 总资产 100w = 10% < 30% 豁免线 → 全核心也放行
    r = _buy(reload_server, "000051", 50000)
    assert r["success"] is True


def test_gate_review_overdue_blocks_core_buy(reload_server, tmp_db):
    """有卫星持仓且复评超期（cadence 5 + grace 2 个交易日无复评）→ 核心买单被拒。"""
    days = [f"2025-01-{d:02d}" for d in range(2, 16)]
    for code in ("000051", "007818", "014881", "018135", "001617"):
        _seed_nav_days(tmp_db, days, code=code)
    _seed_account(tmp_db)
    _seed_holding_actions(tmp_db, "bot105d", "runT", [
        ("000051", "华夏沪深300ETF联接A", "BUY", 300000, 300000, 1.0, "2025-01-02"),
        ("007818", "国泰通信设备C", "BUY", 100000, 100000, 1.0, "2025-01-02"),
        ("014881", "天弘机器人C", "BUY", 100000, 100000, 1.0, "2025-01-02"),
    ])
    ok = asyncio.run(reload_server.portfolio_declare_charter(
        bot_id="bot105d", charter_json=json.dumps(VALID_CHARTER),
        trade_date="2025-01-02", reason="init", run_id="runT"))
    assert json.loads(ok)["success"]
    # 声明日=01-02，8 个交易日后（>5+2）无复评 → 核心买拒、卫星买放行
    r_core = _buy(reload_server, "000051", 50000, date="2025-01-15")
    assert r_core["success"] is False and "复评" in r_core["message"]
    r_sat = _buy(reload_server, "018135", 30000, date="2025-01-15")
    assert r_sat["success"] is True
    # 提交复评后核心买恢复
    rv = asyncio.run(reload_server.portfolio_submit_satellite_review(
        bot_id="bot105d", review_json=json.dumps(VALID_REVIEW),
        trade_date="2025-01-15", run_id="runT"))
    assert json.loads(rv)["success"]
    r_core2 = _buy(reload_server, "000051", 50000, date="2025-01-15")
    assert r_core2["success"] is True
