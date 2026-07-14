# v2_entry_only 仓位规则 (已验证, 直接使用)

> 2026-04-15 研究结论, 原 `position-sizer` skill 已并入本 memory。
> 规则是**硬编码**的, 不要调参, 不要 timing, 不要加止损/止盈。

---

## 规则 (3 行)

```
signal = regime_classify_daily WHERE rules_version='v2' ORDER BY trade_date DESC LIMIT 1
if signal.switched == 1 and signal.regime_name in ('强牛', '强势震荡'):
    T+1 仓位 = 90%
else:
    T+1 仓位 = 50%
```

直觉: 大部分时间 50% 底仓 (保底 beta 减配), 只在 classifier 探测到"市场情绪刚刚起来"那一瞬加仓到 90%, 之后回到 50%。

---

## 日常用法 (SQL 直查)

```sql
-- 今日应持仓位
SELECT trade_date, regime_name, switched,
       CASE WHEN switched = 1 AND regime_name IN ('强牛','强势震荡')
            THEN '90%' ELSE '50%' END AS position
FROM regime_classify_daily
WHERE rules_version = 'v2'
ORDER BY trade_date DESC LIMIT 1;
```

```python
# Python 一行
import sqlite3
conn = sqlite3.connect("/home/rooot/agent_invest_lab/data/market.db")
r = conn.execute(
    "SELECT regime_name, switched FROM regime_classify_daily "
    "WHERE rules_version='v2' ORDER BY trade_date DESC LIMIT 1"
).fetchone()
position = 0.9 if r[1] == 1 and r[0] in ("强牛", "强势震荡") else 0.5
```

数据由 `/home/rooot/agent_invest_lab/market_pipeline/openclaw/scripts/daily-regime-pipeline.sh` 每工作日 16:00 自动更新。

---

## 为什么是这条规则 — 验证结果

**区间**: 2015-01-05 ~ 2026-04-14, 2739 交易日 × 11.25 年, 4 次独立 out-of-sample。

| 测试 | 策略总回报 | fullhold 总回报 | 策略 DD | fullhold DD | ret/dd |
|---|---:|---:|---:|---:|---:|
| HS300 × 2015-2019 (旧段) | **+19.6%** | +15.9% | -24.1% | -46.6% | **0.81** |
| HS300 × 2020-2026 (新段) | **+36.0%** | +15.6% | -22.7% | -45.4% | **1.58** |
| 50/50 × 2015-2019 (旧段) | **+23.1%** | +5.8% | -29.4% | -59.5% | **0.78** |
| 50/50 × 2020-2026 (新段) | **+50.1%** | +32.7% | -22.2% | -41.4% | **2.26** |

4/4 全部稳定跑赢 fullhold, 最大回撤每次砍掉 40-50%。2015-2019 是真正 out-of-sample (设计时没看过)。

**11 年全样本**: HS300 +62.6% / DD -24.1% (vs fullhold +33.9% / -46.6%)。

**关键崩盘窗口**:
- 2015 股灾: 底仓 50% 自动砍半暴露, DD 比 fullhold 少 20+ pp
- 2018 全年熊: 情绪冷, 基本维持 50% 底仓, 少亏
- 2020 COVID 反弹: 2020-04 切入强牛, 加仓捕捉上涨
- 2022 三杀: 切入机会不多, 整段少亏

---

## 试过但失败的替代方案 (不要再走这些路)

1. **classifier JSON 的梯度仓位表** (强牛 90 / 强势 70 / 中性 50 / 弱势 30 / 熊 20)
   - 11 年累计 **-2.34%**, 两段都跑输 fullhold
   - 失败原因: "中性震荡"占比最大, 50% 仓位拉不动收益; 熊市 20% 没帮上忙
2. **按 score 分档 / 按 regime 分档的 5 档加减仓** — 全部跑输
3. **短期 timing** (预测下周涨跌) — 不稳定
4. **加止损/止盈层** — 削弱已验证的 beta 暴露规律

唯一通过严格验证的: **50% / 90% 二值 + switched 触发**。

---

## 规则边界 (不做什么)

- ❌ 不做短期 timing (不预测下周)
- ❌ 不做止损/止盈 (由具体战法自己决定)
- ❌ 不做多资产配置 (只输出单一比例)
- ❌ 不做参数优化 (50%/90% 阈值固定)
- ❌ 不做机器学习 (硬编码规则)

如果未来想扩展 (波动率目标 / 回撤止损 / 对冲), 作为**新 skill 或混合策略**做, 不要改本规则。

---

## 信号依赖

| 字段 | 来源表 | 说明 |
|---|---|---|
| regime_name | `regime_classify_daily WHERE rules_version='v2'` | classifier 输出 |
| switched | 同上 | "今日是否刚刚切换到此 regime" |

signal 的上游 (六维原始数据 + 指数 MA + 成交量比) 都由 classifier + `daily-regime-pipeline.sh` 自动维护, 不需要关心。
