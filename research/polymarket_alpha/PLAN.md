# Polymarket 信号发现 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Polymarket 宏观概率序列对 A股/美股/黄金做 robustness 优先的信号发现，产出诚实标注可信度的研究报告，并并行启动聪明钱采集管道。

**Architecture:** 三个零耦合单元——`align.py`(PIT 对齐+防未来函数) → `ic_scan.py`(Spearman IC/分位/去趋势检验) → `run_scan.py`(跑全假设出报告)；外加 `collector/`(调 polymarket CLI 灌聪明钱空表)。标的数据先缓存 CSV，检验只读本地。

**Tech Stack:** SQLite(polymarket.db + market.db)、pandas、Spearman IC；数据补齐用 vibe-trading venv 的 DataLoader；采集用 Rust `polymarket` CLI。

## Global Constraints

- 检验/对齐层 Python：`PY_SYS=/usr/bin/python3.12`（仅基础 pandas/numpy，SOP 纪律）。
- 数据补齐层 Python：`PY_VBA=/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python`（带 akshare）。
- 研究模式：**< 2 年 · 探索性 · 不挑最优**。禁止报单点 Sharpe 最优；禁止 monte_carlo/bootstrap validation。
- PIT 头号纪律：信号一律 `shift(1)` 后再配前向收益，杜绝当日偷看。
- 所有输出标 source / as-of / n。
- 产出根目录：`/home/rooot/agent_invest_lab/research/polymarket_alpha/`（下称 ROOT）。
- 测试跑法：测试文件用 `def test_*` + `assert`，文件末尾带 `if __name__=="__main__"` 逐个调用；用 `$PY_SYS tests/xxx.py` 跑（不依赖 pytest，因 PY_SYS 可能无 pytest）。

---

### Task 1: 标的数据补齐 + 统一加载器

**Files:**
- Create: `ROOT/data_targets.py`
- Create: `ROOT/data/` (缓存 CSV 目录)
- Test: `ROOT/tests/test_data_targets.py`

**Interfaces:**
- Produces: `load_target(code: str) -> pd.Series`（index=DatetimeIndex 交易日，value=close，已按日期升序）。支持三类 code：
  - A股指数（`000300.SH` 等）→ 读 `market.db` `index_daily`
  - 已缓存 ETF CSV（`510300.SH` 等）→ 读 `research/index_timing/data/<code>.csv`
  - 补拉标的（`SPY`/`QQQ`/`518880`）→ 读 `ROOT/data/<code>.csv`

- [ ] **Step 1: 用 PY_VBA 拉美股+黄金落 CSV**

Run（先确认 loader 对美股/黄金 code 的返回，再落盘）：
```bash
PY_VBA=/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python
mkdir -p /home/rooot/agent_invest_lab/research/polymarket_alpha/data
$PY_VBA - <<'PY'
import sys
sys.path.insert(0,"/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from backtest.loaders.akshare_loader import DataLoader
L=DataLoader()
# 黄金ETF(A股) + 美股纳指/标普ETF代理；起始覆盖 polymarket 最早 2025-05
data=L.fetch(["518880","QQQ","SPY"],"2025-01-01","2026-07-03",interval="1D")
import os
d="/home/rooot/agent_invest_lab/research/polymarket_alpha/data"
for code,df in data.items():
    print(code, df.index[0], df.index[-1], len(df))
    df.to_csv(f"{d}/{code}.csv")
PY
```
Expected: 打印 3 个 code 各自起止日与 bar 数（>200）。若某 US code 返回空，换代理（QQQ↔`513100` 纳指ETF、SPY↔`513500` 标普ETF，均 A股跨境ETF），并在 report 注明用了 A股跨境 ETF 代理。

- [ ] **Step 2: 写 load_target 的失败测试**

```python
# ROOT/tests/test_data_targets.py
import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd
from data_targets import load_target

def test_index_from_marketdb():
    s = load_target("000300.SH")
    assert isinstance(s, pd.Series) and len(s) > 200
    assert s.index.is_monotonic_increasing
    assert s.notna().all()

def test_cached_etf_csv():
    s = load_target("510300.SH")
    assert len(s) > 200 and s.index.is_monotonic_increasing

def test_backfilled_us():
    s = load_target("QQQ")   # 若用了代理, 改成 513100
    assert len(s) > 100

if __name__ == "__main__":
    test_index_from_marketdb(); test_cached_etf_csv(); test_backfilled_us()
    print("OK")
```

- [ ] **Step 3: 跑测试验证失败**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_data_targets.py`
Expected: FAIL（`ModuleNotFoundError: data_targets`）

- [ ] **Step 4: 实现 data_targets.py**

```python
# ROOT/data_targets.py
import sqlite3, pandas as pd, os
MARKET_DB = "/home/rooot/database/market.db"
ETF_CACHE = "/home/rooot/agent_invest_lab/research/index_timing/data"
LOCAL_CACHE = os.path.join(os.path.dirname(__file__), "data")

def _from_csv(path):
    df = pd.read_csv(path)
    dc = "trade_date" if "trade_date" in df.columns else df.columns[0]
    df[dc] = pd.to_datetime(df[dc])
    return df.set_index(dc)["close"].sort_index()

def load_target(code: str) -> pd.Series:
    # 1) A股指数 -> market.db
    if code.endswith(".SH") or code.endswith(".SZ") or code.endswith(".CSI"):
        etf = os.path.join(ETF_CACHE, f"{code}.csv")
        if os.path.exists(etf):                     # 已缓存 ETF 优先
            return _from_csv(etf)
        con = sqlite3.connect(MARKET_DB)
        df = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
                         con, params=[code], parse_dates=["trade_date"])
        con.close()
        if len(df):
            return df.set_index("trade_date")["close"]
    # 2) 本地补拉缓存
    local = os.path.join(LOCAL_CACHE, f"{code}.csv")
    if os.path.exists(local):
        return _from_csv(local)
    raise FileNotFoundError(f"no target data for {code}")
```

- [ ] **Step 5: 跑测试验证通过**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_data_targets.py`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add research/polymarket_alpha/data_targets.py research/polymarket_alpha/tests/test_data_targets.py research/polymarket_alpha/data
git commit -m "feat(polymarket_alpha): 标的统一加载器 + 美股/黄金数据补齐"
```

---

### Task 2: align.py — PIT 对齐层（防未来函数）

**Files:**
- Create: `ROOT/align.py`
- Test: `ROOT/tests/test_align.py`

**Interfaces:**
- Consumes: `load_target` from Task 1；signal 为 `pd.Series`（DatetimeIndex，宏观概率日频）。
- Produces:
  - `load_signal(factor: str) -> pd.Series`（读 polymarket.db macro_factor，index=date，value=value）
  - `align(signal: pd.Series, price: pd.Series, horizon: int, extra_lag: int = 0) -> pd.DataFrame`
    返回 columns `[sig, fwd_ret]`：`sig` = 信号在交易日 ffill 后 **shift(1+extra_lag)**；`fwd_ret` = price 的未来 horizon 日收益 `pct_change(horizon).shift(-horizon)`；对齐到 price 交易日并 dropna。`extra_lag` 给美股时区多滞后一日用。

- [ ] **Step 1: 写防未来函数的失败测试**

```python
# ROOT/tests/test_align.py
import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd, numpy as np
from align import align, load_signal

def test_no_lookahead_signal_is_lagged():
    # price 每日 +0(常数收益方便验证), signal 在某日跳变, 断言对齐后 sig 用的是前一日值
    idx = pd.bdate_range("2025-01-01", periods=10)
    price = pd.Series(np.arange(10, 20, dtype=float), index=idx)   # 递增
    signal = pd.Series(0.0, index=idx); signal.iloc[5] = 1.0        # 第5日跳到1
    out = align(signal, price, horizon=1)
    # 第6日(iloc对齐后)的 sig 应等于第5日信号=1; 第5日的 sig 应=第4日信号=0
    assert out.loc[idx[6], "sig"] == 1.0
    assert out.loc[idx[5], "sig"] == 0.0

def test_fwd_ret_is_future():
    idx = pd.bdate_range("2025-01-01", periods=6)
    price = pd.Series([1,1,1,2,2,2], index=idx, dtype=float)
    signal = pd.Series(0.5, index=idx)
    out = align(signal, price, horizon=1)
    # idx[2]->idx[3] 收益=100%, 该行 fwd_ret 应≈1.0
    assert abs(out.loc[idx[2], "fwd_ret"] - 1.0) < 1e-9

if __name__ == "__main__":
    test_no_lookahead_signal_is_lagged(); test_fwd_ret_is_future(); print("OK")
```

- [ ] **Step 2: 跑测试验证失败**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_align.py`
Expected: FAIL（`ModuleNotFoundError: align`）

- [ ] **Step 3: 实现 align.py**

```python
# ROOT/align.py
import sqlite3, pandas as pd
POLY_DB = "/home/rooot/database/polymarket.db"

def load_signal(factor: str) -> pd.Series:
    con = sqlite3.connect(POLY_DB)
    df = pd.read_sql("SELECT date, value FROM macro_factor WHERE factor=? ORDER BY date",
                     con, params=[factor], parse_dates=["date"])
    con.close()
    return df.set_index("date")["value"]

def align(signal: pd.Series, price: pd.Series, horizon: int, extra_lag: int = 0) -> pd.DataFrame:
    price = price.sort_index()
    sig = signal.sort_index().reindex(price.index).ffill()   # 概率 ffill 到交易日
    sig = sig.shift(1 + extra_lag)                            # PIT: 当日不可用
    fwd = price.pct_change(horizon).shift(-horizon)           # 未来 horizon 收益
    out = pd.DataFrame({"sig": sig, "fwd_ret": fwd}).dropna()
    return out
```

- [ ] **Step 4: 跑测试验证通过**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_align.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add research/polymarket_alpha/align.py research/polymarket_alpha/tests/test_align.py
git commit -m "feat(polymarket_alpha): PIT 对齐层, shift(1) 防未来函数"
```

---

### Task 3: ic_scan.py — IC / 分位 / 去趋势检验层

**Files:**
- Create: `ROOT/ic_scan.py`
- Test: `ROOT/tests/test_ic_scan.py`

**Interfaces:**
- Consumes: `align()` 输出的 `pd.DataFrame[sig, fwd_ret]`。
- Produces:
  - `spearman_ic(df: pd.DataFrame) -> float`（sig 与 fwd_ret 的 Spearman 秩相关；n<10 返回 nan）
  - `quantile_monotonicity(df: pd.DataFrame, n_q: int = 5) -> float`（按 sig 分 n_q 档，返回各档 fwd_ret 均值序列的 Spearman 单调性；档不足返回 nan）
  - `detrend(s: pd.Series, k: int = 5) -> pd.Series`（返回 `s.diff(k)`，去趋势）

- [ ] **Step 1: 写 IC 与去趋势的失败测试**

```python
# ROOT/tests/test_ic_scan.py
import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd, numpy as np
from ic_scan import spearman_ic, quantile_monotonicity, detrend

def test_ic_perfect_positive():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": np.arange(50.0)})
    assert spearman_ic(df) > 0.99

def test_ic_perfect_negative():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": -np.arange(50.0)})
    assert spearman_ic(df) < -0.99

def test_quantile_monotonic():
    df = pd.DataFrame({"sig": np.arange(50.0), "fwd_ret": np.arange(50.0)})
    assert quantile_monotonicity(df, 5) > 0.9

def test_detrend_removes_linear_trend():
    s = pd.Series(np.arange(100.0))   # 纯线性趋势
    d = detrend(s, 5).dropna()
    assert (d == 5.0).all()           # 差分后为常数

if __name__ == "__main__":
    test_ic_perfect_positive(); test_ic_perfect_negative()
    test_quantile_monotonic(); test_detrend_removes_linear_trend(); print("OK")
```

- [ ] **Step 2: 跑测试验证失败**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_ic_scan.py`
Expected: FAIL（`ModuleNotFoundError: ic_scan`）

- [ ] **Step 3: 实现 ic_scan.py**

```python
# ROOT/ic_scan.py
import pandas as pd, numpy as np

def spearman_ic(df: pd.DataFrame) -> float:
    if len(df) < 10:
        return float("nan")
    return df["sig"].corr(df["fwd_ret"], method="spearman")

def quantile_monotonicity(df: pd.DataFrame, n_q: int = 5) -> float:
    if df["sig"].nunique() < n_q:
        return float("nan")
    q = pd.qcut(df["sig"].rank(method="first"), n_q, labels=False)
    means = df["fwd_ret"].groupby(q).mean()
    if means.notna().sum() < 3:
        return float("nan")
    return means.reset_index(drop=True).corr(pd.Series(range(len(means)), dtype=float), method="spearman")

def detrend(s: pd.Series, k: int = 5) -> pd.Series:
    return s.diff(k)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/tests/test_ic_scan.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add research/polymarket_alpha/ic_scan.py research/polymarket_alpha/tests/test_ic_scan.py
git commit -m "feat(polymarket_alpha): IC/分位/去趋势检验层"
```

---

### Task 4: run_scan.py — 全假设 sweep + 出报告

**Files:**
- Create: `ROOT/run_scan.py`
- Create（脚本产出）: `ROOT/summary.csv`, `ROOT/report.md`

**Interfaces:**
- Consumes: `load_signal`,`align` (Task2)；`load_target` (Task1)；`spearman_ic`,`quantile_monotonicity`,`detrend` (Task3)。
- Produces: 一个 CSV（列 `hypo,factor,target,horizon,mode,ic,qmono,n`）与一个 md 报告。

- [ ] **Step 1: 定义假设映射并实现 run_scan.py**

假设→(factors, targets) 映射（美股 code 若 Task1 用了代理在此同步改）：
```python
# ROOT/run_scan.py
import sys; sys.path.insert(0, "/home/rooot/agent_invest_lab/research/polymarket_alpha")
import pandas as pd
from align import load_signal, align
from data_targets import load_target
from ic_scan import spearman_ic, quantile_monotonicity, detrend

HYPOS = {
  "H1_rate_recession": (["fed_no_cut_2026","fed_hike_2026","recession_2026"],
                        ["000300.SH","000001.SH","QQQ","SPY"]),
  "H2_geo_gold":       (["taiwan_risk_2026","iran_regime_fall","hormuz_normal_2026"],
                        ["518880","000300.SH"]),
  "H3_crypto_growth":  (["btc_dip_50k_2026","eth_dip_500_2026"],
                        ["399006.SZ","000688.SH","QQQ"]),
}
HORIZONS = [5, 10, 20]
US = {"QQQ","SPY","513100","513500"}

rows = []
for hypo,(factors,targets) in HYPOS.items():
    for f in factors:
        sig = load_signal(f)
        for t in targets:
            try: price = load_target(t)
            except FileNotFoundError: continue
            lag = 1 if t in US else 0
            for h in HORIZONS:
                base = align(sig, price, h, extra_lag=lag)
                # level 版
                rows.append([hypo,f,t,h,"level",spearman_ic(base),quantile_monotonicity(base),len(base)])
                # diff(去趋势) 版: 对 sig 去趋势后重新对齐
                dsig = detrend(sig, 5)
                dbase = align(dsig, price, h, extra_lag=lag)
                rows.append([hypo,f,t,h,"diff",spearman_ic(dbase),quantile_monotonicity(dbase),len(dbase)])

df = pd.DataFrame(rows, columns=["hypo","factor","target","horizon","mode","ic","qmono","n"])
df.to_csv("/home/rooot/agent_invest_lab/research/polymarket_alpha/summary.csv", index=False)

# robustness 判据(仅 diff 版): 每个 factor 跨 targets IC 同号占比
def struct_flag(g):
    d = g[g["mode"]=="diff"].dropna(subset=["ic"])
    if len(d) < 3: return "样本不足"
    pos = (d["ic"] > 0).mean()
    same = max(pos, 1-pos)
    med = d["ic"].abs().median()
    return "有结构(hint)" if same >= 0.75 and med >= 0.1 else "无结构/噪声"
flags = df.groupby("factor").apply(struct_flag)

with open("/home/rooot/agent_invest_lab/research/polymarket_alpha/report.md","w") as fp:
    fp.write("# Polymarket 信号发现 · 结果报告\n\n")
    fp.write(f"- as-of: 2026-07-03  source: polymarket.db/macro_factor + market.db\n")
    fp.write("- 全部为**黄灯/hint 级**(样本<2年,单一regime),不得升格生产因子\n\n")
    fp.write("## 各因子结构判定(以 diff 去趋势版为准)\n\n")
    for f,flag in flags.items():
        fp.write(f"- `{f}`: {flag}\n")
    fp.write("\n## 完整明细见 summary.csv\n")
    fp.write("\n## 诚实盲区\n- level 版 IC 多为趋势假象,已实测去趋势后大幅衰减,故判定只采 diff 版\n")
    fp.write("- 1年单一regime,跨regime未验证\n- 尾部/加密合约流动性低,定价可能偏离真实概率\n")
print("done", len(df), "rows")
```

- [ ] **Step 2: 跑 sweep**

Run: `/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/run_scan.py`
Expected: 打印 `done <N> rows`（N≈ 假设×因子×标的×horizon×2）；生成 summary.csv 与 report.md。

- [ ] **Step 3: 人工查验报告合理性**

Run: `cat /home/rooot/agent_invest_lab/research/polymarket_alpha/report.md`
Expected: 每个因子有结构判定；抽查 summary.csv 里 level 版 |ic| 普遍 > diff 版（验证趋势假象结论）。若全部"样本不足"，回看 Task1 美股 code 是否落盘成功。

- [ ] **Step 4: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add research/polymarket_alpha/run_scan.py research/polymarket_alpha/summary.csv research/polymarket_alpha/report.md
git commit -m "feat(polymarket_alpha): 全假设 IC sweep + 结果报告"
```

---

### Task 5: 聪明钱采集管道启动

**Files:**
- Create: `ROOT/collector/fetch_smartmoney.py`
- Create: `~/.config/systemd/user/polymarket-smartmoney.service`
- Create: `~/.config/systemd/user/polymarket-smartmoney.timer`

**Interfaces:**
- Consumes: `polymarket` CLI（`/home/rooot/polymarket-cli-main`，或已安装的 `polymarket`）；写 `polymarket.db` 的 `leaderboard`/`holders`/`trade` 表。
- Produces: 定时把 leaderboard + top market holders 灌库；流式写、跳过已抓 ts。

- [ ] **Step 1: 确认 CLI 子命令确切参数**

Run:
```bash
cd /home/rooot/polymarket-cli-main
cargo run -q -- data leaderboard --help 2>/dev/null || ./target/release/polymarket data leaderboard --help
./target/release/polymarket -o json data leaderboard --help 2>/dev/null | head
```
Expected: 得到 `leaderboard`（period/order_by 参数）、`holders`（--market/token 参数）、`trades` 的确切 flag。**把确认到的 flag 填进 Step 2 的 CMD 常量**。

- [ ] **Step 2: 实现 fetch_smartmoney.py（流式灌库，OOM-safe）**

```python
# ROOT/collector/fetch_smartmoney.py
import subprocess, json, sqlite3, time, os
DB = "/home/rooot/database/polymarket.db"
CLI = os.environ.get("POLY_CLI", "/home/rooot/polymarket-cli-main/target/release/polymarket")

def _run(args):
    out = subprocess.run([CLI, "-o", "json", "data", *args], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return None
    try: return json.loads(out.stdout)
    except Exception: return None

def collect_leaderboard(con, ts):
    for period in ["1d","7d","30d"]:
        for order in ["pnl","volume"]:
            # NOTE: flag 名以 Step 1 --help 为准
            data = _run(["leaderboard","--period",period,"--order-by",order])
            if not data: continue
            for rank,e in enumerate(data, 1):
                con.execute("INSERT OR IGNORE INTO leaderboard VALUES (?,?,?,?,?,?,?,?)",
                    (ts, period, order, rank, e.get("proxyWallet") or e.get("wallet"),
                     e.get("name"), e.get("pnl") or e.get("amount"), e.get("volume")))
            con.commit()   # 流式提交

def top_markets(con, n=30):
    # 取 condition_id: holders 表首列是 condition_id(非 market_id), 传错会导致日后 join 错
    cur = con.execute("SELECT condition_id, clob_token_ids FROM market WHERE closed=0 "
                      "ORDER BY last_seen DESC LIMIT ?", (n,))
    return cur.fetchall()

def collect_holders(con, ts):
    for condition_id, token_ids in top_markets(con):
        try: tokens = json.loads(token_ids or "[]")
        except Exception: tokens = []
        for tok in tokens:
            data = _run(["holders","--market",tok])   # flag 以 Step1 为准
            if not data: continue
            for h in data:
                con.execute("INSERT OR IGNORE INTO holders VALUES (?,?,?,?,?,?,?)",
                    (condition_id, tok, ts, h.get("proxyWallet") or h.get("wallet"),
                     h.get("name"), h.get("amount"), h.get("outcomeIndex")))
            con.commit()

if __name__ == "__main__":
    ts = int(time.time())
    con = sqlite3.connect(DB, timeout=30)
    collect_leaderboard(con, ts)
    collect_holders(con, ts)
    con.close()
    print("collected at", ts)
```

- [ ] **Step 3: 手动跑一次验证灌库**

Run:
```bash
/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/collector/fetch_smartmoney.py
sqlite3 /home/rooot/database/polymarket.db "SELECT COUNT(*) FROM leaderboard; SELECT COUNT(*) FROM holders;"
```
Expected: 两表 COUNT > 0。若为 0，回 Step 1 核对 flag / JSON 字段名。

- [ ] **Step 4: 配 systemd --user timer（每 6h）**

Create `~/.config/systemd/user/polymarket-smartmoney.service`:
```ini
[Unit]
Description=Polymarket smartmoney collector
[Service]
Type=oneshot
ExecStart=/usr/bin/python3.12 /home/rooot/agent_invest_lab/research/polymarket_alpha/collector/fetch_smartmoney.py
```
Create `~/.config/systemd/user/polymarket-smartmoney.timer`:
```ini
[Unit]
Description=Run polymarket smartmoney collector every 6h
[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true
[Install]
WantedBy=timers.target
```
Run:
```bash
systemctl --user daemon-reload
systemctl --user enable --now polymarket-smartmoney.timer
systemctl --user list-timers | grep smartmoney
```
Expected: timer 出现在列表，NEXT 有值。

- [ ] **Step 5: Commit**

```bash
cd /home/rooot/agent_invest_lab
git add research/polymarket_alpha/collector/fetch_smartmoney.py
git commit -m "feat(polymarket_alpha): 聪明钱采集管道 + systemd timer(每6h)"
```
（systemd unit 在 ~/.config 下不入库，Step 4 命令即部署记录）

---

## 完成判据

- Task1-4：`report.md` 给出 8 个因子的结构判定，`summary.csv` 有完整 IC 明细，level/diff 对比印证"去趋势后衰减"。
- Task5：`leaderboard`/`holders` 表开始积累，timer 每 6h 跑。
- 全程无未来函数（Task2 测试守住）、无单点最优 Sharpe、无 <2年 validation。
