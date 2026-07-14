# 战法推荐生成 SOP（Agent 能力测试版）

本文档描述"给定一个交易日，完成六维打分 → regime 判定 → 输出战法推荐"的全链路步骤。用于测试 agent 的规则理解和推理能力。不涉及落库。

---

## 总览

```
读取当日原始数据（DB 已有，一条 SQL）
  → 六维打分（每维 -2 ~ +2）
  → 总分求和（-12 ~ +12）
  → Regime 判定（5 档 + 3 日确认 + 逃生门）
  → 战法映射 + 仓位计算
  → 输出结构化建议
```

---

## Step 1：读取当日原始数据

原始数据已由每日 cron pipeline 采集并写入 `market.db`（路径 `/home/rooot/agent_invest_lab/data/market.db`）。agent 只需一条 SQL 读取：

```sql
SELECT
  r.trade_date,
  -- 维度 2-5：市场宽度 + 情绪（regime_raw_daily）
  r.advance_count,                         -- 上涨家数
  r.decline_count,                         -- 下跌家数
  r.flat_count,                            -- 平盘家数
  r.limit_up_count,                        -- 涨停数
  r.limit_down_count,                      -- 跌停数
  r.sentiment_index,                       -- 情绪评分 0-100
  r.max_streak,                            -- 最高连板
  -- 维度 1+6 的派生值（regime_classify_daily，已由 replay.py 算好）
  c.score_ma_position,                     -- 可用于交叉验证
  c.score_volume_trend,
  -- 逃生门需要的涨跌幅（从 regime_classify_daily 的 raw_data 无法直接取，
  -- 但 regime_classify_daily 已包含最终 regime 结果可用于对照）
  c.total_score       AS ref_total_score,
  c.regime_code       AS ref_regime_code,
  c.switched          AS ref_switched,
  c.emergency_switch  AS ref_emergency_switch
FROM regime_raw_daily r
LEFT JOIN regime_classify_daily c
  ON r.trade_date = c.trade_date AND c.rules_version = 'v2'
WHERE r.trade_date = '{目标日期}'
```

### 原始数据字段说明

| 字段 | 含义 | 用于哪个维度 |
|------|------|-------------|
| `advance_count / decline_count / flat_count` | 涨跌平家数 | 维度 2：涨跌家数比 = advance / (advance + decline + flat) |
| `limit_up_count / limit_down_count` | 涨停跌停数 | 维度 3：delta = limit_up - limit_down |
| `sentiment_index` | 情绪评分 0-100 | 维度 4 |
| `max_streak` | 最高连板板数 | 维度 5 |

维度 1（指数均线）和维度 6（成交量趋势）的原始数据需要从 `index_daily` 表计算均线和量比，但 `regime_classify_daily` 已经存了各维度得分，agent 可以直接用 `ref_*` 字段做交叉验证，重点测试维度 2-5 的打分和后续 regime 判定逻辑。

**如果要完整测试维度 1 和 6**，agent 需额外查 `index_daily`，但这属于进阶测试，基础测试中可直接引用 `c.score_ma_position` 和 `c.score_volume_trend`。

---

## Step 2：六维打分

每个维度按以下阈值映射到 -2 ~ +2 的整数分：

### 维度 1：指数 vs 均线（单指数打分）

| 得分 | 条件 |
|------|------|
| +2 | 站上全部均线 且 MA 多头排列 (MA5 > MA20 > MA60 > MA250，严格大于) |
| +1 | 站上 MA5/MA20/MA60 且 MA5 >= MA20（短期未死叉） |
| 0 | 站上 MA20 |
| -1 | 跌破 MA20 |
| -2 | MA 空头排列 (MA5 < MA20 < MA60 < MA250)，**优先检测，不论价格位置** |

- "站上"用 `>=`（含持平）
- HS300 和 CSI1000 各自打分后**取最小值**（不是平均）

### 维度 2：涨跌家数比

| 得分 | 区间 |
|------|------|
| +2 | ratio > 0.75 |
| +1 | 0.60 <= ratio <= 0.75 |
| 0 | 0.40 <= ratio < 0.60 |
| -1 | 0.25 <= ratio < 0.40 |
| -2 | ratio < 0.25 |

### 维度 3：涨停-跌停差 (delta)

| 得分 | 区间 |
|------|------|
| +2 | delta > 80 |
| +1 | 40 <= delta <= 80 |
| 0 | 10 <= delta < 40 |
| -1 | -40 <= delta < 10 |
| -2 | delta < -40 |

### 维度 4：情绪评分 (0-100)

| 得分 | 区间 |
|------|------|
| +2 | > 70 |
| +1 | 55 <= x <= 70 |
| 0 | 40 <= x < 55 |
| -1 | 25 <= x < 40 |
| -2 | < 25 |

### 维度 5：最高连板高度

| 得分 | 条件 |
|------|------|
| +2 | >= 5 板 |
| +1 | 4 板 |
| 0 | 3 板 |
| -1 | 2 板 |
| -2 | <= 1 板 |

### 维度 6：成交量趋势 (MA5/MA20)

| 得分 | 区间 |
|------|------|
| +2 | > 1.30 |
| +1 | 1.10 <= x <= 1.30 |
| 0 | 0.90 <= x < 1.10 |
| -1 | 0.70 <= x < 0.90 |
| -2 | < 0.70 |

### 总分

六维等权相加，范围 [-12, +12]。缺失维度按 0 分计。

---

## Step 3：Regime 判定

### 3.1 分数 → 原始 Regime

| 总分 | Regime | 中文 |
|------|--------|------|
| >= +7 | STRONG_BULL | 强牛 |
| +2 ~ +6 | STRONG_RANGE | 强势震荡 |
| -1 ~ +1 | NEUTRAL_RANGE | 中性震荡 |
| -5 ~ -2 | WEAK_RANGE | 弱势震荡 |
| <= -6 | BEAR | 熊 |

### 3.2 逃生门（优先于 3 日确认）

逃生门在 3 日确认之前检查，触发则直接跳一档，跳过确认流程。

**下行逃生门（OR 宽松，任一条件触发即生效）：**

| 条件 ID | 规则 |
|---------|------|
| total_drop_5 | 今日总分 - 昨日总分 <= -5 |
| index_crash | (HS300 或 CSI1000 当日涨幅 <= -3%) 且 涨跌比 <= 0.25 |
| sentiment_collapse | 情绪评分 <= 20 或 涨停差 <= -40 |

**上行逃生门（AND 严格，全部条件同时满足才生效）：**

| 条件 ID | 规则 |
|---------|------|
| total_surge_5 | 今日总分 - 昨日总分 >= +5 |
| index_rally | HS300 和 CSI1000 当日涨幅均 >= 2% |
| breadth_confirm | 涨跌比 >= 0.80 |

- 下行优先于上行（两者同时触发，走下行）
- 触发后 regime 向对应方向跳一档（最多一档）
- 缺失数据的子条件视为"未触发"

### 3.3 三日确认 + 缓冲区

如果逃生门未触发，进入 3 日确认流程。取最近 3 日总分（含今日）：

**向乐观切换**（today_raw 比 last_regime 更乐观）：
- 只允许逐档升一级
- 条件：3 日得分全部 >= 目标档下限

**向悲观切换**（today_raw 比 last_regime 更悲观）：
- 相邻档：3 日得分全部 <= 目标档上限 - 1（多 1 分缓冲，"宁可踏空不可站岗"）
- 跨档（最多跨 2 档）：同时满足相邻档和跨档的悲观条件

**不满足确认条件**：维持旧 regime，输出 switch_warning

**历史不足 3 日**：bootstrap 模式，维持 last_regime 不切换

### 3.4 确定 last_regime

测试时 agent 需要知道前一交易日的 regime。可以通过以下方式获取：

```sql
SELECT regime_code FROM regime_classify_daily
WHERE trade_date < '{目标日期}' AND rules_version = 'v2'
ORDER BY trade_date DESC LIMIT 1
```

或者 agent 从前几日数据自行推算（更能测试能力）。

---

## Step 4：战法映射 + 仓位计算

### 4.1 查表映射

| Regime | 推荐战法（按优先级） | 禁止战法 | 总仓位上限 | 单票上限 |
|--------|----------------------|----------|-----------|---------|
| **强牛** | S1 突破 → S2 龙头板接力 → S6 趋势持有 | S7, S8 | 90% | 30% |
| **强势震荡** | S4 回踩 → S5 龙回头 → S3 首板接力 | S1, S7 | 70% | 25% |
| **中性震荡** | S5 龙回头 → S4 回踩(strict) | S1, S2, S6 | 50% | 20% |
| **弱势震荡** | S5 龙回头(试错) | S1, S2, S3, S6 | 30% | 15% |
| **熊** | S8 空仓观望 → S7 超跌反弹(1-2成仓一日游) | S1, S2, S3, S4, S6 | 20% | 10% |

### 4.2 战法速查

| ID | 名称 | 一句话 |
|----|------|--------|
| S1 | 突破战法 | 强势股横盘平台放量突破，进场买突破位 |
| S2 | 龙头板接力 | 盯高度板(3板+)梯队龙头，次日竞价接力 |
| S3 | 首板接力 | 当日领涨板块首板，次日竞价低开进 |
| S4 | 回踩战法 | 强势股回踩 5/10 日线不破，次日进 |
| S5 | 龙回头 | 前期龙头冷却后二次放量反包 |
| S6 | 趋势持有 | 指数强势 + 龙头股持仓 5-10 天 |
| S7 | 超跌反弹 | 连续大跌后的技术性反弹，一日游 |
| S8 | 空仓观望 | 不开新仓，只处理已有持仓 |

### 4.3 仓位乘数叠加

在基准仓位上叠加修正（可多重叠加）：

| 条件 | 乘数 |
|------|------|
| `confidence == "medium"` | ×0.8 |
| `confidence == "low"` | ×0.5 |
| `switched == true`（regime 切换当日） | ×0.8 |
| `emergency_switch == true`（逃生门触发当日） | ×0.7 |

```
实际总仓位 = 基准总仓位 × confidence乘数 × switched乘数 × emergency乘数
实际单票上限 = 基准单票上限 × (同上)
```

### 4.4 降级策略

- 某维度数据缺失 → 该维度得分按 0 计入总分
- `confidence` 按缺失数降级：0 维 → high，1-2 维 → medium，>= 3 维 → low

---

## Step 5：输出格式

```
📅 {日期} 市场 Regime 研判

━━ 市场状态 ━━
当前档位：{regime_name}
六维总分：{total_score} / 12
数据可信度：{confidence}
{如果 switched: "⚠️ 今日从 {last_regime_name} 切换至 {regime_name}"}
{如果 emergency_switch: "🚨 逃生门触发 ({direction}): {reasons}"}

━━ 六维明细 ━━
指数均线：  {score} | 原始值: HS300 close={}, MA5/20/60/250=...
涨跌家数：  {score} | 原始值: ratio={}
涨跌停差：  {score} | 原始值: delta={} (涨停{} 跌停{})
情绪评分：  {score} | 原始值: {}
连板高度：  {score} | 原始值: {} 板
成交量趋势：{score} | 原始值: MA5/MA20={}

━━ 推荐战法 ━━
1. {战法名} — {一句话描述} {如有 mode: "(mode)"}
2. ...

━━ 禁止战法 ━━
{列出禁止的战法}

━━ 仓位控制 ━━
总仓位上限：{}%
单票上限：{}%
{列出生效的修正项}

━━ 风险提示 ━━
{按需输出: 缺失维度 / 低可信度 / 切换首日 / bootstrap 等}
```

---

## 验证 agent 输出的检查清单

- [ ] 六维原始数据是否正确采集（能展示原始值）
- [ ] 每个维度的打分是否符合阈值表（边界值归属正确）
- [ ] 六维得分加总 == total_score
- [ ] 总分 → 原始 regime 映射正确
- [ ] 逃生门判断逻辑正确（下行 OR / 上行 AND）
- [ ] 3 日确认逻辑正确（缓冲区、上行逐档、下行可跨档）
- [ ] regime_code → 推荐战法映射正确（禁止战法不出现在推荐里）
- [ ] 仓位乘数计算正确（多重叠加场景）
- [ ] 缺失维度正确处理（得分 0 + confidence 降级）
- [ ] switched / emergency 的特殊提示正确触发

---

## 测试用例建议

可以用以下历史日期测试 agent，覆盖不同 regime 和边界情况：

```sql
-- 找各种 regime 的样本日期
SELECT trade_date, regime_name, total_score, switched, emergency_switch, confidence
FROM regime_classify_daily
WHERE rules_version = 'v2'
  AND trade_date >= '2026-01-01'
ORDER BY trade_date DESC
LIMIT 20
```

重点测试：
1. **普通日**：无切换、无逃生门、confidence=high — 测试基础链路
2. **切换日**：switched=1 — 测试 3 日确认逻辑和仓位乘数
3. **逃生门日**：emergency_switch=1 — 测试逃生门判断
4. **低可信度日**：confidence=medium/low — 测试降级和风险提示
5. **极端日**：总分接近 +12 或 -12 — 测试边界 regime 映射
