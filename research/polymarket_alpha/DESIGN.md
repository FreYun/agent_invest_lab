# Polymarket 信号 → A股/美股 信号发现 · 设计 (spec)

- 日期：2026-07-03
- 方法论依据：[../SOP_factor_mining.md](../SOP_factor_mining.md)
- 研究模式（SOP 步骤3 判定）：样本 ~1 年 → **< 2 年 · 纯探索性 · 不挑最优**。只报 hint 级结论，不做 IS/OOS 挑最优，不跑 monte_carlo/bootstrap validation（SOP 明确说 <2 年这些 p 值/CI 不可靠）。

## 1. 目标

用 Polymarket 预测市场数据，系统性发现「哪些序列对哪些资产（A股/美股/黄金）有稳定预测力」，产出一份诚实标注可信度的研究报告。**这轮不做变现、不包装成生产因子**——纯信号发现 + 并行启动聪明钱采集管道为下一轮攒数据。

## 2. 数据资产盘点（已探查）

| 来源 | 内容 | 可用性 |
|---|---|---|
| `polymarket.db` · `macro_factor` | 17 条宏观事件日频隐含概率 | 最长 355 天（2025-07→2026-07），信号侧主力 |
| `polymarket.db` · `price_history` | 3576 token 日频价 | 最长 ~14 月（2025-05→），日频（间隔 86400s） |
| `polymarket.db` · trade/holders/leaderboard/negrisk/event_snapshot | 聪明钱流/大户/榜单 | **当前全空** → 采集管道启动后从今日攒 |
| `market.db`（6.9G） | A股个股 daily/index_daily/moneyflow_daily/board_* | 标的侧主力 |
| `research/index_timing/data/*.csv` | HS300/ZZ1000/50ETF 等 ETF 日线 | 标的侧，直接复用 |
| 美股 SPX/QQQ 日线 | — | **本轮需现补**（akshare/yfinance） |

**已实测的先验（防自欺）**：macro_factor 各序列在样本内多为单调趋势，与 HS300 的 **level 相关几乎全是趋势假象**（fed_hike fwd10 +0.39、gold_high −0.40 等），去趋势（diff5）后塌到 0.13~0.28。→ **检验必须以去趋势版为准**。

## 3. 决策（已拍板）

1. 美股本轮就补（SPX + QQQ 日线），覆盖"宏观利率→大盘"假设的美股腿。
2. 采集管道用 systemd --user timer（与现有 MCP 一致）。
3. 产出目录 `research/polymarket_alpha/`。

## 4. 假设族（4 条，均有经济逻辑，非暴力扫）

| # | 信号 | → 资产 | 经济逻辑 | 已知失效场景 |
|---|---|---|---|---|
| H1 | fed_no_cut/fed_hike/recession | HS300 / SPX / QQQ 大盘择时 | 利率路径+衰退概率 → 全球流动性 → 估值 | 政策与基本面背离期 |
| H2 | taiwan_risk/iran/hormuz | 黄金基金 / A股风险溢价 / 原油 | 地缘尾部 → 避险溢价 | 尾部事件基率低、噪声大 |
| H3 | btc_dip_50k/eth_dip | 创业板/成长股/QQQ | 加密=24h 风险偏好高敏代理 | 样本最短(~4月)，仅 hint |
| H4 | holders/leaderboard/trade（采集后） | 对应事件标的 contrarian | 聪明钱流 = 预测市场真正护城河 | 本轮仅启动采集，不回测 |

## 5. 架构与数据流

```
signal 侧: polymarket.db(macro_factor + price_history)
标的 侧: market.db + index_timing/*.csv + 美股 SPX/QQQ(新补)
            │
   [align.py]   PIT + 时区对齐, 信号 shift(1)
            │
   [ic_scan.py] Spearman IC + 概率分位分组前向收益 + level/diff 双版 + 跨标的/横向一致性
            │
   report.md + summary.csv   (诚实标注 hint / 黄灯 / 无结构)

并行: [collector/]  polymarket CLI holders/leaderboard/trades → 灌 polymarket.db (systemd timer)
```

### 单元职责（可独立测试）
- `align.py`：输入信号序列 + 标的价，输出 PIT 对齐后的 (signal_t, fwd_ret_{t+h}) 面板。唯一职责=对齐+防未来函数。
- `ic_scan.py`：输入对齐面板，输出 IC / 分位单调性 / 一致性判据。唯一职责=统计检验，不碰数据获取。
- `collector/fetch_smartmoney.py`：调 CLI 灌库，流式写盘+断点续传。唯一职责=采集，与检验层零耦合。

## 6. PIT 对齐（SOP 头号纪律）

- Polymarket 概率为 UTC 日频收盘值。A股：用 **T 日概率 → 预测 T+1 起** 前向收益，`sig = sig.shift(1)`。
- 美股：Polymarket as-of 时刻 vs 美股收盘存在时区错位，统一取"信号日 UTC 收盘 → 预测下一个美股交易日起"，宁可保守多滞后一日，杜绝偷看。
- 所有输出标 source / as-of / n（SOP 纪律）。

## 7. 检验方法（不挑最优，robustness 优先）

- 指标：**Spearman IC**（抗异常值）+ 概率分位（如 5 档）分组的前向收益单调性；horizon = 5 / 10 / 20 日。
- 每条假设同时算 **level 与 diff（去趋势）** 两版，**以 diff 为准**，level 仅参考且显式标趋势风险。
- "有结构"判据（全满足才算）：跨 ≥3 个同类标的 IC 同号、跨 horizon 不反号、diff 版 |IC| 稳定非零。否则记"无结构/噪声"。
- **不报** Sharpe 单点最优、**不做** monte_carlo/bootstrap（样本 <2 年）。

## 8. 聪明钱采集管道（并行，从今日攒）

- `collector/fetch_smartmoney.py`：对活跃度 top-N market 定时调 `polymarket -o json data holders/leaderboard/trades`，写入对应空表；流式 flush + 跳过已抓（OOM-safe）。
- systemd --user timer 挂起（频率待定，初定每 6h）。本轮只启动 + 验证灌库正确，历史回测留下一轮。

## 9. 产出

- `report.md`：每条假设结论（有结构/无结构/hint），配 IC 表与分位图数据。
- `summary.csv`：假设 × 标的 × horizon × {level,diff} 的 IC/一致性明细。
- 诚实标注：全部为**黄灯/hint 级**，样本 <2 年不得升格生产因子。

## 10. 环境

- 检验层：`/usr/bin/python3.12` + 基础 pandas（只做相关/IC/分位），**不需要** vibe-trading venv。
- 采集层：Rust `polymarket` CLI（`/home/rooot/polymarket-cli-main`）+ Python 灌库脚本。

## 11. 盲区（从设计可推，非回测发现）

- 1 年单一 regime：任何"预测力"都可能是这一年特定宏观路径的巧合，跨 regime 未验证。
- 地缘/加密尾部合约流动性低、Polymarket 定价可能系统性偏离真实概率。
- macro_factor 的"代表市场"选取口径若变化会引入序列断点，需在 align 时校验连续性。
- 美股时区对齐取保守滞后，会牺牲部分短 horizon 信号强度。
