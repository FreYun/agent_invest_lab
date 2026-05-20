# Bot 卡片基准线设计

## 目的

在回测看板 (`http://127.0.0.1:48080/`) 的「个人卡片页面」（即选中某个 bot 后的右侧详情视图）的「净值曲线」图上叠加一条**基准线**，让用户能直接对比"bot 主动交易 vs 单纯买入并持有该基金"的净值轨迹。同时按 A 股配色习惯，把图上的**买入点改为红色、卖出点改为绿色**。

## 背景与现状

- 看板服务：`world/src/backtest-dashboard/server.ts` + `index.html`
- 当前在 `http://127.0.0.1:48080/` 监听，绑定 `0.0.0.0`，端口对齐 28080 那一套
- 数据来源：`data/fund.db`
- 当前的「净值曲线」图：单条 bot.series 的折线，叠加买卖点 marker
- 数据现实：
  - 每个 bot **恰好持有 1 只基金**（已经在 `fund_bot_position_snapshots` 上验证过：bot2/4/7/11→510300，bot6→001617）
  - `fund_nav` 表只有 `nav`（单位净值）有数据，`acc_nav` / `daily_return_pct` 全空
  - 现状不存在真正的"复权单位净值"列，先用 `nav` 近似（ETF/QDII 这类品种分红极少，视觉上不会有可见跳坑）

## 设计

### 后端：`server.ts` 输出新增 `benchmark`

`BotDataset` 新增一个可选字段：

```ts
interface BotBenchmark {
  fundCode: string
  fundName: string
  firstBuyDate: string        // bot 首次 buy 该基金的 action_date
  baselineNav: number         // 基准锚点 NAV
  series: Array<{
    trade_date: string
    nav: number               // 原始单位净值
    net_value: number         // nav / baselineNav，起点对齐到 1.0
  }>
}

interface BotDataset {
  // ...其余原字段不变
  benchmark: BotBenchmark | null   // 没有 buy 动作时返回 null
}
```

取数流程（写在 `loadDataset` 的 per-bot 循环里）：

1. 从已经加载好的 `actions` 里 `find(a => a.side === 'buy')`，取最早一条
   - `fund_code` 用这条 action 的 `fund_code`
   - `firstBuyDate` 用这条 action 的 `action_date`
   - 找不到任何 buy 动作 → `benchmark = null`
2. 单条 SQL 拉取 NAV 序列：
   ```sql
   SELECT n.nav_date, n.nav, COALESCE(i.fund_name, n.fund_code) AS fund_name
   FROM fund_nav n
   LEFT JOIN fund_info i ON i.fund_code = n.fund_code
   WHERE n.fund_code = ?
     AND n.nav_date >= ?  -- firstBuyDate
     AND n.nav_date <= ?  -- latestTradeDate
   ORDER BY n.nav_date ASC
   ```
3. `baselineNav` 取序列里**第一条 nav_date ≥ firstBuyDate** 的 nav（用 nav_date 自然排序后取 [0]）；序列空就 `benchmark = null`
4. 归一化：`net_value = nav / baselineNav`
5. 输出 series 时连同 `nav` 一起带上（方便前端 tooltip）

性能：每个 bot 多一条 SQL，bot 数量目前 5 个，可忽略。

### 前端：`renderChart(bot)` 叠加第二条线 + 改 marker 色

#### A. 第二条折线

- 基准线样式：
  - `stroke: var(--accent2)`（`#ffb86b` 暖橙色，已经在 CSS 里）
  - `stroke-width: 2`（比 bot 主线 3 稍细）
  - `stroke-dasharray: 4 4`（虚线，明显区分主线 vs 基准）
  - 不画 area fill（避免和 bot 主线的 area 冲突）
- 在面板标题旁加一个紧凑图例：`■ bot 净值` + `┄ 基准 510300 沪深300ETF`（用对应颜色的小方块/虚线段）

**X 轴对齐**（重要——当前 `linePath` 按 *index* 均分 X，直接复用会把短的基准线拉伸到全宽，得改）：

1. 在 `renderChart` 里先构造一条共享的 X 轴日期轴：
   ```js
   const xAxisDates = [...new Set([
     ...bot.series.map(p => p.trade_date),
     ...(bot.benchmark?.series.map(p => p.trade_date) ?? []),
   ])].sort()
   const dateToIdx = new Map(xAxisDates.map((d, i) => [d, i]))
   ```
2. 把现有 `linePath` 改造或新增一个 `linePathOnAxis(points, xAxisDates, yRange, width, height, padding)`：X 取 `dateToIdx.get(p.trade_date) / (xAxisDates.length - 1)`，Y 用传入的 `yRange.min/max`（不再每条线自己算）
3. bot 主线和基准线都走这条新路径——bot.series 在 xAxisDates 中占满，benchmark.series 自然从首买日对应的 X 开始向右

**Y 轴共享**：

- 在画两条线之前先算全局 yRange：`min/max` 取 `bot.series.net_value` ∪ `benchmark.series.net_value` 的并集
- 现有的 yTicks / baseline (1.0 横线) 用同一个 yRange 重算
- 这意味着 `linePath` 函数签名要小幅扩展（加 `yRange` 参数），调用点同步更新

#### B. 详情页 pill 行加一颗"超额"标签

在 `detail-pillrow` 里追加：

```
<span class="pill">vs 基准 <strong style="color:…">+X.XX%</strong></span>
```

X.XX% = (bot 终点 net_value − benchmark 终点 net_value) × 100，按正负染色（正用 `--gain` 绿、负用 `--loss` 红，沿用 gain/loss 语义）。

#### C. Marker 颜色翻转

CSS 改两行：

```css
.marker-buy  { fill: var(--loss); ... }   /* 原: var(--gain) */
.marker-sell { fill: var(--gain); ... }   /* 原: var(--loss) */
```

**待确认的范围问题**：当前 `.pill.buy` / `.row-badge.buy`（顶部 pill、交易表里的「买入」徽章）也用 `--gain`（绿），相应的 sell 徽章用 `--loss`（红）。两个选项：

- **C1（局部翻转）**：只改 `.marker-buy/-sell`。图上点是红/绿，但顶部「买 N」徽章仍然是绿色、表里「买入」标签也是绿色 → 内部不一致
- **C2（一致翻转）**：把所有 `.buy → --loss` / `.sell → --gain` 一起换，完整套用 A 股「买=红、卖=绿」语义 → 顶部 pill、表格 row-badge、图上 marker 三处一致

推荐 **C2**——更符合中国大陆习惯，且能避免"同一概念两种颜色"的认知负担。

### 不做（YAGNI）

- 左列 bot mini 卡的 sparkline 不画基准（空间太小，画了反而乱）
- 不引入"复权"严格算法，等数据源补齐再说
- 不做 alpha 阴影（bot vs benchmark 间的填充）
- 不做多基准（沪深300、等权等），bot 1 基金的现状下没必要

## 边界情况

| 情况 | 处理 |
|---|---|
| bot 没有任何 buy 动作 | `benchmark = null`，前端不画基准线、不显示 vs 基准 pill |
| bot 持仓有多只基金（防御性） | 用最早 buy 那只 fund_code |
| fund_nav 在 firstBuyDate ~ latestTradeDate 区间无数据 | `benchmark = null`（同上） |
| fund_nav 中间有缺口（周末、停牌） | series 自然稀疏，SVG 折线相邻可用点之间直接连线（不强制按 bot 交易日补齐） |
| baselineNav 为 0 或负数 | 防御：`benchmark = null` |

## 测试

`test/backtest-dashboard.test.ts` 已经在跑端到端的"启服务 → curl /api/backtest/data → 校验内容"。在原有断言后追加：

- 至少一个 bot 的 `benchmark` 非 null
- 该 bot.benchmark.series[0].net_value 接近 1.0（允许 ±1e-9 浮点误差）
- `fundCode` 在 fund_nav 表里能查到

## 实现成本估算

- 后端：~30 行（一个 SQL + 一段映射）
- 前端：~50 行（`linePath` 小幅扩展接受 xAxisDates / yRange + SVG 多画一条 path + 图例 + 超额 pill），CSS 4 行（marker 颜色 + 可能的徽章翻转）
- 测试：~10 行
- 改动范围只限于 `world/src/backtest-dashboard/*` 与对应 test 文件

## 风险

- 单位净值 ≠ 真复权净值：对当前 ETF/QDII 品种影响很小，不影响"对比 bot 主动交易效果"这一核心结论；但若后续接入分红型主动权益基金，需要补复权数据源
- 颜色翻转可能影响其他组件：本设计只动 `index.html` 内联 CSS，不影响 28080 fund 页或其他页
