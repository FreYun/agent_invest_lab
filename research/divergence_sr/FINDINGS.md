# Divergence-SR Factor Mining — 负结果（基本闭门）

**研究目的**：在 [sr_factor/](../sr_factor/) 已挖完"散户单边 contrarian"之后，挖 4 个未尝试的**分歧 / 切换维度**，看是否能：(a) 单独跑赢 baseline，或 (b) 跟 baseline 组合补强。

**数据窗口**：2024-07-11 → 2026-05-26（n ≈ 16 个月稳定数据，392 个交易日重叠区间）。**纯 in-sample，无 IS/OOS**。
**universe**：510300 (HS300) / 512100 (ZZ1000) / 512480 (semi) / 588800 (双创50)
**baseline**：`retail_z_step_10` = 个人指数股票申购 cumN=10 z_win=60 thr=1.0 反向（sr_factor 现有最强）

---

## TL;DR — 全军覆没

| 因子家族 | 假设 | 单独表现 | MC p-value | 与 baseline 相关 | 组合补强 | 结论 |
|---|---|---|---|---|---|---|
| **F1 散户-机构指数差** | 散户买/机构卖=顶 | Cal 1.62-5.10（4/4 弱于 baseline） | 4/4 显著 (<0.05) | +0.728 | 7/4 全降 | ❌ baseline 弱化版 |
| **F2 分歧持续度** | 连续 3 日才动 | Cal 1.17-3.48（4/4 弱） | 1/4 显著 | +0.638 | 4/4 全降 | ❌ 同上 |
| **F3 股债切换** | 散户股 vs 债 z | Cal 0.24-4.21（4/4 弱） | 0/4 显著 | +0.17~+0.21 | 4/4 全降（但 DD 略减） | ⚠️ 低相关、但 alpha 不足以补强 |
| **F4 主动 vs 被动** | 买主动=情绪热 | Cal -0.11~+1.73（hs300/zz1000 负） | 未测 | -0.42~-0.57 | 未测 | ❌ 假设错误 |
| **F5 联合状态** | (个/机) 4 状态 | Cal 1.55-2.85（4/4 弱） | 2/4 显著 | +0.647 | 4/4 全降 | ❌ baseline 弱化版 |

**核心发现**：**baseline（个人指数股票申购反向）已经把申赎数据里的 alpha 榨干**。所有"分歧 / 切换"维度要么是它的高相关弱化版，要么单独 alpha 不显著。

---

## 详细数据

### 1. 单因子全样本回测（cal = annualized return / |max DD|）

| index | buyhold | baseline_retail_10 | f1_diff_step | f2_persist | f3_risk_switch | f4_active_pass | f5_consensus |
|---|---|---|---|---|---|---|---|
| HS300 | 1.19 | **2.25** | 1.62 | 1.17 | 0.37 | -0.11 | 1.55 |
| ZZ1000 | 2.06 | **3.25** | 2.44 | 1.78 | 0.73 | -0.36 | 2.07 |
| SEMI  | 4.39 | **6.95** | 5.10 | 3.48 | 4.21 | 0.78 | 2.83 |
| SC50  | 3.49 | **5.12** | 3.69 | 3.32 | 2.12 | -0.01 | 2.85 |

baseline 在 4/4 指数上 Calmar 都是冠军，与第二名差距 ≥ 0.55。

### 2. Permutation Monte Carlo (N=1000, null = 信号随机洗牌)

只有 `baseline_retail_10` 在 4 个指数上全部 p<0.01。`f1_diff_step` 4/4 p<0.05 但 Sharpe 都低于 baseline。F3/F4/F5 大多 p>0.10。

→ "散户净申购反向"在统计上**真实存在 alpha**；分歧因子要么 alpha 微弱、要么不显著。

### 3. 线性组合 baseline + 第二信号 (70/30 & 50/50)

跑了 4 × 4 × 2 = 32 个组合（4 指数 × 4 第二信号 × 2 权重）。**没有一个组合的 Sharpe 或 Calmar 超过 baseline 单独**。
- 即使 f3_risk_switch 与 baseline 相关度只有 +0.17~+0.21（低到值得加入），加入后 ret 平均跌 20%、shp 跌 0.15-0.25
- 唯一可圈点：SEMI 上 baseline + f3_risk_switch(0.7/0.3) 把 DD 从 -17% 降到 -14%，但 Cal 持平 6.95→6.80（return 牺牲掉了）

详见 [combo_results.csv](combo_results.csv)。

---

## 为什么 baseline 无法被补强？

**个人指数股票申购**就是散户对宽基 ETF 的**最直接情绪输入**。其他维度的本质：
- **机构申购**：噪声大、本身 robustness 检验 inst_index_eq 多数 cell 不显著（[sr_factor/robustness_agg.csv](../sr_factor/robustness_agg.csv) 已验证）
- **股 vs 债切换**：散户换券种很慢，转换信号被淹没在月度新发节奏里
- **主动 vs 被动**：在 ETF 大爆发的 2024-2025，散户买被动是常态，"买主动"反而稀少，结构性翻转
- **联合状态**：本质是 baseline 的状态机版本，无新信息

可以理解为：`market_retail_contrarian` 因子（已打包到 `quant_factor`）就是这条数据线的最优变现，没有更多维度需要挖。

---

## 给下一轮 Claude 的避坑笔记

1. **不要再挖申赎数据的任何"个人 / 机构 / 股 / 债 / 主动 / 被动"组合**——5 个家族都试过，全军覆没。
2. **sr_factor/robustness.py 的 baseline 是 buyhold，不是 retail_z_step_10**——所以那里 `personal_minus_inst` 看着 beats_cal_count=4，其实只是赢 buyhold、输给真正的 baseline。复盘 robustness 表时**用 baseline 当基准比，不要用 b&h**。
3. **n=16 月**这件事还是死的——后续如有更长窗口数据（>4 年），可以补一次 walk-forward 看 baseline 本身是否稳定。当前**所有结果只能算"探索性"，不能用作生产信号**。
4. 真的还想挖申赎数据，可以试的**剩余角度**（但 prior 不高）：
   - **新发基金占比**（SR 数据没有，需另起接口）
   - **券种轮动速度**（券种间月度切换频率）
   - **散户净申购的 hp filter 周期分解**（短中长周期对应不同时序信号）

---

## 下次工作建议

- **方向 2（跨指数 rotation）** —— 把单指数择时变成 cross-section ranking。我之前给用户的 5 个方向里这条还没动，且工具基础最齐全（已有 4 指数数据 + akshare loader）。
- **方向 5（agent 框架本身的 alpha）** —— 20+ bot 用不同 SOUL/METHODOLOGY 跑同标的，meta 层比较哪种结构跑赢 b&h。这条是其他人没在做、只有你们能做的。

---

## 文件清单

- [backtest.py](backtest.py) — 11 变体 × 4 指数 sweep
- [backtest_summary.csv](backtest_summary.csv) — 主表
- [validate.py](validate.py) — 相关性 + MC + bootstrap CI + 组合
- [corr_vs_baseline.csv](corr_vs_baseline.csv) — 与 baseline 的仓位相关性
- [mc_results.csv](mc_results.csv) — Monte Carlo p-value + bootstrap Sharpe CI
- [combo_results.csv](combo_results.csv) — 32 个线性组合结果
- [validate.log](validate.log) — 完整 validate 输出
