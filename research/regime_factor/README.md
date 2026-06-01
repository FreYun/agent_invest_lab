# regime_classify_daily 择时有效性验证（market.db）

测试对象：`/home/rooot/database/market.db` 的 `regime_classify_daily`（v2 规则集）——
`total_score`（原始综合分）与 hysteresis 确认后的 `regime_name` 状态，作为 **50/50 沪深300+中证1000
等权组合** 的择时因子。基准 = 每日再平衡 0.5×r(000300)+0.5×r(000852)。

## 裁定：不稳健，无可包装用法，不包装。

经 4 路对抗式 workflow 复核（steelman / lookahead 审计 / 抢救 / 统计严格性），一致 `not_robust`。

## 证据

| 维度 | 关键数 |
|---|---|
| 真样本外 2014-2022 | 72 个单调 regime→仓位映射只有 **7% 击败组合 Sharpe**（逐年 pre-2023 仅 2/9 年正）|
| 设计期 2023-2026 | 94% 映射击败（v1 规则原生只到 2023+，即设计期=样本内）|
| **Steelman（被推翻）** | 公平样本外 2020-08~2022-12（注册制后/20%涨跌幅后、设计期前）是**最差**的一段：**0/72 映射击败**，rank-IC 反转到 −0.05。"只在现代市场有效"被自己的测试否定——区分通过/失败的是**设计期身份**，不是现代/古早 |
| Lookahead/PIT | 分数构造**干净无泄漏**（固定阈值/后向窗口/无状态情绪）；但 corr(score_t, ret_{t+1})=**0.039** vs 同日 0.726——是**当前 regime 描述器,非预测器**。漏掉 shift(1) 会凭空造出 +0.79~+1.05 Sharpe 的同日泄漏 |
| 抢救（熊过滤/转换/二值） | 名义"两期都赢"的两个变体在 2014-2022 的 T+1 日超额收益**为负**（bootstrap p≈0.55，ΔSharpe 只是低暴露的分母收缩）；T+2 滞后/20bps 成本/去掉2015 全部翻车；熊日在 2023-26 反而是最强反弹日(+39.7bps)，崩盘过滤前提跨期反转 |
| 统计严格性 | best-of-121 映射的 family-wise 置换 p=**0.52**——与随机 regime 对齐不可区分 |

## 结论性质

`total_score`/regime 状态是一个**干净、合理的"当前处于什么 regime"描述器**（同日相关 0.73），
但**预测明日收益的相关性 ≈ 0.04**。它"有效"的全部数字要么是同日泄漏（没做 T+1），要么是
2023-2026 手工调参设计期的过拟合。**作为择时因子不稳健,不包装。** 若要用,只能当"现在是什么
regime"的状态读数,**不能当未来收益预测**。

脚本：[regime_ic.py](regime_ic.py)（IC）、[regime_backtest.py](regime_backtest.py)（3 用法回测）、
[regime_state_robust.py](regime_state_robust.py)（72 映射 × 真样本外/设计期 + 逐年）。
对抗复核：workflow `regime-robustness-adversarial`。
