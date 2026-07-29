# 单指数「持有承诺块」设计（早赎红线可见化 · 防翻烙饼）

- 日期：2026-07-29
- 分支：feat/world-system
- 作用范围：单指数择时 bot（bot1~20，`botKindOf(botId)==='single-fund'`）
- 关联：多基金家族（bot101/102/103）的频繁交易治理（杠杆2/3，见 `project_bot105d_multi_equity_agent`）——本设计是**单指数家族的对应缺口补齐**，机制不同、不复用多基金那套载体/卫星逻辑。

## 1. 问题

单指数 bot 在同一只基金上反复「买了没几天又卖」，赎回费吃掉短线 alpha，出现「标的没坏、手续费亏得比标的下跌还多」。

实证（run `dash-2026-07-27T01-10-07` / bot20d / 基金 019875，2025-01-02→2026-07-23）：

- 65 单 / 31 卖，其中 **6 笔是 7 日内早赎（fee>0），累计白扔 ¥32,412**。
- 019875 费率：**申购 0%，赎回 <7 天 1.5%，≥7 天 0%**。→ 这只基金唯一亏手续费的方式就是「买入后 7 天内卖出」；持满 7 天离场免费。
- 最典型：05-06 买 56 万 → 05-07 卖（费¥4,179）→ 05-08 卖（费¥4,127）→ 05-11 又买回 55 万。5 天翻烙饼扔 ¥8,306，仓位绕一圈回原点。
- 06-22 买 → 06-23 卖（费¥7,904）→ 06-24 买 → 06-25 买：连续 4 天 买-卖-买-买。

对照：weekly 变体（bot20）决策点是日度的 1/5，翻烙饼机会天然少 5 倍——**日度节奏是频率放大器**，但根因是缺少「短持早赎」的系统侧约束。

## 2. 根因（机制缺口）

杠杆2/3 的系统侧交易纪律块（`computeVehicleCooldowns` + `tradeDisciplineBlock`）**只在 `marketReportsBlock` 内装配（message.ts:981），而该块只给 bot101/102/103 用**（message.ts:963 注释写死）。**单指数家族（bot1~20）完全没接**——它们每日只拿到：

- `tradingFeesBlock`（费率表，纯信息，堵不住行为）；
- `singleFundReviewBlock`（每 5 交易日复盘，管方法论有效性，不管 churn）。

即：单指数家族**零系统侧 churn 约束**。且多基金那套「清仓后 10td 冷却」不适配单指数——单指数极少清零，是**部分减仓后反向翻烙饼**，`held.has(code)` 恒为真会跳过冷却。

## 3. 设计原则（用户裁定，2026-07-29）

1. **牙齿不是写死的技术指标逃生口**。用户原话：「不必规则写死…而不是仅看到某个技术指标破位」。
2. **承诺前置到建仓那一刻**。用户原话：「agent 再买入的时候就知道自己必须要持有至少 7 天才能卖出」。
3. **临时提前离场需经过思考的充分理由，交给 agent 决策**（不是『指标破位』四个字，也不是『感觉风险大』的主观挥手）。
4. 继承既有大方向：**裁量有成本 ≠ 裁量被禁止**；**系统侧注入 > 方法论文本**；**先软后硬**（软块先行，跑一个月有滥用再升 MCP 硬闸）。
5. **不拦小仓试探**——「试试」是合法 alpha 探索；块只把试探仓的早赎成本摆到台面，让「标的没坏」时别无意识地当天/次日翻掉吃 1.5%。

## 4. 方案：单指数「持有承诺块」

`world/src/message.ts` 新增块，只对 `botKindOf(botId)==='single-fund'` 渲染，在 `renderDailyMessage`（message.ts:1092）里装配（Day1 与 DayN 两条路径都插；与 `strategyReviewBlock` 平行）。

**数据源（全部现成、确定性、无未来函数）：**

- `dc.account.recentOrders`（daily-context 已构建；字段 `fund_code / order_type / order_date / status / order_amount`）；
- 基金 `redeem_fee_json` 赎回阶梯（`tradingFeesBlock` 已在读，来自 `dc.fundFees`）——**窗口与费率按每只基金自己的阶梯算，不写死 7 日/1.5%**；免赎档 = 首个 `rate=0` 的档；早赎费率 = 命中档费率；
- benchmark 交易日历 `dc.benchmark.pointsByDate`（`computeVehicleCooldowns`/`singleFundReviewBlock` 已在用）；
- `dc.holdings`（`fund_code / shares / latest_nav / market_value / weight`）与 `dc.fundSeries`/benchmark 序列，用于「自买入以来标的涨跌%」。

数据任一缺失（老快照 / 无日历 / 无费率）→ 返回空串，行为完全不变（零回归）。

### 4.1 牙齿① 建仓 = 持有承诺（前置到买入）

块内恒定写明该基金的免赎档（按其赎回阶梯渲染）：

> 【持有承诺 · 系统核算】{fund_code} 免赎档 = 持满 **N 个交易日**；N 日内离场确定亏 **{rate}%** 早赎费。**建仓/加仓 = 承诺持有至少 N 个交易日**——买之前就想清楚你能不能拿住 N 天，别买完就想翻。

（N、rate 来自该基金 redeem_fee_json；多档时列全阶梯，承诺锚定「首个免赎档」。）

### 4.2 牙齿② 窗内离场 · 思考闸

对每笔**仍在早赎窗内**的持仓 lot，系统从 recentOrders + 交易日历确定性算出并列表：

- 买入日 `order_date`、已持 `k / N` 交易日、距免赎还剩 `N-k` 交易日；
- **今日卖出的早赎费估算 ≈ 费率 × 该 lot 市值**（市值口径见 4.4，标 `≈`）；
- 自买入以来标的涨跌 `move%`（fund series/benchmark）。

然后写：

> - {code}：{buy_date} 买入（第 {k}/{N} 交易日，距免赎还剩 {N-k} 交易日）。今日卖出早赎费 ≈ ¥{fee}（{rate}%）；标的自买入 {move}%。
> ⚠️ **今日若要在窗内卖出该份额：先在 mem0 写下经过思考的充分理由再下单**——不是『指标破位』，不是『感觉风险大』，而是论证为什么这个临时情况足以推翻你建仓时的持有承诺。审计会核：窗内卖出而无实质论证 = 违规。

义务明写进当日（软，仍不拦单），与 Task9 系统块口径一致；`先软后硬`——本期不上 MCP 硬拦。

### 4.3 窗内 lot 识别（FIFO 近似）

早赎窗很短（≤ 档天数），只需回看最近 `N` 交易日内的买单：

- 取 recentOrders 里 `order_type='buy'` 且 `order_date` 在最近 N 交易日内（`tdBetween(cal, order_date, asOfDate) < N`）的订单为「窗内 lot」；
- 期间若对同基金有卖出，按 FIFO 先消耗最早的份额（近似：窗内 lot 净额 = Σ 窗内买入 − 该窗内已卖出，下限 0）；
- 该基金窗内净额 = 0 → 不渲染该基金的思考闸行（无在窗份额）。

近似口径够用（软块只需把「还有份额在窗内 + 早赎成本数字」顶到脸上），不追求逐份额精确成本基。

### 4.4 早赎费估算口径

- lot 市值 ≈ 该 lot `order_amount`(买入投入金额，元) × (当前 `latest_nav` / 买入参考 nav)。recentOrders 不带买入 nav → 退化用**成本基**：早赎费 ≈ 费率 × 窗内买入投入金额（元），标 `≈`/`约`。软块只需量级正确。
- 「标的自买入 move%」：优先用 `dc.fundSeries` 该基金净值序列 `(buy_date, asOfDate)`；缺则用 benchmark `pointsByDate`。用于印证「手续费 vs 标的实际波动」——`|move| < rate` 且未硬跌时，块的论点即成立（翻烙饼净亏）。

## 5. 刻意不做（YAGNI / 保裁量）

- 不写死技术指标逃生口（用户明确）。
- 不上 MCP 硬拦（先软后硬）。
- 不加单独「反向翻烙饼冷却」：每次重新买入 = 一次全新 N 日承诺，已覆盖 买→卖→买；申购 0 费，重进本身不花钱，真正成本只在早赎那条腿。
- 不拦小仓试探。
- 不动多基金（bot101/102/103）路径一个字节。

## 6. 组件边界

| 单元 | 职责 | 依赖 | 输出 |
|---|---|---|---|
| `computeInWindowLots(dc, asOfDate, tiersByFund)` | 从 recentOrders+日历+赎回阶梯 算出各基金窗内 lot（买入日/已持 k/N/净额/费率） | recentOrders、benchmark 日历、fundFees | `Map<fund_code, InWindowLot[]>` |
| `holdCommitmentBlock(dc, botId, asOfDate)` | 渲染承诺块（牙齿①恒定 + 牙齿②窗内列表），数据缺失→空串 | 上者 + holdings + fundSeries | string |
| `renderDailyMessage`（改） | 单指数路径插入该块（Day1/DayN 两处） | `botKindOf`、上者 | 完整日消息 |

`computeInWindowLots` 与 `holdCommitmentBlock` 各自可独立测试（纯函数，喂 fixture）。

## 7. 测试（message.test.ts / 新 fixture）

1. 窗内持仓（买入 <N 交易日）→ 渲染承诺 + 该 lot 早赎费 + move% + 思考闸义务行。
2. 满 N 日持仓 → 不渲染思考闸行（承诺行仍在，因它是恒定说明）。
3. 无 recentOrders / 无 benchmark 日历 / 无 fundFees → 空串（零回归）。
4. 多基金 bot（bot101）→ 块不渲染（`botKindOf` 门控）。
5. FIFO：窗内买 A、随后部分卖 → 净额正确扣减；净额 0 → 无思考闸行。
6. 多档赎回阶梯基金 → 承诺锚定首个免赎档，费率取命中档。
7. off-by-one：第 N 交易日 = 仍在窗（当日卖仍收费）；第 N+1 交易日 = 免赎（不渲染）。与既有 `min(…, +1-elapsed)` 口径一致。

## 8. 上线（同杠杆2/3）

- 当前 run **不回溯**；改动需一次普通 pause/resume 重启进程，下一决策日起生效；不重启则只对未来 run 生效。
- 软块先跑；观察一个月单指数家族「7 日内早赎笔数 / 早赎费」是否下降；若 bot 明显滥用「充分理由」绕过，再把窗内卖出升为 MCP 硬闸（server 端，需带客观硬止损标志）。
- 验收指标：单指数 run 的 `fee>0`（<N 日早赎）卖单笔数与累计早赎费显著下降；承诺块窗内列表与实际 lot 一致。

## 9. 改动面

- `world/src/message.ts`：新增 `computeInWindowLots` + `holdCommitmentBlock`；`renderDailyMessage` 单指数路径接线。
- `world/test/message.test.ts`（或同级）：新增上述 7 组用例。
- 不改 daily-context.ts（字段已够）；不改 fund-portfolio-mcp（软块阶段不上硬闸）。
