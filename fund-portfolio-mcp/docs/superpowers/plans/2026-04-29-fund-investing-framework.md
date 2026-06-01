# 虚拟人基金投资三层框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有基金直投链路前插入"能力圈宣告"前置阶段(Phase B-1),让每个 bot 必须先声明自己的投资能力圈(A 宏观/B1 行业轮动/B2 单行业深耕/C 个基 alpha),再驱动后续 B0/B/C 的差异化执行。

**Architecture:** 数据库层加 2 张新表 + 3 列 paradigm 字段;MCP 层加 4 个新工具暴露读写接口;调度层加 1 个 Phase B-1 脚本(读 yaml frontmatter → 闸门 → 范式选择 → 写库);文档层更新 3 个 skill 的 SKILL.md + 1 份链路文档;数据层为 8 个基金 bot 写初版 `能力圈宣告.md`。

**Tech Stack:** Python 3.12, sqlite3, FastMCP (现有 fund-portfolio-mcp), PyYAML, pytest。

**Git policy:** 计划包含建议的 commit 步骤;实际执行时由用户决定何时 commit(用户明确要求"先不用 git" → 执行者应该把 commit 步骤跳过或留到最后批量提交,确认后再做)。

**Spec:** `docs/superpowers/specs/2026-04-29-fund-investing-framework-design.md`

---

## File Structure

### 创建

| 文件 | 责任 |
|---|---|
| `/home/rooot/.openclaw/scripts/fund_capability_lib.py` | yaml frontmatter 解析 + 范式选择纯函数(无副作用,纯逻辑层) |
| `/home/rooot/.openclaw/scripts/fund-phase-b1.py` | Phase B-1 调度脚本:读宣告 → 闸门 → 范式选择 → 调 MCP 写库 |
| `/home/rooot/.openclaw/scripts/update-capability-circle.py` | 季度重评脚本(类似 update-investment-strategy-summaries.py) |
| `/home/rooot/.openclaw/scripts/tests/test_fund_capability_lib.py` | 纯函数 unit tests |
| `/home/rooot/.openclaw/scripts/tests/test_fund_phase_b1.py` | Phase B-1 调度 integration tests(用 tempfile sqlite) |
| `/home/rooot/MCP/fund-portfolio-mcp/test_capability_paradigm.py` | MCP 工具 tests(直连 db.py) |
| `/home/rooot/.openclaw/workspace-bot{1,2,4,5,6,7,11,12}/memory/portfolio/fund/能力圈宣告.md` | 8 个 bot 的初版宣告(共 8 个文件) |

### 修改

| 文件 | 修改内容 |
|---|---|
| `/home/rooot/MCP/fund-portfolio-mcp/db.py` | SCHEMA_SQL 加 `fund_capability_circle` + `fund_paradigm_runs` 两张表;新增 `migrate_paradigm_columns()` 给 3 张老表 ALTER 加 `paradigm` 列 |
| `/home/rooot/MCP/fund-portfolio-mcp/server.py` | 加 4 个 `@mcp.tool()`: `save_capability_circle`, `get_capability_circle`, `save_paradigm_run`, `get_latest_paradigm` |
| `/home/rooot/.openclaw/workspace/skills/portfolio/market-context/SKILL.md` | 新增"范式参数"段:读 latest paradigm,按范式调整输出重点 |
| `/home/rooot/.openclaw/workspace/skills/portfolio/fund-match/SKILL.md` | 新增"范式参数"段:读 latest paradigm,按范式调整选基逻辑 |
| `/home/rooot/.openclaw/workspace/skills/portfolio/fund-review/SKILL.md` | 新增"范式参数"段:读 latest paradigm,按范式调整巡检逻辑 |
| `/home/rooot/.openclaw/workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md` | 在 Phase A.5 与 Phase B0 之间插入"Phase B-1 能力圈宣告"章节 |

---

## Tasks

### Task 1: DB schema — 新增 2 张表 + 老表 ALTER

**Files:**
- Modify: `/home/rooot/MCP/fund-portfolio-mcp/db.py`(SCHEMA_SQL 末尾追加;`init_db()` 调用迁移)
- Test: `/home/rooot/MCP/fund-portfolio-mcp/test_capability_paradigm.py`

- [ ] **Step 1.1: 写 schema 迁移测试**

创建 `/home/rooot/MCP/fund-portfolio-mcp/test_capability_paradigm.py`:

```python
"""Tests for capability_circle / paradigm_runs tables and MCP tools."""
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


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_names(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_init_db_creates_capability_circle_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    assert _table_exists(conn, "fund_capability_circle")
    cols = _column_names(conn, "fund_capability_circle")
    expected = {
        "circle_id", "bot_id", "as_of_date", "macro", "industry_rotation",
        "industry_focus", "fund_alpha", "default_paradigm",
        "secondary_paradigm", "switch_rules_json", "evidence_md",
        "next_assessment_due", "created_at",
    }
    assert expected.issubset(set(cols)), f"missing: {expected - set(cols)}"
    conn.close()


def test_init_db_creates_paradigm_runs_table(tmp_db):
    conn = sqlite3.connect(tmp_db)
    assert _table_exists(conn, "fund_paradigm_runs")
    cols = _column_names(conn, "fund_paradigm_runs")
    expected = {
        "paradigm_run_id", "bot_id", "trade_date", "run_id",
        "paradigm_active", "capability_field", "capability_value",
        "switched_from", "reason", "created_at",
    }
    assert expected.issubset(set(cols))
    conn.close()


def test_init_db_adds_paradigm_column_to_existing_tables(tmp_db):
    conn = sqlite3.connect(tmp_db)
    for table in ("fund_bot_reviews", "fund_bot_actions", "fund_allocation_runs"):
        cols = _column_names(conn, table)
        assert "paradigm" in cols, f"{table} missing paradigm column"
    conn.close()


def test_migration_idempotent_on_existing_db(tmp_db):
    """Re-running init_db on a populated DB must not fail or duplicate columns."""
    import db as db_mod
    # Insert a row to simulate "existing data"
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO fund_bot_reviews (bot_id, review_date, regime, decision) "
        "VALUES (?, ?, ?, ?)", ("bot7", "2026-04-29", "range", "KEEP")
    )
    conn.commit()
    conn.close()
    # Re-init should be a no-op
    db_mod.init_db()
    conn = sqlite3.connect(tmp_db)
    cnt = conn.execute("SELECT COUNT(*) FROM fund_bot_reviews").fetchone()[0]
    assert cnt == 1
    conn.close()
```

- [ ] **Step 1.2: 跑测试,确认全部失败**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py -v 2>&1 | head -30
```

Expected: 4 test failures (`fund_capability_circle does not exist` / `fund_paradigm_runs does not exist` / `paradigm missing`).

- [ ] **Step 1.3: 修改 db.py SCHEMA_SQL,加两张新表**

在 `/home/rooot/MCP/fund-portfolio-mcp/db.py` 中,在 "-- 16. 选品漏斗追踪(基金特有)" 段(第 280-293 行)之后、"-- 索引" 段之前插入:

```sql
-- ============================================================
-- 三层框架表(2026-04-29 新增)
-- ============================================================

-- 17. 能力圈宣告快照(每次重评写一行,以 (bot_id, as_of_date) 唯一)
CREATE TABLE IF NOT EXISTS fund_capability_circle (
    circle_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id                 TEXT NOT NULL,
    as_of_date             TEXT NOT NULL,
    macro                  INTEGER NOT NULL DEFAULT 0,        -- 0/1 bool
    industry_rotation      INTEGER NOT NULL DEFAULT 0,
    industry_focus         TEXT,                              -- 行业名 or NULL
    fund_alpha             INTEGER NOT NULL DEFAULT 0,
    default_paradigm       TEXT,                              -- A | B1 | B2 | C
    secondary_paradigm     TEXT,                              -- 复合能力圈的辅助范式
    switch_rules_json      TEXT,                              -- bot 自写的切换规则 JSON 数组
    evidence_md            TEXT,                              -- 自评叙述 markdown
    next_assessment_due    TEXT,                              -- 下次重评日期
    created_at             TEXT DEFAULT (datetime('now')),
    UNIQUE (bot_id, as_of_date)
);

-- 18. 每日 Phase B-1 输出
CREATE TABLE IF NOT EXISTS fund_paradigm_runs (
    paradigm_run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id                 TEXT NOT NULL,
    trade_date             TEXT NOT NULL,
    run_id                 TEXT,
    paradigm_active        TEXT NOT NULL,                     -- A | B1 | B2 | C | SKIP
    capability_field       TEXT,                              -- macro/industry_rotation/industry_focus/fund_alpha
    capability_value       TEXT,                              -- "true" | 行业名
    switched_from          TEXT,                              -- 上一日范式(若切换)
    reason                 TEXT,                              -- 切换原因 or "single_capability_auto" or "no_capability_skip"
    created_at             TEXT DEFAULT (datetime('now')),
    UNIQUE (bot_id, trade_date)
);
```

并在 "-- 索引" 段加上:

```sql
CREATE INDEX IF NOT EXISTS idx_fund_capability_bot_date ON fund_capability_circle(bot_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_fund_paradigm_bot_date ON fund_paradigm_runs(bot_id, trade_date);
```

- [ ] **Step 1.4: 在 db.py 加 paradigm 列迁移函数**

替换 `init_db()` 函数为:

```python
def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate_paradigm_columns(conn):
    """给 3 张老表加 paradigm 列(如果未加过)。SQLite 不支持 IF NOT EXISTS for ADD COLUMN。"""
    for table in ("fund_bot_reviews", "fund_bot_actions", "fund_allocation_runs"):
        if not _column_exists(conn, table, "paradigm"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN paradigm TEXT")


def init_db():
    """创建数据库和所有表,并执行增量迁移。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _migrate_paradigm_columns(conn)
    conn.commit()
    conn.close()
```

- [ ] **Step 1.5: 跑测试,确认全部通过**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py -v 2>&1 | tail -20
```

Expected: 4 passed.

- [ ] **Step 1.6: 应用到生产 fund.db**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 db.py
```

Expected output: `Database initialized at /home/rooot/.openclaw/data/fund.db`

验证:
```bash
sqlite3 /home/rooot/.openclaw/data/fund.db ".schema fund_capability_circle" | head -5
sqlite3 /home/rooot/.openclaw/data/fund.db "PRAGMA table_info(fund_bot_reviews)" | grep paradigm
```

Expected: 第一个返回 CREATE TABLE 语句,第二个返回 `XX|paradigm|TEXT|0||0`。

- [ ] **Step 1.7: Commit**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp
git add db.py test_capability_paradigm.py
git commit -m "fund.db: add capability_circle + paradigm_runs tables, paradigm column migration"
```

---

### Task 2: MCP 工具 — capability_circle 读写

**Files:**
- Modify: `/home/rooot/MCP/fund-portfolio-mcp/server.py`
- Test: `/home/rooot/MCP/fund-portfolio-mcp/test_capability_paradigm.py`(扩展)

- [ ] **Step 2.1: 加 MCP 工具 tests**

在 `test_capability_paradigm.py` 末尾追加:

```python
import asyncio
import importlib


@pytest.fixture
def reload_server(tmp_db):
    """server.py 在 import 时绑定 db_path,需要在 monkeypatch 后 reload。"""
    import server
    importlib.reload(server)
    return server


def test_save_and_get_capability_circle(reload_server):
    s = reload_server
    payload = {
        "bot_id": "bot7",
        "as_of_date": "2026-04-29",
        "macro": False,
        "industry_rotation": False,
        "industry_focus": "科技",
        "fund_alpha": True,
        "default_paradigm": "B2",
        "secondary_paradigm": "C",
        "switch_rules_json": '[{"trigger":"科技拥挤>95%","from":"B2","to":"C"}]',
        "evidence_md": "## B2 · 科技\n- 跟踪 8 个月\n",
        "next_assessment_due": "2026-07-29",
    }
    result = asyncio.run(s.save_capability_circle(**payload))
    assert '"success": true' in result
    got = asyncio.run(s.get_capability_circle(bot_id="bot7"))
    assert "industry_focus" in got and "科技" in got
    assert "B2" in got


def test_get_capability_circle_returns_none_when_missing(reload_server):
    s = reload_server
    out = asyncio.run(s.get_capability_circle(bot_id="bot999"))
    assert '"found": false' in out


def test_save_capability_circle_rejects_b1_b2_conflict(reload_server):
    s = reload_server
    out = asyncio.run(s.save_capability_circle(
        bot_id="bot7", as_of_date="2026-04-29",
        macro=False, industry_rotation=True, industry_focus="科技",
        fund_alpha=False,
        default_paradigm="B1",
    ))
    assert '"success": false' in out
    assert "B1" in out and "B2" in out  # error mentions the conflict
```

- [ ] **Step 2.2: 跑测试,确认失败**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py::test_save_and_get_capability_circle -v 2>&1 | tail -10
```

Expected: AttributeError or similar — `save_capability_circle` not defined.

- [ ] **Step 2.3: 在 server.py 末尾追加 capability_circle 工具**

在 `server.py` 中 `def main():` 之前(约第 1535 行)插入:

```python
# ============================================================
# H. 能力圈宣告与 Phase B-1 范式(2026-04-29 三层框架)
# ============================================================

@mcp.tool()
async def save_capability_circle(
    bot_id: str,
    as_of_date: str,
    macro: bool = False,
    industry_rotation: bool = False,
    industry_focus: str = "",
    fund_alpha: bool = False,
    default_paradigm: str = "",
    secondary_paradigm: str = "",
    switch_rules_json: str = "",
    evidence_md: str = "",
    next_assessment_due: str = "",
) -> str:
    """保存(或覆盖)某 bot 在某 as_of_date 的能力圈宣告。

    互斥规则: industry_rotation=true 时 industry_focus 必须为空;反之亦然。
    至少有一项能力为真,否则在写入时仍会接受(用于"无能力圈"边界场景),
    但 paradigm_active 在 Phase B-1 会输出 SKIP。
    """
    # 互斥校验
    has_b1 = bool(industry_rotation)
    has_b2 = bool(industry_focus.strip())
    if has_b1 and has_b2:
        return json.dumps({
            "success": False,
            "message": "B1 (industry_rotation=true) 与 B2 (industry_focus 非空) 互斥,不能同时声明",
        }, ensure_ascii=False)

    focus = industry_focus.strip() or None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_capability_circle "
            "(bot_id, as_of_date, macro, industry_rotation, industry_focus, "
            " fund_alpha, default_paradigm, secondary_paradigm, switch_rules_json, "
            " evidence_md, next_assessment_due) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, as_of_date) DO UPDATE SET "
            " macro=excluded.macro, industry_rotation=excluded.industry_rotation, "
            " industry_focus=excluded.industry_focus, fund_alpha=excluded.fund_alpha, "
            " default_paradigm=excluded.default_paradigm, "
            " secondary_paradigm=excluded.secondary_paradigm, "
            " switch_rules_json=excluded.switch_rules_json, "
            " evidence_md=excluded.evidence_md, "
            " next_assessment_due=excluded.next_assessment_due",
            (bot_id, as_of_date, int(macro), int(industry_rotation), focus,
             int(fund_alpha), default_paradigm or None, secondary_paradigm or None,
             switch_rules_json or None, evidence_md or None,
             next_assessment_due or None)
        )
    return json.dumps({"success": True, "bot_id": bot_id, "as_of_date": as_of_date},
                      ensure_ascii=False)


@mcp.tool()
async def get_capability_circle(bot_id: str, as_of_date: str = "") -> str:
    """读取 bot 最新(或指定日期)的能力圈宣告。"""
    with get_conn() as conn:
        if as_of_date:
            row = conn.execute(
                "SELECT * FROM fund_capability_circle "
                "WHERE bot_id = ? AND as_of_date = ?",
                (bot_id, as_of_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fund_capability_circle WHERE bot_id = ? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (bot_id,)
            ).fetchone()
    if not row:
        return json.dumps({"found": False, "bot_id": bot_id}, ensure_ascii=False)
    d = dict(row)
    d["macro"] = bool(d["macro"])
    d["industry_rotation"] = bool(d["industry_rotation"])
    d["fund_alpha"] = bool(d["fund_alpha"])
    return json.dumps({"found": True, **d}, ensure_ascii=False)
```

- [ ] **Step 2.4: 跑 capability_circle 测试,确认通过**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py -v -k "capability" 2>&1 | tail -15
```

Expected: 3 capability tests passed.

- [ ] **Step 2.5: Commit**

```bash
git add server.py test_capability_paradigm.py
git commit -m "fund-portfolio-mcp: add save/get_capability_circle tools"
```

---

### Task 3: MCP 工具 — paradigm_runs 读写

**Files:**
- Modify: `/home/rooot/MCP/fund-portfolio-mcp/server.py`
- Test: `/home/rooot/MCP/fund-portfolio-mcp/test_capability_paradigm.py`(扩展)

- [ ] **Step 3.1: 加 paradigm 工具 tests**

在 `test_capability_paradigm.py` 末尾追加:

```python
def test_save_and_get_paradigm_run(reload_server):
    s = reload_server
    out = asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        run_id="cron-2026-04-29",
        paradigm_active="B2",
        capability_field="industry_focus",
        capability_value="科技",
        reason="single_capability_auto",
    ))
    assert '"success": true' in out
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot7"))
    assert '"paradigm_active": "B2"' in got
    assert "科技" in got


def test_save_paradigm_run_idempotent_same_day(reload_server):
    """Same (bot_id, trade_date) 写第二次应该 UPDATE 而不是 INSERT。"""
    s = reload_server
    asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        paradigm_active="B2", capability_field="industry_focus",
        capability_value="科技", reason="initial",
    ))
    asyncio.run(s.save_paradigm_run(
        bot_id="bot7", trade_date="2026-04-29",
        paradigm_active="C", capability_field="fund_alpha",
        capability_value="true", reason="switched", switched_from="B2",
    ))
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot7"))
    assert '"paradigm_active": "C"' in got
    assert '"switched_from": "B2"' in got


def test_get_latest_paradigm_when_none(reload_server):
    s = reload_server
    out = asyncio.run(s.get_latest_paradigm(bot_id="bot999"))
    assert '"found": false' in out


def test_save_paradigm_run_with_skip(reload_server):
    s = reload_server
    out = asyncio.run(s.save_paradigm_run(
        bot_id="bot999", trade_date="2026-04-29",
        paradigm_active="SKIP", reason="no_capability_skip",
    ))
    assert '"success": true' in out
    got = asyncio.run(s.get_latest_paradigm(bot_id="bot999"))
    assert '"paradigm_active": "SKIP"' in got
```

- [ ] **Step 3.2: 跑测试,确认失败**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py -v -k "paradigm_run" 2>&1 | tail -10
```

Expected: AttributeError — `save_paradigm_run` not defined.

- [ ] **Step 3.3: 在 server.py 加 paradigm_runs 工具**

在 Task 2 加的 `get_capability_circle` 之后追加:

```python
@mcp.tool()
async def save_paradigm_run(
    bot_id: str,
    trade_date: str,
    paradigm_active: str,
    run_id: str = "",
    capability_field: str = "",
    capability_value: str = "",
    switched_from: str = "",
    reason: str = "",
) -> str:
    """保存某 bot 某日的 Phase B-1 输出。

    paradigm_active: A | B1 | B2 | C | SKIP
    capability_field: macro | industry_rotation | industry_focus | fund_alpha
    """
    valid = {"A", "B1", "B2", "C", "SKIP"}
    if paradigm_active not in valid:
        return json.dumps({
            "success": False,
            "message": f"paradigm_active 必须是 {sorted(valid)} 之一,当前: {paradigm_active}",
        }, ensure_ascii=False)
    trade_date = _normalize_trade_date(trade_date)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fund_paradigm_runs "
            "(bot_id, trade_date, run_id, paradigm_active, capability_field, "
            " capability_value, switched_from, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, trade_date) DO UPDATE SET "
            " run_id=excluded.run_id, paradigm_active=excluded.paradigm_active, "
            " capability_field=excluded.capability_field, "
            " capability_value=excluded.capability_value, "
            " switched_from=excluded.switched_from, reason=excluded.reason",
            (bot_id, trade_date, run_id or None, paradigm_active,
             capability_field or None, capability_value or None,
             switched_from or None, reason or None)
        )
    return json.dumps({
        "success": True, "bot_id": bot_id, "trade_date": trade_date,
        "paradigm_active": paradigm_active,
    }, ensure_ascii=False)


@mcp.tool()
async def get_latest_paradigm(bot_id: str, trade_date: str = "") -> str:
    """读取 bot 最近一日(或指定 trade_date)的 paradigm。"""
    with get_conn() as conn:
        if trade_date:
            row = conn.execute(
                "SELECT * FROM fund_paradigm_runs "
                "WHERE bot_id = ? AND trade_date = ?",
                (bot_id, trade_date)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fund_paradigm_runs WHERE bot_id = ? "
                "ORDER BY trade_date DESC LIMIT 1",
                (bot_id,)
            ).fetchone()
    if not row:
        return json.dumps({"found": False, "bot_id": bot_id}, ensure_ascii=False)
    return json.dumps({"found": True, **dict(row)}, ensure_ascii=False)
```

- [ ] **Step 3.4: 跑测试,确认通过**

```bash
cd /home/rooot/MCP/fund-portfolio-mcp && python3 -m pytest test_capability_paradigm.py -v 2>&1 | tail -20
```

Expected: 11 passed (4 schema + 3 capability_circle + 4 paradigm_runs)。

- [ ] **Step 3.5: 重启 fund-portfolio-mcp 服务并验证工具暴露**

```bash
lsof -ti:18071 | xargs kill 2>/dev/null
cd /home/rooot/MCP/fund-portfolio-mcp && nohup python3 server.py --transport streamable-http --port 18071 > /tmp/fund-mcp.log 2>&1 &
sleep 2
curl -s http://localhost:18071/health 2>/dev/null || echo "no health endpoint, check log"
tail -20 /tmp/fund-mcp.log
```

Expected: 服务启动成功,日志里看到所有工具注册。

- [ ] **Step 3.6: Commit**

```bash
git add server.py test_capability_paradigm.py
git commit -m "fund-portfolio-mcp: add save/get_paradigm_run tools"
```

---

### Task 4: 能力圈解析与范式选择库 (`fund_capability_lib.py`)

纯函数库:解析 yaml frontmatter、应用闸门规则、做范式选择。无 IO 副作用,易于单测。

**Files:**
- Create: `/home/rooot/.openclaw/scripts/fund_capability_lib.py`
- Create: `/home/rooot/.openclaw/scripts/tests/__init__.py`(空文件,让 pytest 识别 tests 目录)
- Create: `/home/rooot/.openclaw/scripts/tests/test_fund_capability_lib.py`

- [ ] **Step 4.1: 创建空目录 + tests 包**

```bash
mkdir -p /home/rooot/.openclaw/scripts/tests
touch /home/rooot/.openclaw/scripts/tests/__init__.py
```

- [ ] **Step 4.2: 写 unit tests**

创建 `/home/rooot/.openclaw/scripts/tests/test_fund_capability_lib.py`:

```python
"""Unit tests for fund_capability_lib (pure functions, no IO)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fund_capability_lib import (
    parse_capability_md,
    select_paradigm,
    CapabilityCircle,
    GATE_SKIP,
)


VALID_MD = """---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: 科技
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B2
secondary_paradigm: C
switch_rules:
  - trigger: 科技赛道拥挤度持续 3 周 >95%
    from: B2
    to: C
---

## B2 · 科技
- 跟踪 8 个月
"""


def test_parse_returns_capability_circle():
    cc = parse_capability_md(VALID_MD)
    assert isinstance(cc, CapabilityCircle)
    assert cc.macro is False
    assert cc.industry_focus == "科技"
    assert cc.fund_alpha is True
    assert cc.default_paradigm == "B2"
    assert cc.secondary_paradigm == "C"
    assert len(cc.switch_rules) == 1
    assert cc.switch_rules[0]["from"] == "B2"


def test_parse_b1_b2_mutex_violation_raises():
    md = VALID_MD.replace("industry_rotation: false", "industry_rotation: true")
    with pytest.raises(ValueError) as e:
        parse_capability_md(md)
    assert "B1" in str(e.value) and "B2" in str(e.value)


def test_parse_missing_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_capability_md("# 没有 frontmatter")


def test_select_paradigm_skip_when_all_false():
    cc = CapabilityCircle(macro=False, industry_rotation=False,
                          industry_focus=None, fund_alpha=False,
                          default_paradigm=None, secondary_paradigm=None,
                          switch_rules=[])
    decision = select_paradigm(cc, current_paradigm=None,
                                last_switch_date=None, today="2026-04-29")
    assert decision.paradigm_active == GATE_SKIP
    assert "no_capability" in decision.reason


def test_select_paradigm_single_capability_auto_b2():
    cc = CapabilityCircle(macro=False, industry_rotation=False,
                          industry_focus="黄金", fund_alpha=False,
                          default_paradigm="B2", secondary_paradigm=None,
                          switch_rules=[])
    decision = select_paradigm(cc, current_paradigm=None,
                                last_switch_date=None, today="2026-04-29")
    assert decision.paradigm_active == "B2"
    assert decision.capability_field == "industry_focus"
    assert decision.capability_value == "黄金"
    assert decision.reason == "single_capability_auto"


def test_select_paradigm_uses_default_when_composite_no_signal():
    cc = CapabilityCircle(macro=False, industry_rotation=False,
                          industry_focus="科技", fund_alpha=True,
                          default_paradigm="B2", secondary_paradigm="C",
                          switch_rules=[])
    decision = select_paradigm(cc, current_paradigm="B2",
                                last_switch_date="2026-03-01", today="2026-04-29")
    assert decision.paradigm_active == "B2"  # 保持当前


def test_select_paradigm_blocks_switch_within_30_days():
    """切换间隔 < 30 天必须保持当前范式,即使 default 不同。"""
    cc = CapabilityCircle(macro=False, industry_rotation=False,
                          industry_focus="科技", fund_alpha=True,
                          default_paradigm="C", secondary_paradigm="B2",
                          switch_rules=[])
    decision = select_paradigm(cc, current_paradigm="B2",
                                last_switch_date="2026-04-15",  # 14 days ago
                                today="2026-04-29")
    assert decision.paradigm_active == "B2"  # 保持
    assert "30" in decision.reason or "cooldown" in decision.reason


def test_select_paradigm_first_run_uses_default():
    cc = CapabilityCircle(macro=False, industry_rotation=False,
                          industry_focus="科技", fund_alpha=True,
                          default_paradigm="B2", secondary_paradigm="C",
                          switch_rules=[])
    decision = select_paradigm(cc, current_paradigm=None,
                                last_switch_date=None, today="2026-04-29")
    assert decision.paradigm_active == "B2"
    assert decision.reason == "first_run_default"
```

- [ ] **Step 4.3: 跑测试,确认全部失败**

```bash
cd /home/rooot/.openclaw && python3 -m pytest scripts/tests/test_fund_capability_lib.py -v 2>&1 | tail -10
```

Expected: ImportError(`fund_capability_lib` 还没建)。

- [ ] **Step 4.4: 实现 `fund_capability_lib.py`**

创建 `/home/rooot/.openclaw/scripts/fund_capability_lib.py`:

```python
"""能力圈解析 + 范式选择 — 纯函数库,无 IO 副作用。

由 fund-phase-b1.py 调用。yaml frontmatter 解析依赖 PyYAML(若不存在,fallback 到简单解析)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError as _:
    yaml = None  # noqa: N816  fallback below


GATE_SKIP = "SKIP"
SWITCH_COOLDOWN_DAYS = 30


@dataclass
class CapabilityCircle:
    macro: bool
    industry_rotation: bool
    industry_focus: Optional[str]
    fund_alpha: bool
    default_paradigm: Optional[str]
    secondary_paradigm: Optional[str]
    switch_rules: list[dict] = field(default_factory=list)
    last_assessment_date: Optional[str] = None
    next_assessment_due: Optional[str] = None

    def is_empty(self) -> bool:
        return not (self.macro or self.industry_rotation or
                    bool(self.industry_focus) or self.fund_alpha)

    def active_paradigms(self) -> list[str]:
        out = []
        if self.macro:
            out.append("A")
        if self.industry_rotation:
            out.append("B1")
        if self.industry_focus:
            out.append("B2")
        if self.fund_alpha:
            out.append("C")
        return out


@dataclass
class ParadigmDecision:
    paradigm_active: str             # A | B1 | B2 | C | SKIP
    capability_field: Optional[str]
    capability_value: Optional[str]
    switched_from: Optional[str]
    reason: str


def parse_capability_md(md_text: str) -> CapabilityCircle:
    """从 markdown 文件内容里抽取 yaml frontmatter,返回 CapabilityCircle。

    Raises ValueError 若无 frontmatter 或互斥规则违反。
    """
    if not md_text.startswith("---"):
        raise ValueError("能力圈宣告 markdown 必须以 yaml frontmatter '---' 开头")
    try:
        end = md_text.index("---", 3)
    except ValueError:
        raise ValueError("找不到 yaml frontmatter 结束 '---'")
    fm = md_text[3:end].strip()

    if yaml is None:
        raise RuntimeError("PyYAML 未安装,请 pip install pyyaml")
    data = yaml.safe_load(fm) or {}

    levels = data.get("capability_levels", {})
    macro = bool(levels.get("macro", False))
    rotation = bool(levels.get("industry_rotation", False))
    focus_raw = levels.get("industry_focus")
    focus = focus_raw.strip() if isinstance(focus_raw, str) and focus_raw.strip() else None
    alpha = bool(levels.get("fund_alpha", False))

    if rotation and focus:
        raise ValueError(
            f"B1(industry_rotation=true) 与 B2(industry_focus={focus!r}) 互斥,不能同时声明"
        )

    return CapabilityCircle(
        macro=macro,
        industry_rotation=rotation,
        industry_focus=focus,
        fund_alpha=alpha,
        default_paradigm=data.get("default_paradigm"),
        secondary_paradigm=data.get("secondary_paradigm"),
        switch_rules=data.get("switch_rules", []) or [],
        last_assessment_date=data.get("last_assessment_date"),
        next_assessment_due=data.get("next_assessment_due"),
    )


def _capability_field_value(cc: CapabilityCircle, paradigm: str) -> tuple[str, str]:
    if paradigm == "A":
        return "macro", "true"
    if paradigm == "B1":
        return "industry_rotation", "true"
    if paradigm == "B2":
        return "industry_focus", cc.industry_focus or ""
    if paradigm == "C":
        return "fund_alpha", "true"
    return "", ""


def _days_between(start_iso: str, end_iso: str) -> int:
    s = datetime.fromisoformat(start_iso).date()
    e = datetime.fromisoformat(end_iso).date()
    return (e - s).days


def select_paradigm(
    cc: CapabilityCircle,
    current_paradigm: Optional[str],
    last_switch_date: Optional[str],
    today: str,
) -> ParadigmDecision:
    """决定本日的 paradigm。

    规则:
    1. 能力圈全空 → SKIP。
    2. 单一能力圈 → 自动选定该范式。
    3. 复合能力圈:
       a. 首次运行(current_paradigm 为 None)→ default_paradigm。
       b. 切换间隔 < 30 天 → 保持 current_paradigm。
       c. 否则保持 current_paradigm(切换由 bot 在策略摘要里写规则,本框架不预设)。

    > 注意:本函数不实现具体的"切换信号"——bot 在策略摘要里写自定切换规则。
    > 框架只保证范式参数被传下去 + 30 天冷却期纪律。
    """
    if cc.is_empty():
        return ParadigmDecision(
            paradigm_active=GATE_SKIP,
            capability_field=None, capability_value=None,
            switched_from=current_paradigm,
            reason="no_capability_skip",
        )

    actives = cc.active_paradigms()

    # 单一能力圈 → 自动
    if len(actives) == 1:
        p = actives[0]
        f, v = _capability_field_value(cc, p)
        return ParadigmDecision(
            paradigm_active=p,
            capability_field=f, capability_value=v,
            switched_from=None,
            reason="single_capability_auto",
        )

    # 复合能力圈
    if current_paradigm is None:
        target = cc.default_paradigm or actives[0]
        f, v = _capability_field_value(cc, target)
        return ParadigmDecision(
            paradigm_active=target,
            capability_field=f, capability_value=v,
            switched_from=None,
            reason="first_run_default",
        )

    # 30 天冷却保持
    if last_switch_date:
        try:
            days = _days_between(last_switch_date, today)
        except Exception:
            days = SWITCH_COOLDOWN_DAYS  # 解析失败按未冷却处理
        if days < SWITCH_COOLDOWN_DAYS:
            f, v = _capability_field_value(cc, current_paradigm)
            return ParadigmDecision(
                paradigm_active=current_paradigm,
                capability_field=f, capability_value=v,
                switched_from=None,
                reason=f"cooldown_keep_{days}/{SWITCH_COOLDOWN_DAYS}d",
            )

    # 默认保持当前(框架不预设切换信号,bot 自己在策略摘要里写)
    f, v = _capability_field_value(cc, current_paradigm)
    return ParadigmDecision(
        paradigm_active=current_paradigm,
        capability_field=f, capability_value=v,
        switched_from=None,
        reason="composite_keep_current",
    )
```

- [ ] **Step 4.5: 安装 PyYAML(若未安装)**

```bash
python3 -c "import yaml" 2>&1 || pip install pyyaml
```

- [ ] **Step 4.6: 跑测试,确认通过**

```bash
cd /home/rooot/.openclaw && python3 -m pytest scripts/tests/test_fund_capability_lib.py -v 2>&1 | tail -15
```

Expected: 7 passed.

- [ ] **Step 4.7: Commit**

```bash
cd /home/rooot/.openclaw
git add scripts/fund_capability_lib.py scripts/tests/test_fund_capability_lib.py scripts/tests/__init__.py
git commit -m "scripts: add fund_capability_lib (yaml frontmatter parser + paradigm selector)"
```

---

### Task 5: Phase B-1 调度脚本 `fund-phase-b1.py`

读 8 个 bot 的 `能力圈宣告.md` → 调 lib 做范式决策 → 用 sqlite3 直连写库(模式与 `fund-phase-d.py` 一致)。

**Files:**
- Create: `/home/rooot/.openclaw/scripts/fund-phase-b1.py`
- Create: `/home/rooot/.openclaw/scripts/tests/test_fund_phase_b1.py`

- [ ] **Step 5.1: 写 integration tests**

创建 `/home/rooot/.openclaw/scripts/tests/test_fund_phase_b1.py`:

```python
"""Integration tests for fund-phase-b1.py — uses tempfile sqlite + fake bot dirs."""
import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


SAMPLE_VALID_MD = """---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: 科技
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B2
secondary_paradigm: C
---
## B2 · 科技
- 跟踪 8 个月
"""

SAMPLE_EMPTY_MD = """---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: null
  fund_alpha: false
---
## (空)
"""


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Build a fake .openclaw/workspace-bot7 with 能力圈宣告.md."""
    root = tmp_path / "openclaw"
    bot_dir = root / "workspace-bot7" / "memory" / "portfolio" / "fund"
    bot_dir.mkdir(parents=True)
    (bot_dir / "能力圈宣告.md").write_text(SAMPLE_VALID_MD, encoding="utf-8")

    bot_empty = root / "workspace-bot999" / "memory" / "portfolio" / "fund"
    bot_empty.mkdir(parents=True)
    (bot_empty / "能力圈宣告.md").write_text(SAMPLE_EMPTY_MD, encoding="utf-8")

    db_path = tmp_path / "fund.db"
    # Init schema by importing fund-portfolio-mcp's db module
    sys.path.insert(0, "/home/rooot/MCP/fund-portfolio-mcp")
    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    db_mod.init_db()

    monkeypatch.setenv("OPENCLAW_ROOT", str(root))
    monkeypatch.setenv("FUND_DB_PATH", str(db_path))
    yield {"root": root, "db": db_path}


def _import_phase_b1():
    spec = importlib.util.spec_from_file_location(
        "fund_phase_b1",
        "/home/rooot/.openclaw/scripts/fund-phase-b1.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_for_bot_writes_paradigm_run(tmp_workspace):
    m = _import_phase_b1()
    result = m.run_for_bot(bot_id="bot7", trade_date="2026-04-29",
                            run_id="test-run-1")
    assert result["paradigm_active"] == "B2"
    conn = sqlite3.connect(str(tmp_workspace["db"]))
    rows = conn.execute(
        "SELECT paradigm_active, capability_field, capability_value, reason "
        "FROM fund_paradigm_runs WHERE bot_id='bot7'"
    ).fetchall()
    conn.close()
    assert rows == [("B2", "industry_focus", "科技", "first_run_default")]


def test_run_for_bot_skip_when_no_capability(tmp_workspace):
    m = _import_phase_b1()
    result = m.run_for_bot(bot_id="bot999", trade_date="2026-04-29",
                            run_id="test-run-2")
    assert result["paradigm_active"] == "SKIP"
    conn = sqlite3.connect(str(tmp_workspace["db"]))
    row = conn.execute(
        "SELECT paradigm_active, reason FROM fund_paradigm_runs "
        "WHERE bot_id='bot999'"
    ).fetchone()
    conn.close()
    assert row == ("SKIP", "no_capability_skip")


def test_run_for_bot_missing_file_returns_skip(tmp_workspace):
    m = _import_phase_b1()
    result = m.run_for_bot(bot_id="bot42", trade_date="2026-04-29",
                            run_id="test-run-3")
    assert result["paradigm_active"] == "SKIP"
    assert "missing_declaration" in result["reason"]


def test_run_for_bot_keeps_current_within_cooldown(tmp_workspace):
    m = _import_phase_b1()
    # First write a previous paradigm 14 days ago
    conn = sqlite3.connect(str(tmp_workspace["db"]))
    conn.execute(
        "INSERT INTO fund_paradigm_runs (bot_id, trade_date, paradigm_active, "
        "capability_field, capability_value, reason) VALUES (?, ?, ?, ?, ?, ?)",
        ("bot7", "2026-04-15", "C", "fund_alpha", "true", "first_run_default")
    )
    conn.commit()
    conn.close()
    result = m.run_for_bot(bot_id="bot7", trade_date="2026-04-29",
                            run_id="test-run-4")
    assert result["paradigm_active"] == "C"
    assert "cooldown" in result["reason"]
```

- [ ] **Step 5.2: 跑测试,确认失败**

```bash
cd /home/rooot/.openclaw && python3 -m pytest scripts/tests/test_fund_phase_b1.py -v 2>&1 | tail -10
```

Expected: ImportError — `fund-phase-b1.py` 不存在。

- [ ] **Step 5.3: 实现 `fund-phase-b1.py`**

创建 `/home/rooot/.openclaw/scripts/fund-phase-b1.py`:

```python
#!/usr/bin/env python3
"""Phase B-1 — 能力圈宣告闸门 + 范式选择。

执行流程(per bot):
  1. 读 workspace-{bot}/memory/portfolio/fund/能力圈宣告.md
  2. 解析 yaml frontmatter → CapabilityCircle
  3. 查 fund_paradigm_runs 取上一日 paradigm + 上次切换日期
  4. select_paradigm → ParadigmDecision
  5. 写 fund_paradigm_runs (UPSERT by bot_id+trade_date)

CLI:
  python3 fund-phase-b1.py --trade-date 2026-04-29 --run-id cron-2026-04-29
  python3 fund-phase-b1.py --bot bot7 --trade-date 2026-04-29 --dry-run

环境变量:
  OPENCLAW_ROOT     默认 /home/rooot/.openclaw
  FUND_DB_PATH      默认 /home/rooot/.openclaw/data/fund.db

写库时直接 sqlite3 直连(与 fund-phase-d.py 一致),不依赖 MCP 服务可用。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fund_capability_lib import (
    CapabilityCircle,
    GATE_SKIP,
    parse_capability_md,
    select_paradigm,
)


ROOT = Path(os.environ.get("OPENCLAW_ROOT", "/home/rooot/.openclaw"))
DB_PATH = Path(os.environ.get("FUND_DB_PATH", "/home/rooot/.openclaw/data/fund.db"))
FUND_BOT_IDS = ["bot1", "bot2", "bot4", "bot5", "bot6", "bot7", "bot11", "bot12"]

log = logging.getLogger("fund-phase-b1")


def _capability_md_path(bot_id: str) -> Path:
    return ROOT / f"workspace-{bot_id}" / "memory" / "portfolio" / "fund" / "能力圈宣告.md"


def _last_paradigm_state(conn: sqlite3.Connection, bot_id: str, today: str) -> tuple[str | None, str | None]:
    """返回 (current_paradigm, last_switch_date)。

    current_paradigm: 上一日的 paradigm_active(若有)
    last_switch_date: 最近一次 switched_from 非 NULL 的 trade_date(若从未切换,用最早 trade_date)
    """
    row = conn.execute(
        "SELECT paradigm_active FROM fund_paradigm_runs "
        "WHERE bot_id = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
        (bot_id, today)
    ).fetchone()
    current = row[0] if row else None

    row2 = conn.execute(
        "SELECT trade_date FROM fund_paradigm_runs "
        "WHERE bot_id = ? AND switched_from IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        (bot_id,)
    ).fetchone()
    if row2:
        last_switch = row2[0]
    else:
        # No prior switch — use earliest record date (proxy for "first declared")
        row3 = conn.execute(
            "SELECT MIN(trade_date) FROM fund_paradigm_runs WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()
        last_switch = row3[0] if row3 and row3[0] else None
    return current, last_switch


def _write_paradigm_run(
    conn: sqlite3.Connection,
    bot_id: str,
    trade_date: str,
    run_id: str,
    paradigm_active: str,
    capability_field: str | None,
    capability_value: str | None,
    switched_from: str | None,
    reason: str,
) -> None:
    conn.execute(
        "INSERT INTO fund_paradigm_runs "
        "(bot_id, trade_date, run_id, paradigm_active, capability_field, "
        " capability_value, switched_from, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(bot_id, trade_date) DO UPDATE SET "
        " run_id=excluded.run_id, paradigm_active=excluded.paradigm_active, "
        " capability_field=excluded.capability_field, "
        " capability_value=excluded.capability_value, "
        " switched_from=excluded.switched_from, reason=excluded.reason",
        (bot_id, trade_date, run_id or None, paradigm_active,
         capability_field, capability_value, switched_from, reason)
    )


def run_for_bot(bot_id: str, trade_date: str, run_id: str = "",
                dry_run: bool = False) -> dict:
    """执行一个 bot 的 Phase B-1。返回 ParadigmDecision-like dict。"""
    md_path = _capability_md_path(bot_id)
    db_path = Path(os.environ.get("FUND_DB_PATH", str(DB_PATH)))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if not md_path.exists():
        decision = {
            "bot_id": bot_id, "paradigm_active": GATE_SKIP,
            "capability_field": None, "capability_value": None,
            "switched_from": None,
            "reason": "missing_declaration_skip",
        }
        if not dry_run:
            _write_paradigm_run(
                conn, bot_id, trade_date, run_id,
                GATE_SKIP, None, None, None, "missing_declaration_skip"
            )
            conn.commit()
        conn.close()
        return decision

    md_text = md_path.read_text(encoding="utf-8")
    try:
        cc = parse_capability_md(md_text)
    except ValueError as e:
        decision = {
            "bot_id": bot_id, "paradigm_active": GATE_SKIP,
            "capability_field": None, "capability_value": None,
            "switched_from": None,
            "reason": f"parse_error_skip:{e}",
        }
        if not dry_run:
            _write_paradigm_run(
                conn, bot_id, trade_date, run_id,
                GATE_SKIP, None, None, None, decision["reason"]
            )
            conn.commit()
        conn.close()
        return decision

    current, last_switch = _last_paradigm_state(conn, bot_id, trade_date)
    pd = select_paradigm(cc, current_paradigm=current,
                         last_switch_date=last_switch, today=trade_date)

    decision = {
        "bot_id": bot_id, "paradigm_active": pd.paradigm_active,
        "capability_field": pd.capability_field,
        "capability_value": pd.capability_value,
        "switched_from": pd.switched_from,
        "reason": pd.reason,
    }
    if not dry_run:
        _write_paradigm_run(
            conn, bot_id, trade_date, run_id,
            pd.paradigm_active, pd.capability_field, pd.capability_value,
            pd.switched_from, pd.reason,
        )
        conn.commit()
    conn.close()
    return decision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--bot", help="只跑某个 bot;不指定则全部 8 个")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    bots = [args.bot] if args.bot else FUND_BOT_IDS
    results = []
    for bot in bots:
        try:
            r = run_for_bot(bot, args.trade_date, args.run_id, args.dry_run)
            results.append(r)
            log.info("[%s] paradigm=%s reason=%s",
                     bot, r["paradigm_active"], r["reason"])
        except Exception as e:
            log.exception("[%s] failed: %s", bot, e)
            results.append({"bot_id": bot, "paradigm_active": "ERROR", "reason": str(e)})

    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r["paradigm_active"] == "ERROR"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5.4: 跑测试,确认通过**

```bash
cd /home/rooot/.openclaw && python3 -m pytest scripts/tests/test_fund_phase_b1.py -v 2>&1 | tail -15
```

Expected: 4 passed.

- [ ] **Step 5.5: dry-run 验证(用真实 fund.db 但不实际写)**

> 此步在 Task 7(写入 8 bot 文件)之后才能完整跑通。这里只验证脚本不崩。

```bash
cd /home/rooot/.openclaw
python3 scripts/fund-phase-b1.py --trade-date 2026-04-29 --bot bot7 --dry-run 2>&1 | tail -10
```

Expected: 输出 JSON,要么 `missing_declaration_skip`(若 Task 7 还没跑),要么 paradigm 决策。脚本 exit 0。

- [ ] **Step 5.6: Commit**

```bash
git add scripts/fund-phase-b1.py scripts/tests/test_fund_phase_b1.py
git commit -m "scripts: add fund-phase-b1.py (capability gate + paradigm selection)"
```

---

### Task 6: 三个 skill 的 SKILL.md 添加范式参数说明

不动 skill 的核心逻辑——只在 SKILL.md 顶部添加一段"范式参数处理"说明,让各 skill 知道:每次执行前先查 `fund_paradigm_runs` 拿当日 paradigm,据此调整输出/选品/巡检逻辑;具体如何调整由 skill 内部 + bot 自己的策略摘要决定(框架不预设)。

**Files:**
- Modify: `/home/rooot/.openclaw/workspace/skills/portfolio/market-context/SKILL.md`
- Modify: `/home/rooot/.openclaw/workspace/skills/portfolio/fund-match/SKILL.md`
- Modify: `/home/rooot/.openclaw/workspace/skills/portfolio/fund-review/SKILL.md`

- [ ] **Step 6.1: 给 market-context/SKILL.md 加段**

读 `/home/rooot/.openclaw/workspace/skills/portfolio/market-context/SKILL.md`,定位 frontmatter `---` 末尾后第一个 `#` 标题之前(用 grep 找具体行号),在标题之前插入:

```markdown
## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm:

```sql
SELECT paradigm_active, capability_field, capability_value
FROM fund_paradigm_runs
WHERE bot_id = ? AND trade_date = ?;
```

| paradigm | 输出重点 |
|---|---|
| A 大类资产轮动 | 完整宏观判断 + 大类目标比例(equity/bond/gold/cash) |
| B1 多行业轮动 | 简化宏观 + 各行业相对强弱排序;asset_target 可模糊 |
| B2 单行业深耕 | 简化宏观 + capability_value 指定行业的景气/拥挤度;asset_target 可模糊 |
| C 个基 alpha | 完整宏观判断(必须给 regime,供 Phase C 矩阵的列轴用);asset_target 不强求 |
| SKIP | 该 bot 当日不参与基金链路,跳过 |

具体的"宏观要拆多细"、"行业排名怎么算"、"拥挤度看什么指标"等细节,由本 skill 内部的判断逻辑 + bot 的 `基金投资策略摘要.md` 共同决定;框架只规定输出重点的边界。
```

具体编辑步骤:

```bash
# 找到 frontmatter 闭合行
grep -n "^---" /home/rooot/.openclaw/workspace/skills/portfolio/market-context/SKILL.md | head -3
```

然后用 Edit tool 精确插入:在 frontmatter 闭合 `---` 之后的第一个内容块之前。

- [ ] **Step 6.2: 给 fund-match/SKILL.md 加段**

类似地,在 `fund-match/SKILL.md` 中加:

```markdown
## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm。不同 paradigm 走不同选基逻辑:

| paradigm | 选基逻辑提示 |
|---|---|
| A | 跳过四层漏斗,直接从"大类代表性产品白名单"选 4-7 只(每类资产 1-2 只)。白名单由各 bot 在 `基金投资策略摘要.md` 自维护 |
| B1 | Layer 1 按 bot 自定义的 3-5 个目标行业分别筛选;Layer 2-4 在每个行业内挑业绩 + 经理 |
| B2 | Layer 1 直接锁定 capability_value 行业;Layer 2-4 在该行业内深选 + 配缓冲产品 |
| C | 完全走现有四层漏斗(L1 资产匹配 → L2 风格行业 → L3 业绩 → L4 个性终选) |

具体阈值、行业列表、缓冲产品类型等由 bot 自定;框架只规定 paradigm 必须先读。
```

- [ ] **Step 6.3: 给 fund-review/SKILL.md 加段**

```markdown
## 范式参数(2026-04-29 三层框架)

执行前必须查询当日 paradigm。不同 paradigm 走不同巡检逻辑:

| paradigm | 巡检逻辑提示 |
|---|---|
| A | **不走单基金矩阵**:只看大类偏离度,触发条件是宏观信号变化或大类偏离 >5%。单基金不做止盈止损 |
| B1 | 单基金矩阵 + 行业相对强弱排序叠加:某行业排名跌出前列 → 减仓该行业 |
| B2 | 单基金矩阵 + 主战行业拥挤度/景气度叠加:行业拥挤过高 → 即使单基金状态 HOLD 也减仓到下限 |
| C | 完全走现有 Phase C 矩阵(产品 × regime → 矩阵 → 动能修饰 → 人设偏移) |

具体阈值、信号来源等由 bot 自定;框架只规定 paradigm 决定走哪条主分支。
```

- [ ] **Step 6.4: 验证三个 skill 可被 grep 到新段**

```bash
for f in /home/rooot/.openclaw/workspace/skills/portfolio/{market-context,fund-match,fund-review}/SKILL.md; do
  echo "=== $f ==="
  grep -A1 "范式参数(2026-04-29" "$f" | head -3
done
```

Expected: 每个文件都能匹配到。

- [ ] **Step 6.5: Commit**

```bash
cd /home/rooot/.openclaw
git add workspace/skills/portfolio/{market-context,fund-match,fund-review}/SKILL.md
git commit -m "skills: add paradigm parameter section to market-context/fund-match/fund-review"
```

---

### Task 7: 8 个 bot 写初版 `能力圈宣告.md`

按各 bot 现有的 `投资策略摘要.md` + 投顾文档里的 8 bot 表格,起草初版。**这是首次落地,内容偏保守 — 季度重评时由 bot 自己用证据加强或收窄。**

**Files:**
- Create: `/home/rooot/.openclaw/workspace-bot{1,2,4,5,6,7,11,12}/memory/portfolio/fund/能力圈宣告.md`

- [ ] **Step 7.1: 写一个共享模板字符串**

定义模板(直接写在每个 bot 文件里,不抽公共文件 — bot 后续会按需自定义):

```markdown
---
capability_levels:
  macro: <BOOL>
  industry_rotation: <BOOL>
  industry_focus: <NULL_OR_INDUSTRY_NAME>
  fund_alpha: <BOOL>
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: <PARADIGM>
secondary_paradigm: <NULL_OR_PARADIGM>
switch_rules: []
---

# {{BotName}} 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

<填具体内容>

## 范式选择规则

<bot 自己写的切换规则,首次可留空>

## 季度复盘提示

每季度第一个工作日由 update-capability-circle 脚本触发,bot 基于本季度的:
- `fund_bot_reviews` / `fund_bot_actions`(投资动作和结论)
- 日记和研究记忆
- 产品业绩验证

审视:是否有新增能力?是否有被证伪的能力?写进新版 `能力圈宣告.md`。
```

- [ ] **Step 7.2: 为每个 bot 写具体内容**

按以下初版决策矩阵(基于 `workspace/skills/portfolio/_shared_docs/tougu/虚拟人投顾流程与Skill总览.md` 的 8 bot 表格):

| bot | macro | rotation | focus | alpha | default | secondary | 备注 |
|---|---|---|---|---|---|---|---|
| bot1 来财妹妹 | true | false | null | true | A | C | 全天候/慢慢投是宏观能力的体现;指增/慢慢投也偏 alpha |
| bot2 狗哥 | false | false | "科技、媒体、通信" | true | B2 | C | 主业是 TMT 单一主题深耕 |
| bot4 研报阿泽 | false | true | null | true | B1 | C | 多赛道成长轮动 + 研报选基 |
| bot5 宣妈慢慢变富 | false | false | "黄金" | true | C | B2 | 主业是 alpha(固收+/低波);黄金深耕作辅助 |
| bot6 James | true | false | null | true | A | C | 多元基金超市 + 三条腿配置 |
| bot7 老K | false | false | "科技" | true | B2 | C | 科技深耕 + 指增 alpha |
| bot11 小奶龙 | false | true | null | true | B1 | C | 成长赛道轮动 + 宽基量化 |
| bot12 小天爱黄金 | false | false | "黄金" | false | B2 | null | 单一黄金能力,无 alpha 选基能力 |

> **重要**: 这些是基于现有摘要的**推测**,bot 上线后首个季度会用真实证据校准。脚本写出后,**应让用户先 review 这张表再批量写文件**。

逐个 bot 写文件:

**bot1** (`/home/rooot/.openclaw/workspace-bot1/memory/portfolio/fund/能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: true
  industry_rotation: false
  industry_focus: null
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: A
secondary_paradigm: C
switch_rules: []
---

# 来财妹妹 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### A 宏观周期把握 ✓
**自评判断**: 全天候/慢慢投策略是宏观周期能力的体现 — 通过股/债/金/现金的固定权重对冲不同周期。
**首次落地证据**:(等季度重评时由 bot 自己根据这一季度的实际宏观判断记录加强)

### C 个基 alpha ✓
**自评判断**: 长期偏好均衡型 + 慢慢投系列产品,选品上表现为对低波 alpha 产品的偏好。
**首次落地证据**:(等季度重评时由 bot 自己用具体被验证的产品选择加强)

## 范式选择规则

首次落地默认走范式 A(大类资产轮动);若发现某个产品的 alpha 显著优于大类配置贡献,可由 bot 自己写规则切换到 C。

## 季度复盘提示

[同模板,略]
```

**bot2** (`workspace-bot2/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: "科技、媒体、通信"
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B2
secondary_paradigm: C
switch_rules: []
---

# 狗哥说财 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### B2 单行业深耕 · TMT(科技、媒体、通信) ✓
**自评判断**: 长期跟踪 TMT 主题(半导体/AI/算力/消费电子/通信),具备单一行业的产业链 + 估值分位 + 拥挤度判断能力。
**首次落地证据**:(等季度重评时由 bot 自己加强)

### C 个基 alpha ✓
**自评判断**: 在 TMT 内部能识别行业主题基金的经理风格、超额稳定性。
**首次落地证据**:(等季度重评时由 bot 自己加强)

## 范式选择规则

首次落地默认走 B2;若主战行业拥挤过高(由 bot 自己定义阈值),可切换到 C 走更纯粹的 alpha 选基。

## 季度复盘提示
[同模板]
```

**bot4** (`workspace-bot4/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: true
  industry_focus: null
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B1
secondary_paradigm: C
switch_rules: []
---

# 研报阿泽 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### B1 多行业轮动 ✓
**自评判断**: 跟踪科技成长 / 新质生产力 / 储能 / 商业航天 / 半导体设备等多个赛道,基于研报证据做赛道间相对强弱判断。
**首次落地证据**:(等季度重评时由 bot 自己用研报和持仓表现加强)

### C 个基 alpha ✓
**自评判断**: 研报驱动选基,能筛出方向对 + 经理稳的产品。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

默认走 B1 多行业轮动;无新研报证据时可保持当前组合不动。

## 季度复盘提示
[同模板]
```

**bot5** (`workspace-bot5/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: "黄金"
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: C
secondary_paradigm: B2
switch_rules: []
---

# 宣妈慢慢变富 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### C 个基 alpha ✓ (主)
**自评判断**: 主业是低波 alpha(固收+/类货基/低波债),通过产品选择获取稳健收益。
**首次落地证据**:(等季度重评时加强)

### B2 单行业深耕 · 黄金 ✓ (辅)
**自评判断**: 对黄金主题有持续跟踪,但黄金权重在组合里不主导,属于辅助配置。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

默认走 C(个基 alpha 选品挑稳健产品);黄金强势/极端避险情境时可切到 B2。

## 季度复盘提示
[同模板]
```

**bot6** (`workspace-bot6/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: true
  industry_rotation: false
  industry_focus: null
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: A
secondary_paradigm: C
switch_rules: []
---

# 爱理财 James 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### A 宏观周期把握 ✓
**自评判断**: 三条腿(权益/固收+/黄金)配置是大类视角的体现。
**首次落地证据**:(等季度重评时加强)

### C 个基 alpha ✓
**自评判断**: 多元基金超市选品需要 alpha 识别能力。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

默认走 A 大类轮动;识别到优质 alpha 产品时可在卫星仓 + 切换到 C。

## 季度复盘提示
[同模板]
```

**bot7** (`workspace-bot7/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: "科技"
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B2
secondary_paradigm: C
switch_rules: []
---

# 老 K 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### B2 单行业深耕 · 科技 ✓
**自评判断**: 长期跟踪 A 股科技 + 美股科技映射 + 半导体 + 算力,具备行业拥挤度和估值判断能力。
**首次落地证据**:(等季度重评时加强)

### C 个基 alpha ✓
**自评判断**: 中证 500 指增 + 全球多资产 + 海外科技指增的产品 alpha 识别能力。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

默认走 B2;科技拥挤度过高时可切换到 C 走更纯指增组合。

## 季度复盘提示
[同模板]
```

**bot11** (`workspace-bot11/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: true
  industry_focus: null
  fund_alpha: true
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B1
secondary_paradigm: C
switch_rules: []
---

# 小奶龙 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### B1 多行业轮动 ✓
**自评判断**: 成长赛道(科创/创业板/A500/中证 500)+ 量化 + 低波多赛道并行,按相对强弱调权。
**首次落地证据**:(等季度重评时加强)

### C 个基 alpha ✓
**自评判断**: 全指量化和低波固收+ 的产品选择需要 alpha 识别。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

默认走 B1;若赛道整体走弱可压低权益仓位 + 切到 C。

## 季度复盘提示
[同模板]
```

**bot12** (`workspace-bot12/.../能力圈宣告.md`):

```markdown
---
capability_levels:
  macro: false
  industry_rotation: false
  industry_focus: "黄金"
  fund_alpha: false
last_assessment_date: 2026-04-29
next_assessment_due: 2026-07-29
default_paradigm: B2
secondary_paradigm: null
switch_rules: []
---

# 小天爱黄金 能力圈宣告

**首次自评日期**: 2026-04-29
**重评窗口**: 季度(下次 2026-07-29)

## 当前能力圈

### B2 单行业深耕 · 黄金 ✓
**自评判断**: 跟踪央行购金、机构目标价、黄金持仓表现,具备黄金 cycle 的判断能力。
**首次落地证据**:(等季度重评时加强)

## 范式选择规则

只有 B2 单一能力,无切换;若季度复盘时发现具备 alpha 选基能力可加上 C。

## 季度复盘提示
[同模板]
```

- [ ] **Step 7.3: 验证 8 个文件能被 fund-phase-b1.py 解析**

```bash
cd /home/rooot/.openclaw
for b in 1 2 4 5 6 7 11 12; do
  python3 scripts/fund-phase-b1.py --trade-date 2026-04-29 --bot bot$b --dry-run 2>&1 | tail -2
done
```

Expected: 每个 bot 输出一行 JSON,paradigm_active 不是 ERROR/SKIP(SKIP 只对没文件的 bot)。

- [ ] **Step 7.4: 真实运行一次,把数据落到 fund.db**

```bash
cd /home/rooot/.openclaw
python3 scripts/fund-phase-b1.py --trade-date 2026-04-29 --run-id manual-init-2026-04-29 2>&1 | tail -20
```

验证:
```bash
sqlite3 /home/rooot/.openclaw/data/fund.db \
  "SELECT bot_id, paradigm_active, capability_field, capability_value, reason FROM fund_paradigm_runs ORDER BY bot_id"
```

Expected: 8 行,各 bot 的 paradigm 与 Step 7.2 矩阵一致。

- [ ] **Step 7.5: Commit**

```bash
git add workspace-bot{1,2,4,5,6,7,11,12}/memory/portfolio/fund/能力圈宣告.md
git commit -m "fund: initial 能力圈宣告.md for 8 fund-investing bots"
```

---

### Task 8: 季度重评脚本 `update-capability-circle.py`

模式照抄 `update-investment-strategy-summaries.py`(并发 2 + per-bot subprocess)。每季度第一个工作日 cron 触发,prompt 让 bot 基于本季度的投资记录重写 `能力圈宣告.md`。

**Files:**
- Create: `/home/rooot/.openclaw/scripts/update-capability-circle.py`

- [ ] **Step 8.1: 创建脚本**

```python
#!/usr/bin/env python3
"""Quarterly updater for per-bot capability_circle declarations.

每季度第一个工作日触发(cron 配置在 Task 9 文档里写明,实际 cron 接入由运维做)。
对每个有 fund holdings 的 bot,启动 openclaw CLI 让它读自己的近三个月投资记录、研究记忆、
日记,再重写 memory/portfolio/fund/能力圈宣告.md。

并发: MAX_PARALLEL=2,串行批次。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path("/home/rooot/.openclaw")
DB_PATH = ROOT / "data" / "fund.db"
LOG_PATH = ROOT / "logs" / "capability-circle-update.log"
OPENCLAW_BIN = shutil.which("openclaw") or "/home/rooot/.npm-global/bin/openclaw"
BOT_TIMEOUT = 1200      # 单个 bot 最长执行时间(秒) — 比 strategy summary 长
MAX_PARALLEL = 2

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("capability-circle-update")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]
    log.propagate = False


FUND_BOT_IDS = ["bot1", "bot2", "bot4", "bot5", "bot6", "bot7", "bot11", "bot12"]


def declaration_path(bot_id: str) -> Path:
    return ROOT / f"workspace-{bot_id}" / "memory" / "portfolio" / "fund" / "能力圈宣告.md"


def build_prompt(bot_id: str, today: str, quarter_start: str) -> str:
    return f"""请基于这一季度({quarter_start} → {today})的实际投资记录,重写你的 `memory/portfolio/fund/能力圈宣告.md`。

## 重评流程

1. 读以下数据源:
   - 数据库:fund_bot_reviews / fund_bot_actions / fund_paradigm_runs (查 bot_id={bot_id} AND review_date >= '{quarter_start}')
   - memory/diary/(本季度日记)
   - memory/research/(本季度研究记录)
   - memory/portfolio/fund/(基金选择 + 巡检 + 策略摘要)

2. 审视:
   - 当前宣告的每项能力(macro / industry_rotation / industry_focus / fund_alpha)是否有新的 **被验证的证据**?
   - 是否有 **被证伪** 的能力?(如某个行业判断连续多次错误)
   - 是否有 **新增** 能力?(只能由本季度具体证据支持,不能凭空"我学了 XXX")

3. 演进硬规则:
   - 一旦从 false 切到 true,必须列出至少 1 条具体证据(研究记录路径 + 历史预测验证 + 持续跟踪 8 周以上)
   - 一旦从 true 切到 false,必须列出至少 1 条证伪证据(预测连续错误次数、持仓亏损归因)
   - **本季度新增能力**只能在季度重评窗口里加;**收窄能力**任何时候都允许

4. 重写整个 `能力圈宣告.md`(yaml frontmatter + 正文证据);旧版本会被覆盖,如果担心丢失内容可以先备份到 memory/archive/

## 注意

- 框架定义见 `workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md` 中"Phase B-1 能力圈宣告"章节
- 你的 default_paradigm / secondary_paradigm 也要重新评估是否合适
- 写完后,通过 fund-portfolio-mcp 的 `save_capability_circle` 工具同步到数据库

完成后给我一个简短摘要:本季度哪些能力被加强、哪些被收窄、是否变更 default_paradigm。
"""


def run_bot(bot_id: str, today: str, quarter_start: str) -> tuple[str, int, str]:
    prompt = build_prompt(bot_id, today, quarter_start)
    # CLI 模式与 update-investment-strategy-summaries.py 一致
    cmd = [
        OPENCLAW_BIN, "agent",
        "--agent", bot_id,
        "--message", prompt,
        "--thinking", "medium",
        "--timeout", str(BOT_TIMEOUT),
        "--json",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True,
            timeout=BOT_TIMEOUT + 60, encoding="utf-8",
        )
        elapsed = int(time.time() - t0)
        return bot_id, proc.returncode, f"elapsed={elapsed}s\n{proc.stdout[-2000:]}"
    except subprocess.TimeoutExpired:
        return bot_id, -1, f"TIMEOUT after {BOT_TIMEOUT}s"
    except Exception as e:
        return bot_id, -2, f"ERROR: {e}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=date.today().isoformat(),
                        help="重评的 as-of 日期")
    parser.add_argument("--quarter-start", default="",
                        help="季度起点(默认 today - 90d)")
    parser.add_argument("--bot", help="只跑指定 bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 prompt 不调用 openclaw")
    args = parser.parse_args()

    today = args.today
    quarter_start = args.quarter_start or (
        datetime.fromisoformat(today) - timedelta(days=90)
    ).date().isoformat()

    bots = [args.bot] if args.bot else FUND_BOT_IDS
    log.info("Quarterly capability re-eval: today=%s quarter_start=%s bots=%s",
             today, quarter_start, bots)

    if args.dry_run:
        for b in bots:
            print(f"=== {b} ===")
            print(build_prompt(b, today, quarter_start))
        return

    failed = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(run_bot, b, today, quarter_start): b for b in bots}
        for fut in as_completed(futs):
            bot_id, rc, msg = fut.result()
            if rc == 0:
                log.info("[%s] OK\n%s", bot_id, msg)
            else:
                log.error("[%s] rc=%s msg=%s", bot_id, rc, msg)
                failed.append(bot_id)

    if failed:
        log.error("Failed bots: %s", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 8.2: dry-run 验证 prompt 生成**

```bash
cd /home/rooot/.openclaw
python3 scripts/update-capability-circle.py --today 2026-04-29 --bot bot7 --dry-run 2>&1 | head -30
```

Expected: 输出完整 prompt,包含 quarter_start 和重评步骤。

- [ ] **Step 8.3: Commit**

```bash
git add scripts/update-capability-circle.py
git commit -m "scripts: add update-capability-circle.py for quarterly re-evaluation"
```

---

### Task 9: 更新 `基金投资完整链路.md` 文档

在 Phase A.5 与 Phase B0 之间插入 Phase B-1 章节。

**Files:**
- Modify: `/home/rooot/.openclaw/workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md`

- [ ] **Step 9.1: 找到插入点**

```bash
grep -n "^## " /home/rooot/.openclaw/workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md | head -20
```

定位 "## 五、Phase B0" 章节(应该在第 73 行附近),在它之前插入新章节。

- [ ] **Step 9.2: 插入 Phase B-1 章节**

在 "## 五、Phase B0" 之前插入:

```markdown
## 四点五、Phase B-1:能力圈宣告 + 范式选择(2026-04-29 三层框架前置闸门)

> **设计理由与完整规格**见 `docs/superpowers/specs/2026-04-29-fund-investing-framework-design.md`

### 位置

```
Phase A.5 数据新鲜度闸门
─────────────────  ↓ 新增 ↓
Phase B-1 能力圈宣告 + 范式选择     ← 本节
─────────────────  ↑ 新增 ↑
Phase B0 市场环境判断
```

### 做的事

1. 读 `workspace-{bot}/memory/portfolio/fund/能力圈宣告.md` 顶部 yaml frontmatter
2. 闸门:`capability_levels` 全 false/null → 写 `paradigm_active=SKIP`,跳过 B0/B/C
3. 范式选择(per bot):
   - 单能力圈 → 自动绑定该范式
   - 复合能力圈 → 首次走 default_paradigm;后续保持当前(切换信号由 bot 在策略摘要里自定);切换间隔 ≥ 30 天
4. 写入 `fund_paradigm_runs(bot_id, trade_date, paradigm_active, capability_field, capability_value, switched_from, reason)`

### 范式与四种能力圈

| 范式 | 能力圈字段 | 持仓主轴 | 风控决策依据 |
|---|---|---|---|
| A | macro=true | 大类资产 | 宏观信号 → 整组合大类比例重定;不下沉到单基金 |
| B1 | industry_rotation=true | 行业 | 行业相对强弱 → 行业间权重切换 |
| B2 | industry_focus 非空 | 单一行业 | 主战行业自身周期 → 行业内加减仓 |
| C | fund_alpha=true | 单基金 | 单基金浮盈 + 动能 + regime → 个基止盈止损/SWITCH |

### 后续 Phase 的契约

- B0 / B / C 在执行前必须先查 `fund_paradigm_runs` 拿当日 paradigm
- 各 skill 内部根据 paradigm 走差异化分支(具体差异化逻辑由各 skill SKILL.md 描述,框架不预设)
- 数据库表 `fund_bot_reviews / fund_bot_actions / fund_allocation_runs` 各加 `paradigm` 字段做溯源

### 闸门后果(paradigm_active = SKIP)

- 当日 B0/B/C 不为该 bot 执行
- Phase D 仍可生成空快照(若已有持仓,继续按净值刷新)
- 写一行 SKIP 记录到 `fund_paradigm_runs`,运维可在 dashboard 看到"X bot 当日因无能力圈未参与"

### 季度重评

- 触发: `update-capability-circle.py`(每季度第 1 工作日 cron)
- 输入: 本季度 `fund_bot_reviews / fund_bot_actions / fund_paradigm_runs` + bot 自己的日记 / 研究记忆
- 输出: 重写各 bot 的 `能力圈宣告.md`(yaml frontmatter + 正文证据)
- 演进硬规则:
  - 新增能力(false → true)必须有具体证据
  - 收窄能力(true → false)随时允许
  - 非季度窗口禁止新增,允许收窄
```

- [ ] **Step 9.3: 在文档"## 二、整体架构"图里加上 Phase B-1**

定位约第 28 行的 `Phase A.5  数据新鲜度闸门` 那段,改为:

```
Phase A    市场数据刷新(research-mcp)         → 基金池 + 市场指标更新
Phase A.5  数据新鲜度闸门                       → 判断是否允许进入决策链路
Phase B-1  能力圈宣告 + 范式选择                → 闸门 + 写 fund_paradigm_runs
Phase B0   三大类资产判断 + 配置比例             → 股/债/黄金/现金目标比例(按范式调整重点)
Phase B    个性化选基(四层漏斗)                → 资产匹配 → 风格行业 → 业绩 → 个性终选(按范式分支)
Phase C    每日巡检(择时×调仓 一轮完成)        → 逐只扫描→组合汇总→资金再分配→统一出单(按范式分支)
Phase D    收益快照                             → 记录当日净值和损益
Phase E    markdown 归档                        → 从事实源反写到文件
```

- [ ] **Step 9.4: 更新"## 十二、数据库 fund.db"章节的表清单**

在表数 16 → 18,加入两张新表;在表清单(第 962 行附近的 markdown 表)末尾追加:

```markdown
| `fund_capability_circle` | Bot 执行 | 季度重评 | Phase B-1 读 | 能力圈宣告快照(每次重评一行) |
| `fund_paradigm_runs` | Bot 执行 | Phase B-1 每日 | B0/B/C 入口读 | 当日范式选择(bot_id+trade_date 唯一) |
```

并在"### 与 tougu.db 对比"表里把 fund.db 表数改成 18。

- [ ] **Step 9.5: 验证文档结构正确**

```bash
grep -n "^## " /home/rooot/.openclaw/workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md
```

Expected: 看到新的 "## 四点五、Phase B-1" 章节排在 "## 五、Phase B0" 之前。

- [ ] **Step 9.6: Commit**

```bash
git add workspace/skills/portfolio/_shared_docs/fund-investment/基金投资完整链路.md
git commit -m "docs: insert Phase B-1 chapter into 基金投资完整链路.md"
```

---

## Self-Review

### Spec Coverage Check

| Spec section | Coverage |
|---|---|
| § 二、三层框架总图 | Task 9(链路文档章节)+ Task 6(三 skill 文档) |
| § 三、能力圈分类(A/B1/B2/C)+ 自评机制 | Task 1(DB 字段)+ Task 4(yaml 解析 + 互斥校验)+ Task 7(8 bot 初版) |
| § 四、四种范式形态特征 | Task 6(三 skill 各自描述)+ Task 9(链路文档表) |
| § 五、Phase B-1 位置 + 闸门 + 切换 30 天 | Task 4(SWITCH_COOLDOWN_DAYS) + Task 5(脚本)+ Task 9(文档) |
| § 六、能力圈宣告存储 + 季度演进 | Task 7(8 bot 文件)+ Task 8(季度脚本) |
| § 七、与现有链路接入 + 加 paradigm 字段 | Task 1(ALTER COLUMN)+ Task 6(SKILL.md)+ Task 9(文档) |
| § 八、待落地清单 | 全部 9 个 task 一一对应 |

### Placeholder Scan

无 TBD/TODO/"implement later"。每段代码完整;每个测试都有断言;Step 7.2 中"等季度重评时由 bot 自己加强"是**意图说明**(不是占位符),交给运行时补全是设计的一部分。

### Type/Name Consistency

- `CapabilityCircle` / `ParadigmDecision` / `GATE_SKIP` / `SWITCH_COOLDOWN_DAYS` 在 Task 4 lib + Task 5 调度脚本里命名一致
- DB 字段名:`paradigm_active`、`capability_field`、`capability_value`、`switched_from`、`reason` — Task 1/2/3/4/5 全部一致
- MCP 工具命名:`save_capability_circle` / `get_capability_circle` / `save_paradigm_run` / `get_latest_paradigm` — Task 2/3 命名一致

### 边界场景

- 解析失败 → SKIP(reason=parse_error_skip:xxx),Task 5 处理
- 文件缺失 → SKIP(reason=missing_declaration_skip),Task 5 处理
- B1/B2 互斥 → 解析阶段 raise ValueError,Task 4 测试覆盖
- 30 天冷却 → 保持 current_paradigm,Task 4 测试覆盖
- 单能力圈自动绑定 → Task 4 测试覆盖
- 复合能力圈首次 → 走 default_paradigm,Task 4 测试覆盖
- 全空能力圈 → SKIP,Task 4/5 测试覆盖

---

_Plan written: 2026-04-29. 9 tasks, ~50 个 step,核心 lib 7 个 unit tests + MCP tools 11 个 integration tests + Phase B-1 4 个 integration tests。每个 task 末尾留 commit step,用户可批量提交。_
