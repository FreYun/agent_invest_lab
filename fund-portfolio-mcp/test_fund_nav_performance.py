"""Tests for fund_nav_performance table + _compute_fund_nav_performance + get_fund_perf.nav_intervals

口径与 fund_bot_performance 完全对齐：
- 窗口用交易日（1m=21 / 3m=63 / 6m=126 / 1y=252）
- rf = 1.8% 年化 → rf_daily = 1.8 / 252
- 区间口径，全部不年化
- 不满窗口兜底 since_inception，fallback=1
- 数据源：fund_nav.acc_nav（return / MDD）+ daily_return_pct（vol / sharpe）
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


def _column_names(conn, table: str):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _seed_fund_nav(conn, fund_code: str, acc_navs: list[float], start_date: str = "2024-01-02"):
    """直接写 fund_nav 一段连续序列。daily_return_pct 用 acc_nav 自算。"""
    from datetime import datetime, timedelta
    d0 = datetime.strptime(start_date, "%Y-%m-%d")
    prev: float | None = None
    for i, acc in enumerate(acc_navs):
        nav_date = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        daily = ((acc - prev) / prev * 100) if prev else 0.0
        conn.execute(
            "INSERT INTO fund_nav (fund_code, nav_date, nav, acc_nav, daily_return_pct, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (fund_code, nav_date, acc, acc, daily)
        )
        prev = acc
    conn.commit()


# ============================================================
# 1. 表结构 + 列与 bot 业绩表对齐
# ============================================================

def test_init_db_creates_fund_nav_performance_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fund_nav_performance'"
    ).fetchall()
    assert rows
    cols = set(_column_names(conn, "fund_nav_performance"))
    expected = {
        "fund_code", "trade_date", "period",
        "return_pct", "max_drawdown_pct", "volatility_pct", "sharpe_ratio", "calmar_ratio",
        "data_points", "window_target_days", "fallback", "updated_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"
    conn.close()


def test_fund_nav_perf_matches_bot_perf_metric_columns(tmp_db):
    """列结构（除主键差异外）应与 fund_bot_performance 完全对齐——这是用户的核心要求。"""
    conn = sqlite3.connect(tmp_db)
    bot_cols = set(_column_names(conn, "fund_bot_performance"))
    fund_cols = set(_column_names(conn, "fund_nav_performance"))
    metric_cols = {
        "period", "return_pct", "max_drawdown_pct", "volatility_pct",
        "sharpe_ratio", "calmar_ratio", "data_points", "window_target_days",
        "fallback", "updated_at",
    }
    assert metric_cols.issubset(bot_cols)
    assert metric_cols.issubset(fund_cols)
    # 唯一差异：bot 表有 bot_id + run_id；fund 表用 fund_code 替代，没 run_id
    bot_only = bot_cols - fund_cols
    fund_only = fund_cols - bot_cols
    assert bot_only == {"bot_id", "run_id"}, f"bot exclusive cols: {bot_only}"
    assert fund_only == {"fund_code"}, f"fund exclusive cols: {fund_only}"
    conn.close()


# ============================================================
# 2. _compute_fund_nav_performance 数学正确性
# ============================================================

def test_writes_five_periods_with_fallback_when_short(reload_server, tmp_db):
    s = reload_server
    with s.get_conn() as conn:
        _seed_fund_nav(conn, "000001", [1.0, 1.01, 1.02, 1.015, 1.025])
        s._compute_fund_nav_performance(conn, "000001", "2024-01-06")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT period, data_points, fallback FROM fund_nav_performance "
        "WHERE fund_code='000001' AND trade_date='2024-01-06' ORDER BY period"
    ).fetchall()
    periods = {r["period"] for r in rows}
    assert periods == {"1m", "3m", "6m", "1y", "since_inception"}
    by_period = {r["period"]: r for r in rows}
    # 5 个数据点 < 21（1m）/ 63 / 126 / 252 → 全部 fallback
    for p in ("1m", "3m", "6m", "1y"):
        assert by_period[p]["fallback"] == 1
        assert by_period[p]["data_points"] == 5
    assert by_period["since_inception"]["fallback"] == 0
    conn.close()


def test_return_uses_acc_nav_endpoints(reload_server, tmp_db):
    """30 天单调上涨，1m 窗口 return = acc_nav[-1]/acc_nav[-21] - 1，不应等于整段 10%。"""
    s = reload_server
    navs = [1.0 + 0.10 * i / 29 for i in range(30)]
    with s.get_conn() as conn:
        _seed_fund_nav(conn, "000002", navs)
        s._compute_fund_nav_performance(conn, "000002", "2024-01-31")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = {r["period"]: r for r in conn.execute(
        "SELECT * FROM fund_nav_performance WHERE fund_code='000002' AND trade_date='2024-01-31'"
    ).fetchall()}
    # since_inception: 整段 10%
    assert abs(rows["since_inception"]["return_pct"] - 10.0) < 0.01
    # 1m: 取最后 21 个 acc_nav 端点
    expected_1m = (navs[-1] / navs[-21] - 1) * 100
    assert abs(rows["1m"]["return_pct"] - expected_1m) < 0.01
    assert rows["1m"]["fallback"] == 0
    # 不满窗口的 3m/6m/1y → fallback to since_inception
    for p in ("3m", "6m", "1y"):
        assert rows[p]["fallback"] == 1
        assert abs(rows[p]["return_pct"] - 10.0) < 0.01
    conn.close()


def test_sharpe_uses_rf_18_pct_and_no_annualization(reload_server, tmp_db):
    s = reload_server
    # 30 天 +0.1% mean + 交替 ±0.05% 噪声
    daily_returns_pct = [0.1 + (0.05 if i % 2 == 0 else -0.05) for i in range(30)]
    acc_navs = [1.0]
    for r in daily_returns_pct:
        acc_navs.append(acc_navs[-1] * (1 + r / 100))
    with s.get_conn() as conn:
        _seed_fund_nav(conn, "000003", acc_navs)
        s._compute_fund_nav_performance(conn, "000003", "2024-02-01")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sharpe_ratio, volatility_pct FROM fund_nav_performance "
        "WHERE fund_code='000003' AND trade_date='2024-02-01' AND period='since_inception'"
    ).fetchone()
    # 重算：since_inception 序列 31 天，首日 daily_return = 0
    full_dailies = [0.0] + daily_returns_pct
    n = len(full_dailies)
    mean_d = sum(full_dailies) / n
    var_d = sum((r - mean_d) ** 2 for r in full_dailies) / (n - 1)
    expected_std = var_d ** 0.5
    rf_daily = 1.8 / 252
    expected_sharpe = (mean_d - rf_daily) / expected_std
    assert abs(row["volatility_pct"] - expected_std) < 1e-6
    assert abs(row["sharpe_ratio"] - expected_sharpe) < 1e-6
    # 没年化：区间夏普绝对值应该 < 5
    assert abs(row["sharpe_ratio"]) < 5
    conn.close()


def test_calmar_v_shape(reload_server, tmp_db):
    """V 形 acc_nav: 1.00 → 0.90 → 1.05；MDD = -10%, return = +5%, calmar = 0.5"""
    s = reload_server
    navs = [1.0 - 0.10 * i / 10 for i in range(11)]
    navs += [0.90 + (1.05 - 0.90) * i / 10 for i in range(1, 11)]
    with s.get_conn() as conn:
        _seed_fund_nav(conn, "000004", navs)
        s._compute_fund_nav_performance(conn, "000004", "2024-01-22")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT return_pct, max_drawdown_pct, calmar_ratio FROM fund_nav_performance "
        "WHERE fund_code='000004' AND trade_date='2024-01-22' AND period='since_inception'"
    ).fetchone()
    assert abs(row["return_pct"] - 5.0) < 0.01
    assert abs(row["max_drawdown_pct"] - (-10.0)) < 0.01
    assert abs(row["calmar_ratio"] - 0.5) < 0.01
    conn.close()


def test_calmar_null_when_no_drawdown(reload_server, tmp_db):
    s = reload_server
    navs = [1.0 + 0.01 * i for i in range(10)]  # 单调上涨
    with s.get_conn() as conn:
        _seed_fund_nav(conn, "000005", navs)
        s._compute_fund_nav_performance(conn, "000005", "2024-01-11")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT calmar_ratio FROM fund_nav_performance "
        "WHERE fund_code='000005' AND trade_date='2024-01-11' AND period='since_inception'"
    ).fetchone()
    assert row["calmar_ratio"] is None
    conn.close()


# ============================================================
# 3. upsert_fund_nav 自动级联触发 perf 刷新
# ============================================================

def test_upsert_fund_nav_triggers_perf_compute(reload_server, tmp_db):
    s = reload_server
    # 一次 upsert 5 天 NAV → 应自动落 5 行 fund_nav_performance（每个 period 一行）
    navs_json = json.dumps([
        {"fund_code": "000006", "nav_date": f"2024-01-{i+2:02d}",
         "nav": 1.0 + 0.01 * i, "acc_nav": 1.0 + 0.01 * i,
         "daily_return_pct": 0.0 if i == 0 else ((0.01 * i) / (0.01 * (i-1) + 1.0) * 100 if i > 0 else 0.0)}
        for i in range(5)
    ])
    asyncio.run(s.upsert_fund_nav(navs_json))
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT period FROM fund_nav_performance WHERE fund_code='000006' ORDER BY period"
    ).fetchall()
    assert {r["period"] for r in rows} == {"1m", "3m", "6m", "1y", "since_inception"}
    # 应只在批次最新日落 perf（不是 5 天每天都落）
    dates = conn.execute(
        "SELECT DISTINCT trade_date FROM fund_nav_performance WHERE fund_code='000006'"
    ).fetchall()
    assert len(dates) == 1, f"应该只在批次最新一天落 perf，实际落了 {[d['trade_date'] for d in dates]}"
    assert dates[0]["trade_date"] == "2024-01-06"
    conn.close()


def test_upsert_fund_nav_multi_fund_each_gets_perf(reload_server, tmp_db):
    s = reload_server
    # 一次 upsert 两只基金的 NAV
    navs_json = json.dumps([
        {"fund_code": "000007", "nav_date": "2024-01-02", "nav": 1.0, "acc_nav": 1.0, "daily_return_pct": 0},
        {"fund_code": "000007", "nav_date": "2024-01-03", "nav": 1.02, "acc_nav": 1.02, "daily_return_pct": 2.0},
        {"fund_code": "000008", "nav_date": "2024-01-02", "nav": 1.0, "acc_nav": 1.0, "daily_return_pct": 0},
        {"fund_code": "000008", "nav_date": "2024-01-03", "nav": 1.01, "acc_nav": 1.01, "daily_return_pct": 1.0},
    ])
    asyncio.run(s.upsert_fund_nav(navs_json))
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    fund_codes = conn.execute(
        "SELECT DISTINCT fund_code FROM fund_nav_performance ORDER BY fund_code"
    ).fetchall()
    assert [r["fund_code"] for r in fund_codes] == ["000007", "000008"]
    conn.close()


# ============================================================
# 4. get_fund_perf 暴露 nav_intervals
# ============================================================

def test_get_fund_perf_returns_nav_intervals(reload_server, tmp_db):
    s = reload_server
    navs_json = json.dumps([
        {"fund_code": "000009", "nav_date": f"2024-01-{i+2:02d}",
         "nav": 1.0 + 0.01 * i, "acc_nav": 1.0 + 0.01 * i,
         "daily_return_pct": 0.5}
        for i in range(5)
    ])
    asyncio.run(s.upsert_fund_nav(navs_json))
    payload = asyncio.run(s.get_fund_perf("000009"))
    obj = json.loads(payload)
    assert obj["success"] is True
    # 老 fund_performance 表空
    assert obj["intervals"] == []
    # 新 nav_intervals 应该有 5 行
    assert len(obj["nav_intervals"]) == 5
    periods = {r["period"] for r in obj["nav_intervals"]}
    assert periods == {"1m", "3m", "6m", "1y", "since_inception"}
    # 每行应有完整指标 + fallback bool
    for r in obj["nav_intervals"]:
        for k in ("return_pct", "max_drawdown_pct", "volatility_pct",
                  "sharpe_ratio", "calmar_ratio", "data_points",
                  "window_target_days", "fallback"):
            assert k in r, f"{r['period']} missing {k}"
        assert isinstance(r["fallback"], bool)
    # meta
    assert obj["nav_perf_meta"]["rf_annual_pct"] == 1
    assert obj["nav_perf_meta"]["trading_days_per_year"] == 252
    assert obj["nav_perf_meta"]["windows_trading_days"]["1m"] == 21


def test_get_fund_perf_fails_when_truly_empty(reload_server, tmp_db):
    s = reload_server
    payload = asyncio.run(s.get_fund_perf("999999"))
    obj = json.loads(payload)
    assert obj["success"] is False
    assert "无绩效数据" in obj["message"]
