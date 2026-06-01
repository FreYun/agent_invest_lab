# 创新药择时因子挖掘报告

日期：2026-06-01  
研究目录：`research/innodrug_sr`  
口径：PIT 数据优先；交易信号统一 T+1；单边成本 5bp；短样本不包装生产因子。

## 1. 结论摘要

本轮围绕创新药 ETF / 创新药指数做了多轮因子挖掘，覆盖申赎、板块资金流、趋势、拥挤度、估值、市场温度、股债性价比、VIX、美债、成长风格、重仓股资金流等维度。

最终结论分三层：

| 层级 | 因子 | 定级 | 用法 |
|---|---|---|---|
| 主仓位锚 | `market_retail_contrarian_20_90` | 绿灯，但不是创新药专属 | 作为创新药仓位的市场级情绪锚；散户全市场申购过热时降仓，赎回恐慌时偏多 |
| 本地确认因子 | `sr_trend_confirm_combo` | 黄灯+ | 作为创新药专属确认信号；不单独硬交易，不注册生产 `quant_factor` |
| 单因子 hint | 创新药自身散户申赎反向 | 黄灯 | 观察创新药自身散户过热/恐慌；只作解释和确认 |

一句话：**除了总体申赎，最有价值的新发现是创新药自身“散户申赎反向 + 趋势确认”的复合情绪因子；它比单一创新药申赎更稳，但仍因样本太短未达绿灯。**

## 2. 数据与样本

### 2.1 创新药自身申赎

- 数据源：`fund_index_subscription_redemption`，指数 `931152`。
- 区间：2024-08-01 至 2026-05-26。
- 主要口径：个人/散户。机构数据极稀疏，仅约 36 天有记录，不能形成连续序列。
- 原始申赎含自然日和周末，因此回归轮已修正为：先对齐 ETF 交易日，再 rolling 计算。
- 核心字段：申请、赎回、净申赎、净申赎除定投。

### 2.2 价格代理

| 代理 | 含义 |
|---|---|
| `159992.SZ` | 主标的，创新药 ETF |
| `516080.SH` | 创新药 ETF 代理 |
| `159839.SZ` | 创新药 ETF 代理 |
| `512010.SH` | 泛医药 ETF，用作迁移/弱泛化探针 |

前三个本质都跟踪创新药相关指数，跨标的稳健性主要是 tracking-error 层面，不是完全独立的行业泛化。

### 2.3 板块与外部变量

| 维度 | 数据 | 结论 |
|---|---|---|
| 板块行情/估值/主力流入 | `sector_market(BK000208)`，2019-04 至 2026-05 | 主力资金流无稳定领先性 |
| 板块动量/拥挤度 | `sector_factor` / `sector_factor_detail` | IR 动量和集中度可作确认分量 |
| 市场温度 | `market_temperature` | 单独不稳，接入 combo 后拖累 |
| 宽基/成长风格 | 科创50、创业板、沪深300、中证1000、中证500等 | 短窗解释力强，交易化不稳 |
| 股债性价比 | `market_index_gzxjb` | 更适合组合风控，不是创新药专属 alpha |
| 50ETF VIX | `macro_50etf_vix` | 有情绪解释力，但不能稳定增强创新药规则 |
| 美债收益率 | `bond_yield_curve(curve_type="us")` | 逻辑成立，实证不稳 |
| 重仓股资金流 | `stock_capital_flow` | 当前持仓诊断可用，日频因子未通过 |

## 3. 研究纪律

本轮沿用 `research/SOP_factor_mining.md`：

- 信号必须 T+1，不能当日信号当日交易。
- 成本统一用单边 5bp。
- 因子优先看 Calmar 和回撤，不只看 Sharpe。
- 申赎样本不足 2 年，不做“单点最优”式生产包装。
- 参数 robustness 比单个回测点更重要。
- 对短样本、长 rolling 窗、重叠前瞻收益保持怀疑。

## 4. 候选因子一：创新药自身散户申赎反向

### 4.1 构造

核心变量：

```text
imbalance = (个人申请 - 个人赎回) / (个人申请 + 个人赎回)
retail_imb_z15_90 = zscore(rolling_sum(imbalance, 15), 90)
retail_net_z15_90 = zscore(rolling_sum(个人净申赎, 15), 90)
```

交易读法：

- z 高：散户申购过热，后续 20 日偏弱，反向减仓。
- z 低：散户赎回/冷却，后续反弹概率提高，反向加仓。

### 4.2 证据

前瞻 20 日 Spearman IC：

| 分量 | fwd20 IC | 解释 |
|---|---:|---|
| `retail_imb_z15_90` | -0.356 | 散户申赎强度反向，方向最稳 |
| `retail_net_z15_90` | -0.330 | 散户净申赎反向，同源有效 |

第一轮 sweep 的关键结果：

| 方向 | mean dCalmar | 全 4 个价格代理击败 buyhold 占比 |
|---|---:|---:|
| contrarian | +0.88 | 72% |
| trend | -0.46 | 5% |

这说明创新药自身申赎不是完全噪声，方向也符合“散户追涨杀跌”的经济逻辑。

### 4.3 为什么不是绿灯

- 可评估样本只有约 15 个月。
- 月度胜率只有 41% 至 53%。
- 三只创新药 ETF 不是独立标的，本质仍是同一行业指数暴露。
- 最好的 `retail_net_fast_contra` 存在 selection bias 风险。

定级：**黄灯。可作情绪 hint，不单独注册生产因子。**

## 5. 候选因子二：`sr_trend_confirm_combo`

这是本轮除总体申赎外最好的创新药专属候选。

### 5.1 构造

```text
bear_score = retail_imb_z15_60 - 0.5 * ir_mom_z60 - 0.5 * conc_z60

若 bear_score > 1：减仓/空仓
若 bear_score < -1：加仓/满仓
中间沿用上一状态或中性仓位
```

分量含义：

| 分量 | 方向 | 经济含义 |
|---|---|---|
| `retail_imb_z15_60` | 越高越 bearish | 创新药散户申购过热，反向警示 |
| `ir_mom_z60` | 越高越 bullish | 板块信息比率动量强，趋势确认 |
| `conc_z60` | 越高越 bullish | 成交/拥挤结构偏景气确认，不作为过热反向 |

核心直觉：**散户过热但行业趋势不强，才是更可信的减仓场景；散户恐慌但趋势没有坏，才是更可信的反弹场景。**

### 5.2 主标的结果

在 `159992.SZ` 上，T+1、5bp：

| 因子 | 年化 | Sharpe | Calmar | maxDD | 翻转/年 |
|---|---:|---:|---:|---:|---:|
| `sr_trend_confirm_combo` | +10.9% | +0.66 | +0.93 | -11.7% | 约 8 |

对比：buyhold 在同窗口 Calmar 约 +0.13 至 +0.42，视 warmup/窗口口径略有差异。

### 5.3 跨代理检查

| 代理 | combo Calmar | buyhold Calmar | dCalmar |
|---|---:|---:|---:|
| 159992 | +1.02 | +0.42 | +0.60 |
| 516080 | +1.14 | +0.52 | +0.63 |
| 159839 | +0.26 | +0.02 | +0.24 |
| 512010 | -0.19 | -0.05 | -0.13 |

结论：创新药内部代理 3/3 有改善，泛医药代理失败。它更像**创新药自身情绪/趋势确认器**，不是泛医药因子。

### 5.4 证伪结果

第三轮权重扰动中：

| 组合 | beat-rate | 解释 |
|---|---:|---|
| 真趋势分量 `retail - mom - conc` | 79% | 明显高于随机，说明动量/集中度带来增量信息 |
| 噪声对照 `retail - inflow` | 49% | 约等于随机 |
| 随机 wiggle 对照 | 54% | 约等于随机 |

这说明 combo 的提升不是简单“多加一列就更好”的 artifact。

### 5.5 定级

`sr_trend_confirm_combo` 是**黄灯+**：

- 优点：经济逻辑清楚；低参数；优于单一创新药申赎；证伪测试通过；翻转健康。
- 硬伤：样本只有 15 个月；H2 子样本仍未完全翻正；月度胜率不到 50%；未经创新药牛市段验证。

建议用法：作为创新药 bot 的第二意见，不单独硬规则化。若它与市场级申赎锚同向，信号可信度提高；若冲突，以市场级锚为主。

## 6. 绿灯锚：`market_retail_contrarian_20_90`

虽然用户这次关心“除了总体申赎”，但报告必须明确：当前真正过关的还是市场级总体申赎锚。

### 6.1 构造

```text
market_retail_contrarian_20_90
= 基于全市场“个人净申赎指数型股票基金”的 20 日累计 / 90 日 z-score

z > +0.5：散户全市场申购过热，仓位降到 0
z < -0.5：散户全市场赎回/恐慌，仓位升到 1
中间维持上一状态
```

### 6.2 创新药代理结果

| proxy | 年化 | Sharpe | Calmar | buyhold Calmar | dCalmar | maxDD | 暴露 | 翻转/年 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 159992 | +21.9% | +1.21 | +1.87 | +0.42 | +1.45 | -11.7% | 0.55 | 11.1 |
| 516080 | +23.5% | +1.30 | +2.05 | +0.52 | +1.54 | -11.4% | 0.55 | 11.1 |
| 159839 | +18.1% | +1.07 | +1.47 | +0.02 | +1.45 | -12.3% | 0.55 | 11.1 |
| 512010 | +11.1% | +0.79 | +1.08 | -0.05 | +1.14 | -10.2% | 0.55 | 11.1 |

参数邻域检查：`market_retail_contrarian_20_90` 在 4 个代理上 beat-rate 100%，median dCalmar +1.45，min dCalmar +1.14。

### 6.3 定级

定级：**绿灯，交易使用层面可作为创新药仓位主锚。**

但它不是本轮新挖出的创新药专属因子，而是已有市场级情绪因子在创新药上的迁移验证。不要把它改名包装成“创新药自身因子”。

## 7. 被证伪或弃用的方向

| 方向 | 结论 | 原因 |
|---|---|---|
| 主力净流入，当天 | 红灯 | fwd20 IC 近 0，回测无稳定 edge |
| 主力净流入，长累计 | 红灯 | 15 个月短窗很漂亮，但 7 年子段显示只有最近一段有效，是慢趋势伪相关 |
| PE/PB 估值单因子 | 红灯 | IC 有时有方向，但交易规则不成立，翻转/稳定性差 |
| 创新药板块趋势单因子 | 红灯/分量可用 | 牛市 beta 明显，跨 regime 失效；只能做 combo 确认分量 |
| 市场温度 | 红灯 | 单独不稳，接入 combo 后拖累 |
| 成长/科创/创业板相对强弱 | 红灯 | 短窗口解释力强，H2/跨代理不稳 |
| 股债性价比 | 黄灯风控 | 更适合组合层面风控，不是创新药专属 alpha |
| 50ETF VIX | 黄灯解释变量 | 情绪解释力有，但不能稳定增强交易规则 |
| 美债收益率 | 红灯 | 逻辑成立，实证不稳 |
| 重仓股资金流 | 红灯 | 当前诊断有用，日频择时未过证伪 |
| 港股/南向/恒科代理 | 红灯 | 非 PIT 诊断未通过，不进入硬因子 |

## 8. 推荐落地方式

### 8.1 仓位优先级

1. 主锚：`market_retail_contrarian_20_90`。
2. 确认：`sr_trend_confirm_combo`。
3. 解释：创新药自身散户申赎 z、IR 动量、集中度。

### 8.2 建议规则

不要把本地 combo 写成单独开平仓硬规则。更适合这样用：

| 场景 | 动作建议 |
|---|---|
| 市场级申赎锚减仓，且 `sr_trend_confirm_combo` 也偏 bearish | 提高减仓置信度，避免追高 |
| 市场级申赎锚加仓，且本地创新药申赎恐慌、趋势未坏 | 提高反弹候选置信度 |
| 市场级锚与本地 combo 冲突 | 以市场级锚为主，本地 combo 只降信心不反向硬覆盖 |
| 本地 combo 单独发信号 | 只记录为 shadow / research note，不自动交易 |

### 8.3 继续观察条件

本地 `sr_trend_confirm_combo` 若要从黄灯+升到绿灯，至少需要：

- 再积累 6 至 12 个月申赎数据。
- 经历一次创新药强趋势上涨期，验证不会过早减仓。
- H2/滚动子样本 Calmar 不再转负。
- 月度超额胜率提高到 50% 以上，或者虽胜率低但回撤改善稳定且收益不被明显牺牲。
- 在至少一个非同指数医药/成长代理上不显著失效。

## 9. 复现路径

主要脚本：

```bash
cd /home/rooot/agent_invest_lab/research/innodrug_sr

# 创新药自身申赎相关性、sweep、月度分解
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python analyze.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python sweep.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python verify.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python decomp.py

# 板块资金流/趋势/拥挤/估值
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python analyze_sector.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python sector_backtest.py

# 组合因子与证伪
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python combo_diag.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python combo.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python inflow_probe.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python inflow_falsify.py

# 回归式诊断与交易日口径修正
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python regression_mine.py

# 外部数据扩展与市场级申赎锚迁移验证
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python alt_data_mine.py
/home/rooot/.local/share/uv/tools/vibe-trading-ai/bin/python market_anchor_check.py
```

关键输出：

- `FINDINGS.md`：逐轮研究流水与细节。
- `data/market_anchor_summary.csv`：市场级申赎锚汇总。
- `data/market_anchor_proxy_check.csv`：创新药/医药代理逐项结果。
- `data/alt_combo_candidates.csv`：alt-data 候选接入结果。
- `data/alt_combo_sweep.csv`：alt-data gate 参数扰动结果。

## 10. 最终判定

| 因子 | 最终判定 | 是否建议注册/生产化 |
|---|---|---|
| `market_retail_contrarian_20_90` | 绿灯，创新药可用的主仓位锚 | 已有市场级 `quant_factor`，无需注册成创新药专属 |
| `sr_trend_confirm_combo` | 黄灯+，本轮最佳创新药专属候选 | 暂不注册；shadow 6 至 12 个月 |
| 创新药自身散户申赎反向 | 黄灯 | 不注册；作为 combo 分量和解释信号 |
| 其它新增维度 | 红灯或仅解释变量 | 不注册 |

最终回答：**有挖出一个好用的创新药本地确认因子 `sr_trend_confirm_combo`，但它不是绿灯；真正绿灯仍是总体申赎 `market_retail_contrarian_20_90`。当前最稳的实盘做法，是总体申赎做主锚，本地 combo 做确认，不把本地 combo 单独生产化。**