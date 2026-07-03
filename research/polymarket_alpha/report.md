# Polymarket 信号发现 · 结果报告

- as-of: 2026-07-03  source: polymarket.db/macro_factor + market.db
- 全部为**黄灯/hint 级**(样本<2年,单一regime),不得升格生产因子

## 各因子结构判定(以 diff 去趋势版为准)

- `btc_dip_50k_2026`: 有结构(hint)
- `eth_dip_500_2026`: 样本不足
- `fed_hike_2026`: 无结构/噪声
- `fed_no_cut_2026`: 有结构(hint)
- `hormuz_normal_2026`: 无结构/噪声
- `iran_regime_fall`: 无结构/噪声
- `recession_2026`: 有结构(hint)
- `taiwan_risk_2026`: 有结构(hint)

## 完整明细见 summary.csv

## 诚实盲区
- level 版 IC 整体均值/中位数高于 diff 版(0.205 vs 0.138),但逐行仅约 53% 满足 level>diff、个别 target 上 diff 版反而更高——趋势假象在汇总统计上可见,但并非逐条普遍,故仅以 diff 版做结构判定、level 版不作依据
- 1年单一regime,跨regime未验证
- 尾部/加密合约流动性低,定价可能偏离真实概率
