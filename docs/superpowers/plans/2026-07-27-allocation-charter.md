# Allocation Charter（配置宪章）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给多基金类 bot 增加"宪章"机制——bot 自声明配置结构承诺（单基上限/卫星下限/复评周期），fund-portfolio-mcp 交易层物理执行，防止退化成单基金择时器。

**Architecture:** 三层：① fund-portfolio-mcp（Python）新增宪章表 + 3 个 bot-only 工具 + 买单结构闸门；② world fund-portfolio-proxy 对新工具注入 trade_date/run_id；③ world run.ts/message.ts 在 run 初始化标记宪章义务、每日注入声明/复评提示块。Spec 见 `docs/superpowers/specs/2026-07-27-allocation-charter-design.md`。

**Tech Stack:** Python 3.12（mcp/sqlite3/pytest）、TypeScript（Node ≥22.6，`--experimental-strip-types`，node:test）。

## Global Constraints

- Python 一律用 `/usr/bin/python3.12`（裸 python3 会落到缺 mcp 包的 3.11）。
- pytest 在 `fund-portfolio-mcp/` 目录下运行：`cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest`。
- world 测试：`cd /home/rooot/agent_invest_lab/world && npm test`。
- 类别底线（多基金类，写死系统侧）：`single_fund_max_ratio ≤ 0.75`、`satellite_min_ratio ≥ 0.15`、`0 ≤ min_equity_threshold ≤ 0.50`、`1 ≤ satellite_review_cadence_days ≤ 10`。
- 容忍带 ±5pp（`CHARTER_TOLERANCE = 0.05`）；修订冷却 20 交易日；复评宽限 2 交易日。
- 所有比例相对**权益市值**（非总资产）。卖单永不拦截。
- 未启用宪章的 bot/run（表中无行）行为完全不变（全量向后兼容，现有 test_*.py 必须全绿）。
- **Spec 偏差（已在 spec 中注记）**：宪章修订不再限定"深研日"（deep_research_mode=agent-triggered，调度器无法预标定深研日），改为仅靠 20 交易日冷却 + 理由必填约束。

---

### Task 1: DB schema——宪章表与卫星复评表

**Files:**
- Modify: `fund-portfolio-mcp/db.py`（`init_db()` 的 SCHEMA 字符串区，参照 fund_bot_orders 表的写法，约 173 行附近）
- Test: `fund-portfolio-mcp/test_charter.py`（新建）

**Interfaces:**
- Produces: 表 `fund_bot_charters`（列见下）与 `fund_bot_satellite_reviews`；后续 Task 直接 SQL 读写。

- [ ] **Step 1: 写失败测试**

```python
# fund-portfolio-mcp/test_charter.py
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py::test_charter_tables_exist -v`
Expected: FAIL（assert "fund_bot_charters" in names）

- [ ] **Step 3: 在 db.py SCHEMA 中加两张表**

在 `fund_bot_actions` 表定义之后追加（与现有表同一个 SCHEMA 常量/executescript 区域）：

```sql
CREATE TABLE IF NOT EXISTS fund_bot_charters (
    charter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    declared_date TEXT NOT NULL DEFAULT '',      -- 交易日；status='required' 占位行为空
    core_fund_codes TEXT NOT NULL DEFAULT '[]',  -- JSON array of fund_code
    single_fund_max_ratio REAL NOT NULL DEFAULT 0,
    satellite_min_ratio REAL NOT NULL DEFAULT 0,
    min_equity_threshold REAL NOT NULL DEFAULT 0,
    satellite_review_cadence_days INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',       -- required | active | superseded
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_charters_bot_run ON fund_bot_charters(bot_id, run_id, status);

CREATE TABLE IF NOT EXISTS fund_bot_satellite_reviews (
    sat_review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    review_date TEXT NOT NULL,
    payload TEXT NOT NULL,                       -- 完整结构化 JSON（原样存档，离线可审计）
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sat_reviews_bot_run ON fund_bot_satellite_reviews(bot_id, run_id, review_date);
```

注意：db.py 已有 `_migrate_*` 模式，但这两张表全新、用 `CREATE TABLE IF NOT EXISTS` 即可，老库热升级也安全，无需迁移函数。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/db.py fund-portfolio-mcp/test_charter.py
git commit -m "feat(charter): add charter and satellite review tables"
```

---

### Task 2: 宪章声明/查询工具（declare + get + 类别底线 + 修订冷却）

**Files:**
- Modify: `fund-portfolio-mcp/server.py`（常量加在 `_load_curated_buyable_codes` 之后 ~182 行；工具加在 `portfolio_get_buyable_funds` ~2333 行之后；`_BOT_ONLY_ALLOWED` 集合 ~81 行）
- Test: `fund-portfolio-mcp/test_charter.py`

**Interfaces:**
- Consumes: Task 1 的两张表；现有 `_require_run_id` / `get_conn` / `_normalize_trade_date`。
- Produces:
  - `_load_active_charter(conn, bot_id, run_id) -> dict | None`（status='active' 最新行，core_fund_codes 已 json.loads 成 set）
  - `_charter_required(conn, bot_id, run_id) -> bool`（存在 status='required' 或 'active' 行）
  - `_trading_days_between(conn, d1, d2) -> int`（`SELECT COUNT(DISTINCT nav_date) FROM fund_nav WHERE nav_date > ? AND nav_date <= ?`）
  - MCP 工具 `portfolio_declare_charter(bot_id, charter_json, trade_date, reason, run_id)`
  - MCP 工具 `portfolio_get_my_charter(bot_id, run_id)`
  - 常量 `CHARTER_CLASS_BOUNDS`、`CHARTER_AMEND_COOLDOWN_TDAYS = 20`、`CHARTER_TOLERANCE = 0.05`、`CHARTER_REVIEW_GRACE_TDAYS = 2`

- [ ] **Step 1: 写失败测试**（追加到 test_charter.py）

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v -k declare`
Expected: FAIL（AttributeError: module 'server' has no attribute 'portfolio_declare_charter'）

- [ ] **Step 3: 实现常量 + 帮助函数 + 两个工具**

常量（`_load_curated_buyable_codes` 之后）：

```python
# ── Allocation Charter（配置宪章）───────────────────────────────
# 多基金类 bot 的结构承诺：bot 自声明，系统只卡类别底线，交易层物理执行。
# spec: docs/superpowers/specs/2026-07-27-allocation-charter-design.md
CHARTER_CLASS_BOUNDS = {
    "single_fund_max_ratio": (0.0, 0.75),
    "satellite_min_ratio": (0.15, 1.0),
    "min_equity_threshold": (0.0, 0.50),
    "satellite_review_cadence_days": (1, 10),
}
CHARTER_AMEND_COOLDOWN_TDAYS = 20
CHARTER_TOLERANCE = 0.05           # 结构闸门 ±5pp 容忍带
CHARTER_REVIEW_GRACE_TDAYS = 2     # 复评过期宽限（交易日）


def _trading_days_between(conn, d1: str, d2: str) -> int:
    """(d1, d2] 之间的全局交易日数（以 fund_nav 的 distinct nav_date 为日历）。"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT nav_date) AS n FROM fund_nav WHERE nav_date > ? AND nav_date <= ?",
        (d1, d2)).fetchone()
    return int(row["n"] or 0)


def _load_active_charter(conn, bot_id: str, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM fund_bot_charters WHERE bot_id=? AND run_id=? AND status='active' "
        "ORDER BY charter_id DESC LIMIT 1", (bot_id, run_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["core_fund_codes"] = set(json.loads(d["core_fund_codes"] or "[]"))
    except json.JSONDecodeError:
        d["core_fund_codes"] = set()
    return d


def _charter_required(conn, bot_id: str, run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
        "AND status IN ('required','active') LIMIT 1", (bot_id, run_id)).fetchone()
    return row is not None


def _validate_charter_fields(payload: dict) -> str | None:
    """返回错误信息；None = 合法。"""
    codes = payload.get("core_fund_codes")
    if not isinstance(codes, list) or not codes or not all(isinstance(c, str) and c for c in codes):
        return "core_fund_codes 必须是非空 fund_code 数组"
    for key, (lo, hi) in CHARTER_CLASS_BOUNDS.items():
        v = payload.get(key)
        if not isinstance(v, (int, float)):
            return f"{key} 缺失或非数值"
        if not (lo <= float(v) <= hi):
            return f"{key}={v} 超出类别底线 [{lo}, {hi}]（多基金类 bot 硬约束，不可声明豁免）"
    return None
```

工具（`portfolio_get_buyable_funds` 之后；两个函数名都加进 `_BOT_ONLY_ALLOWED`）：

```python
@mcp.tool()
async def portfolio_declare_charter(
    bot_id: str, charter_json: str, trade_date: str, reason: str = "", run_id: str = "",
) -> str:
    """声明/修订本 bot 的配置宪章（多基金 bot 专用）。

    charter_json 必填字段：core_fund_codes（核心宽基 fund_code 数组，其余池内基金自动算卫星）、
    single_fund_max_ratio（单基 ≤ 权益市值比，≤0.75）、satellite_min_ratio（卫星桶 ≥ 权益市值比，≥0.15）、
    min_equity_threshold（权益/总资产低于此值时结构约束豁免，≤0.50）、
    satellite_review_cadence_days（卫星复评周期交易日，1~10）。
    首次声明随时可做；修订需距上次声明 ≥20 个交易日且 reason 必填（写明依据什么研究修订）。
    数值应源自你自己的 METHODOLOGY/IDENTITY 配置区间——这是你对自己架构的承诺，声明后由交易系统物理执行。
    """
    err = _require_run_id(run_id)
    if err:
        return err
    err = _require_reason(reason, "宪章声明/修订")
    if err:
        return err
    try:
        payload = json.loads(charter_json)
    except json.JSONDecodeError as e:
        return json.dumps({"success": False, "message": f"charter_json 不是合法 JSON: {e}"}, ensure_ascii=False)
    if not isinstance(payload, dict):
        return json.dumps({"success": False, "message": "charter_json 必须是 JSON object"}, ensure_ascii=False)
    verr = _validate_charter_fields(payload)
    if verr:
        return json.dumps({"success": False, "message": verr}, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        if not _get_account(conn, bot_id):
            return json.dumps({"success": False, "message": f"bot {bot_id} 无账户"}, ensure_ascii=False)
        current = _load_active_charter(conn, bot_id, run_id)
        if current:
            gap = _trading_days_between(conn, current["declared_date"], trade_date)
            if gap < CHARTER_AMEND_COOLDOWN_TDAYS:
                return json.dumps({
                    "success": False,
                    "message": f"宪章修订冷却中：距上次声明（{current['declared_date']}）仅 {gap} 个交易日，"
                               f"需 ≥{CHARTER_AMEND_COOLDOWN_TDAYS}。防止规则反复收紧/放松。",
                }, ensure_ascii=False)
            conn.execute("UPDATE fund_bot_charters SET status='superseded' "
                         "WHERE charter_id=?", (current["charter_id"],))
        # 吃掉 world 侧 charter_require 写入的占位行
        conn.execute("UPDATE fund_bot_charters SET status='superseded' "
                     "WHERE bot_id=? AND run_id=? AND status='required'", (bot_id, run_id))
        conn.execute(
            "INSERT INTO fund_bot_charters (bot_id, run_id, declared_date, core_fund_codes, "
            " single_fund_max_ratio, satellite_min_ratio, min_equity_threshold, "
            " satellite_review_cadence_days, reason, status) VALUES (?,?,?,?,?,?,?,?,?, 'active')",
            (bot_id, run_id, trade_date, json.dumps(sorted(set(payload["core_fund_codes"]))),
             float(payload["single_fund_max_ratio"]), float(payload["satellite_min_ratio"]),
             float(payload["min_equity_threshold"]), int(payload["satellite_review_cadence_days"]),
             reason))
    return json.dumps({
        "success": True, "bot_id": bot_id, "declared_date": trade_date,
        "amended": bool(current),
        "note": "宪章已生效：交易层将按此执行单基上限/卫星下限/复评节奏。修订需 ≥20 交易日冷却。",
    }, ensure_ascii=False)


@mcp.tool()
async def portfolio_get_my_charter(bot_id: str, run_id: str = "") -> str:
    """查看本 bot 当前生效的配置宪章与卫星复评状态。"""
    err = _require_run_id(run_id)
    if err:
        return err
    with get_conn() as conn:
        charter = _load_active_charter(conn, bot_id, run_id)
        if not charter:
            required = _charter_required(conn, bot_id, run_id)
            return json.dumps({
                "declared": False, "required": required,
                "note": "宪章未声明" + ("；本 run 已启用宪章义务，声明前买入单会被拒" if required else ""),
            }, ensure_ascii=False)
        last_review = conn.execute(
            "SELECT MAX(review_date) AS d FROM fund_bot_satellite_reviews "
            "WHERE bot_id=? AND run_id=?", (bot_id, run_id)).fetchone()
        out = {k: v for k, v in charter.items() if k != "core_fund_codes"}
        out["core_fund_codes"] = sorted(charter["core_fund_codes"])
        out["declared"] = True
        out["last_satellite_review_date"] = last_review["d"]
        return json.dumps(out, ensure_ascii=False)
```

- [ ] **Step 4: 跑测试确认通过（含旧测试回归）**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py test_run_id_isolation.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_charter.py
git commit -m "feat(charter): declare/get charter tools with class bounds and amend cooldown"
```

---

### Task 3: 卫星复评提交工具

**Files:**
- Modify: `fund-portfolio-mcp/server.py`（工具加在 portfolio_get_my_charter 之后；函数名加进 `_BOT_ONLY_ALLOWED`）
- Test: `fund-portfolio-mcp/test_charter.py`

**Interfaces:**
- Consumes: `_load_active_charter`、`_replay_fund_account_state`（现有，server.py:585，返回 `{"cash", "positions": {code: {...shares...}}, ...}`）、`_load_curated_buyable_codes`。
- Produces: MCP 工具 `portfolio_submit_satellite_review(bot_id, review_json, trade_date, run_id)`；帮助函数 `_satellite_codes(conn, bot_id, run_id, trade_date, charter) -> set[str]`（当前活跃非核心持仓）。

- [ ] **Step 1: 写失败测试**（追加到 test_charter.py）

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v -k review`
Expected: FAIL（no attribute 'portfolio_submit_satellite_review'）

- [ ] **Step 3: 实现**

```python
def _satellite_codes(conn, bot_id: str, run_id: str, trade_date: str, charter: dict) -> set[str]:
    state = _replay_fund_account_state(conn, bot_id, trade_date, run_id=run_id)
    return {c for c in state["positions"] if c not in charter["core_fund_codes"]}


@mcp.tool()
async def portfolio_submit_satellite_review(
    bot_id: str, review_json: str, trade_date: str, run_id: str = "",
) -> str:
    """提交卫星仓周期复评（宪章义务）。review_json 必填结构：
    {"mainline_thesis": "≥30字主线判断", "holdings": [{"fund_code", "verdict": keep|rotate|exit,
    "rationale": "≥20字"}...须覆盖全部卫星持仓], "candidates": [{"fund_code": 池内代码,
    "comparison": "≥20字对比"} ×≥2]}。复评过期会导致核心基金买单被拒。"""
    err = _require_run_id(run_id)
    if err:
        return err
    try:
        payload = json.loads(review_json)
    except json.JSONDecodeError as e:
        return json.dumps({"success": False, "message": f"review_json 不是合法 JSON: {e}"}, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        charter = _load_active_charter(conn, bot_id, run_id)
        if not charter:
            return json.dumps({"success": False, "message": "未声明宪章，先调 portfolio_declare_charter"}, ensure_ascii=False)
        thesis = payload.get("mainline_thesis")
        if not isinstance(thesis, str) or len(thesis) < 30:
            return json.dumps({"success": False, "message": "mainline_thesis 必填且 ≥30 字"}, ensure_ascii=False)
        holdings = payload.get("holdings")
        if not isinstance(holdings, list):
            return json.dumps({"success": False, "message": "holdings 必须是数组"}, ensure_ascii=False)
        for h in holdings:
            if (not isinstance(h, dict) or h.get("verdict") not in ("keep", "rotate", "exit")
                    or not isinstance(h.get("rationale"), str) or len(h["rationale"]) < 20):
                return json.dumps({"success": False,
                    "message": "holdings 每项需 {fund_code, verdict: keep|rotate|exit, rationale ≥20字}"},
                    ensure_ascii=False)
        need = _satellite_codes(conn, bot_id, run_id, trade_date, charter)
        got = {h.get("fund_code") for h in holdings}
        missing = need - got
        if missing:
            return json.dumps({"success": False,
                "message": f"复评须覆盖全部卫星持仓，缺: {sorted(missing)}"}, ensure_ascii=False)
        cands = payload.get("candidates")
        if not isinstance(cands, list) or len(cands) < 2 or any(
                not isinstance(c, dict) or not c.get("fund_code")
                or not isinstance(c.get("comparison"), str) or len(c["comparison"]) < 20
                for c in cands):
            return json.dumps({"success": False,
                "message": "candidates 候选对比 ≥2 项，每项 {fund_code, comparison ≥20字}"}, ensure_ascii=False)
        curated = _load_curated_buyable_codes(run_id, bot_id)
        if curated is not None:
            outside = [c["fund_code"] for c in cands if c["fund_code"] not in curated]
            if outside:
                return json.dumps({"success": False,
                    "message": f"候选必须来自本 bot 可买池，池外: {outside}"}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO fund_bot_satellite_reviews (bot_id, run_id, review_date, payload) "
            "VALUES (?,?,?,?)", (bot_id, run_id, trade_date, json.dumps(payload, ensure_ascii=False)))
    return json.dumps({"success": True, "bot_id": bot_id, "review_date": trade_date,
                       "covered_holdings": sorted(got & need) if need else [],
                       "note": "卫星复评已落库；下次到期日按宪章 cadence 顺延"}, ensure_ascii=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_charter.py
git commit -m "feat(charter): satellite review submission tool with schema and coverage checks"
```

---

### Task 4: 买单结构闸门（宪章执行核心）

**Files:**
- Modify: `fund-portfolio-mcp/server.py`（帮助函数加在 `_strict_nav` 之后 ~1568 行；钩子插入 `portfolio_place_buy_order` 的现金校验之后、INSERT 之前，~1656 行）
- Test: `fund-portfolio-mcp/test_charter.py`

**Interfaces:**
- Consumes: Task 2/3 的 `_load_active_charter`/`_charter_required`/`_satellite_codes`/`_trading_days_between` 及常量；现有 `_replay_fund_account_state`/`_get_nav`/`_bot_run_cash_view`。
- Produces: `_charter_gate_buy(conn, bot_id, run_id, trade_date, fund_code, amount) -> str | None`（返回拒单 JSON 字符串；None = 放行）。

- [ ] **Step 1: 写失败测试**（追加到 test_charter.py；矩阵覆盖 6 个场景）

```python
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
```

注：`test_gate_single_fund_cap` 数字推演——持仓核心 40w、卫星 11w、nav 全 1.0；再买核心 20w：核心 60w/权益 71w = 84.5% > 60%+5pp → 拒。`test_gate_satellite_floor_blocks_core_buy`——买核心 10w 后卫星 11w/权益 61w = 18.0% < 25%-5pp → 拒。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v -k gate`
Expected: `test_gate_no_charter_row_passes` PASS（现状即此），其余 FAIL（买单未被拒）

- [ ] **Step 3: 实现 `_charter_gate_buy` 并挂进 place_buy_order**

```python
def _charter_gate_buy(conn, bot_id: str, run_id: str, trade_date: str,
                      fund_code: str, amount: float) -> str | None:
    """宪章结构闸门（仅 BUY）。返回拒单 JSON；None=放行。卖单永不进此函数。"""
    if not _charter_required(conn, bot_id, run_id):
        return None                      # 本 run/bot 未启用宪章 → 完全旧行为
    charter = _load_active_charter(conn, bot_id, run_id)
    if not charter:
        return json.dumps({"success": False,
            "message": "本 run 已启用宪章义务但尚未声明：先调 portfolio_declare_charter "
                       "按你的 METHODOLOGY 配置区间声明单基上限/卫星下限/复评周期，再下买单。"},
            ensure_ascii=False)
    core = charter["core_fund_codes"]
    # ── 模拟成交后结构：replay 持仓市值 + pending buy 金额 - pending sell 份额 + 本单 ──
    state = _replay_fund_account_state(conn, bot_id, trade_date, run_id=run_id)
    mv: dict[str, float] = {}
    for code, pos in state["positions"].items():
        nav, _ = _get_nav(conn, code, trade_date)
        mv[code] = float(pos["shares"]) * nav
    for row in conn.execute(
            "SELECT fund_code, COALESCE(SUM(order_amount),0) AS s FROM fund_bot_orders "
            "WHERE bot_id=? AND order_run_id=? AND order_type='buy' AND status='pending' "
            "GROUP BY fund_code", (bot_id, run_id)):
        mv[row["fund_code"]] = mv.get(row["fund_code"], 0.0) + float(row["s"])
    for row in conn.execute(
            "SELECT fund_code, COALESCE(SUM(pending_sell_shares),0) AS s FROM fund_bot_holdings "
            "WHERE bot_id=? AND run_id=? AND status='active' GROUP BY fund_code",
            (bot_id, run_id)):
        nav, _ = _get_nav(conn, row["fund_code"], trade_date)
        mv[row["fund_code"]] = max(mv.get(row["fund_code"], 0.0) - float(row["s"]) * nav, 0.0)
    mv[fund_code] = mv.get(fund_code, 0.0) + amount
    equity = sum(mv.values())
    if equity <= 1e-6:
        return None
    view = _bot_run_cash_view(conn, bot_id, run_id, trade_date)
    total_assets = equity + max(float(view["cash_available"]) - amount, 0.0) + float(view["cash_receivable"])
    # ── 豁免线：防御态不强拆结构 ──
    if total_assets > 1e-6 and equity / total_assets < float(charter["min_equity_threshold"]):
        return None
    tol = CHARTER_TOLERANCE
    # ── 单基上限（对所有基金生效）──
    cap = float(charter["single_fund_max_ratio"])
    if mv[fund_code] / equity > cap + tol:
        return json.dumps({"success": False,
            "message": f"宪章拒单：{fund_code} 成交后将占权益 {mv[fund_code]/equity:.1%} > "
                       f"single_fund_max_ratio {cap:.0%}（+{tol:.0%} 容忍带）。"
                       f"可改买卫星基金或减小金额。当前权益结构: "
                       + json.dumps({k: round(v/equity, 3) for k, v in mv.items()}, ensure_ascii=False)},
            ensure_ascii=False)
    if fund_code in core:
        # ── 卫星下限（只闸核心买单：买核心会稀释卫星占比）──
        sat_mv = sum(v for k, v in mv.items() if k not in core)
        floor = float(charter["satellite_min_ratio"])
        if sat_mv / equity < floor - tol:
            return json.dumps({"success": False,
                "message": f"宪章拒单：成交后卫星桶将占权益 {sat_mv/equity:.1%} < "
                           f"satellite_min_ratio {floor:.0%}（-{tol:.0%} 容忍带）。"
                           f"先配置/加仓卫星基金（主线→候选→选品），或减小核心买入金额。"},
                ensure_ascii=False)
        # ── 复评牙齿（只闸核心买单；无卫星持仓不欠复评）──
        sat_holdings = _satellite_codes(conn, bot_id, run_id, trade_date, charter)
        if sat_holdings:
            last = conn.execute(
                "SELECT MAX(review_date) AS d FROM fund_bot_satellite_reviews "
                "WHERE bot_id=? AND run_id=?", (bot_id, run_id)).fetchone()
            baseline = last["d"] or charter["declared_date"]
            overdue_after = int(charter["satellite_review_cadence_days"]) + CHARTER_REVIEW_GRACE_TDAYS
            gap = _trading_days_between(conn, baseline, trade_date)
            if gap > overdue_after:
                return json.dumps({"success": False,
                    "message": f"宪章拒单：卫星复评过期（距上次 {baseline} 已 {gap} 交易日 > "
                               f"cadence {charter['satellite_review_cadence_days']}+宽限 {CHARTER_REVIEW_GRACE_TDAYS}）。"
                               f"先调 portfolio_submit_satellite_review 完成复评，再买核心基金。"},
                    ensure_ascii=False)
    return None
```

挂载：`portfolio_place_buy_order` 内、现金校验（`if amount > cash + 1e-6`）通过之后、`_fund_fee_rates` 之前插入：

```python
        gate_err = _charter_gate_buy(conn, bot_id, run_id, trade_date, fund_code, amount)
        if gate_err:
            return gate_err
```

- [ ] **Step 4: 跑全量测试**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest -v`
Expected: 全 PASS（含全部既有 test_*.py——无宪章行时闸门必须零影响）

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/server.py fund-portfolio-mcp/test_charter.py
git commit -m "feat(charter): structural gate on buy orders (cap/floor/exemption/review teeth)"
```

---

### Task 5: cli_tools 宪章命令（world 系统侧接口）

**Files:**
- Modify: `fund-portfolio-mcp/cli_tools.py`（sub parser 区 ~60-120 行；dispatch 在 `main()` ~253 行）
- Test: `fund-portfolio-mcp/test_charter.py`

**Interfaces:**
- Consumes: Task 1 表；Task 2 的 `_trading_days_between` 逻辑（cli 内联同款 SQL 即可，勿 import server）。
- Produces:
  - `charter_require --bot-id X --run-id Y`：幂等插入 status='required' 占位行（已有 required/active 行则跳过），stdout 输出 JSON `{"ok": true, "existing": bool}`。
  - `charter_status --bot-id X --run-id Y --date YYYY-MM-DD`：stdout 输出 JSON `{"required": bool, "declared": bool, "cadence": int|null, "last_review_date": str|null, "declared_date": str|null, "review_due": bool, "review_overdue": bool, "satellite_holding_count": int}`（world 每日调用，驱动 prompt 注入）。

- [ ] **Step 1: 写失败测试**（追加到 test_charter.py；直接调函数而非 subprocess，遵循 cli_tools 现有可测结构——若 cli_tools 只有 argparse main，则用 `subprocess.run([sys.executable, "cli_tools.py", ...], env={**os.environ, "FUND_DB_PATH": tmp_db})` 模式，以 cli_tools 实际读取 DB 路径的机制为准，实现时先读 cli_tools.py 头部确认 DB 注入方式）

```python
import subprocess, sys

def _cli(tmp_db, *args):
    env = dict(os.environ)
    env["FUND_MCP_DB_PATH"] = tmp_db   # 以 db.py 实际支持的环境变量为准，实现时核对
    out = subprocess.run(
        ["/usr/bin/python3.12", "cli_tools.py", *args],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.abspath("cli_tools.py")))
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v -k cli`
Expected: FAIL（unknown command charter_require）

- [ ] **Step 3: 实现两个子命令**

在 sub parser 区追加：

```python
    p_creq = sub.add_parser("charter_require")
    p_creq.add_argument("--bot-id", required=True)
    p_creq.add_argument("--run-id", required=True)

    p_cstat = sub.add_parser("charter_status")
    p_cstat.add_argument("--bot-id", required=True)
    p_cstat.add_argument("--run-id", required=True)
    p_cstat.add_argument("--date", required=True)
```

dispatch 分支（在 `main()` 中，模仿现有命令的 `get_conn()` 用法）：

```python
    elif args.command == "charter_require":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                "AND status IN ('required','active') LIMIT 1",
                (args.bot_id, args.run_id)).fetchone()
            if row:
                print(json.dumps({"ok": True, "existing": True}))
            else:
                conn.execute(
                    "INSERT INTO fund_bot_charters (bot_id, run_id, status) VALUES (?,?,'required')",
                    (args.bot_id, args.run_id))
                print(json.dumps({"ok": True, "existing": False}))

    elif args.command == "charter_status":
        with get_conn() as conn:
            active = conn.execute(
                "SELECT declared_date, satellite_review_cadence_days FROM fund_bot_charters "
                "WHERE bot_id=? AND run_id=? AND status='active' ORDER BY charter_id DESC LIMIT 1",
                (args.bot_id, args.run_id)).fetchone()
            required = conn.execute(
                "SELECT 1 FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                "AND status IN ('required','active') LIMIT 1",
                (args.bot_id, args.run_id)).fetchone() is not None
            out = {"required": required, "declared": active is not None,
                   "cadence": None, "declared_date": None, "last_review_date": None,
                   "review_due": False, "review_overdue": False, "satellite_holding_count": 0}
            if active:
                out["cadence"] = int(active["satellite_review_cadence_days"])
                out["declared_date"] = active["declared_date"]
                last = conn.execute(
                    "SELECT MAX(review_date) AS d FROM fund_bot_satellite_reviews "
                    "WHERE bot_id=? AND run_id=?", (args.bot_id, args.run_id)).fetchone()
                out["last_review_date"] = last["d"]
                baseline = last["d"] or active["declared_date"]
                gap = conn.execute(
                    "SELECT COUNT(DISTINCT nav_date) AS n FROM fund_nav "
                    "WHERE nav_date > ? AND nav_date <= ?", (baseline, args.date)).fetchone()["n"]
                out["review_due"] = gap >= out["cadence"]
                out["review_overdue"] = gap > out["cadence"] + 2
                # 卫星持仓数：active holdings 里非 core 的行数
                charter_row = conn.execute(
                    "SELECT core_fund_codes FROM fund_bot_charters WHERE bot_id=? AND run_id=? "
                    "AND status='active' ORDER BY charter_id DESC LIMIT 1",
                    (args.bot_id, args.run_id)).fetchone()
                core = set(json.loads(charter_row["core_fund_codes"] or "[]"))
                sat = conn.execute(
                    "SELECT COUNT(*) AS n FROM fund_bot_holdings WHERE bot_id=? AND run_id=? "
                    "AND status='active' AND shares > 1e-6", (args.bot_id, args.run_id)).fetchall()
                rows = conn.execute(
                    "SELECT fund_code FROM fund_bot_holdings WHERE bot_id=? AND run_id=? "
                    "AND status='active' AND shares > 1e-6", (args.bot_id, args.run_id)).fetchall()
                out["satellite_holding_count"] = sum(1 for r in rows if r["fund_code"] not in core)
                if out["satellite_holding_count"] == 0:
                    out["review_due"] = False
                    out["review_overdue"] = False
            print(json.dumps(out, ensure_ascii=False))
```

（实现时删掉上面多余的 `sat = ...fetchall()` 行——以 rows 计数为准。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/rooot/agent_invest_lab/fund-portfolio-mcp && /usr/bin/python3.12 -m pytest test_charter.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add fund-portfolio-mcp/cli_tools.py fund-portfolio-mcp/test_charter.py
git commit -m "feat(charter): charter_require/charter_status cli commands for world integration"
```

---

### Task 6: fund-portfolio-proxy 对新工具注入 trade_date/run_id

**Files:**
- Modify: `world/src/fund-portfolio-proxy/server.ts:30`
- Test: `world/test/fund-portfolio-proxy.test.ts`（若已存在则追加；不存在则新建，模式参照 world/test/ 现有 *.test.ts）

**Interfaces:**
- Consumes: 现有 `TRADE_DATE_TOOLS` 机制（schema strip + tools/call 注入）。
- Produces: bot 视角下 `portfolio_declare_charter` / `portfolio_submit_satellite_review` 的 schema 不含 trade_date/run_id，调用时由 proxy 强制注入世界日期与 run_id（与买卖单同构——bot 不可伪造声明日期）。

- [ ] **Step 1: 修改集合**

```typescript
const TRADE_DATE_TOOLS = new Set([
  'portfolio_place_buy_order',
  'portfolio_place_sell_order',
  'portfolio_declare_charter',
  'portfolio_submit_satellite_review',
])
```

- [ ] **Step 2: 加/改测试**

先 `ls /home/rooot/agent_invest_lab/world/test/` 看有无 fund-portfolio-proxy 测试；有则在其中追加断言（对 `stripFromToolSchema` 或端到端 mock upstream 的现有写法），验证新工具名的 schema 中 `trade_date` 被剥除、tools/call 参数被注入。若完全没有 proxy 测试且现有测试均为集成式，最低要求：新建轻量单测导出 `stripFromToolSchema`（需在 server.ts 加 `export`）：

```typescript
// world/test/fund-portfolio-proxy.test.ts
import test from 'node:test'
import assert from 'node:assert'
// 需要把 stripFromToolSchema 与 TRADE_DATE_TOOLS 从 server.ts export 出来
import { stripFromToolSchema } from '../src/fund-portfolio-proxy/server.ts'

test('charter tools get trade_date stripped from schema', () => {
  for (const name of ['portfolio_declare_charter', 'portfolio_submit_satellite_review']) {
    const tool = {
      name,
      inputSchema: {
        properties: { bot_id: {}, trade_date: {}, run_id: {} },
        required: ['bot_id', 'trade_date', 'run_id'],
      },
    } as Record<string, unknown>
    stripFromToolSchema(tool, true)
    const schema = tool.inputSchema as { properties: Record<string, unknown>; required: string[] }
    assert.ok(!('trade_date' in schema.properties), name + ' trade_date should be stripped')
    assert.ok(!('run_id' in schema.properties), name + ' run_id should be stripped')
    assert.deepStrictEqual(schema.required, ['bot_id'])
  }
})
```

- [ ] **Step 3: 跑测试**

Run: `cd /home/rooot/agent_invest_lab/world && npm test`
Expected: 全 PASS

- [ ] **Step 4: Commit**

```bash
git add world/src/fund-portfolio-proxy/server.ts world/test/fund-portfolio-proxy.test.ts
git commit -m "feat(charter): proxy injects trade_date/run_id for charter tools"
```

---

### Task 7: world 集成——config 开关、init 标记义务、每日宪章提示块

**Files:**
- Modify: `world/src/config.ts`（接口 ~94 行 fundMcpCli 附近加字段；yaml 解析处加读取）
- Modify: `world/src/run.ts`（init：~728 行 init_fund_account 循环后；daily：~1404 行 renderDailyMessage 调用处）
- Modify: `world/src/message.ts`（DailyMessageContext 加字段 + renderDailyMessage 渲染块）
- Test: `world/test/`（message 渲染若有现有测试文件则追加 charterBlock 断言；无则以 npm test 全绿 + Task 9 smoke 为验收）

**Interfaces:**
- Consumes: Task 5 的 `charter_require`/`charter_status` cli JSON；现有 `runFundCli(config.fundMcpCli, cmd, args)`（run.ts:50）；`botKindOf(botId)`（message.ts 导出，'multi-fund' 判定）。
- Produces:
  - config 字段 `charterEnforcement?: boolean`（yaml key `charter_enforcement`，缺省 false）
  - `DailyMessageContext.charterBlock?: string`
  - daily prompt 中的【配置宪章】提示块

- [ ] **Step 1: config.ts 加字段**

接口加：

```typescript
  // 配置宪章（allocation charter）：true 时 run init 对 multi-fund bot 写宪章义务占位，
  // 每日注入声明/复评提示，fund-portfolio-mcp 侧执行结构闸门。缺省 false（现有 run 全部不受影响）。
  charterEnforcement?: boolean
```

yaml 解析处（模仿 `fundInitReset` 等 boolean 字段的现有写法）：

```typescript
  if (raw.charter_enforcement !== undefined && typeof raw.charter_enforcement !== 'boolean') {
    throw new Error('world config: "charter_enforcement" must be boolean')
  }
  // ...对象组装处：
  charterEnforcement: raw.charter_enforcement === true,
```

- [ ] **Step 2: run.ts init 写宪章义务**

在 init_fund_account 循环（~line 723-733 `for` 内 `runFundCli(... 'init_fund_account' ...)` 之后）追加：

```typescript
      if (config.charterEnforcement && botKindOf(botId) === 'multi-fund') {
        const cr = await runFundCli(config.fundMcpCli, 'charter_require',
          ['--bot-id', botId, '--run-id', runId], { timeoutMs: 30_000 })
        log(worldRoot, runId, `charter_require ${botId}: ${cr.trim().slice(0, 120)}`)
      }
```

（`botKindOf` 已在 run.ts:11 import。）

- [ ] **Step 3: run.ts daily 取宪章状态 + message.ts 渲染**

message.ts `DailyMessageContext` 加字段：

```typescript
  // 【配置宪章】提示块：宪章未声明（Day 1）→ 声明指引；卫星复评到期/过期 → 复评提醒。
  // 由 run.ts 每日调 cli charter_status 组装；空/缺省 → 跳过整块（宪章未启用的 run 即此）。
  charterBlock?: string
```

`renderDailyMessage` 内（historyWindow 块之后、dailyContext 之前的拼接顺序处）加：

```typescript
  const charterPart = ctx.charterBlock ? '\n\n' + ctx.charterBlock : ''
```

并拼进最终模板字符串（与相邻块同样的插接方式）。

run.ts 在构建 daily message ctx 前（~1404 行 renderDailyMessage 调用点之前）：

```typescript
      let charterBlock: string | undefined
      if (config.charterEnforcement && config.fundMcpCli && botKindOf(b.botId) === 'multi-fund') {
        try {
          const raw = await runFundCli(config.fundMcpCli, 'charter_status',
            ['--bot-id', b.botId, '--run-id', runId, '--date', date], { timeoutMs: 30_000 })
          const st = JSON.parse(raw.trim().split('\n').at(-1) ?? '{}') as {
            required: boolean; declared: boolean; review_due: boolean
            review_overdue: boolean; cadence: number | null; last_review_date: string | null
          }
          if (st.required && !st.declared) {
            charterBlock = [
              '【配置宪章：今日必须声明】',
              '本 run 启用了配置宪章。你必须先调 mcp__fund_portfolio_mcp__portfolio_declare_charter，',
              '依据你 METHODOLOGY 的配置区间声明：core_fund_codes（核心宽基）、single_fund_max_ratio（≤0.75）、',
              'satellite_min_ratio（≥0.15）、min_equity_threshold（≤0.50）、satellite_review_cadence_days（1~10）。',
              '声明前所有买入单都会被拒。声明后由交易系统物理执行——这是你对自己架构的承诺。',
            ].join('\n')
          } else if (st.declared && (st.review_due || st.review_overdue)) {
            charterBlock = [
              `【配置宪章：卫星复评${st.review_overdue ? '已过期' : '今日到期'}】`,
              `上次复评 ${st.last_review_date ?? '（从未）'}，cadence=${st.cadence} 交易日。`,
              '调 mcp__fund_portfolio_mcp__portfolio_submit_satellite_review 提交结构化复评：每只卫星持仓给出',
              'keep/rotate/exit 结论（≥20字理由）+ ≥2 只池内候选对比 + 主线判断（≥30字）。',
              st.review_overdue ? '复评过期期间核心基金买单会被拒。' : '',
            ].filter(Boolean).join('\n')
          }
        } catch (err) {
          log(worldRoot, runId, `charter_status ${b.botId} FAILED: ${err instanceof Error ? err.message : String(err)}`)
        }
      }
```

并把 `charterBlock` 传入 renderDailyMessage 的 ctx 对象。注意 MCP 工具前缀（`mcp__fund_portfolio_mcp__`）以该 run 实际 server alias 为准——grep run.ts/message.ts 中现有 `mcp__fund_portfolio_mcp__portfolio_place_buy_order` 字样保持一致。

- [ ] **Step 4: 跑 world 测试 + 类型检查**

Run: `cd /home/rooot/agent_invest_lab/world && npm test`
Expected: 全 PASS（--experimental-strip-types 下类型错误会在加载时抛）

- [ ] **Step 5: Commit**

```bash
git add world/src/config.ts world/src/run.ts world/src/message.ts
git commit -m "feat(charter): world config flag, init requirement, daily charter prompt block"
```

---

### Task 8: smoke 配置启用宪章 + spec 注记偏差

**Files:**
- Modify: `world/config/world-bot105d-smoke10d-agenticdeep.yaml`（顶层加 `charter_enforcement: true`）
- Modify: `docs/superpowers/specs/2026-07-27-allocation-charter-design.md`（§4 修订条款注记）

**Interfaces:**
- Consumes: Task 7 的 config 解析。
- Produces: 可直接跑的 smoke 验证配置。

- [ ] **Step 1: yaml 加一行**

```yaml
charter_enforcement: true
```

- [ ] **Step 2: spec §4 修订条款下加注记**

```markdown
> **实现注记（2026-07-27）**：修订不再限定"深研日"——daily-agenticdeep 管线的 deep research
> 为 agent-triggered（bot 会话内自触发），调度器无法预先标定深研日。改为仅以 20 交易日冷却
> + reason 必填约束修订频率。防棘轮效果等价（冷却是主要约束），实现大幅简化。
```

- [ ] **Step 3: Commit**

```bash
git add world/config/world-bot105d-smoke10d-agenticdeep.yaml docs/superpowers/specs/2026-07-27-allocation-charter-design.md
git commit -m "feat(charter): enable charter in bot105d smoke config; note spec deviation"
```

---

### Task 9: 端到端 smoke 验证

**Files:** 无代码改动（运行验证）。

- [ ] **Step 1: 启动依赖服务**（若未在跑）

```bash
cd /home/rooot/agent_invest_lab && ./restart.sh && ./restart-fund-mcp-bot-only.sh
```

检查 `/tmp/ttjj-data-pit-mcp.log`、`/tmp/fund-portfolio-mcp-bot-only.log` 无启动错误。注意：测 MCP 不要并发跑多个 world run。

- [ ] **Step 2: 跑 10 天 smoke**

```bash
cd /home/rooot/agent_invest_lab/world && npm start -- --config config/world-bot105d-smoke10d-agenticdeep.yaml
```

Expected: run 正常完成（退出码 0）。

- [ ] **Step 3: 验收查询**

```bash
/usr/bin/python3.12 - <<'EOF'
import sqlite3, json
con = sqlite3.connect('/home/rooot/agent_invest_lab/data/fund.db'); con.row_factory = sqlite3.Row
run = con.execute("SELECT run_id FROM fund_bot_charters ORDER BY charter_id DESC LIMIT 1").fetchone()
assert run, "无宪章行——init 或声明失败"
rid = run["run_id"]
ch = con.execute("SELECT status, declared_date, satellite_min_ratio, single_fund_max_ratio "
                 "FROM fund_bot_charters WHERE run_id=? ORDER BY charter_id", (rid,)).fetchall()
print("charters:", [dict(r) for r in ch])
rv = con.execute("SELECT review_date FROM fund_bot_satellite_reviews WHERE run_id=?", (rid,)).fetchall()
print("reviews:", [r["review_date"] for r in rv])
od = con.execute("SELECT order_date, fund_code, order_type FROM fund_bot_orders "
                 "WHERE order_run_id=? ORDER BY order_id", (rid,)).fetchall()
print("orders:", [dict(r) for r in od])
EOF
```

Expected：存在一行 status='active' 宪章（Day 1 声明）；若 smoke 期内触发 cadence，有 reviews 行；订单未违反结构（人工目检：核心/卫星分布）。

- [ ] **Step 4: 检查 run 日志中的宪章交互**

```bash
grep -l 'declare_charter\|宪章' /home/rooot/agent_invest_lab/world/runtime/runs/bot105d-smoke10d-*/rl-openclaw/agents/bot105d/sessions/*.jsonl | head -3
```

Expected: Day 1 会话内有 declare_charter 调用且成功；若有拒单，bot 能按报错自我修正（观察后续同日订单）。

- [ ] **Step 5: 记录验证结果**（不 commit 代码；将结论写进 PR/汇报）

后续全量验证（不在本计划内，由用户决定启动）：重跑 daily r2（复制 `world-bot105d-daily-agenticdeep-2025-01-2026-07.yaml` 加 `charter_enforcement: true`、换 run_id），验收"每月 distinct fund_code 数不衰减到 1 + 复评按期落库 + 绩效对照基线不显著劣化"。

---

## Self-Review 结论

- **Spec 覆盖**：§4 宪章模型→Task 1/2；§5 交易闸门→Task 4；§6 复评→Task 3/4/5；§7 改动面→Task 1-8 一一对应（bot 工作区 METHODOLOGY 说明并入 Task 7 的 prompt 块，不再单独改 bots/ 文件——声明指引已在每日 prompt 中，避免双份漂移）；§8 验证→Task 9。
- **偏差**：spec §4"深研日修订"改为"冷却期修订"，Task 8 已把注记写回 spec。
- **类型一致性**：`_charter_gate_buy`/`_load_active_charter`/`_satellite_codes`/`charter_status` JSON 字段在 Task 2-7 间已核对一致；测试数字推演已验算。
- **实现时需现场核对的两处**（已在任务内标注）：cli_tools 测试的 DB 路径注入环境变量名（读 db.py 确认）；MCP 工具名前缀 `mcp__fund_portfolio_mcp__`（grep 现有用法确认）。
