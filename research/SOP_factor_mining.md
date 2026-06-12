# 用 vibe-trading 做因子挖掘 SOP（给其他 Claude 窗口）

**写这份的目的**：研究部已经做过 2 轮因子挖掘（[index_timing/](index_timing/) + [sr_factor/](sr_factor/)），下一个 Claude 窗口照着走可以直接复用代码 + 避开踩过的坑。

**vibe-trading-ai 是什么**：A 股 / 港美股 / crypto 通用回测框架，**重点是它的 `backtest.validation` 套件**（monte carlo 显著性检验、bootstrap Sharpe CI、walk-forward 一致性）——因子挖掘最需要的统计验证它都内置了，不用自己造轮子。

---

## 第 1 部分：vibe-trading 库速览

### 1.1 安装位置 + 环境

```bash
# 已经装好的位置
VBA=/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai
PYTHON=$VBA/bin/python      # 这是 python 3.11，带 pandas/numpy/akshare/tushare 的完整环境
CLI=$VBA/bin/vibe-trading   # CLI 入口
MCP=$VBA/bin/vibe-trading-mcp  # MCP server 入口（暂未使用）

# Python import 时用
sys.path.insert(0, "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
# 或者直接用 venv python: $VBA/bin/python
```

**注意**：系统 python 3.12 用不了 vibe-trading 自带的 numpy（编译时绑定的是 python 3.11）。**必须用 `$VBA/bin/python`** 才能跑 `import akshare` / `from backtest...`。

### 1.2 包结构 + 关键模块

```
backtest/
├── runner.py            ← CLI 入口: python -m backtest.runner <run_dir>
├── models.py            ← Position / TradeRecord / EquitySnapshot dataclasses
├── metrics.py           ← calc_metrics(equity_curve, trades, ...) 标准指标
├── validation.py        ★ monte_carlo_test / bootstrap_sharpe_ci / walk_forward_analysis
├── correlation.py       ← 跨标的相关性矩阵
├── benchmark.py         ← 与 buy&hold / 指数对比
├── run_card.py          ← 报告生成
├── engines/             ← 各市场回测引擎
│   ├── base.py          ← BaseEngine.run_backtest() 通用循环
│   ├── china_a.py       ★ A 股引擎（含 T+1、涨跌停、ST 等规则）
│   ├── china_futures.py
│   ├── crypto.py
│   ├── global_equity.py
│   ├── forex.py
│   └── composite.py     ← 多市场组合
├── loaders/             ← 数据加载器（统一 fetch 接口）
│   ├── akshare_loader.py ★ 免费 A股/ETF/HK/US（无 token）
│   ├── tushare.py        ★ Tushare（需 token，已配置 reference_local_llm_data_creds）
│   ├── yfinance_loader.py / okx.py / ccxt_loader.py / futu.py
│   └── registry.py       ← resolve_loader(market) 自动路由
└── optimizers/          ← 多标的组合优化（单因子用不到）
    ├── equal_volatility.py / max_diversification.py
    ├── mean_variance.py / risk_parity.py
```

### 1.3 三种使用方式（按场景挑）

| 场景 | 推荐方式 | 命令 / API |
|---|---|---|
| **跑 1-2 个因子的全套验证**（含 monte carlo + CI） | runner CLI | 写 `<run_dir>/config.json` + `<run_dir>/code/signal_engine.py`，`python -m backtest.runner <run_dir>` |
| **批量 sweep 40-100 个因子变体** | 库 import + 自写 backtest | 只用 `from backtest.loaders.akshare_loader import AkshareLoader`，回测引擎自己写（更快、不起 subprocess） |
| **挖完后给统计置信** | 库 import validation | `from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis` |
| **自然语言探索** | `vibe-trading -p "..."` 或 interactive | 一次性 idea 验证；不适合产线挖掘 |

---

## 第 2 部分：因子挖掘 9 步流程（每步配 vibe-trading 用法）

### 步骤 0：必读纪律（任何一步都不能跳）

| 纪律 | 为什么 | 违反会怎样 |
|---|---|---|
| **先探数据可用性，再设计因子** | 起止日决定能不能做 IS/OOS | 设计完才发现 n=16 个月——白干 |
| **样本不够 5 年不要做单点最优挑选** | 单 OOS = 单次蒙特卡洛抽样，标准误大到吞掉所有"显著" | 报 "Sharpe 2.5" 而 95% CI 包含 0 |
| **挑因子用 Calmar 不用 Sharpe** | Sharpe 重视均值、Calmar 重视回撤——实盘体验差很多 | 找到 Sharpe 高但 -40% DD 的因子，用户扛不住 |
| **5bp 单边成本是底线** | A 股 ETF 实际成本 + 滑点 3-8bp | 高翻转因子在回测里赢，实盘亏成本 |
| **T+1 处理（`pos = sig.shift(1)`）** | 信号当日可见、当日不能进场 | 偷看未来 → 回测漂亮、实盘归零 |
| **所有数字标 source / as-of / n** | 不标的数字是噪声 | 决策被无来源数字带偏 |
| **不要伪精确** | "Sharpe = 1.847" 在 n=400 时四位有效数字全是噪声 | 给用户错误的置信感 |

---

### 步骤 1：假设设计（1-2 段话）

**写下来**：
- 因子假设是什么（一句话）
- 经济逻辑（一句话）——例如"散户大额净申购堆积 → 资金尾声 → 后续 5-20 日大盘走弱"
- 它在什么环境会失效（一句话）——"政策黑天鹅 / 风格切换拐点"

**没有经济逻辑的纯数据挖掘大概率是过拟合，停手**。

---

### 步骤 2：数据探查（10 分钟，必做）

#### 用 vibe-trading 的 loader 探数据
```python
import sys
sys.path.insert(0, "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from backtest.loaders.akshare_loader import AkshareLoader

loader = AkshareLoader()
data = loader.fetch(["510300.SH", "512100.SH"], "2017-01-01", "2026-05-26", interval="1D")
# data: Dict[code, DataFrame]，columns = [open, high, low, close, volume]
for code, df in data.items():
    print(code, df.index[0], df.index[-1], len(df))
```

#### 用 simworld-mcp 探非行情数据
```python
# 二分搜索最早可用日（举例：申赎数据）
for d in ["2017-01-04", "2020-01-04", "2024-01-04", "2025-01-04", "2026-01-04"]:
    r = call_simworld_mcp("fund_subscription_redemption_summary", {"calc_date": d})
    print(d, "rows:", len(r.get("items") or []))
```

**通过判据**：清楚知道
- 最早 / 最晚日期、bar 数
- 每条记录字段含义
- 是否有 PIT 闸门、闸门时刻

**之前踩过的坑**：
- 指数 `market_index_quote` 只返回 close，**没有 high/low** → 切到 close 代理或换 ETF
- `fund_subscription_redemption_summary` 只有 2024-07-11 之后的数据
- akshare 通过 `_is_etf_listed` 自动路由 ETF 代码到 `fund_etf_hist_sina`（不要手动 prefix `sh/sz`，loader 已处理）

---

### 步骤 3：决定研究模式（根据样本量）

| 样本量 | 模式 | 能宣称什么 | vibe-trading 工具 |
|---|---|---|---|
| **≥ 5 年** | IS/OOS 严格 | "OOS 击败 buyhold，生产因子候选" | runner CLI + validation 全套 |
| **2-5 年** | IS/OOS 探索性 | "OOS 表现一致，建议 shadow 6-12 月" | 自写 backtest + validation 加 CI |
| **< 2 年** | 纯探索性，**不挑最优** | "有结构，仅作 hint" | 相关性 + 分位组合 + parameter sweep |
| **< 6 月** | 不做 | "数据不足" | — |

**< 2 年时必做**：参数 robustness sweep（看大半个参数空间是不是普遍 work），不是只挑最好的角落。

---

### 步骤 4：数据准备（缓存到本地 CSV）

**所有数据先 fetch 到 CSV**——sweep 时不要每次重新 fetch。

#### OHLCV：直接调 vibe-trading 的 loader
```python
from backtest.loaders.akshare_loader import AkshareLoader
loader = AkshareLoader()
data = loader.fetch(["510300.SH", "512100.SH", "512480.SH", "588800.SH"],
                    "2016-01-01", "2026-05-26", interval="1D")
for code, df in data.items():
    df.to_csv(f"data/{code}.csv")  # 直接落盘
```

#### PIT 单点数据：流式写盘 + 断点续传（**OOM-safe，必须**）
模板见 [sr_factor/fetch_sr.py](sr_factor/fetch_sr.py)：每抓一天就 `f.flush()`、跳过已抓的日期、不囤内存。系统经常很紧（多个 bot run 在跑），不流式写盘会被 OOM kill。

#### 已经缓存的数据（直接复用，不要重抓）
- `research/index_timing/data/510300.SH.csv` HS300 ETF 2016-2026
- `research/index_timing/data/512100.SH.csv` ZZ1000 ETF 2016-2026
- `research/sr_factor/data/512480.SH.csv` 半导体 ETF 2019-2026
- `research/sr_factor/data/588800.SH.csv` 双创50 ETF 2023-2026
- `research/sr_factor/sr_daily.csv` 全市场基金申赎汇总 2024-07~2026-05
- `/home/ubuntu/rooot/MCP/simworld-mcp/sr_cache.csv` 同上（MCP 跑时读这个）

---

### 步骤 5：候选因子族设计

A 股单指数择时常见 5 族（覆盖完再考虑别的）：

| 族 | 默认仓位 | 触发 | 代表因子 |
|---|---|---|---|
| **default-long de-risk** | 1.0 | 出 danger flag 才切 0 | crash, crash+bear, deep-dd, vol-trim |
| **trend-on long-only** | 0 | 趋势成立才进 1 | MA200+sw20, donchian-60/120 |
| **DD-first** | 0..1 | 按回撤档机械切 | trail_dd, chand, dd_ladder, vol_target |
| **momentum / mean-reversion** | binary | N 日收益符号 | mom_60, RSI_panic, bbands_break |
| **资金面 / 情绪 contrarian** | 0..1 | 散户情绪 z | retail_sr_z, vix_extreme |

**模板**：[index_timing/local_sweep.py](index_timing/local_sweep.py) 里 44 个候选因子可直接复用。

**设计原则**：
1. 每个因子的"是什么 / 会漏什么"要能一句话说清——说不清的是凭空堆参数
2. 变体之间要正交（5 个变体都是 MA20 改阈值 = 1 个因子）
3. 5bp 成本下年翻转 < 30 健康、> 100 打问号

---

### 步骤 6：第一轮 sweep（**40+ 因子用自写 backtest，不用 runner**）

#### 为什么不用 runner
vibe-trading 的 `python -m backtest.runner <run_dir>` 每次起 subprocess + 重连数据源，**40 因子 sweep 要 30+ 分钟且容易超时**。runner 适合 1-2 个因子的"正经一次"。

#### 自写 backtest 模板（**已经验证 work**）
直接 copy [index_timing/local_sweep.py](index_timing/local_sweep.py) 里的 `backtest()`：

```python
def backtest(df_window, sig, eval_start):
    sig = sig.clip(0.0, 1.0).fillna(0.0).reindex(df_window.index).fillna(0.0)
    ret = df_window["close"].pct_change().fillna(0.0)
    pos = sig.shift(1).fillna(0.0)          # ← T+1 处理，关键
    turnover = pos.diff().abs().fillna(pos.iloc[0])
    cost = turnover * 0.0005                # ← 5bp 单边
    strat_ret = pos * ret - cost
    mask = df_window.index >= pd.Timestamp(eval_start)  # ← 砍掉 warmup
    sr = strat_ret.loc[mask]
    # 算 sharpe, calmar, max_dd, sortino, exposure, flips ...
```

**排名用 Calmar，不用 Sharpe**。**通过判据**：top-K 因子的 Calmar 明显优于 buyhold（差距 > 0.3），且 OOS 同方向。

---

### 步骤 7：Robustness sweep（**关键，决定能不能升格**）

#### 为什么必须做
第 6 步的"赢家"很容易是过拟合——测了 40 个因子挑最好的，selection bias 必然 inflate 表现。

#### 怎么做
跑参数空间 sweep（如 `4 src × 4 cumN × 3 z_win × 3 thr = 144 配置`），看赢家邻域整体是否赢。

**模板**：[sr_factor/robustness.py](sr_factor/robustness.py)（576 个回测的完整实现）。关键统计输出：
- 跨参数 mean / median dCalmar（vs buyhold）
- "全部指数都击败 buyhold" 的配置占比
- 按信号源分组的 beat rate（看哪些源真信号、哪些方向反了）
- cumN × z_win heatmap（看最优区域是连续的还是孤岛）

**通过判据**（任一不通过就停手）：
- ≥ 40% 配置在大多数指数上击败 buyhold Calmar（< 10% 是过拟合）
- median dCalmar > +0.3（中央趋势就好，不只是头部）
- 最优区域在参数空间中央，不在角落
- 跨多个标的方向一致（3/4 反向 = 不是 robust 信号）

#### 例子
- `market_retail_contrarian_15_90`：robustness sweep 88% Calmar 击败率（144 配置）→ 包装了
- 假如只 10% beat → 不包装

---

### 步骤 8：用 vibe-trading 的 validation 套件给统计置信（**这是 vibe-trading 真正独特的地方**）

**自己写 robustness sweep 只看"跨参数稳定"；validation 给"vs 随机基线显著"和"vs 子样本一致"**——两者互补。

```python
import sys
sys.path.insert(0, "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
from backtest.validation import monte_carlo_test, bootstrap_sharpe_ci, walk_forward_analysis
from backtest.models import TradeRecord  # 输入要这个格式

# 1) Monte Carlo 置换检验：随机洗牌交易顺序能不能复制 Sharpe / DD
#    需要 trades 列表（每笔有 pnl, entry_time）
mc = monte_carlo_test(trades, initial_capital=1_000_000, n_simulations=1000, seed=42)
# 返回：actual_sharpe, p_value_sharpe, actual_max_dd, p_value_max_dd, ...
# p_value_sharpe < 0.05 = 显著优于"随机交易顺序"
# 注意：这是路径检验，不是收益预测能力检验

# 2) Bootstrap Sharpe CI：resample 日收益估 Sharpe 的 95% CI
ci = bootstrap_sharpe_ci(equity_curve, n_bootstrap=1000, confidence=0.95)
# 返回：observed_sharpe, ci_lower, ci_upper, median_sharpe, prob_positive
# ci_lower > 0 才算 Sharpe 显著为正；prob_positive > 95% 是更严的判据

# 3) Walk-Forward 一致性：拆 N 个子窗，看各窗表现
wf = walk_forward_analysis(equity_curve, trades, n_windows=5)
# 返回：per-window {return, sharpe, max_dd, win_rate, ...} + 一致性指标
# 5 个窗里至少 4 个 Sharpe > 0 才算时间稳定
```

**通过判据组合**：
- `monte_carlo p_value_sharpe < 0.05` AND
- `bootstrap ci_lower > 0` (95% CI 完全为正) AND
- `walk_forward` 至少 4/5 子窗 Sharpe > 0

**前提**：要做这些验证，回测得产出 `equity_curve` (pd.Series) 和 `trades` (List[TradeRecord])。自写的 `backtest()` 默认只算 metrics 不存 trades——产线因子要补一步把 trades 列表也存出来。

#### 如果只有 < 2 年样本，跳过 validation 套件
样本太短 p 值 / CI 都不可靠；老老实实做 robustness sweep 就够了。

---

### 步骤 9：决定是否包装

#### 不要包装的情况（任一）
- 样本 < 2 年 且 robustness < 40%
- 经济逻辑说不清
- 只在 1 个标的上 work
- 翻转 > 100 次/年
- monte_carlo p_value > 0.10（如果做了 validation）
- bootstrap ci_lower < 0（如果做了 validation）

#### 可以包装的情况
- 样本 ≥ 5 年 OOS Calmar > 1.0 + monte_carlo p < 0.05
- 或样本 2-5 年 + 跨指数 + 跨参数 robust + 经济逻辑成立

#### 包装时同时写清楚
- 是 OOS 验证因子（绿灯）还是探索性因子（黄灯）
- 已知盲区（**从公式可推、不是回测发现**）
- 与现有因子的相关性

---

### 步骤 10：包装到 `quant_factor` + 同步 bot methodology

#### simworld-mcp 的 `quant_factor` 工具
位置：[`/home/ubuntu/rooot/MCP/simworld-mcp/server.py`](../../MCP/simworld-mcp/server.py) 末尾 `_QUANT_FACTORS` 注册表。两种 kind：

```python
_QUANT_FACTORS = {
    "your_factor_name": {
        "kind": "index_close",       # 或 "market_sr"
        "symbol": "000852.SH",       # index_close 用；market_sr 设 None
        "alias": "中证1000",
        "min_bars": 80,              # 预热所需 bar 数
        "compute": _your_compute_fn, # index_close: takes close series; market_sr: takes t_date
        "doc": "...",                # 给 bot 看的释义
    },
}
```

#### 包完之后必做的 3 件事
1. **重启 simworld-mcp**：`bash /home/ubuntu/rooot/MCP/simworld-mcp/restart.sh`
2. **smoke test**：`server.quant_factor(simulated_datetime="2026-05-26 15:30:00")` 几个历史时点
3. **加进 world.yaml 的 `simworld_tools` 白名单**：[`world/config/world.yaml`](../world/config/world.yaml)（已有 `quant_factor` 条目就不用动）

#### bot methodology 同步
- 通用文案改 [`bots/common/agents_common.md`](../bots/common/agents_common.md) + 跑 [`bots/common/sync.py`](../bots/common/sync.py) 同步到 20 个 bot
- 特定 bot（如 bot14 是 ZZ1000）改 `bots/botN/METHODOLOGY.md`，把因子写进"量化择时辅助"小节，包括：
  - 是什么（一句话）
  - 会漏什么（已知盲区）
  - 怎么用（与框架打分如何配合，**不要钉死硬规则**——见诚实纪律）

---

## 第 3 部分：已经生产化的因子（**不要重复挖**）

| 因子名 | kind | 标的 | 验证程度 |
|---|---|---|---|
| `zz1000_donchian_60` | index_close | 000852 | OOS 2.4 年 Cal≈1.06 |
| `zz1000_dd_ladder_10_20` | index_close | 000852 | OOS 2.4 年 Cal≈0.82 |
| `market_retail_contrarian_15_90` | market_sr | 全市场 | 探索性，n=16 月 robust 88% |
| `market_retail_contrarian_20_90` | market_sr | 全市场 | 同上，平滑版 |

**下一步候选挖矿方向**（按可行性排）：
1. 50ETF VIX 极值 contrarian（数据 `macro_50etf_vix` 历史长）
2. 创业板指 (399006) / 半导体 (931865) / 双创50 (931643) 的趋势 + DD 因子（复用 index_timing 模板）
3. 北向资金 contrarian（沪深300 / 上证50 适用，ZZ1000 不灵）
4. 板块拥挤度 → 板块择时（`sector_factor_detail` 拆出的拥挤度子因子）

---

## 第 4 部分：失败模式 / 红旗

看到这些信号就停手 / 回退：

| 红旗 | 原因 | 怎么办 |
|---|---|---|
| 只在 1 个标的上 work | 极可能是单标的样本噪声 | 至少在 3 个同类标的上验证 |
| 最优参数在网格角落 | 撞到测试边界，真实最优可能在网格外 | 扩展网格再跑 |
| Sharpe > 3 在 < 2 年样本 | 几乎肯定有偷看未来 / 数据泄漏 | 检查 sig.shift(1)、检查 PIT 闸门 |
| 翻转 > 200 次/年 | 实盘成本吞噬一切 | 加大 cost 到 10bp 重测 |
| IS / OOS 差距 > 50% | 过拟合 | 缩减参数 / 简化因子 |
| 只能用复杂 ML 才 work | 简单规则下不 work，信号弱 | 不要包装 |
| 因子和已有因子相关性 > 0.8 | 冗余 | 不要包装 |
| 经济逻辑说不清 | 故事是事后编的 | 不要包装 |
| monte_carlo p_value > 0.10 | 随机交易也能复制 | 不要包装 |
| bootstrap ci_lower < 0 | 95% CI 包含 0，不显著为正 | 不要包装 |
| walk-forward 仅 1-2 个子窗 Sharpe > 0 | 时间不稳定，靠某段窗口撑起整体 | 不要包装 |

---

## 第 5 部分：诚实纪律（写给 methodology / AGENTS.md 的）

之前在 bot14 METHODOLOGY 踩过坑：把因子用法写成 12 行硬规则（"踩踏时砍到防守仓"、"风格切换前 10-20 天因子失效"），用户立刻指出"这些是你拍脑袋还是科学的"。**结论：只能写从因子公式可推、或有数据支持的事，不能写没回测过的条件规则。**

✅ **可以写**：
- "donchian 在窄区间会反复无效突破"（突破系统的数学性质）
- "dd_ladder 要 120 日峰值刷新才触发，单日闪崩跟不上"（公式直接可读）
- "机构同公式呈 trend-following，不可作 contrarian 用"（robustness sweep 验证）

❌ **不能写**：
- "因子=0 且流动性恶化 → 加速降仓"（"加速降仓"没回测过）
- "风格切换前 10-20 天因子失效"（"10-20 天"凭空编）
- "冲突时默认信因子方向"（没有"因子 vs 框架"对比回测）

**正确写法**：列出从公式可推的盲区 + 给 bot 一个判断框架（"先看是不是落进盲区，不在就把因子和框架打分对照"），让 bot **判断**，不让 bot **机械执行未经验证的规则**。

---

## 第 6 部分：资源索引（快查）

### 工具 / 命令
| 用途 | 命令 / 路径 |
|---|---|
| vibe-trading venv python | `/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python` |
| vibe-trading site-packages（import 路径） | `/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages` |
| vibe-trading CLI（交互/单次） | `vibe-trading` 或 `vibe-trading -p "..."` |
| simworld-mcp 端口 | 18078 |
| simworld-mcp 重启 | `bash /home/ubuntu/rooot/MCP/simworld-mcp/restart.sh` |
| 系统 python（pandas 3.0 + numpy 2.4） | `/usr/bin/python3.12`（仅基础 pandas，不能 import backtest 包） |

### 已有研究产物（可直接读 / 改）
| 文件 | 用途 |
|---|---|
| [index_timing/local_sweep.py](index_timing/local_sweep.py) | self-contained backtest 引擎 + 44 因子族模板 |
| [index_timing/summary.csv](index_timing/summary.csv) | ZZ1000 / HS300 因子 IS/OOS 结果 |
| [sr_factor/fetch_sr.py](sr_factor/fetch_sr.py) | 流式 + 断点续传 MCP 数据 fetch 模板 |
| [sr_factor/analyze.py](sr_factor/analyze.py) | 相关性 + 分位组合分析模板 |
| [sr_factor/robustness.py](sr_factor/robustness.py) | 参数空间 sweep 模板（576 个回测） |
| [sr_factor/backtest_summary.csv](sr_factor/backtest_summary.csv) | 4 指数 × 5 变体探索性结果 |
| [sr_factor/robustness_full.csv](sr_factor/robustness_full.csv) | 576 配置完整 robustness 结果 |

### 关键文件位置速查
- simworld-mcp 因子注册表：`/home/ubuntu/rooot/MCP/simworld-mcp/server.py`（末尾 `_QUANT_FACTORS`）
- world 工具白名单：`/home/ubuntu/rooot/agent_invest_lab/world/config/world.yaml`
- bot 通用模板：`/home/ubuntu/rooot/agent_invest_lab/bots/common/agents_common.md`
- bot 方法论：`/home/ubuntu/rooot/agent_invest_lab/bots/botN/METHODOLOGY.md`

### vibe-trading 库内重要文件（看源码用）
- `backtest/runner.py` — config schema + 入口
- `backtest/engines/base.py` — `BaseEngine.run_backtest()` 通用循环
- `backtest/engines/china_a.py` — A 股 T+1 + 涨跌停规则
- `backtest/loaders/akshare_loader.py` — 自动路由 ETF / 股票 / HK / US
- `backtest/validation.py` — **3 个验证工具的实现**，看签名 + 返回字段
- `backtest/metrics.py` — `calc_metrics()` 完整指标
- `backtest/models.py` — `TradeRecord` / `Position` / `EquitySnapshot` 数据结构

---

## 一句话总结

**用 vibe-trading 的 loader 拉数据、自写快速 backtest 做 sweep、用 vibe-trading 的 validation 套件（monte_carlo + bootstrap_ci + walk_forward）确认显著性、用 robustness sweep 确认不是过拟合、用经济逻辑解释为什么 work、用诚实的语言告诉 bot 它会漏什么——剩下的就是写代码。**
