# 历史案例库

记录 market-regime 管道在真实复盘数据上的判断结果，用于校准阈值、解释行为、发现边缘情况。

数据源：[workspace-bot11/memory/review-output/](/home/rooot/.openclaw/workspace-bot11/memory/review-output/) 的真实复盘 + `market.db.regime_classify_daily` 表 (v2 规则)。

---

## 一、24 天回测全景（2026-03-12 ~ 2026-04-14）

bot11 跑了 23 个交易日的连续回放（4/6 清明休市跳过）。完整轨迹：

| 日期 | 总分 | 当日 raw | 最终 regime | 备注 |
|---|---|---|---|---|
| 03-12 | -1 | 中性 | 中性 | bootstrap 起点 |
| 03-13 | -2 | 弱势 | 中性 | warning 但历史不足 |
| 03-16 | -3 | 弱势 | 中性 | warning 持续 |
| **03-17** | **-8** | 熊 | **弱势** | 🚨 EMERGENCY_DOWN |
| 03-18 | -2 | 弱势 | 弱势 | 维持 |
| **03-19** | **-8** | 熊 | **熊** | 🚨 EMERGENCY_DOWN |
| 03-20 | -8 | 熊 | 熊 | 🚨 emergency 仍触发但已达端点 |
| 03-23 | -8 | 熊 | 熊 | 同上 |
| 03-24 | +7 | 强牛 | 熊 | 单日反弹但 3 日窗口被重置 |
| 03-25 | +7 | 强牛 | 熊 | 同上 |
| 03-26 | -4 | 弱势 | 熊 | 🚨 emergency 再触发 |
| 03-27 | +4 | 强势震荡 | 熊 | 维持 |
| 03-30 | +2 | 强势震荡 | 熊 | 维持 |
| 03-31 | -3 | 弱势 | 熊 | 🚨 emergency 再触发 |
| 04-01 | +4 | 强势震荡 | 熊 | 维持 |
| 04-02 | -3 | 弱势 | 熊 | 🚨 emergency 再触发 |
| 04-03 | -3 | 弱势 | 熊 | 维持 |
| 04-07 | +5 | 强势震荡 | 熊 | 维持 |
| **04-08** | **+7** | 强牛 | **弱势** | 3 日确认达成 (BEAR → WEAK) |
| **04-09** | **0** | 中性 | **熊** | 🚨 EMERGENCY_DOWN 立刻打回 |
| 04-10 | +5 | 强势震荡 | 熊 | 历史重置后只有 1 日 |
| 04-13 | +4 | 强势震荡 | 熊 | 历史 2 日，bootstrap 不切换 |
| **04-14** | **+4** | 强势震荡 | **弱势** | 3 日 [+5, +4, +4] 全部 ≥ -5，达成 |

**整段画像**：行情从中性快速崩塌到熊（2 次 emergency 串联，6 个交易日完成），随后在熊里反复 18 个交易日，期间出现 5 次单日反弹尝试均被 emergency_down 打回，直到 4/8 凑齐第一次 3 日确认才出熊，但出熊次日就被 4/9 emergency 一巴掌打回去，再用 4 个交易日才真正脱熊。这是经典的 **下行容易上行难** 的非对称切换效果。

---

## 二、关键案例详解

### Case 1: NEUTRAL → WEAK 的入场（2026-03-17，emergency_down）

```json
{
  "date": "2026-03-17",
  "score": -8,
  "breakdown": {"ma_position": -1, "advance_decline": -2, "sentiment_delta": -1,
                "sentiment_index": -2, "streak_height": -2, "volume_trend": 0},
  "last_regime": "中性震荡",
  "regime": "弱势震荡",
  "emergency_switch": true,
  "emergency_reason": ["total_drop_5", "sentiment_collapse"]
}
```

**触发点**：
- 总分昨日 -3 → 今日 -8，跌 5 分 → `total_drop_5` ✓
- 情绪评分 20 ≤ 20 → `sentiment_collapse` ✓

**行为**：emergency_down 触发，从 NEUTRAL 跳一档到 WEAK（注意是 **跳一档**，不是直接跳到 raw 的熊；逃生门一次只动一格）。

**为什么是合理的**：HS300 -0.73% / CSI1000 -2.33% 看起来不算极端，但 **涨跌家数比 0.16**（极少股票上涨）+ **情绪评分 20** + **0 板** 的组合说明盘面已经结构性恶化。3 日确认会等 2 天才反应，逃生门当天就降档，避免错过下跌窗口。

---

### Case 2: WEAK → BEAR 的入场（2026-03-19，emergency_down）

```json
{
  "date": "2026-03-19",
  "score": -8,
  "last_regime": "弱势震荡",
  "regime": "熊",
  "emergency_switch": true,
  "emergency_reason": ["total_drop_5", "sentiment_collapse"]
}
```

**剧本**：3/18 反弹了一天（总分 -2），3/19 直接 -8，再次跌 6 分。emergency_down 从 WEAK 再降一档到 BEAR。

**为什么需要两次 emergency**：从 NEUTRAL 直接到 BEAR 必须跨 2 档，而逃生门规则一次只动一格——这是为了避免**单日噪音直接定性极端 regime**。两次串联触发说明真的连续两次结构性塌陷，证据足够。

---

### Case 3: BEAR 里 +7 仍然出不去（2026-03-24/25）

```json
{
  "date": "2026-03-24",
  "score": 7,
  "last_regime": "熊",
  "regime": "熊",
  "switched": false,
  "switch_warning": "历史不足 3 日, 启动模式维持 熊; 今日得分 +7 已进入 强牛 区间"
}
```

**为什么 +7 还在熊**：
1. 上一次 emergency 在 3/23 触发，[scoring.py:_build_scores_window](../scripts/scoring.py) 会把窗口重置
2. 重置后历史只有 1 个交易日，进入 bootstrap 模式
3. 即使是相邻档（BEAR → WEAK，要求 3 日全部 ≥ -5），也不够 3 日

**这是设计意图，不是 bug**：spec §6 明确"宁可踏空不可站岗"。3/24 +7 单日很漂亮，但 3/23 之前是 -8，盘面刚从悬崖上跳起来，不能立刻归位。果然 3/26 又掉到 -4 重新触发 emergency。

---

### Case 4: BEAR 里反复触发 emergency_down（3/26, 3/31, 4/2）

每次行情试图反弹（3/24-25 +7+7、3/27 +4、4/1 +4），紧接着的下一日又跌回 emergency 阈值。共触发 5 次 emergency_down（含 3/17 和 3/19 的入场和 4/9 的回头），每次都重置 3 日窗口，导致根本凑不齐"3 日全部 ≥ -5"的出熊条件。

**实战意义**：对下游 strategy skill 而言，看到 `emergency_switch == true` 这个标志位就应该把仓位乘 0.7（见 [mapping.md 的乘数表](mapping.md)）。这一段是空仓避坑期，不是机会期。

---

### Case 5: BEAR → WEAK 真正出熊（2026-04-08，3 日确认）

```json
{
  "date": "2026-04-08",
  "score": 7,
  "breakdown": {"ma_position": 0, "advance_decline": 2, "sentiment_delta": 2,
                "sentiment_index": 2, "streak_height": 1, "volume_trend": 0},
  "last_regime": "熊",
  "regime": "弱势震荡",
  "switched": true,
  "emergency_switch": false
}
```

**3 日窗口**：[4/3 -3, 4/7 +5, 4/8 +7]
- 检查相邻档 WEAK_RANGE（下限 -5）：-3 ≥ -5 ✓，+5 ≥ -5 ✓，+7 ≥ -5 ✓
- 全部满足 → 切换到 WEAK_RANGE

**但只能跳一档**：raw regime 是 STRONG_BULL（+7），可即使如此也只能从 BEAR 升到相邻的 WEAK。这是 [regime_rules.py:138-153](../scripts/regime_rules.py#L138-L153) 的"上行只允许相邻档"规则——上行严格、不允许跨档。

---

### Case 6: 出熊次日立刻被打回（2026-04-09，emergency_down）

```json
{
  "date": "2026-04-09",
  "score": 0,
  "last_regime": "弱势震荡",
  "regime": "熊",
  "emergency_switch": true,
  "emergency_reason": ["total_drop_5"]
}
```

**触发点**：总分 +7 → 0，跌 7 分 → `total_drop_5` ✓

**为什么这个判断是对的**：4/8 +7 的反弹本身只满足了 3 日确认的最低要求，第二天就熄火说明前一天是脉冲不是趋势反转。emergency_down 在这种"假突破后立刻熄火"的场景能有效保护下游不追高。

---

### Case 7: 真正脱熊（2026-04-14）

```json
{
  "date": "2026-04-14",
  "score": 4,
  "last_regime": "熊",
  "regime": "弱势震荡",
  "switched": true
}
```

**3 日窗口**（4/9 emergency 重置后重新累积）：[4/10 +5, 4/13 +4, 4/14 +4]
- 全部 ≥ -5 ✓
- 切换到 WEAK_RANGE

**注意**：4/13 的 regime 还是熊不是因为分数不够，而是 emergency 重置后只有 [+5, +4] 两天历史，bootstrap 模式不切换。4/14 凑齐 3 天才达成确认。这是为什么从 4/9 emergency 触发到 4/14 真正出熊**整整需要 4 个交易日**。

---

## 三、未覆盖的 regime（待历史数据补充）

回测周期（3 月初到 4 月中）市场偏弱，**STRONG_RANGE / STRONG_BULL 没有出现过**。需要后续在更乐观的行情或历史数据上回放：

| 待验证日期 | 市场背景 | 预期 regime | 验证重点 |
|---|---|---|---|
| 2024-09-24 | "924" 急涨起点 | NEUTRAL → STRONG_RANGE/STRONG_BULL | 上行逃生门是否触发（AND 严格条件） |
| 2024-09-30 | 924 后第 5 个交易日 | STRONG_BULL（确认达成） | 3 日确认是否能跨档识别强牛 |
| 2024-10-08 | 急涨后回落 | STRONG_BULL → STRONG_RANGE/NEUTRAL | 下行 emergency 是否过度反应 |
| 2025-01-27 | 春节前最后交易日 | ? | 量能塌缩 + 节前避险 |
| 2025-02-05 | 春节后开盘 | ? | 跳空缺口对 MA 维度的影响 |

填充方式：用 bot11 的 daily_review.py 跑历史日期生成复盘 MD，再 `python3 scripts/classify.py --date=YYYY-MM-DD --from-review=path/to/复盘.md`。

---

## 四、案例模板

新增案例时套用：

```markdown
### Case N: 一句话标题（YYYY-MM-DD）

\`\`\`json
{
  "date": "YYYY-MM-DD",
  "score": ±N,
  "breakdown": {"ma_position": ?, "advance_decline": ?, ...},
  "last_regime": "?",
  "regime": "?",
  "switched": ?,
  "emergency_switch": ?,
  "emergency_reason": [...]
}
\`\`\`

**触发点 / 3 日窗口**：解释规则如何被命中

**为什么这个判断是对的（或错的）**：事后市场表现 + 评价

**下游影响**：是否提示 strategy skill 加乘数、是否值得加仓/减仓
```

---

## 五、统计观察（24 天回测）

- **emergency_down 触发频率**：8 次 / 23 个交易日 ≈ 35%。说明 3 月这段行情极度不稳定，平均每 3 天就出一次结构性恶化。正常市场预期应该 < 10%，超过 30% 意味着行情本身就异常。
- **emergency_up 触发次数**：0 次。AND 严格条件 + 双指数 ≥ 2% + 涨跌比 ≥ 0.80 的组合在弱市里几乎不可能凑齐——这是设计意图。
- **regime 切换次数**（含 emergency）：5 次（3/17 NEUTRAL→WEAK, 3/19 WEAK→BEAR, 4/8 BEAR→WEAK, 4/9 WEAK→BEAR, 4/14 BEAR→WEAK）。每次切换都是有事件支撑的，没有发现"分数抖动导致来回切换"的案例。
- **bootstrap 持续天数**：运行启动后前 2 天必然 bootstrap，每次 emergency 后 3 日内会再次进入 bootstrap。整段共有约 8 个交易日处于 bootstrap 状态，期间 `switch_warning` 字段提示用户当前判断信心较低。

**校准结论**：当前阈值在弱市/反复市表现合理，没有出现明显的"该切没切"或"不该切乱切"。强势行情的覆盖度需要补充历史数据后再校准。
