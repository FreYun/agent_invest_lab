# 估值×情绪极值 风险管理择时（ERP × VIX）

研究部第 4 轮择时挖掘。承接 VIX 三轮（[../vix_factor/](../vix_factor/README.md)，结论：VIX 单用不可包装）后，
系统翻 simworld 全部"做仓位择时"工具 + 做 IC/回归，找到唯一可包装的组合。

## 结论

**股债性价比(ERP，慢估值锚) × 50ETF VIX 极值(快情绪触发)** 组合出一个**黄灯·风险管理择时 overlay**，
已包装为 `hs300_erp_vix_timing` / `zz1000_erp_vix_timing`（quant_factor）。

## 路径

1. **测绩 simworld 择时工具**（[timing_ic.py](timing_ic.py)）：对所有连续 PIT 信号（gzxjb ERP、市场温度+6子因子、
   VIX z、MA200乖离、动量）做 Spearman IC + 非重叠显著性 + 多元回归 OOS R²。结果：温度/动量/MA乖离 OOS 基本噪声；
   多元组合 **OOS R²=−0.12（过拟合）**；**唯一有真信息的是 ERP**。
2. **ERP 多 horizon**（[erp_horizon.py](erp_horizon.py)）：ERP 3年分位 IC 随 horizon 增强（池化 fwd60 +0.31 p0.02、
   fwd250 +0.75 p0.00），OOS 同向（fwd60 +0.32）——真估值信号。但**单用作 sizing 会在 2024-26 踏空**
   （[erp_backtest.py](erp_backtest.py)：OOS 暴露塌到 10-17%，因 ERP 全程喊"贵"）。
3. **ERP×VIX 交互**（[erp_vix_combo.py](erp_vix_combo.py)）：fwd60 矩阵 IS→OOS 一致——便宜+(非自满)→强买，
   **贵+自满→强卖（IS −10.4%/OOS −8.6%）**；关键：**贵+恐慌 OOS 仍 +1.6%（要买）**，只有贵+自满才是真卖点。
   → **VIX 极值修好了 ERP 踏空病**（别在"贵但恐慌"误减仓）。
4. **组合回测**（[erp_vix_strategy.py](erp_vix_strategy.py)）：combo OOS 双指数击败 buyhold Sharpe(+0.21/+0.34)
   + Calmar，暴露回升到 0.55（不踏空），IS 也正。
5. **Robustness**（[erp_vix_robust.py](erp_vix_robust.py)）：**243 参数配置 100% OOS 双指数击败 buyhold**，
   IS+OOS 双正，暴露 0.46-0.65，参数全平滑无悬崖（H 越长越好，印证熊市回弹后置）。非过拟合角落。

## 公式（已注册参数）

```
base    = clip(0.35 + 0.60*ERP_3年分位, 0, 1)     # 便宜→高、贵→低，封顶不踏空
panic   = (VIX_50etf 252日z > 1.5) 后 20 日窗口    # 恐慌加仓触发
complac = (VIX_50etf 252日z < -1.5)                # 自满减仓触发
仓位    = clip(base + 0.40*panic − 0.35*complac, 0, 1)   # T+1 进场, 5bp
```

## 强度与诚实定级（黄灯）

- **是风险管理器、非收益增强器**：OOS Sharpe +0.21~+0.34、**回撤减半**，但牛市让出绝对收益。
- **2025-2026 验证**（[stress 见对话]）：风险调整后**仍有效**（2025-26 ΔSharpe +0.36/+0.37、回撤 −5% vs −10%、
  −9% vs −16%），但**绝对收益跑输**（2025 −7.7%/−9.2%，因牛市低暴露）。大部分 OOS 超额来自 **2024 V 型年**
  （ZZ1000 单年靠 VIX 抄 2月/9月大底 +20.9%）。
- **已知盲区**（公式/回测可推）：(1) 牛市持续上涨→低暴露跑输绝对收益，只赢风险调整；(2) 超额集中于 V 型年，
  磨人长熊里恐慌反弹后置、需更长持有；(3) ERP 绝对水平受利率 regime 影响（2024-26 债券崩塌使股票显"贵"），
  靠 3年分位+VIX 部分缓解；(4) OOS 仅 2.4 年单 regime；(5) ERP 只覆盖 5 宽基，双创50 无此因子。
- 作客观第二意见、**权重 ≤ 主维度**。

## 产物

- 因子注册 `hs300_erp_vix_timing` / `zz1000_erp_vix_timing`（kind `erp_vix`）于 simworld server.py，
  `quant_factor` 已在 world.yaml 白名单。
- smoke test：2026-05-26 仓位 0.35（贵+无恐慌，防御）；2022-04/2024-02/2024-09 三个历史大底均触发满仓。
- 数据缓存 `data/`（gzxjb_000300/000852、temperature）；VIX 复用 ../vix_factor/data/。
