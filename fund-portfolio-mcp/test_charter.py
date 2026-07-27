"""Allocation Charter（配置宪章）机制测试。

覆盖：表结构、declare/amend 工具、卫星复评工具、买单结构闸门矩阵、cli 子命令。
fixture 模式与 test_run_id_isolation.py 一致：tmp_db + reload_server。
"""
import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
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
    """核心大额买单稀释卫星破下限 → 拒；当日 pending 卫星买单计入结构（修结构闭环）。

    环境（来自 _setup_review_env）：宪章 cap=60%、floor=25%、容忍带 ±5%（有效 cap 65%、有效 floor 20%）；
    已成交持仓核心 000051=40w、卫星合计 11w（007818=6w+014881=5w），nav 全 1.0，账户 100w 现金。

    数值推导：
      步骤1 直接买核心 10w：
        pending 买单：{}（无）；mv={000051:50w, sats:11w}；equity=61w
        cap: 50w/61w=82.0% > 65% → 上限违反
        floor: 11w/61w=18.0% < 20% → 下限违反
        两者均触发 → 拒单，message 含 satellite_min_ratio（也含 single_fund_max_ratio）

      步骤2 买卫星 018135 5w：
        核心未 pending（步骤1 被拒未写单）；mv={000051:40w,007818:6w,014881:5w,018135:5w}；equity=56w
        018135 cap: 5w/56w=8.9% < 65% → 卫星买单放行（不检 floor）→ 成功

      步骤3 再买核心 10w（pending 卫星 5w 已计入）：
        pending 买单：{018135:5w}；replay mv + pending → {000051:40w+10w=50w,007818:6w,014881:5w,018135:5w}
        equity=66w；floor: 16w/66w=24.2% ≥ 20% → floor 修复
        cap: 50w/66w=75.8% > 65% → 仍超单基上限
        → 拒单，message 仅含 single_fund_max_ratio，不含 satellite_min_ratio
    """
    _setup_review_env(reload_server, tmp_db)
    # 步骤1：直接买核心 10w → 卫星 11w/61w=18.0% < 20%，同时 cap 82.0% > 65% → 拒（两条均报）
    r_core = _buy(reload_server, "000051", 100000)
    assert r_core["success"] is False and "satellite_min_ratio" in r_core["message"]
    # 步骤2：买卫星 5w → 结构闸不拦卫星，自身占比远低于单基上限 → 放行
    r_sat = _buy(reload_server, "018135", 50000)
    assert r_sat["success"] is True
    # 步骤3：再买核心 10w → pending 卫星计入后 floor 16w/66w=24.2% ≥ 20% 已修复；
    # 但核心 50w/66w=75.8% > 65% 仍超单基上限 → 拒且消息只含 cap 不含 floor
    r_core2 = _buy(reload_server, "000051", 100000)
    assert r_core2["success"] is False
    assert "single_fund_max_ratio" in r_core2["message"]
    assert "satellite_min_ratio" not in r_core2["message"]


def test_gate_pending_buy_counts_toward_cap(reload_server, tmp_db):
    """同日拆单不能绕过单基上限：pending 核心买单计入后后续买单被拒。

    环境：_setup_review_env 后账户 initial_capital=100w，replay 已扣成交 51w（000051=40w+sats=11w），
    replay cash=49w。为支撑累计 65w 买单（30w+10w+15w+20w 中前三笔成功共 55w）需 replay cash ≥ 55w，
    不足（49w），故在 _setup_review_env 后直接更新账户 initial_capital=150w
    （replay cash=150w-51w=99w，支撑前三笔 55w 后剩余 44w ≥ 第四笔 20w）。

    注意：server 用 (bot_id, run_id, fund_code, date, amount) 做 pending 重复检测；
    三笔核心买单金额须各不相同（10w/15w/20w），避免误判为重复单导致绕过闸门。

    数值推导（nav 全 1.0，有效 cap=65%，有效 floor=20%）：
      步骤1 买卫星 018135 30w（pending={}）：
        mv={000051:40w, 007818:6w, 014881:5w, 018135:30w}；equity=81w
        018135: 30w/81w=37.0% < 65%；卫星买 → 放行 ✓

      步骤2 第一笔核心 10w（pending={018135:30w}）：
        mv={000051:50w, sats:41w}；equity=91w
        cap: 50w/91w=54.9% < 65%；floor: 41w/91w=45.1% ≥ 20% → 放行 ✓
        旧口径（不计 pending 卫星）：sats=11w，floor=11w/61w=18.0% < 20% → 会被 floor 误拒
        ⟹ 步骤2 通过本身证明 pending 卫星已计入，floor 判断已修正。

      步骤3 第二笔核心 15w（pending={018135:30w, 000051:10w}）：
        mv={000051:65w, sats:41w}；equity=106w
        cap: 65w/106w=61.3% < 65% → 放行 ✓

      步骤4 第三笔核心 20w（pending={018135:30w, 000051:25w}）：
        mv={000051:85w, sats:41w}；equity=126w
        cap: 85w/126w=67.5% > 65% → 拒（single_fund_max_ratio）✓
        旧口径（不计 pending）：000051=40w+20w=60w，sats=11w，equity=71w，
        60w/71w=84.5%>65% → 旧口径也会拒，但 floor 检查会先触发（11w/71w=15.5%<20%）；
        此测试的核心价值在于步骤2/3 在新口径下能通过（旧口径会因 floor 先拒），
        再由步骤4 验证累积 pending 核心终止单基上限。
    """
    _setup_review_env(reload_server, tmp_db)
    # 调大 initial_capital 以支撑本测试累计 55w（前三笔）买单（见推导）
    import sqlite3 as _sqlite3
    conn2 = _sqlite3.connect(tmp_db)
    conn2.execute("UPDATE fund_bot_accounts SET initial_capital=1500000, cash=1500000 WHERE bot_id='bot105d'")
    conn2.commit(); conn2.close()
    # 步骤1：买卫星 30w → 卫星桶 41w，权益 81w，核心 49.4% < 65%，floor 50.6% ≥ 20% → 过
    assert _buy(reload_server, "018135", 300000)["success"] is True
    # 步骤2：第一笔核心 10w（pending 卫星 30w 计入）→ 核心 50w/91w=54.9% < 65%，floor 45.1% ≥ 20% → 过
    # （旧口径无 pending 卫星：floor=11w/61w=18%<20% 会拒，此步通过证明修复有效）
    assert _buy(reload_server, "000051", 100000)["success"] is True
    # 步骤3：第二笔核心 15w → pending 计入后核心 65w/106w=61.3% < 65% → 过
    assert _buy(reload_server, "000051", 150000)["success"] is True
    # 步骤4：第三笔核心 20w → pending 计入后核心 85w/126w=67.5% > 65% → 拒（拆单防绕过）
    r = _buy(reload_server, "000051", 200000)
    assert r["success"] is False and "single_fund_max_ratio" in r["message"]


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


# ── Task 5: cli_tools 宪章子命令测试 ─────────────────────────────────────────

def _cli(tmp_db, *args):
    env = dict(os.environ)
    env["FUND_DB_PATH"] = tmp_db
    cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_tools.py")
    out = subprocess.run(
        ["/usr/bin/python3.12", cli_path, *args],
        capture_output=True, text=True, env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cli_charter_require_and_status(reload_server, tmp_db):
    _seed_nav_days(tmp_db, ["2025-01-02", "2025-01-03"])
    _seed_account(tmp_db)
    r1 = _cli(tmp_db, "charter_require", "--bot-id", "bot105d", "--run-id", "runT")
    assert r1["ok"] is True and r1["existing"] is False
    r2 = _cli(tmp_db, "charter_require", "--bot-id", "bot105d", "--run-id", "runT")
    assert r2["existing"] is True     # 幂等
    s = _cli(tmp_db, "charter_status", "--bot-id", "bot105d", "--run-id", "runT",
             "--date", "2025-01-03")
    assert s["required"] is True and s["declared"] is False
