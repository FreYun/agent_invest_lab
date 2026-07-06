# Polymarket 信号发现 · 结果报告

- 生成时间: 2026-07-06  数据源: polymarket.db/macro_factor + market.db
- 全部为**黄灯/hint 级**(样本<2年,单一regime),不得升格生产因子

## 各因子结构判定(以 diff 去趋势版为准)

| factor | 判定 | diff 版有效 (target×horizon) 数 |
|---|---|---|
| `btc_dip_50k_2026` | 有结构(hint) | 9 |
| `eth_dip_500_2026` | 样本不足 | 0 |
| `fed_hike_2026` | 无结构/噪声 | 12 |
| `fed_no_cut_2026` | 有结构(hint) | 12 |
| `hormuz_normal_2026` | 无结构/噪声 | 6 |
| `iran_regime_fall` | 无结构/噪声 | 6 |
| `recession_2026` | 有结构(hint) | 12 |
| `taiwan_risk_2026` | 有结构(hint) | 6 |

注: "样本不足"= diff 版有效组合<3, 通常是该 factor 在 polymarket.db 里历史太短(如 eth_dip_500_2026 仅 ~4 个月), 不同于"信号弱"的"无结构/噪声"。

## 完整明细见 summary.csv

## 诚实盲区
- level 版 IC 整体均值/中位数高于 diff 版(0.205 vs 0.138),但逐行仅约 53% 满足 level>diff、个别 target 上 diff 版反而更高——趋势假象在汇总统计上可见,但并非逐条普遍,故仅以 diff 版做结构判定、level 版不作依据
- 1年单一regime,跨regime未验证
- 尾部/加密合约流动性低,定价可能偏离真实概率
