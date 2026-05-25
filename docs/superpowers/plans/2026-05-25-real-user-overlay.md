# 真实用户曲线叠加到回测看板 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在回测看板每个 bot 的净值图上，叠加「同一只基金的真实用户」资金加权收益曲线，直观对比 agent 与真实用户孰强孰弱。

**Architecture:** Python 落库脚本把 `Result_7.csv` 解析成 3 张 `fund.db` 表（周期 / 交易 / 预计算净值曲线），曲线用资金加权重建、终点锁定到权威 `ClearReturn2`。TS 服务端按 bot 选取代表性用户（每 bot ≤10，多基金按候选数平均分配），只在 per-bot 详情接口返回。前端在现有 SVG 图上加细、半透明的用户曲线，按基金配色。

**Tech Stack:** Python 3 (stdlib `sqlite3`/`csv`, pytest)，TypeScript（Node 内置 test runner + `--experimental-strip-types`），原生 SVG。

设计依据：`docs/superpowers/specs/2026-05-25-real-user-overlay-design.md`

---

## 关键事实（实现者须知）

- `data/` 整个目录被 `.gitignore` 忽略（含 `fund.db` 与将要放入的 CSV）。**只提交代码，不提交数据**。`git add` 时永远不要加 `data/` 下的文件。
- 看板和 MCP 默认读同一个 DB：`data/fund.db`（server.ts 的 `DEFAULT_DB`；`db.py` 的 `$FUND_DB_PATH or $OPENCLAW_ROOT/data/fund.db`）。
- `fund_nav(fund_code, nav_date, nav, ...)`，主键 `(fund_code, nav_date)`，按 `nav_date` 升序取曲线。
- CSV 表头：`#,FundCode,Customerno,id,StartDate,EndDate,ClearReturn2,BigLossRate,BigProfitRate,C_BUSINTYPE,C_BUSINNAME,C_CFMAMOUNT,C_CFMVOL,C_TRANSACTIONDATE`。`id` 即 cycle 全局唯一键；同一 cycle 多笔交易 = 多行。
- `C_BUSINNAME` 全量 12 种取值（已核对计数）：定时定额投资确认 / 申购确认 / 赎回确认 / 转入投资账户 / 转出投资账户 / 设置分红方式确认 / 转换确认 / 转托管确认 / 转托管入确认 / 份额转卡转入 / 份额转卡转出 / 强行赎回。
- Python 测试用 `fund-portfolio-mcp/.venv/bin/pytest`。TS 测试用 `cd world && npm test`（= `node --experimental-strip-types --test test/*.test.ts`）。
- server.ts 已导出纯函数 `pickBenchmarkFund` 并被 TS 测试直接 import——新纯函数同样 `export` 后直接单测。

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `data/user/Result_7.csv` | 原始用户数据（gitignored） | 从仓库根目录移入 |
| `scripts/ingest_user_data.py` | 解析 CSV → 重建曲线 → 落 3 表；纯函数 `classify`/`reconstruct_curve` 可单测 | 新建 |
| `scripts/test_ingest_user_data.py` | pytest：分类映射、曲线重建各场景、终点锁定与守护分支、CSV 分组 | 新建 |
| `world/src/backtest-dashboard/server.ts` | 新增 `RealUserSeries` 类型、纯函数 `allocateQuota`/`selectRepresentative`、`tableExists`/`loadRealUsers`，并接进 `loadBotForRun`；summary 剥离 `realUsers` | 改 |
| `world/src/backtest-dashboard/index.html` | `renderChart` 渲染用户细曲线 + 按基金图例 + CSS | 改 |
| `world/test/backtest-dashboard.test.ts` | 断言 `allocateQuota`/`selectRepresentative` 行为；`/api/backtest/bot` 返回 `realUsers` 数组且 ≤10 | 改 |

---

## Task 1: 把 CSV 移到 data/user/

**Files:**
- Move: `Result_7.csv` → `data/user/Result_7.csv`

- [ ] **Step 1: 建目录并移动文件**

```bash
mkdir -p data/user
git mv Result_7.csv data/user/Result_7.csv 2>/dev/null || mv Result_7.csv data/user/Result_7.csv
```

（`Result_7.csv` 当前未被 git 跟踪、且 `data/` 被忽略，所以 `git mv` 会失败回退到普通 `mv`，符合预期。）

- [ ] **Step 2: 验证落位与行数**

Run: `wc -l data/user/Result_7.csv && head -1 data/user/Result_7.csv`
Expected: `38440 data/user/Result_7.csv`，表头以 `#,FundCode,Customerno,id,...` 开头。

- [ ] **Step 3: 确认无东西可提交（数据被忽略）**

Run: `git status --short data/ Result_7.csv`
Expected: 无输出（`data/` 被 `.gitignore` 忽略，CSV 不进版本库）。本任务不产生提交。

---

## Task 2: Python — `classify()` 交易类型映射

**Files:**
- Create: `scripts/ingest_user_data.py`
- Test: `scripts/test_ingest_user_data.py`

- [ ] **Step 1: 写失败测试**

写入 `scripts/test_ingest_user_data.py`：

```python
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ingest_user_data import classify


def test_classify_buy():
    assert classify("申购确认") == "buy"
    assert classify("定时定额投资确认") == "buy"


def test_classify_sell():
    assert classify("赎回确认") == "sell"
    assert classify("强行赎回") == "sell"


def test_classify_xfer_in():
    assert classify("转入投资账户") == "xfer_in"
    assert classify("转托管入确认") == "xfer_in"
    assert classify("份额转卡转入") == "xfer_in"


def test_classify_xfer_out():
    assert classify("转出投资账户") == "xfer_out"
    assert classify("份额转卡转出") == "xfer_out"


def test_classify_skip():
    assert classify("设置分红方式确认") == "skip"
    assert classify("转换确认") == "skip"
    assert classify("转托管确认") == "skip"
    assert classify("") == "skip"
    assert classify("未知类型") == "skip"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest_user_data'`

- [ ] **Step 3: 写最小实现**

写入 `scripts/ingest_user_data.py`：

```python
#!/usr/bin/env python3
"""Ingest real-user fund cycles (Result_7.csv) into fund.db.

Spec: docs/superpowers/specs/2026-05-25-real-user-overlay-design.md

Rebuilds 3 tables (idempotent DROP + CREATE; other tables untouched):
  real_user_cycles  — one row per (user, fund) holding cycle
  real_user_txns    — transaction detail
  real_user_curves  — pre-computed money-weighted net-value path,
                      endpoint pinned to the authoritative ClearReturn2

Usage:
    python3 scripts/ingest_user_data.py             # write to lab DB
    python3 scripts/ingest_user_data.py --dry-run   # parse + report, no writes
    python3 scripts/ingest_user_data.py --db PATH --csv PATH
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass, field

# Make `import db` resolve whether run from repo root or scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "fund-portfolio-mcp"))

EPS = 1e-4


def classify(busin_name: str) -> str:
    """Map a fund transaction's Chinese busin_name to its effect on the holding.

    buy/sell move both shares and external cash; xfer_in/xfer_out move shares
    only (no cost basis change); skip = dividend-method / conversion / custody
    rows we deliberately ignore in v1 (endpoint pinning absorbs the drift)."""
    name = (busin_name or "").strip()
    if name in ("申购确认", "定时定额投资确认"):
        return "buy"
    if name in ("赎回确认", "强行赎回"):
        return "sell"
    if name in ("转入投资账户", "转托管入确认", "份额转卡转入"):
        return "xfer_in"
    if name in ("转出投资账户", "份额转卡转出"):
        return "xfer_out"
    return "skip"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q`
Expected: PASS（5 个测试）

- [ ] **Step 5: 提交**

```bash
git add scripts/ingest_user_data.py scripts/test_ingest_user_data.py
git commit -m "feat(user-overlay): classify real-user txn types"
```

---

## Task 3: Python — `reconstruct_curve()` 资金加权 + 终点锁定

**Files:**
- Modify: `scripts/ingest_user_data.py`
- Test: `scripts/test_ingest_user_data.py`

- [ ] **Step 1: 追加失败测试**

在 `scripts/test_ingest_user_data.py` 顶部 import 行追加 `reconstruct_curve`：

```python
from ingest_user_data import classify, reconstruct_curve
```

在文件末尾追加：

```python
def _buy(date, amount, vol):
    return {"busin_name": "申购确认", "amount": amount, "vol": vol, "txn_date": date}


def _sell(date, amount, vol):
    return {"busin_name": "赎回确认", "amount": amount, "vol": vol, "txn_date": date}


def _xfer_in(date, vol):
    return {"busin_name": "转入投资账户", "amount": 0.0, "vol": vol, "txn_date": date}


def test_reconstruct_dca_scale():
    nav = {"2025-01-02": 1.0, "2025-01-03": 1.1, "2025-01-04": 1.2}
    txns = [_buy("2025-01-02", 100.0, 100.0)]
    series, method = reconstruct_curve(txns, nav, "2025-01-02", "2025-01-04", 0.20)
    assert method == "scale"
    assert abs(series[0]["net_value"] - 1.0) < 1e-9
    assert abs(series[-1]["net_value"] - 1.20) < 1e-9
    assert [p["trade_date"] for p in series] == ["2025-01-02", "2025-01-03", "2025-01-04"]


def test_reconstruct_scale_to_authoritative_return():
    # raw roi_end = 0.20 but authoritative ClearReturn2 = 0.10 (fees/dividends) → f=0.5
    nav = {"2025-01-02": 1.0, "2025-01-03": 1.1, "2025-01-04": 1.2}
    series, method = reconstruct_curve([_buy("2025-01-02", 100.0, 100.0)], nav,
                                       "2025-01-02", "2025-01-04", 0.10)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 1.10) < 1e-9
    assert abs(series[1]["net_value"] - 1.05) < 1e-9


def test_reconstruct_partial_redeem():
    nav = {"d1": 1.0, "d2": 2.0, "d3": 2.0}
    txns = [_buy("d1", 100.0, 100.0), _sell("d2", 100.0, 50.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d3", 1.0)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 2.0) < 1e-9


def test_reconstruct_full_redeem():
    nav = {"d1": 1.0, "d2": 1.5, "d3": 1.5}
    txns = [_buy("d1", 100.0, 100.0), _sell("d2", 150.0, 100.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d3", 0.5)
    assert method == "scale"
    assert abs(series[-1]["net_value"] - 1.5) < 1e-9


def test_reconstruct_xfer_in_adds_shares_no_cost():
    nav = {"d1": 1.0, "d2": 1.0}
    txns = [_buy("d1", 100.0, 100.0), _xfer_in("d2", 100.0)]
    series, method = reconstruct_curve(txns, nav, "d1", "d2", 1.0)
    assert method == "scale"
    # 200 shares × nav 1.0, invested still 100 → raw roi_end = 1.0
    assert abs(series[-1]["net_value"] - 2.0) < 1e-9


def test_endpoint_pin_shift_on_signflip():
    nav = {"d1": 1.0, "d2": 1.2}
    series, method = reconstruct_curve([_buy("d1", 100.0, 100.0)], nav, "d1", "d2", -0.1)
    assert method == "shift"
    assert abs(series[-1]["net_value"] - 0.9) < 1e-9  # 1 + (-0.1)


def test_endpoint_pin_shift_on_zero_roi():
    # only a transfer-in, no buy → invested = 0 → raw roi all 0 → additive shift
    nav = {"d1": 1.0, "d2": 1.0}
    series, method = reconstruct_curve([_xfer_in("d1", 100.0)], nav, "d1", "d2", 0.05)
    assert method == "shift"
    assert all(abs(p["net_value"] - 1.05) < 1e-9 for p in series)


def test_reconstruct_empty_when_no_nav_in_window():
    series, method = reconstruct_curve([_buy("d1", 100.0, 100.0)], {}, "d1", "d2", 0.1)
    assert series == []
    assert method == "empty"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q`
Expected: FAIL — `ImportError: cannot import name 'reconstruct_curve'`

- [ ] **Step 3: 实现 `_raw_roi_path` + `reconstruct_curve`**

在 `scripts/ingest_user_data.py` 的 `classify` 之后追加：

```python
def _raw_roi_path(txns, dates, nav_by_date):
    """Money-weighted roi(t) for each NAV date in `dates` (ascending).

    roi(t) = (mv(t) + proceeds(t) - invested(t)) / invested(t)
    Transactions are folded in cumulatively as their txn_date is reached;
    invested(t) <= 0 ⇒ roi defined as 0 (no cost basis yet)."""
    txns_sorted = sorted(txns, key=lambda t: t.get("txn_date") or "")
    shares = invested = proceeds = 0.0
    ti = 0
    out = []
    for d in dates:
        while ti < len(txns_sorted) and (txns_sorted[ti].get("txn_date") or "") <= d:
            t = txns_sorted[ti]
            ti += 1
            kind = classify(t.get("busin_name", ""))
            vol = float(t.get("vol") or 0.0)
            amt = float(t.get("amount") or 0.0)
            if kind == "buy":
                shares += vol
                invested += amt
            elif kind == "sell":
                shares -= vol
                proceeds += amt
            elif kind == "xfer_in":
                shares += vol
            elif kind == "xfer_out":
                shares -= vol
            # skip: no effect
        nav = nav_by_date.get(d)
        if nav is None:
            continue
        mv = shares * float(nav)
        roi = (mv + proceeds - invested) / invested if invested > 1e-9 else 0.0
        out.append((d, roi))
    return out


def reconstruct_curve(txns, nav_by_date, start_date, end_date, clear_return2):
    """Return (series, method).

    series = [{'trade_date', 'net_value'}] with net_value = 1 + roi_adj(t).
    Endpoint is pinned to the authoritative clear_return2:
      - same-sign & |roi_end|>=EPS  → multiplicative scale f = c / roi_end  (method 'scale')
      - otherwise (sign flip / ~0)  → additive shift = c - roi_end          (method 'shift')
    method 'empty' when no NAV date falls inside [start_date, end_date]."""
    dates = sorted(d for d in nav_by_date if start_date <= d <= end_date)
    raw = _raw_roi_path(txns, dates, nav_by_date) if dates else []
    if not raw:
        return [], "empty"
    roi_end = raw[-1][1]
    c = float(clear_return2)
    same_sign = (roi_end > 0 and c > 0) or (roi_end < 0 and c < 0)
    if abs(roi_end) >= EPS and same_sign:
        f = c / roi_end
        series = [{"trade_date": d, "net_value": 1.0 + roi * f} for d, roi in raw]
        return series, "scale"
    shift = c - roi_end
    series = [{"trade_date": d, "net_value": 1.0 + roi + shift} for d, roi in raw]
    return series, "shift"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q`
Expected: PASS（共 13 个测试）

- [ ] **Step 5: 提交**

```bash
git add scripts/ingest_user_data.py scripts/test_ingest_user_data.py
git commit -m "feat(user-overlay): money-weighted curve reconstruction with endpoint pinning"
```

---

## Task 4: Python — CSV 解析与按 cycle 分组

**Files:**
- Modify: `scripts/ingest_user_data.py`
- Test: `scripts/test_ingest_user_data.py`

- [ ] **Step 1: 追加失败测试**

import 行改为：

```python
from ingest_user_data import classify, reconstruct_curve, _read_csv, _group_cycles
```

文件末尾追加：

```python
def test_group_cycles(tmp_path):
    csv_text = (
        "#,FundCode,Customerno,id,StartDate,EndDate,ClearReturn2,BigLossRate,BigProfitRate,"
        "C_BUSINTYPE,C_BUSINNAME,C_CFMAMOUNT,C_CFMVOL,C_TRANSACTIONDATE\n"
        "1,012894,cust1,cycA,2025-01-02,2025-06-30,0.05,-0.04,0.16,122,申购确认,100.0,100.0,2025-01-02\n"
        "2,012894,cust1,cycA,2025-01-02,2025-06-30,0.05,-0.04,0.16,124,赎回确认,60.0,50.0,2025-03-10\n"
        "3,000051,cust2,cycB,2025-02-01,2025-07-01,0.20,,,139,定时定额投资确认,50.0,40.0,2025-02-01\n"
    )
    p = tmp_path / "mini.csv"
    p.write_text(csv_text, encoding="utf-8")

    cycles = _group_cycles(_read_csv(str(p)))
    assert set(cycles.keys()) == {"cycA", "cycB"}

    a = cycles["cycA"]
    assert a.fund_code == "012894"
    assert a.customerno == "cust1"
    assert a.start_date == "2025-01-02"
    assert a.end_date == "2025-06-30"
    assert abs(a.clear_return2 - 0.05) < 1e-9
    assert len(a.txns) == 2
    assert a.txns[0]["busin_name"] == "申购确认"
    assert abs(a.txns[1]["vol"] - 50.0) < 1e-9

    b = cycles["cycB"]
    assert b.fund_code == "000051"
    assert b.big_loss_rate is None   # empty cell → None
    assert len(b.txns) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py::test_group_cycles -q`
Expected: FAIL — `ImportError: cannot import name '_read_csv'`

- [ ] **Step 3: 实现 dataclass + 解析/分组**

在 `scripts/ingest_user_data.py` 的 `reconstruct_curve` 之后追加：

```python
@dataclass
class Cycle:
    cycle_id: str
    fund_code: str
    customerno: str
    start_date: str
    end_date: str
    clear_return2: float
    big_loss_rate: float | None
    big_profit_rate: float | None
    txns: list = field(default_factory=list)


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_float_or_none(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _group_cycles(rows):
    cycles: dict[str, Cycle] = {}
    for r in rows:
        cid = r["id"]
        c = cycles.get(cid)
        if c is None:
            c = Cycle(
                cycle_id=cid,
                fund_code=r["FundCode"],
                customerno=r.get("Customerno", ""),
                start_date=r["StartDate"],
                end_date=r["EndDate"],
                clear_return2=_to_float(r.get("ClearReturn2")),
                big_loss_rate=_to_float_or_none(r.get("BigLossRate")),
                big_profit_rate=_to_float_or_none(r.get("BigProfitRate")),
            )
            cycles[cid] = c
        c.txns.append({
            "busin_type": r.get("C_BUSINTYPE", ""),
            "busin_name": r.get("C_BUSINNAME", ""),
            "amount": _to_float(r.get("C_CFMAMOUNT")),
            "vol": _to_float(r.get("C_CFMVOL")),
            "txn_date": r.get("C_TRANSACTIONDATE", ""),
        })
    return cycles
```

- [ ] **Step 4: 跑测试确认通过**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q`
Expected: PASS（共 14 个测试）

- [ ] **Step 5: 提交**

```bash
git add scripts/ingest_user_data.py scripts/test_ingest_user_data.py
git commit -m "feat(user-overlay): parse Result_7.csv and group by cycle id"
```

---

## Task 5: Python — 建表、读 NAV、落库 + main()

**Files:**
- Modify: `scripts/ingest_user_data.py`

- [ ] **Step 1: 实现 DB 胶水与入口**

在 `scripts/ingest_user_data.py` 的 `_group_cycles` 之后追加：

```python
_SCHEMA = """
DROP TABLE IF EXISTS real_user_curves;
DROP TABLE IF EXISTS real_user_txns;
DROP TABLE IF EXISTS real_user_cycles;

CREATE TABLE real_user_cycles (
  cycle_id        TEXT PRIMARY KEY,
  fund_code       TEXT NOT NULL,
  customerno      TEXT,
  start_date      TEXT NOT NULL,
  end_date        TEXT NOT NULL,
  clear_return2   REAL,
  big_loss_rate   REAL,
  big_profit_rate REAL
);
CREATE INDEX idx_ruc_fund ON real_user_cycles(fund_code);

CREATE TABLE real_user_txns (
  cycle_id    TEXT NOT NULL,
  fund_code   TEXT NOT NULL,
  busin_type  TEXT,
  busin_name  TEXT NOT NULL,
  amount      REAL,
  vol         REAL,
  txn_date    TEXT NOT NULL
);
CREATE INDEX idx_rut_cycle ON real_user_txns(cycle_id);

CREATE TABLE real_user_curves (
  cycle_id    TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  net_value   REAL NOT NULL,
  PRIMARY KEY (cycle_id, trade_date)
);
"""


def _default_db_path() -> str:
    if os.environ.get("FUND_DB_PATH"):
        return os.environ["FUND_DB_PATH"]
    import db as db_mod
    return db_mod.DB_PATH


def _load_nav(conn, fund_codes):
    """{fund_code: {nav_date: nav}} for the given funds (skips NULL nav)."""
    nav: dict[str, dict[str, float]] = {}
    codes = [c for c in fund_codes if c]
    if not codes:
        return nav
    placeholders = ",".join("?" * len(codes))
    cur = conn.execute(
        f"SELECT fund_code, nav_date, nav FROM fund_nav "
        f"WHERE fund_code IN ({placeholders}) AND nav IS NOT NULL",
        codes,
    )
    for fund_code, nav_date, nav_val in cur:
        nav.setdefault(fund_code, {})[nav_date] = float(nav_val)
    return nav


def ingest(csv_path, conn):
    cycles = _group_cycles(_read_csv(csv_path))
    nav = _load_nav(conn, {c.fund_code for c in cycles.values()})
    conn.executescript(_SCHEMA)
    stats = {"cycles": 0, "txns": 0, "curve_points": 0,
             "scale": 0, "shift": 0, "empty": 0}
    for c in cycles.values():
        conn.execute(
            "INSERT INTO real_user_cycles VALUES (?,?,?,?,?,?,?,?)",
            (c.cycle_id, c.fund_code, c.customerno, c.start_date, c.end_date,
             c.clear_return2, c.big_loss_rate, c.big_profit_rate),
        )
        stats["cycles"] += 1
        for t in c.txns:
            conn.execute(
                "INSERT INTO real_user_txns VALUES (?,?,?,?,?,?,?)",
                (c.cycle_id, c.fund_code, t["busin_type"], t["busin_name"],
                 t["amount"], t["vol"], t["txn_date"]),
            )
            stats["txns"] += 1
        series, method = reconstruct_curve(
            c.txns, nav.get(c.fund_code, {}), c.start_date, c.end_date, c.clear_return2)
        stats[method] += 1
        for pt in series:
            conn.execute(
                "INSERT INTO real_user_curves VALUES (?,?,?)",
                (c.cycle_id, pt["trade_date"], pt["net_value"]),
            )
            stats["curve_points"] += 1
    conn.commit()
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=os.path.join(HERE, "..", "data", "user", "Result_7.csv"))
    ap.add_argument("--db", default=_default_db_path())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.dry_run:
        cycles = _group_cycles(_read_csv(args.csv))
        funds = {c.fund_code for c in cycles.values()}
        txns = sum(len(c.txns) for c in cycles.values())
        print(f"[dry-run] cycles={len(cycles)} funds={len(funds)} txns={txns}")
        return 0

    conn = sqlite3.connect(args.db)
    try:
        stats = ingest(args.csv, conn)
    finally:
        conn.close()
    print(f"ingested into {args.db}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: dry-run 自检**

Run: `python3 scripts/ingest_user_data.py --dry-run`
Expected: `[dry-run] cycles=3788 funds=421 txns=38439`（与 CSV 统计一致）

- [ ] **Step 3: 真正落库到 data/fund.db**

Run: `python3 scripts/ingest_user_data.py`
Expected: 形如 `ingested into .../data/fund.db: {'cycles': 3788, 'txns': 38439, 'curve_points': <数十万>, 'scale': <多数>, 'shift': <少数>, 'empty': <少数>}`

- [ ] **Step 4: 校验表与终点锁定**

Run:
```bash
sqlite3 data/fund.db "SELECT count(*) FROM real_user_cycles; SELECT count(*) FROM real_user_curves;"
sqlite3 data/fund.db "
  SELECT c.cycle_id,
         round(c.clear_return2,4) AS authoritative,
         round(v.net_value-1,4)  AS curve_end
  FROM real_user_cycles c
  JOIN real_user_curves v ON v.cycle_id=c.cycle_id
  WHERE v.trade_date=(SELECT max(trade_date) FROM real_user_curves WHERE cycle_id=c.cycle_id)
  LIMIT 5;"
```
Expected: `real_user_cycles` = 3788；每行 `authoritative ≈ curve_end`（终点锁定生效，差值在浮点误差内）。

- [ ] **Step 5: 提交（仅脚本，不含数据）**

```bash
git add scripts/ingest_user_data.py
git commit -m "feat(user-overlay): build real_user_* tables from Result_7.csv"
```

---

## Task 6: server.ts — `allocateQuota()` 配额分配

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`
- Test: `world/test/backtest-dashboard.test.ts`

- [ ] **Step 1: 追加失败测试**

在 `world/test/backtest-dashboard.test.ts` 第 10 行 import 改为：

```ts
import { pickBenchmarkFund, allocateQuota, selectRepresentative } from '../src/backtest-dashboard/server.ts'
```

文件末尾追加：

```ts
test('allocateQuota: 单池上限封顶到候选数', () => {
  const q = allocateQuota(new Map([['A', 12]]), 10)
  assert.equal(q.get('A'), 10)
})

test('allocateQuota: 双池平均分', () => {
  const q = allocateQuota(new Map([['A', 20], ['B', 20]]), 10)
  assert.equal(q.get('A'), 5)
  assert.equal(q.get('B'), 5)
})

test('allocateQuota: 余数给候选更多的池,且不超可用', () => {
  const q = allocateQuota(new Map([['A', 3], ['B', 20]]), 10)
  assert.equal(q.get('A'), 3)            // capped by availability
  assert.equal(q.get('B'), 7)            // base 5 + remainder 2
  assert.equal(q.get('A')! + q.get('B')!, 10)
})

test('allocateQuota: 候选总量不足时不超总可用', () => {
  const q = allocateQuota(new Map([['A', 4], ['B', 4]]), 10)
  assert.equal(q.get('A'), 4)
  assert.equal(q.get('B'), 4)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && npm test 2>&1 | head -30`
Expected: FAIL — `allocateQuota` 不是导出成员（编译/导入错误）

- [ ] **Step 3: 实现 `allocateQuota`**

在 `world/src/backtest-dashboard/server.ts` 的 `classifyAction`（第 206 行结束）之后追加：

```ts
/** 每个 bot 最多展示的真实用户数。 */
const MAX_REAL_USERS = 10

/** 在候选基金池间分配总配额：先 floor(total/n) 平均，余数按候选数从多到少补，
 *  每池不超过其候选数。返回 fund_code -> 配额。 */
export function allocateQuota(poolSizes: Map<string, number>, total: number): Map<string, number> {
  const alloc = new Map<string, number>()
  const pools = [...poolSizes.keys()]
  for (const p of pools) alloc.set(p, 0)
  if (!pools.length || total <= 0) return alloc
  const base = Math.floor(total / pools.length)
  for (const p of pools) alloc.set(p, Math.min(base, poolSizes.get(p) ?? 0))
  let remaining = total - [...alloc.values()].reduce((s, v) => s + v, 0)
  const order = [...pools].sort((a, b) => (poolSizes.get(b)! - poolSizes.get(a)!) || a.localeCompare(b))
  while (remaining > 0) {
    let progressed = false
    for (const p of order) {
      if (remaining <= 0) break
      if (alloc.get(p)! < (poolSizes.get(p) ?? 0)) {
        alloc.set(p, alloc.get(p)! + 1)
        remaining--
        progressed = true
      }
    }
    if (!progressed) break  // 所有池已封顶,剩余配额无处可放
  }
  return alloc
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd world && npm test 2>&1 | tail -20`
Expected: 新增 4 个 allocateQuota 测试 PASS（selectRepresentative 测试此时仍会因未导出而失败——下个任务补；本步只确认 allocateQuota 通过、其余既有测试不回归）

> 注：若 `selectRepresentative` 的 import 让整套测试无法加载，可临时只跑：`node --experimental-strip-types --test --test-name-pattern allocateQuota test/backtest-dashboard.test.ts`，Task 7 完成后再跑全量。

- [ ] **Step 5: 提交**

```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(user-overlay): allocateQuota for per-fund user budget"
```

---

## Task 7: server.ts — `selectRepresentative()` 代表性选取

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`
- Test: `world/test/backtest-dashboard.test.ts`

- [ ] **Step 1: 追加失败测试**

在 `world/test/backtest-dashboard.test.ts` 末尾追加：

```ts
test('selectRepresentative: quota>=n 全取', () => {
  const arr = [1, 2, 3]
  assert.deepEqual(selectRepresentative(arr, 5), [1, 2, 3])
})

test('selectRepresentative: 覆盖最差/最好/中位,去重,且为输入子集', () => {
  const arr = Array.from({ length: 20 }, (_, i) => i)  // 升序 0..19
  const picked = selectRepresentative(arr, 10)
  assert.equal(picked.length, 10)
  assert.ok(picked.includes(0), '含最差(0)')
  assert.ok(picked.includes(19), '含最好(19)')
  assert.ok(picked.includes(10), '含中位(round(0.5*19))')
  assert.equal(new Set(picked).size, picked.length, '无重复')
  for (const v of picked) assert.ok(arr.includes(v), '是输入子集')
  // 返回保持升序(便于稳定渲染)
  for (let i = 1; i < picked.length; i++) assert.ok(picked[i] > picked[i - 1])
})

test('selectRepresentative: quota<=0 或空输入 → 空', () => {
  assert.deepEqual(selectRepresentative([1, 2, 3], 0), [])
  assert.deepEqual(selectRepresentative([], 5), [])
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && npm test 2>&1 | head -30`
Expected: FAIL — `selectRepresentative` 未导出

- [ ] **Step 3: 实现 `selectRepresentative`**

在 `server.ts` 刚加的 `allocateQuota` 之后追加：

```ts
/** 在按 clear_return2 升序的候选里挑 quota 个代表：先锚定 最差/最好/中位/p25/p75,
 *  再按均匀分布补足,去重,返回保持升序的子集。 */
export function selectRepresentative<T>(sortedAsc: T[], quota: number): T[] {
  const n = sortedAsc.length
  if (quota <= 0 || n === 0) return []
  if (quota >= n) return [...sortedAsc]
  const idxAt = (frac: number) => Math.round(frac * (n - 1))
  const picks: number[] = []
  const take = (i: number) => { if (picks.length < quota && !picks.includes(i)) picks.push(i) }
  for (const a of [0, 1, 0.5, 0.25, 0.75]) take(idxAt(a))   // 代表性锚点
  for (let s = 0; picks.length < quota && s < n * 2; s++) take(idxAt(s / Math.max(1, quota - 1)))
  for (let i = 0; picks.length < quota && i < n; i++) take(i)  // 兜底填满
  return picks.sort((a, b) => a - b).map(i => sortedAsc[i])
}
```

- [ ] **Step 4: 跑全量测试确认通过**

Run: `cd world && npm test 2>&1 | tail -25`
Expected: 全部 PASS（含 Task 6 的 allocateQuota 与本任务 selectRepresentative；既有测试不回归）

- [ ] **Step 5: 提交**

```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(user-overlay): selectRepresentative spread of user cycles"
```

---

## Task 8: server.ts — 类型、loadRealUsers、接进 loadBotForRun

**Files:**
- Modify: `world/src/backtest-dashboard/server.ts`
- Test: `world/test/backtest-dashboard.test.ts`

- [ ] **Step 1: 追加失败测试（集成断言）**

在 `world/test/backtest-dashboard.test.ts` 内，`/api/backtest/bot` 已取到 `one` 之后（约第 89-90 行 `assert.equal(one.runId, target.runId)` 之后）插入：

```ts
    // realUsers: 仅 per-bot 详情返回；数组,数量受 MAX_REAL_USERS(10) 约束
    const oneRU = one as Bot & {
      realUsers?: Array<{ fundCode: string; cycleId: string; clearReturn2: number; series: Array<{ trade_date: string; net_value: number }> }>
    }
    assert.ok(Array.isArray(oneRU.realUsers), 'per-bot 响应含 realUsers 数组')
    assert.ok(oneRU.realUsers!.length <= 10, 'realUsers ≤ 10')
    for (const u of oneRU.realUsers!) {
      assert.ok(typeof u.fundCode === 'string' && u.fundCode.length > 0, '用户有 fundCode')
      assert.ok(Array.isArray(u.series), '用户有 series 数组')
    }
```

并在 `/api/backtest/data` 的 summary 校验循环（约第 69-78 行）里追加一条：summary 不带 realUsers。在该循环体内加：

```ts
      assert.equal((b as { realUsers?: unknown }).realUsers, undefined, `summary 不含 realUsers (${b.botId})`)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd world && npm test 2>&1 | tail -25`
Expected: FAIL — `realUsers` 在 `/api/backtest/bot` 上是 `undefined`（尚未实现）

- [ ] **Step 3: 加 `RealUserSeries` 类型 + `BotDataset` 字段**

在 `server.ts` 的 `BotBenchmark` 接口（第 116 行结束）之后追加：

```ts
interface RealUserSeries {
  fundCode: string
  fundName: string
  cycleId: string
  clearReturn2: number
  bigLossRate: number | null
  bigProfitRate: number | null
  txnCount: number
  series: { trade_date: string; net_value: number }[]
}
```

在 `BotDataset` 接口里 `benchmark: BotBenchmark | null`（第 144 行）之后加一行：

```ts
  realUsers: RealUserSeries[]
```

把 summary 类型（第 160 行）改为同时剥离 `realUsers`：

```ts
type BotDatasetSummary = Omit<BotDataset, 'holdingsByDate' | 'realUsers'>
```

- [ ] **Step 4: 实现 `tableExists` + `loadRealUsers`**

在 `loadBenchmark`（第 257 行结束）之后追加：

```ts
async function tableExists(dbPath: string, name: string): Promise<boolean> {
  const rows = await queryRows<{ n: number }>(dbPath,
    `SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name=${quoteSql(name)}`)
  return num(rows[0]?.n) > 0
}

/** 取 bot 触碰过的基金上的真实用户曲线：候选→按基金分配配额→池内代表性选取→拉曲线。
 *  real_user_* 表不存在(未落库)时返回 []——保证功能可加性,不破坏看板。 */
async function loadRealUsers(dbPath: string, fundCodes: string[]): Promise<RealUserSeries[]> {
  const codes = [...new Set(fundCodes)].filter(Boolean)
  if (!codes.length) return []
  if (!(await tableExists(dbPath, 'real_user_cycles'))) return []
  const inList = codes.map(quoteSql).join(',')
  const cycles = await queryRows<{
    cycle_id: string; fund_code: string; fund_name: string
    clear_return2: number | null; big_loss_rate: number | null; big_profit_rate: number | null
    txn_count: number
  }>(dbPath, `
    SELECT c.cycle_id, c.fund_code,
           COALESCE(i.fund_name, c.fund_code) AS fund_name,
           c.clear_return2, c.big_loss_rate, c.big_profit_rate,
           (SELECT COUNT(*) FROM real_user_txns t WHERE t.cycle_id = c.cycle_id) AS txn_count
    FROM real_user_cycles c
    LEFT JOIN fund_info i ON i.fund_code = c.fund_code
    WHERE c.fund_code IN (${inList})
      AND EXISTS (SELECT 1 FROM real_user_curves v WHERE v.cycle_id = c.cycle_id)
    ORDER BY c.fund_code ASC, c.clear_return2 ASC
  `)
  if (!cycles.length) return []

  const byFund = new Map<string, typeof cycles>()
  for (const c of cycles) {
    const arr = byFund.get(c.fund_code) ?? []
    arr.push(c)
    byFund.set(c.fund_code, arr)
  }
  const poolSizes = new Map<string, number>()
  for (const [code, arr] of byFund) poolSizes.set(code, arr.length)
  const quota = allocateQuota(poolSizes, MAX_REAL_USERS)

  const chosen: typeof cycles = []
  for (const [code, arr] of byFund) {
    chosen.push(...selectRepresentative(arr, quota.get(code) ?? 0))  // arr 已按 clear_return2 升序
  }
  if (!chosen.length) return []

  const curveRows = await queryRows<{ cycle_id: string; trade_date: string; net_value: number }>(dbPath, `
    SELECT cycle_id, trade_date, net_value
    FROM real_user_curves
    WHERE cycle_id IN (${chosen.map(c => quoteSql(c.cycle_id)).join(',')})
    ORDER BY cycle_id ASC, trade_date ASC
  `)
  const curveByCycle = new Map<string, { trade_date: string; net_value: number }[]>()
  for (const r of curveRows) {
    const arr = curveByCycle.get(r.cycle_id) ?? []
    arr.push({ trade_date: r.trade_date, net_value: num(r.net_value) })
    curveByCycle.set(r.cycle_id, arr)
  }

  return chosen.map(c => ({
    fundCode: c.fund_code,
    fundName: c.fund_name,
    cycleId: c.cycle_id,
    clearReturn2: num(c.clear_return2),
    bigLossRate: c.big_loss_rate,
    bigProfitRate: c.big_profit_rate,
    txnCount: num(c.txn_count),
    series: curveByCycle.get(c.cycle_id) ?? [],
  })).filter(u => u.series.length > 0)
}
```

- [ ] **Step 5: 接进 `loadBotForRun` 并返回**

在 `loadBotForRun` 里 `const benchmark = ...`（第 379-381 行）之后追加：

```ts
  const touchedFunds = [...new Set([
    ...allHoldings.map(h => h.fund_code),
    ...actions.map(a => a.fund_code),
  ])].filter(Boolean)
  const realUsers = await loadRealUsers(dbPath, touchedFunds)
```

在 return 对象里 `benchmark,`（第 422 行）之后加一行：

```ts
    realUsers,
```

`loadDataset` 里剥离 summary 的解构（第 439 行）改为：

```ts
      const { holdingsByDate: _drop, realUsers: _dropUsers, ...summary } = bot
```

- [ ] **Step 6: 跑全量测试确认通过**

Run: `cd world && npm test 2>&1 | tail -25`
Expected: 全部 PASS（含新集成断言：`/api/backtest/bot` 有 `realUsers` 数组、summary 无 `realUsers`）

- [ ] **Step 7: 提交**

```bash
git add world/src/backtest-dashboard/server.ts world/test/backtest-dashboard.test.ts
git commit -m "feat(user-overlay): server selects + serves real-user curves per bot"
```

---

## Task 9: index.html — 渲染用户细曲线 + 图例 + CSS

**Files:**
- Modify: `world/src/backtest-dashboard/index.html`

- [ ] **Step 1: 加 CSS**

在第 31 行 `.bench-line{...}` 同一条 style 行内、`.lg-swatch.lg-bench{background:var(--accent2)}` 之后追加（即在该行末尾 `</...>` 前补两条规则）：

```css
.user-line{fill:none;stroke-width:1;stroke-linecap:round;stroke-linejoin:round;opacity:.32}.user-line:hover{opacity:.85;stroke-width:1.6}.lg-swatch.lg-user{height:2px;opacity:.6}
```

具体做法：把第 31 行结尾的 `.lg-swatch.lg-bench{background:var(--accent2)}` 替换为
`.lg-swatch.lg-bench{background:var(--accent2)}.user-line{fill:none;stroke-width:1;stroke-linecap:round;stroke-linejoin:round;opacity:.32}.user-line:hover{opacity:.85;stroke-width:1.6}.lg-swatch.lg-user{height:2px;opacity:.6}`

- [ ] **Step 2: renderChart — 收集用户序列进坐标轴与值域**

在 `renderChart` 内，第 267 行 `for (const p of bSeries) dateSet.add(p.trade_date)` 之后插入：

```js
  const users = (bot.realUsers || []).map(u => ({
    ...u,
    pts: filterByRange(u.series || [], state.rangeFrom, state.rangeTo),
  })).filter(u => u.pts.length)
  for (const u of users) for (const p of u.pts) dateSet.add(p.trade_date)
```

把 `allVals`（第 271-275 行）改为纳入用户值：

```js
  const allVals = [
    ...series.map(p => Number(p.net_value || 1)),
    ...bSeries.map(p => Number(p.net_value || 1)),
    ...users.flatMap(u => u.pts.map(p => Number(p.net_value || 1))),
    1,
  ]
```

- [ ] **Step 3: renderChart — 计算用户路径**

在 `benchCoords` 定义（第 281 行）之后插入：

```js
  const userPaths = users.map(u => ({
    u,
    color: fundColor(u.fundCode),
    d: linePathByDate(u.pts, dateToIdx, xAxisDates.length, yMin, yMax, width, height, pad).d,
    win: u.series.length ? `${u.series[0].trade_date}→${u.series[u.series.length - 1].trade_date}` : '',
  }))
  const userFunds = [...new Map(users.map(u => [u.fundCode, u.fundName])).entries()]
```

- [ ] **Step 4: renderChart — 图例纳入用户分组**

把 `legend` 定义（第 326-328 行）整体替换为：

```js
  const legendItems = [`<span class="lg-item"><span class="lg-swatch lg-bot"></span>${esc(bot.botId)} 净值</span>`]
  if (benchmark) legendItems.push(`<span class="lg-item"><span class="lg-swatch lg-bench"></span>基准 ${esc(benchmark.fundCode)} ${esc(benchmark.fundName)}</span>`)
  for (const [code, name] of userFunds) legendItems.push(`<span class="lg-item"><span class="lg-swatch lg-user" style="background:${fundColor(code)}"></span>真实用户 ${esc(code)}${userFunds.length > 1 ? ' ' + esc(name) : ''}</span>`)
  const legend = (benchmark || userFunds.length) ? `<div class="chart-legend">${legendItems.join('')}</div>` : ''
```

- [ ] **Step 5: renderChart — 在 SVG 里画用户曲线（置于 bot 线之下）**

在 SVG 字符串里，baseline 行之后、`+ \`<path d="${botCoords.area}" class="area"></path>\``（第 339 行）之前插入：

```js
      + userPaths.map(p => `<path d="${p.d}" class="user-line" stroke="${p.color}"><title>${esc(`真实用户 ${p.u.fundCode} ${p.u.fundName} · ${p.win} · 收益 ${(p.u.clearReturn2 * 100).toFixed(1)}% · ${p.u.txnCount}笔`)}</title></path>`).join('')
```

- [ ] **Step 6: 启动看板,浏览器人工验证**

Run（后台起服务）: `cd world && npm run backtest -- --host 127.0.0.1 --port 18888 &`
然后用浏览器（Playwright MCP）打开 `http://127.0.0.1:18888/`：
- 选一个单基金、已建仓的 bot（如收益榜首），确认图上出现多条细半透明曲线，颜色与该基金一致，图例多出「真实用户 <code>」。
- hover 用户细线，`<title>` 显示 基金/窗口/收益/笔数。
- 切换时间区间预设（YTD / 近3月），用户曲线随之裁剪、不报错。
- 选一个未建仓 bot（基准回退到沪深300），确认无用户曲线时图与图例正常（不抛错、不出现空「真实用户」图例）。
- 看控制台无 JS 报错。

Expected: 上述全部成立。若不成立，回到对应 step 修正后重验。

- [ ] **Step 7: 提交**

```bash
git add world/src/backtest-dashboard/index.html
git commit -m "feat(user-overlay): render real-user curves on the nav chart"
```

---

## Task 10: 收尾校验

**Files:** 无新增改动（仅验证）

- [ ] **Step 1: 全量测试回归**

Run: `fund-portfolio-mcp/.venv/bin/pytest scripts/test_ingest_user_data.py -q && cd world && npm test 2>&1 | tail -15`
Expected: Python 14 PASS；TS 全 PASS。

- [ ] **Step 2: 幂等性自检（再跑一次 ingest 不重复膨胀）**

Run: `python3 scripts/ingest_user_data.py && sqlite3 data/fund.db "SELECT count(*) FROM real_user_cycles"`
Expected: 仍是 `3788`（DROP+CREATE 幂等）。

- [ ] **Step 3: 关停后台看板进程**

Run: `pkill -f "backtest-dashboard/server.ts" 2>/dev/null; echo done`
Expected: `done`

---

## 不在本计划范围（沿用 spec §10）

- 指数级聚合匹配（仅做精确同基金）。
- 现金分红 / 申赎手续费精确建模（用终点锁定吸收）。
- 转换确认 / 转托管确认 的精确方向还原（v1 跳过）。
- 用户曲线进入十字光标 tooltip（v1 仅靠 SVG `<title>` hover；避免 10 条线把 tooltip 撑爆）。
- 真实用户 vs bot 的统计显著性 / 胜率汇总面板。
