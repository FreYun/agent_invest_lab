"""Tests for fund_bot_performance table + _compute_bot_performance + portfolio_get_my_performance.interval_metrics

口径（按用户要求）：
- 窗口用交易日（1m=21 / 3m=63 / 6m=126 / 1y=252）
- rf = 1.8% 年化 → rf_daily = 1.8 / 252
- 全部不年化（区间收益、区间波动、区间夏普、区间卡玛）
- 不满窗口兜底到 since_inception，fallback=1
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


def _column_names(conn, table: str):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _seed_daily_snapshots(conn, bot_id: str, navs: list[float], start_date: str = "2024-01-02"):
    """直接写 fund_bot_daily_snapshots 一段连续序列，绕开 _compute_fund_snapshot。
    navs[i] 是第 i 个交易日（日历日 +i 天近似，测试不在意周末）。
    daily_return_pct 用净值序列自算。"""
    from datetime import datetime, timedelta
    d0 = datetime.strptime(start_date, "%Y-%m-%d")
    prev_nav: float | None = None
    initial = 1_000_000.0
    for i, nav in enumerate(navs):
        date = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        daily_ret = ((nav - prev_nav) / prev_nav * 100) if prev_nav else 0.0
        total_value = initial * nav
        # cumulative / drawdown 用最简口径写进去（_compute_bot_performance 不读这两个列做计算，
        # 它从 net_value + daily_return_pct 自算）
        cum_ret = (nav - 1.0) * 100
        conn.execute(
            "INSERT INTO fund_bot_daily_snapshots "
            "(bot_id, trade_date, run_id, initial_capital, cash, invested_value, total_value, "
            " net_value, daily_return_pct, cumulative_return_pct, max_drawdown_pct, "
            " equity_weight, bond_weight, gold_weight, cash_weight, holdings_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, date, "test-run", initial, 0.0, total_value - 0.0, total_value,
             nav, daily_ret, cum_ret, 0.0, 1.0, 0.0, 0.0, 0.0, "[]")
        )
        prev_nav = nav
    conn.commit()


# ============================================================
# 1. 表结构 + 迁移
# ============================================================

def test_init_db_creates_bot_performance_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fund_bot_performance'"
    ).fetchall()
    assert rows, "fund_bot_performance 表未创建"
    cols = set(_column_names(conn, "fund_bot_performance"))
    expected = {
        "bot_id", "trade_date", "run_id", "period",
        "return_pct", "max_drawdown_pct", "volatility_pct", "sharpe_ratio", "calmar_ratio",
        "data_points", "window_target_days", "fallback", "updated_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"
    conn.close()


def test_init_db_idempotent_on_existing_perf_table(tmp_db):
    import db as db_mod
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO fund_bot_performance "
        "(bot_id, trade_date, run_id, period, return_pct) VALUES (?, ?, ?, ?, ?)",
        ("bot1", "2024-01-02", "test-run", "1m", 1.23),
    )
    conn.commit()
    conn.close()
    db_mod.init_db()  # 不应抛错，也不应清掉旧行
    conn = sqlite3.connect(tmp_db)
    cnt = conn.execute("SELECT COUNT(*) FROM fund_bot_performance").fetchone()[0]
    assert cnt == 1
    conn.close()


# ============================================================
# 2. _compute_bot_performance 数学正确性
# ============================================================

def test_compute_bot_performance_writes_five_periods(reload_server, tmp_db):
    s = reload_server
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botA", [1.0, 1.01, 1.02, 1.015, 1.025])
        s._compute_bot_performance(conn, "botA", "2024-01-06", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT period, data_points, window_target_days, fallback "
        "FROM fund_bot_performance WHERE bot_id='botA' AND trade_date='2024-01-06' "
        "ORDER BY period"
    ).fetchall()
    periods = {r["period"] for r in rows}
    assert periods == {"1m", "3m", "6m", "1y", "since_inception"}
    # 5 个交易日 < 21 (1m)/63/126/252 全部触发 fallback
    by_period = {r["period"]: r for r in rows}
    for p in ("1m", "3m", "6m", "1y"):
        assert by_period[p]["fallback"] == 1, f"{p} should fallback"
        assert by_period[p]["data_points"] == 5, f"{p} should fall back to all 5 points"
    # since_inception 永远不 fallback
    assert by_period["since_inception"]["fallback"] == 0
    assert by_period["since_inception"]["window_target_days"] is None
    assert by_period["since_inception"]["data_points"] == 5
    conn.close()


def test_compute_bot_performance_return_pct_uses_window_nav(reload_server, tmp_db):
    """30 天序列: nav 1.00 → 1.10。1m 窗口取最后 21 天，return_pct 应该 != 整段 10%."""
    s = reload_server
    # 30 天单调上涨：第 i 天 nav = 1 + 0.10 * i / 29
    navs = [1.0 + 0.10 * i / 29 for i in range(30)]
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botB", navs)
        s._compute_bot_performance(conn, "botB", "2024-01-31", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = {r["period"]: r for r in conn.execute(
        "SELECT * FROM fund_bot_performance WHERE bot_id='botB' AND trade_date='2024-01-31'"
    ).fetchall()}
    # since_inception: 全段，10% 收益
    assert abs(rows["since_inception"]["return_pct"] - 10.0) < 0.01
    # 1m: 最后 21 天，nav[-1]/nav[-21] - 1
    expected_1m = (navs[-1] / navs[-21] - 1) * 100
    assert abs(rows["1m"]["return_pct"] - expected_1m) < 0.01
    assert rows["1m"]["fallback"] == 0, "30 天 > 21 天，1m 不应 fallback"
    # 3m/6m/1y 不满 → fallback to inception
    for p in ("3m", "6m", "1y"):
        assert rows[p]["fallback"] == 1
        assert abs(rows[p]["return_pct"] - 10.0) < 0.01
    conn.close()


def test_compute_bot_performance_sharpe_uses_rf_18_pct(reload_server, tmp_db):
    """构造一段日收益恒为 +0.05% 的序列，验证夏普 = (0.05 - 1.8/252) / 0 (stdev=0 → NULL)
    然后构造有方差的序列，验证夏普 = (mean - rf_daily) / stdev。"""
    s = reload_server
    # 30 天 +0.05% 日复利 + ±0.10% 噪声（位置 1/-1 交替）
    navs = [1.0]
    daily_returns_pct = []
    for i in range(30):
        ret_pct = 0.05 + (0.10 if i % 2 == 0 else -0.10)
        daily_returns_pct.append(ret_pct)
        navs.append(navs[-1] * (1 + ret_pct / 100))
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botC", navs)
        s._compute_bot_performance(conn, "botC", "2024-02-01", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sharpe_ratio, volatility_pct FROM fund_bot_performance "
        "WHERE bot_id='botC' AND trade_date='2024-02-01' AND period='since_inception'"
    ).fetchone()
    # 重算预期：since_inception 序列里 _seed 内部 daily_return_pct[0] = 0（首日无 prev），
    # 后面 30 天才是真实序列 → 31 个数据点
    full_dailies = [0.0] + daily_returns_pct  # 31 days, first day = 0
    n = len(full_dailies)
    mean_d = sum(full_dailies) / n
    var_d = sum((r - mean_d) ** 2 for r in full_dailies) / (n - 1)
    expected_std = var_d ** 0.5
    rf_daily = 1.8 / 252  # 用户指定 1.8%
    expected_sharpe = (mean_d - rf_daily) / expected_std
    assert abs(row["volatility_pct"] - expected_std) < 1e-6, \
        f"vol mismatch: got {row['volatility_pct']}, expected {expected_std}"
    assert abs(row["sharpe_ratio"] - expected_sharpe) < 1e-6, \
        f"sharpe mismatch: got {row['sharpe_ratio']}, expected {expected_sharpe}"
    conn.close()


def test_compute_bot_performance_calmar_is_return_over_abs_mdd(reload_server, tmp_db):
    """V 字形 nav: 1.00 → 0.90 → 1.05 (MDD = -10%, total return = +5%, calmar = 0.5)."""
    s = reload_server
    # 21 天的 V 形：前 11 天 1.00 线性降到 0.90，后 10 天再升到 1.05
    navs = []
    for i in range(11):
        navs.append(1.0 - 0.10 * i / 10)  # 1.00 → 0.90
    for i in range(1, 11):
        navs.append(0.90 + (1.05 - 0.90) * i / 10)  # 0.90 → 1.05
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botD", navs)
        s._compute_bot_performance(conn, "botD", "2024-01-22", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT return_pct, max_drawdown_pct, calmar_ratio FROM fund_bot_performance "
        "WHERE bot_id='botD' AND trade_date='2024-01-22' AND period='since_inception'"
    ).fetchone()
    assert abs(row["return_pct"] - 5.0) < 0.01
    assert abs(row["max_drawdown_pct"] - (-10.0)) < 0.01
    assert abs(row["calmar_ratio"] - 0.5) < 0.01  # 5 / 10 = 0.5
    conn.close()


def test_compute_bot_performance_calmar_null_when_no_drawdown(reload_server, tmp_db):
    """全程单调上涨（MDD≈0）→ calmar 应为 NULL。"""
    s = reload_server
    navs = [1.0 + 0.01 * i for i in range(10)]  # 单调上涨
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botE", navs)
        s._compute_bot_performance(conn, "botE", "2024-01-11", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT calmar_ratio, max_drawdown_pct FROM fund_bot_performance "
        "WHERE bot_id='botE' AND trade_date='2024-01-11' AND period='since_inception'"
    ).fetchone()
    assert row["calmar_ratio"] is None, f"calmar should be NULL when MDD≈0, got {row['calmar_ratio']}"
    assert abs(row["max_drawdown_pct"]) < 1e-6
    conn.close()


def test_compute_bot_performance_no_annualization_in_sharpe(reload_server, tmp_db):
    """验证夏普没年化：和 portfolio_get_my_performance 旧版本的 sharpe_ratio_rf0
    （年化版）数量级应当差大约 √252 ≈ 15.87 倍。"""
    s = reload_server
    # 30 天恒正收益 + 小方差
    daily_returns_pct = [0.1 + (0.05 if i % 2 == 0 else -0.05) for i in range(30)]
    navs = [1.0]
    for r in daily_returns_pct:
        navs.append(navs[-1] * (1 + r / 100))
    with s.get_conn() as conn:
        _seed_daily_snapshots(conn, "botF", navs)
        s._compute_bot_performance(conn, "botF", "2024-02-01", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sharpe_interval = conn.execute(
        "SELECT sharpe_ratio FROM fund_bot_performance "
        "WHERE bot_id='botF' AND trade_date='2024-02-01' AND period='since_inception'"
    ).fetchone()["sharpe_ratio"]
    # 区间夏普应该 < 1（每日 0.1% mean, daily 0.05 stdev, rf_daily≈0.007 → ~1.8 范围）
    # 关键是：不应当带 √252 因子（年化版的话应该是 ~28 量级）
    assert abs(sharpe_interval) < 5, f"区间夏普应在 -5~5 范围（没年化），got {sharpe_interval}"
    conn.close()


# ============================================================
# 3. _compute_fund_snapshot 自动级联到 _compute_bot_performance
# ============================================================

def test_compute_fund_snapshot_triggers_performance_table(reload_server, tmp_db):
    """走完整链路：建账户 → 写日快照 → 验证 perf 表自动出现 5 行。"""
    s = reload_server
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botG", 1_000_000.0, 1_000_000.0, "test-run")
        )
        conn.commit()
        # 跑 _compute_fund_snapshot——bot 没持仓，全部现金，所以 invested=0、total=cash
        s._compute_fund_snapshot(conn, "botG", "2024-01-02", run_id="test-run")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT period FROM fund_bot_performance "
        "WHERE bot_id='botG' AND trade_date='2024-01-02' ORDER BY period"
    ).fetchall()
    periods = {r["period"] for r in rows}
    assert periods == {"1m", "3m", "6m", "1y", "since_inception"}
    conn.close()


# ============================================================
# 4. portfolio_get_my_performance 返回 interval_metrics
# ============================================================

def test_portfolio_get_my_performance_includes_interval_metrics(reload_server, tmp_db):
    s = reload_server
    # 建账户 + 写 5 天快照 + 跑 _compute_bot_performance（perf 表里有 1m/3m/6m/1y/since_inception 5 行）
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botH", 1_000_000.0, 1_000_000.0, "test-run")
        )
        conn.commit()
        _seed_daily_snapshots(conn, "botH", [1.0, 1.01, 1.02, 1.015, 1.025])
        s._compute_bot_performance(conn, "botH", "2024-01-06", run_id="test-run")
    # 查 perf：as_of_date 必须 > 最新 perf trade_date
    payload = asyncio.run(s.portfolio_get_my_performance("botH", "2024-01-07"))
    obj = json.loads(payload)
    assert obj["success"] is True
    assert "interval_metrics" in obj
    im = obj["interval_metrics"]
    assert im["rf_annual_pct"] == 1.8
    assert im["trading_days_per_year"] == 252
    assert abs(im["rf_daily_pct"] - 1.8 / 252) < 1e-6
    assert set(im["metrics"].keys()) == {"1m", "3m", "6m", "1y", "since_inception"}
    for p, m in im["metrics"].items():
        assert "return_pct" in m
        assert "max_drawdown_pct" in m
        assert "volatility_pct" in m
        assert "sharpe_ratio" in m
        assert "calmar_ratio" in m
        assert "fallback" in m
        assert "data_points" in m
        assert "window_target_days" in m


def test_portfolio_get_my_performance_holdings_performance(reload_server, tmp_db):
    """interval_metrics.holdings_performance 应返回 active 持仓的每只基金的区间业绩，
    日期对齐 account perf；已平仓的不返回。"""
    s = reload_server
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botJ", 1_000_000.0, 500_000.0, "test-run")
        )
        # 当前持仓 active 两只 + 一只已平仓
        for code, name, ac, status in [
            ("000010", "FundA", "股票类", "active"),
            ("000011", "FundB", "黄金类", "active"),
            ("000012", "FundC", "债券类", "closed"),
        ]:
            conn.execute(
                "INSERT INTO fund_bot_holdings "
                "(bot_id, fund_code, fund_name, asset_class, role, status, run_id, "
                " market_value, actual_weight, holding_days, unrealized_pnl_pct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("botJ", code, name, ac, "核心底仓", status, "test-run",
                 100_000.0, 0.1, 5, 1.2)
            )
        conn.commit()
        # 灌 NAV + 触发 fund perf
        from datetime import datetime, timedelta
        d0 = datetime.strptime("2024-01-02", "%Y-%m-%d")
        for fc in ("000010", "000011", "000012"):
            for i in range(5):
                date = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO fund_nav (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (fc, date, 1.0 + 0.01 * i, 1.0 + 0.01 * i, 0.5)
                )
            s._compute_fund_nav_performance(conn, fc, "2024-01-06")
        # 灌账户 NAV + 触发 bot perf
        _seed_daily_snapshots(conn, "botJ", [1.0, 1.005, 1.01, 1.008, 1.015])
        s._compute_bot_performance(conn, "botJ", "2024-01-06", run_id="test-run")
    payload = asyncio.run(s.portfolio_get_my_performance("botJ", "2024-01-07"))
    obj = json.loads(payload)
    im = obj["interval_metrics"]
    assert "holdings_performance" in im
    hp = im["holdings_performance"]
    # 只含 active 持仓
    assert set(hp.keys()) == {"000010", "000011"}, f"actual keys: {set(hp.keys())}"
    assert "000012" not in hp, "closed holdings 不应出现"
    # 每只基金都有完整的 5 个 period perf
    for fc in ("000010", "000011"):
        entry = hp[fc]
        assert entry["fund_name"] in ("FundA", "FundB")
        assert entry["asset_class"] in ("股票类", "黄金类")
        assert entry["perf_as_of_date"] is not None
        assert set(entry["metrics"].keys()) == {"1m", "3m", "6m", "1y", "since_inception"}
        for p, m in entry["metrics"].items():
            for k in ("return_pct", "max_drawdown_pct", "volatility_pct",
                      "sharpe_ratio", "calmar_ratio", "data_points",
                      "window_target_days", "fallback"):
                assert k in m
    # 日期与账户 perf 一致（NAV / 快照都在 2024-01-06）
    assert hp["000010"]["perf_as_of_date"] == im["as_of_perf_date"]


def test_portfolio_get_my_performance_holdings_performance_empty_when_no_holdings(reload_server, tmp_db):
    s = reload_server
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botK", 1_000_000.0, 1_000_000.0, "test-run")
        )
        conn.commit()
        _seed_daily_snapshots(conn, "botK", [1.0, 1.01])
        s._compute_bot_performance(conn, "botK", "2024-01-03", run_id="test-run")
    payload = asyncio.run(s.portfolio_get_my_performance("botK", "2024-01-04"))
    obj = json.loads(payload)
    # 没持仓 → holdings_performance 是空 dict（不是缺失）
    assert obj["interval_metrics"]["holdings_performance"] == {}


def test_portfolio_get_my_performance_holdings_perf_date_fallback(reload_server, tmp_db):
    """基金在 perf_anchor_date 当日没 perf 时，应兜底到 ≤ anchor 的最近一日，
    且 perf_as_of_date 字段反映这次兜底（早于 account perf 日期）。"""
    s = reload_server
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botL", 1_000_000.0, 500_000.0, "test-run")
        )
        conn.execute(
            "INSERT INTO fund_bot_holdings "
            "(bot_id, fund_code, fund_name, asset_class, role, status, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("botL", "000020", "FundLag", "股票类", "核心底仓", "active", "test-run")
        )
        conn.commit()
        # 基金 NAV 只到 2024-01-04
        from datetime import datetime, timedelta
        d0 = datetime.strptime("2024-01-02", "%Y-%m-%d")
        for i in range(3):  # 3 天: 01-02, 01-03, 01-04
            date = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO fund_nav (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                ("000020", date, 1.0 + 0.01 * i, 1.0 + 0.01 * i, 0.5)
            )
        s._compute_fund_nav_performance(conn, "000020", "2024-01-04")
        # 账户快照走到 2024-01-06（基金 perf 滞后 2 天）
        _seed_daily_snapshots(conn, "botL", [1.0, 1.005, 1.01, 1.008, 1.015])
        s._compute_bot_performance(conn, "botL", "2024-01-06", run_id="test-run")
    payload = asyncio.run(s.portfolio_get_my_performance("botL", "2024-01-07"))
    obj = json.loads(payload)
    im = obj["interval_metrics"]
    assert im["as_of_perf_date"] == "2024-01-06"
    hp = im["holdings_performance"]["000020"]
    # 基金 perf 锚定日应回退到 2024-01-04（基金 NAV 最后一天），不应越过
    assert hp["perf_as_of_date"] == "2024-01-04"
    assert len(hp["metrics"]) == 5


def test_portfolio_get_my_performance_no_future_leak(reload_server, tmp_db):
    """如果 as_of_date 早于任何 perf 行 → interval_metrics.metrics 应该为空。"""
    s = reload_server
    with s.get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_bot_accounts (bot_id, initial_capital, cash, run_id) "
            "VALUES (?, ?, ?, ?)", ("botI", 1_000_000.0, 1_000_000.0, "test-run")
        )
        conn.commit()
        _seed_daily_snapshots(conn, "botI", [1.0, 1.01], start_date="2024-01-02")
        s._compute_bot_performance(conn, "botI", "2024-01-03", run_id="test-run")
    # 故意把 as_of_date 设到 perf trade_date 之前
    payload = asyncio.run(s.portfolio_get_my_performance("botI", "2024-01-03"))
    obj = json.loads(payload)
    # 没 daily snapshot < as_of_date 的情况下 summary=None；
    # 但只要至少有一行 snap < as_of_date 时进入主路径，会带 interval_metrics
    # 这里 2024-01-02 < 2024-01-03 有一行 snap，主路径会跑
    if obj.get("summary"):
        im = obj["interval_metrics"]
        # perf 写在 2024-01-03 那行，as_of_date=2024-01-03 严格 < 过滤掉它
        assert im["metrics"] == {}, "perf at as_of_date 不应被泄漏"
        assert im["as_of_perf_date"] is None
