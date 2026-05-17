# fund-portfolio-mcp read-side run_id 隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 bot 通过 fund-portfolio-mcp 读自己的 holdings/orders/perf 时只看到本 run 的数据，旧 run 数据保留但对新 run 不可见。

**Architecture:** 三块改动 ——
1. `server.py` 严格层 3 个工具（portfolio_get_my_history/performance/trades）：加 `_require_run_id` + 把所有 SELECT 的 `AND run_id=?` 从可选变必带；
2. 松散层 4 个 admin 工具：加可选 `run_id` 参数，非空才过滤；
3. CLI + world：`cli_tools.py` 的 2 个 read 子命令加 `--run-id` 必填，`daily-context.ts` 把 `opts.runId` 透传给 CLI。

**Tech Stack:** Python 3.10+ / pytest / SQLite WAL / FastMCP；TypeScript / Node:test / streamable-http MCP。

---

## File Structure

**新建：**
- `fund-portfolio-mcp/test_run_id_isolation.py` — 严格层 + 松散层 unit 测试

**修改：**
- `fund-portfolio-mcp/server.py` — 7 个工具函数签名 + 内部 SELECT
- `fund-portfolio-mcp/cli_tools.py` — 2 个子命令加 `--run-id`
- `world/src/daily-context.ts` — 2 个 fetch 函数透 runId
- `world/test/run.test.ts` — 1 个新 test case 验证 CLI 参数透传（避免大型 e2e，单点验证）

---

## Task 1: Unit-test 脚手架 + 第一个失败测试

**Files:**
- Create: `fund-portfolio-mcp/test_run_id_isolation.py`

- [ ] **Step 1: 创建测试文件，含 fixtures 与多 run 种子数据**

```python
# fund-portfolio-mcp/test_run_id_isolation.py
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
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.close()
    assert rows == [("runA", "510300"), ("runB", "008528")]
```

- [ ] **Step 2: 运行确认 fixtures 跑通**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v`
Expected: 1 passed (test_seed_data_inserts_both_runs)

- [ ] **Step 3: Commit**

```bash
git add fund-portfolio-mcp/test_run_id_isolation.py
git commit -m "test(fund-portfolio): scaffold per-run isolation test fixtures + seed helper

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: portfolio_get_my_history 改成 strict

**Files:**
- Modify: `fund-portfolio-mcp/server.py:1548-1629`
- Test: `fund-portfolio-mcp/test_run_id_isolation.py` (新增 test)

- [ ] **Step 1: 在 test_run_id_isolation.py 末尾追加失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认 strict 测失败、filter 测通过（现在的代码 if run_id 已经过滤了）**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "history"`
Expected:
- test_get_my_history_empty_run_id_returns_error: **FAIL**（当前函数允许空 run_id 返回跨 run 全量）
- test_get_my_history_filters_by_run_id: PASS

- [ ] **Step 3: 改 server.py 让 portfolio_get_my_history strict**

在 [server.py:1548](../../fund-portfolio-mcp/server.py#L1548) `portfolio_get_my_history` 函数体开头（`with get_conn() as conn:` 之前）加 strict check，并把所有 `if run_id:` 分支改成无条件追加：

```python
@mcp.tool()
async def portfolio_get_my_history(
    bot_id: str,
    limit: int = 30,
    fund_code: str = "",
    run_id: str = "",
) -> str:
    """Bot 查看自己的账户/持仓/订单历史，一次返回 4 块：

      account     当前账户：cash / cash_in_transit / total = cash + in_transit + 持仓市值
      holdings    所有持仓行（active + closed），含 pending_sell_shares
      orders      最近 N 单订单（pending + confirmed + 其他），按 order_date desc
      summary     当前 pending 单计数与冻结金额合计

    传 fund_code 可只看那只基金；不传 = 全部。
    run_id 必填（proxy 自动注入）：只返回该 run 自己的 holdings/orders/资金状态。"""
    err = _require_run_id(run_id)
    if err:
        return err
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        h_args: list = [bot_id, run_id]
        h_sql = "SELECT * FROM fund_bot_holdings WHERE bot_id=? AND run_id=?"
        if fund_code:
            h_sql += " AND fund_code=?"
            h_args.append(fund_code)
        h_sql += " ORDER BY status, fund_code"
        holdings = [dict(r) for r in conn.execute(h_sql, h_args).fetchall()]

        o_args: list = [bot_id, run_id, run_id]
        o_sql = ("SELECT * FROM fund_bot_orders WHERE bot_id=? "
                 "AND (order_run_id=? OR settle_run_id=?)")
        if fund_code:
            o_sql += " AND fund_code=?"
            o_args.append(fund_code)
        o_sql += " ORDER BY order_date DESC, order_id DESC LIMIT ?"
        o_args.append(int(max(1, limit)))
        orders = [dict(r) for r in conn.execute(o_sql, o_args).fetchall()]

        market_value = sum(float(h.get("market_value") or 0.0) for h in holdings if h.get("status") == "active")
        view = _bot_run_cash_view(conn, bot_id, run_id)
        cash = float(view["cash_available"])
        in_transit = float(view["cash_in_transit"])
        receivable = float(view["cash_receivable"])
        total = cash + in_transit + receivable + market_value

        pending = [o for o in orders if o.get("status") == "pending"]
        pending_buy_amount = sum(float(o["order_amount"] or 0.0) for o in pending if o["order_type"] == "buy")
        pending_sell_shares = sum(float(o["order_amount"] or 0.0) for o in pending if o["order_type"] == "sell")

        return json.dumps({
            "success": True,
            "bot_id": bot_id,
            "account": {
                "initial_capital": _r(float(account["initial_capital"] or 0.0)),
                "cash_available": _r(cash),
                "cash_in_transit": _r(in_transit),
                "cash_receivable": _r(receivable),
                "market_value": _r(market_value),
                "total_value": _r(total),
            },
            "holdings": holdings,
            "orders": orders,
            "summary": {
                "active_holdings": sum(1 for h in holdings if h.get("status") == "active"),
                "pending_orders": len(pending),
                "pending_buy_amount": _r(pending_buy_amount),
                "pending_sell_shares": _r(pending_sell_shares, 6),
            },
        }, ensure_ascii=False)
```

注意：去掉了"不传 run_id 走 account 行回退"的分支，因为 strict 之后 run_id 一定非空。`_bot_run_cash_view` 是已存在的函数。

- [ ] **Step 4: 跑测试确认两个测试都过**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "history"`
Expected: 2 passed

- [ ] **Step 5: 确认不挂回归测试**

Run: `cd fund-portfolio-mcp && uv run pytest test_bot_performance.py test_redeem_t1_settlement.py test_fund_nav_performance.py test_run_id_migration.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_run_id_isolation.py
git commit -m "feat(fund-portfolio): portfolio_get_my_history strict run_id required

Empty run_id now returns _require_run_id error; SELECT for holdings/orders
always filters by run_id. Cash view always uses _bot_run_cash_view (no
legacy account-row fallback).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: portfolio_get_my_performance 改成 strict + 修两个漏 run_id 的 SELECT

**Files:**
- Modify: `fund-portfolio-mcp/server.py:1747-2068`
- Test: `fund-portfolio-mcp/test_run_id_isolation.py` (新增 test)

- [ ] **Step 1: 在 test_run_id_isolation.py 追加失败测试**

```python
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
```

- [ ] **Step 2: 跑测试，失败/通过状态预期**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "performance"`
Expected:
- test_get_my_performance_empty_run_id_returns_error: FAIL（当前函数无 strict 检查）
- test_get_my_performance_filters_by_run_id: 部分 FAIL — daily_series 已按 run_id 过滤（line 1799），但 `holdings_performance` 那块 SQL（[server.py:1971-1977](../../fund-portfolio-mcp/server.py#L1971-L1977)）没过滤，会同时返回 runA 的 510300 和 runB 的 008528。

- [ ] **Step 3: 改 server.py 让 portfolio_get_my_performance strict**

在 [server.py:1747](../../fund-portfolio-mcp/server.py#L1747) 函数体开头加 strict check，并修两处 SQL：

3a. 函数最开头加：
```python
async def portfolio_get_my_performance(
    bot_id: str,
    as_of_date: str,
    daily_series_limit: int = 120,
    run_id: str = "",
) -> str:
    """...（docstring 保持原样，把 run_id 文档改成"必填，proxy 自动注入"）..."""
    err = _require_run_id(run_id)
    if err:
        return err
    as_of_date = _normalize_trade_date(as_of_date)
    ...
```

3b. snap_sql / ord_sql / closed_sql 把 `if run_id:` 分支拆掉，改成无条件追加 `AND run_id=?`（line 1792-1803 / 1850-1859 / 1869-1878）：

```python
        snap_sql = (
            "SELECT trade_date, initial_capital, cash, invested_value, total_value, net_value, "
            " daily_return_pct, cumulative_return_pct, max_drawdown_pct "
            "FROM fund_bot_daily_snapshots "
            "WHERE bot_id=? AND trade_date < ? AND run_id=? ORDER BY trade_date"
        )
        snaps = conn.execute(snap_sql, (bot_id, as_of_date, run_id)).fetchall()
```

```python
        ord_sql = (
            "SELECT order_type, order_amount, confirmed_amount, confirmed_shares, fee, status, order_date "
            "FROM fund_bot_orders "
            "WHERE bot_id=? AND order_date < ? AND status='confirmed' "
            "AND (order_run_id=? OR settle_run_id=?)"
        )
        orders = conn.execute(ord_sql, (bot_id, as_of_date, run_id, run_id)).fetchall()
```

```python
        closed_sql = (
            "SELECT fund_code, fund_name, entry_date, exit_date "
            "FROM fund_bot_holdings "
            "WHERE bot_id=? AND status='closed' AND exit_date < ? AND run_id=? "
            "ORDER BY exit_date"
        )
        closed_holdings = conn.execute(closed_sql, (bot_id, as_of_date, run_id)).fetchall()
```

3c. 把 `MAX(run_id)` 那段（[line 1940-1945](../../fund-portfolio-mcp/server.py#L1940-L1945)）改成直接用本 run 的 run_id：

```python
        if perf_anchor_date:
            perf_rows = conn.execute(
                "SELECT period, return_pct, max_drawdown_pct, volatility_pct, sharpe_ratio, "
                "  calmar_ratio, data_points, window_target_days, fallback "
                "FROM fund_bot_performance "
                "WHERE bot_id = ? AND trade_date = ? AND run_id = ?",
                (bot_id, perf_anchor_date, run_id),
            ).fetchall()
            for pr in perf_rows:
                interval_metrics[pr["period"]] = {
                    "return_pct": pr["return_pct"],
                    ...
                }
```

注意：保留原结构，只删 `perf_run_row = conn.execute("SELECT MAX(run_id)...` 那 5 行；其余字段不变。

3d. holdings_performance 那段（[line 1971-1977](../../fund-portfolio-mcp/server.py#L1971-L1977)）的 active 持仓 SELECT 加 run_id：

```python
        holdings_performance: dict = {}
        if perf_anchor_date:
            held = conn.execute(
                "SELECT fund_code, fund_name, asset_class, role, market_value, "
                "       actual_weight, holding_days, unrealized_pnl_pct "
                "FROM fund_bot_holdings "
                "WHERE bot_id = ? AND status = 'active' AND run_id = ? ORDER BY fund_code",
                (bot_id, run_id),
            ).fetchall()
            ...
```

- [ ] **Step 4: 跑测试确认两个测试都过**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "performance"`
Expected: 2 passed

- [ ] **Step 5: 跑回归**

Run: `cd fund-portfolio-mcp && uv run pytest test_bot_performance.py test_fund_nav_performance.py -q`
Expected: pass — `test_bot_performance.py` 里现有的 `portfolio_get_my_performance` 调用现在都不带 run_id 入参吗？需 grep 检查。如果不带，必须先在那些测试里补 run_id 入参或在 seed 时把 run_id 留空跟 daily_snapshots 默认 `''` 对齐 —— 见下面 Step 6。

- [ ] **Step 6: 检查 test_bot_performance.py 调用兼容性**

`grep -n "portfolio_get_my_performance" fund-portfolio-mcp/test_bot_performance.py`

每个调用点形如：
```python
payload = asyncio.run(s.portfolio_get_my_performance("botH", "2024-01-07"))
```
seed 数据里这些 bot 的 run_id 是 `'test-run'`（test_bot_performance.py 的 `_seed_daily_snapshots` 写死了），所以传 run_id 进去就行。改成：
```python
payload = asyncio.run(s.portfolio_get_my_performance("botH", "2024-01-07", run_id="test-run"))
```

5 个调用点（line 307 / 366 / 400 / 436 / 458）逐个加 `run_id="test-run"`。再跑测试确认通过。

- [ ] **Step 7: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_run_id_isolation.py fund-portfolio-mcp/test_bot_performance.py
git commit -m "feat(fund-portfolio): portfolio_get_my_performance strict run_id required

- Add _require_run_id at function entry
- Remove conditional 'if run_id:' branches (snaps/orders/closed_holdings SELECTs always filter)
- holdings_performance SELECT now joins on run_id (was bot+status only)
- fund_bot_performance read uses direct run_id (drop MAX(run_id) fallback)
- Update test_bot_performance.py callers to pass run_id='test-run'

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: portfolio_get_my_trades 改成 strict

**Files:**
- Modify: `fund-portfolio-mcp/server.py:2197-2300`
- Test: `fund-portfolio-mcp/test_run_id_isolation.py` (新增 test)

- [ ] **Step 1: 追加测试**

```python
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
```

- [ ] **Step 2: 跑测试**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "trades"`
Expected: empty_run_id_returns_error FAIL, filters_by_run_id PASS

- [ ] **Step 3: 改 server.py**

在 [server.py:2197](../../fund-portfolio-mcp/server.py#L2197) `portfolio_get_my_trades` 加 strict check，把 `if run_id:` 分支改成无条件：

```python
async def portfolio_get_my_trades(
    bot_id: str,
    as_of_date: str,
    limit: int = 100,
    fund_code: str = "",
    run_id: str = "",
) -> str:
    """...（docstring 中 run_id 文档改成"必填，proxy 自动注入"）..."""
    err = _require_run_id(run_id)
    if err:
        return err
    as_of_date = _normalize_trade_date(as_of_date)
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)

        sql = ("SELECT order_id, fund_code, fund_name, order_type, status, "
               " order_date, confirm_date, order_amount, reference_nav, "
               " confirm_nav, confirmed_shares, confirmed_amount, fee, action_reason "
               "FROM fund_bot_orders "
               "WHERE bot_id=? AND order_date < ? "
               "AND (order_run_id=? OR settle_run_id=?)")
        args: list = [bot_id, as_of_date, run_id, run_id]
        if fund_code:
            sql += " AND fund_code=?"
            args.append(fund_code)
        sql += " ORDER BY order_date DESC, order_id DESC"
        rows = conn.execute(sql, args).fetchall()
        ...（下面保持不变）
```

- [ ] **Step 4: 跑测试**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "trades"`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_run_id_isolation.py
git commit -m "feat(fund-portfolio): portfolio_get_my_trades strict run_id required

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 松散层 admin 工具加可选 run_id

**Files:**
- Modify: `fund-portfolio-mcp/server.py:1071` (get_fund_holdings), `:3168` (get_fund_review_history), `:3684` (get_fund_curve), `:3705` (get_fund_position_snapshots)
- Test: `fund-portfolio-mcp/test_run_id_isolation.py`

- [ ] **Step 1: 追加测试，验证可选语义**

```python
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
```

- [ ] **Step 2: 跑测试**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "get_fund_holdings"`
Expected: test_get_fund_holdings_no_run_id_returns_all PASS（当前就是跨 run 视图）；test_get_fund_holdings_with_run_id_filters FAIL（函数还没有 run_id 参数）。

- [ ] **Step 3: 改 server.py 给 4 个 admin 工具加可选 run_id**

3a. `get_fund_holdings`（[server.py:1071](../../fund-portfolio-mcp/server.py#L1071)）：
```python
@mcp.tool()
async def get_fund_holdings(bot_id: str, run_id: str = "") -> str:
    """查询 bot 的当前持仓 + 账户。
    run_id 可选：非空 → 只看该 run 的 active 持仓；空 → 跨 run 全量视图（admin/dashboard 默认）。"""
    with get_conn() as conn:
        account = _get_account(conn, bot_id)
        if not account:
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)
        # 把下面这条 SQL 由"AND status='active'" 改成可选追加 run_id 过滤
        sql = (
            "SELECT h.fund_code, h.fund_name, h.share_class, h.asset_class, h.role, "
            "  h.shares, h.pending_sell_shares, h.amount_invested, h.market_value, "
            "  h.unrealized_pnl, h.unrealized_pnl_pct, h.target_weight, h.actual_weight, "
            "  h.holding_days, h.high_nav, h.entry_date "
            "FROM fund_bot_holdings h "
            "WHERE h.bot_id = ? AND h.status = 'active'"
        )
        args = [bot_id]
        if run_id:
            sql += " AND h.run_id = ?"
            args.append(run_id)
        sql += " ORDER BY h.market_value DESC"
        holdings = [dict(r) for r in conn.execute(sql, args).fetchall()]
        ...（下面保持不变）
```

3b. `get_fund_review_history`（[server.py:3168](../../fund-portfolio-mcp/server.py#L3168)）— reviews 主查改成可选 run_id：

```python
@mcp.tool()
async def get_fund_review_history(bot_id: str, limit: int = 10, run_id: str = "") -> str:
    """查询 bot 最近的巡检历史（含调仓动作和关联订单）。
    run_id 可选：非空 → 只看该 run 的 reviews；空 → 跨 run 全量（admin 默认）。"""
    with get_conn() as conn:
        sql = "SELECT * FROM fund_bot_reviews WHERE bot_id = ?"
        params: list = [bot_id]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY review_date DESC LIMIT ?"
        params.append(limit)
        reviews = conn.execute(sql, params).fetchall()

        result = []
        for r in reviews:
            actions = conn.execute(
                "SELECT * FROM fund_bot_actions WHERE review_id = ?",
                (r["review_id"],)
            ).fetchall()

            orders = conn.execute(
                "SELECT * FROM fund_bot_orders WHERE review_id = ?",
                (r["review_id"],)
            ).fetchall()

            result.append({
                **dict(r),
                "actions": [dict(a) for a in actions],
                "orders": [dict(o) for o in orders],
            })

        return json.dumps({"success": True, "bot_id": bot_id, "reviews": result}, ensure_ascii=False)
```

注意：actions / orders 查的是 review_id（review 的子项），review 行筛掉就够了，不需要再筛 run_id。

3c. `get_fund_curve`（[server.py:3684](../../fund-portfolio-mcp/server.py#L3684)）：

```python
@mcp.tool()
async def get_fund_curve(bot_id: str, start_date: str = "", end_date: str = "", run_id: str = "") -> str:
    """获取 bot 的净值曲线（每日快照序列）。
    run_id 可选：非空 → 只看该 run；空 → 跨 run 全量（admin 默认）。"""
    with get_conn() as conn:
        query = "SELECT * FROM fund_bot_daily_snapshots WHERE bot_id = ?"
        params: list = [bot_id]
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY trade_date"

        rows = conn.execute(query, params).fetchall()
        return json.dumps({
            "success": True, "bot_id": bot_id,
            "count": len(rows), "snapshots": [dict(r) for r in rows],
        }, ensure_ascii=False)
```

3d. `get_fund_position_snapshots`（[server.py:3705](../../fund-portfolio-mcp/server.py#L3705)）：

```python
@mcp.tool()
async def get_fund_position_snapshots(bot_id: str, trade_date: str = "", run_id: str = "") -> str:
    """获取某日的持仓级快照。默认取最近一天。
    run_id 可选：非空 → 只看该 run；空 → 跨 run 全量（admin 默认）。"""
    with get_conn() as conn:
        if not trade_date:
            latest_sql = "SELECT MAX(trade_date) as d FROM fund_bot_position_snapshots WHERE bot_id = ?"
            latest_args: list = [bot_id]
            if run_id:
                latest_sql += " AND run_id = ?"
                latest_args.append(run_id)
            latest = conn.execute(latest_sql, latest_args).fetchone()
            trade_date = latest["d"] if latest and latest["d"] else ""

        if not trade_date:
            return json.dumps({"success": False, "message": "无快照数据"}, ensure_ascii=False)

        sql = ("SELECT * FROM fund_bot_position_snapshots "
               "WHERE bot_id = ? AND trade_date = ?")
        args: list = [bot_id, trade_date]
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        sql += " ORDER BY weight DESC"
        rows = conn.execute(sql, args).fetchall()

        return json.dumps({
            "success": True, "bot_id": bot_id, "trade_date": trade_date,
            "positions": [dict(r) for r in rows],
        }, ensure_ascii=False)
```

- [ ] **Step 4: 跑测试**

Run: `cd fund-portfolio-mcp && uv run pytest test_run_id_isolation.py -v -k "get_fund_holdings"`
Expected: 2 passed

- [ ] **Step 5: 跑回归 sanity**

Run: `cd fund-portfolio-mcp && uv run pytest -q`
Expected: 所有 test 通过

- [ ] **Step 6: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_run_id_isolation.py
git commit -m "feat(fund-portfolio): admin tools accept optional run_id filter

get_fund_holdings / get_fund_review_history / get_fund_curve /
get_fund_position_snapshots 加可选 run_id 参数：非空过滤、空跨 run。
dashboard 默认行为不变。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: cli_tools.py get_my_history / get_my_performance 加 --run-id 必填

**Files:**
- Modify: `fund-portfolio-mcp/cli_tools.py:86-94, 110-113`

- [ ] **Step 1: 改 cli_tools.py 加 --run-id 必填 arg + dispatch**

在 [cli_tools.py:86](../../fund-portfolio-mcp/cli_tools.py#L86)：
```python
    p_hist = sub.add_parser("get_my_history")
    p_hist.add_argument("--bot-id", required=True)
    p_hist.add_argument("--run-id", required=True,
                        help="本轮 run id（world 透传）。read 工具 strict 之后必填。")
    p_hist.add_argument("--limit", type=int, default=30)
    p_hist.add_argument("--fund-code", default="")

    p_perf = sub.add_parser("get_my_performance")
    p_perf.add_argument("--bot-id", required=True)
    p_perf.add_argument("--run-id", required=True,
                        help="本轮 run id（world 透传）。")
    p_perf.add_argument("--as-of-date", required=True)
    p_perf.add_argument("--daily-series-limit", type=int, default=120)
```

dispatch 段（[cli_tools.py:110-113](../../fund-portfolio-mcp/cli_tools.py#L110-L113)）：
```python
    if args.cmd == "get_my_history":
        return await portfolio_get_my_history(args.bot_id, args.limit, args.fund_code, args.run_id)
    if args.cmd == "get_my_performance":
        return await portfolio_get_my_performance(args.bot_id, args.as_of_date, args.daily_series_limit, args.run_id)
```

文档头部的用法示例（[cli_tools.py:12-13](../../fund-portfolio-mcp/cli_tools.py#L12-L13)）也加 `--run-id`：
```python
"""
...
Usage:
  python cli_tools.py init_fund_account     --bot-id bot1 --run-id <runid> --initial-capital 1000000 [--reset]
  python cli_tools.py settle_pending_orders --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15
  python cli_tools.py close_my_day          --bot-id bot1 --run-id <runid> --trade-date 2024-03-14
  python cli_tools.py get_buyable_funds
  python cli_tools.py get_my_history        --bot-id bot1 --run-id <runid> [--limit 30] [--fund-code 510300]
  python cli_tools.py get_my_performance    --bot-id bot1 --run-id <runid> --as-of-date 2024-03-15 [--daily-series-limit 120]
"""
```

- [ ] **Step 2: 手验**

```bash
cd fund-portfolio-mcp
uv run python cli_tools.py get_my_history --bot-id bot11 2>&1 | head -3
```
Expected: argparse 报错 `argument --run-id is required`，exit 2。

```bash
uv run python cli_tools.py get_my_history --bot-id bot11 --run-id dash-2026-05-17T02-37-21 --limit 5 2>&1 | head -3
```
Expected: 返回 JSON。

- [ ] **Step 3: Commit**

```bash
git add fund-portfolio-mcp/cli_tools.py
git commit -m "feat(fund-portfolio-cli): require --run-id on get_my_history / get_my_performance

匹配 server.py 的 strict 检查，避免 daily-context.ts 漏传 run_id 时静默返回错误数据。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: world/src/daily-context.ts 把 runId 透给 CLI

**Files:**
- Modify: `world/src/daily-context.ts:193-241` (fetchAccountSnapshot), `:254-270+` (fetchPerformance), 调用点（约 line 656-665）

- [ ] **Step 1: 读现有调用点确认 opts.runId 可拿到**

Run: `grep -n "fetchAccountSnapshot\|fetchPerformance\|opts.runId" world/src/daily-context.ts | head -20`
Expected: 看到调用 `fetchAccountSnapshot({ fundMcpCli: ..., botId: ..., asOfDate: ... })` 在 buildDailyContext 内部；buildDailyContext 已有 `runId: string` 在 opts 里（around line 632）。

- [ ] **Step 2: 改 fetchAccountSnapshot 签名 + CLI args**

[daily-context.ts:193-199](../../world/src/daily-context.ts#L193-L199)：
```ts
async function fetchAccountSnapshot(opts: {
  fundMcpCli: string
  botId: string
  runId: string
  asOfDate: string
}): Promise<AccountSnapshot | null> {
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_my_history',
      ['--bot-id', opts.botId, '--run-id', opts.runId, '--limit', '30'],
      { timeoutMs: 30_000 })
    ...
```

- [ ] **Step 3: 改 fetchPerformance 签名 + CLI args**

[daily-context.ts:254-266](../../world/src/daily-context.ts#L254-L266)：
```ts
async function fetchPerformance(opts: {
  fundMcpCli: string
  botId: string
  runId: string
  asOfDate: string
  dailySeriesLimit: number
}): Promise<PerformanceData | null> {
  try {
    const r = await runFundCli(opts.fundMcpCli, 'get_my_performance', [
      '--bot-id', opts.botId,
      '--run-id', opts.runId,
      '--as-of-date', opts.asOfDate,
      '--daily-series-limit', String(opts.dailySeriesLimit),
    ], { timeoutMs: 30_000 })
    ...
```

- [ ] **Step 4: 改 buildDailyContext 调用处把 runId 透进去**

[daily-context.ts:656](../../world/src/daily-context.ts#L656) 和 :663（先 grep 确认确切行号）：
```ts
    ? fetchAccountSnapshot({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate })
    ...
    ? fetchPerformance({ fundMcpCli: opts.fundMcpCli, botId: opts.botId, runId: opts.runId, asOfDate: opts.asOfDate, dailySeriesLimit: pnlTrendDays })
```

- [ ] **Step 5: 跑 world 类型检查**

Run: `cd world && npm run check`
Expected: tsc 不报错。

- [ ] **Step 6: Commit**

```bash
git add world/src/daily-context.ts
git commit -m "feat(world): thread runId to fetchAccountSnapshot / fetchPerformance CLI calls

匹配 cli_tools.py 的 --run-id 必填，daily-context 透传 buildDailyContext.opts.runId。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: World 集成测试 — 两 run 数据互不可见

**Files:**
- Modify: `world/test/run.test.ts`（追加一个 test case；不另起新文件，避免 fixture 复制）

- [ ] **Step 1: 检查现有 run.test.ts 的辅助函数**

Run: `grep -n "test(\|function\|runWorld" world/test/run.test.ts | head -20`
确认是否有可复用的 mini world fixture。如果没有，就在新 test case 内联用 sqlite3 直接 seed + assert，绕过 runWorld。

- [ ] **Step 2: 在 run.test.ts 末尾追加 test case**

```ts
import { spawnSync } from 'node:child_process'
import { mkdtempSync as mkdtempSync2 } from 'node:fs'

test('cli_tools get_my_history requires --run-id (strict layer)', async () => {
  // 验证 cli_tools.py 在 --run-id 缺失时退出非零，证明 strict 链路打通。
  // 跑真实 cli_tools.py，需要 fund-portfolio-mcp 的 uv 环境就绪。
  const repoRoot = join(HERE, '..', '..')  // world/test → repo root
  const fundDir = join(repoRoot, 'fund-portfolio-mcp')
  const r = spawnSync('uv', ['run', 'python', 'cli_tools.py', 'get_my_history', '--bot-id', 'bot_does_not_exist'], { cwd: fundDir, encoding: 'utf8' })
  assert.notStrictEqual(r.status, 0, `expected non-zero exit when --run-id missing, got ${r.status}, stderr: ${r.stderr}`)
  assert.match(r.stderr, /--run-id/, `argparse error should mention --run-id, got: ${r.stderr}`)
})
```

注意：这里只验证 CLI 层 strict 是否生效（最薄的 e2e）。深层的"两 run 不互窜"由 fund-portfolio-mcp 的 pytest 已经覆盖；world 测试只兜底 CLI 调用契约。

- [ ] **Step 3: 跑 world test**

Run: `cd world && npm test 2>&1 | tail -30`
Expected: 新 test 通过；其他 test 不变。

如果 uv 环境不可用导致 spawnSync 报 ENOENT，把 test 改成 `t.skip()` 加注释说明依赖。

- [ ] **Step 4: Commit**

```bash
git add world/test/run.test.ts
git commit -m "test(world): assert cli_tools get_my_history rejects missing --run-id

CLI 契约最薄的 e2e 验证；深度隔离用例在 fund-portfolio-mcp/test_run_id_isolation.py。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 现场修复 bot11 当前 run 的视野（一次性手工脚本，不入库）

**目的：** Spec 明确不修 reset 路径。但 bot11 的现状是新 run 已经跑起来了，几个旧 run 的 active 持仓还在 DB 里。新代码上线后，旧 active 持仓对新 run 不可见——但本次 commit 上线前 bot11 已经看到了"错误的 356 万组合"，下一日打开会变干净。这是正确行为，不需要额外动作。

- [ ] **Step 1: 验证修复落地**

执行 SQL 烟雾测试：
```bash
sqlite3 /home/rooot/agent_invest_lab/data/fund.db "
SELECT run_id, COUNT(*), SUM(market_value)
FROM fund_bot_holdings
WHERE bot_id='bot11' AND status='active'
GROUP BY run_id;
"
```
当前应能看到 4 个 run_id 的行各自存在。

跑一次新代码下的 cli_tools.py：
```bash
cd fund-portfolio-mcp
uv run python cli_tools.py get_my_history --bot-id bot11 --run-id dash-2026-05-17T02-37-21 --limit 30 | python3 -c "import sys, json; d = json.load(sys.stdin); print('holdings count:', len([h for h in d['holdings'] if h['status']=='active']), 'codes:', [h['fund_code'] for h in d['holdings'] if h['status']=='active'])"
```
Expected: holdings count: 1, codes: ['510300']（只看到本 run 那一行）。

- [ ] **Step 2: 不 commit（仅本地验证）**

---

## Self-Review

**Spec coverage：**
- ✓ 严格层 3 工具（Task 2/3/4）
- ✓ CLI 必填 run_id（Task 6）
- ✓ daily-context.ts 透传（Task 7）
- ✓ 松散层 4 工具（Task 5）
- ✓ 内部 helper / 漏 run_id 的 SELECT（Task 3 step 3c/3d 覆盖 portfolio_get_my_performance 内部漏的；其他严格层 read 工具内 SELECT 在 Task 2/4 覆盖）
- ✓ 单元 + 集成测试（Task 1-5 含 pytest；Task 8 CLI 契约 e2e）
- ✓ 不改 schema / --reset / NULL 老订单

**Type / 名称一致性：**
- ✓ `_require_run_id` 是已存在函数（server.py:120 附近）
- ✓ `_bot_run_cash_view` 已存在（server.py 内被 close_my_day 使用过）
- ✓ runId 在 daily-context.ts buildDailyContext 已有

**Placeholder scan：**
- 无 TBD/TODO
- 每步含完整代码或确切命令

**Scope:** 单个 PR 即可完成；9 个任务共约 15-20 个 commit。

---

## Notes

- Task 8 故意只做"薄 e2e"——重隔离正确性由 pytest 兜底，避免在 world 跑真实 fund-portfolio-mcp 进程的复杂依赖。
- Task 9 是"现场状态确认"，不写代码、不 commit；纯粹证明 fix 落地后 bot11 的视野干净。
