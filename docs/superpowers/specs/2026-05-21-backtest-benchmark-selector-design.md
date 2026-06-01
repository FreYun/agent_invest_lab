# 回测看板:参照曲线可选 + 空仓日持仓修复

## 目的

回测看板(`world/src/backtest-dashboard/`)的 bot 详情页有三个问题要解决:

1. **未建仓的 bot 没有参照曲线** —— 基准标的取自「机器人首笔买入的基金」,从没建过仓的 bot 取不到标的,整条参照曲线消失。
2. **仓位面板不随光标变化** —— 悬停到某天时,如果那天没有持仓记录(如建仓前的空仓日),右侧「持仓快照」会回退显示**最新持仓**,与光标 tooltip 里的「总仓位 0.0%」矛盾。
3. **参照曲线不能在前端选** —— 用户希望能手动切换参照基准(宽基指数),而不只是看自动挑的首买基金。

## 背景与现状

- 看板服务:`world/src/backtest-dashboard/server.ts` + `index.html`,默认端口 48080,数据源 `data/fund.db`。
- 参照曲线由 `loadBenchmark()`([server.ts:206](../../../world/src/backtest-dashboard/server.ts#L206))生成:
  - `fundCode` = 首笔 buy 的基金,否则首个持仓;都没有则返回 `null`。
  - `anchorDate` = bot 首个净值日(`firstTradeDate`),`baselineNav` = anchor 当天 nav,序列 `net_value = nav / baselineNav`(起点对齐 1.0)。
- 持仓面板由 `renderHoldings(bot, dateOverride)`([index.html:544](../../../world/src/backtest-dashboard/index.html#L544))渲染;光标移动时 `drawCursor()` 调用它并传入悬停日期。
- 参照数据全部来自 `fund_nav`(865 只基金,含沪深300/中证500/创业板指等指数型基金),**无独立纯指数表**。所以「选参照」= 「选一只基金当基准」。

## 设计

### A. 未建仓也有参照曲线(server.ts)

`loadBenchmark` 增加默认指数兜底:当 `firstBuy` 与 `holdings[0]` 都取不到 `fundCode` 时,改用默认指数 **沪深300 / 510300**,而不是返回 `null`。anchor 逻辑不变(取 bot 首个净值日;若该日 510300 无 nav,则用 510300 在窗口内的首个 nav 日作 anchor)。

结果:任何 bot 至少有一条参照曲线。

### B. 仓位面板随光标(index.html, bug fix)

`renderHoldings` 改为:传了 `dateOverride` 就**严格使用当天记录**,无记录则视为空仓,不回退最新:

```js
const list = dateOverride ? (bot.holdingsByDate?.[dateOverride] || []) : bot.holdings
```

空列表时面板显示空仓状态(沿用现有空态文案,或「当日空仓」)。默认视图(`dateOverride` 为空)仍显示最新持仓,行为不变。

### C. 前端参照曲线选择器(server.ts + index.html)

**curated 候选清单**(写死在前端):

| 显示名 | fund_code | 标的 |
|---|---|---|
| 首买基金(默认) | —— | 机器人首笔买入的基金;未建仓 bot 此项禁用 |
| 沪深300 | 510300 | 沪深300ETF华泰柏瑞 |
| 中证500 | 000962 | 天弘中证500ETF联接A |
| 创业板指 | 001592 | 天弘创业板ETF联接A |
| 中证1000 | 006486 | 广发中证1000ETF联接A |
| 上证50 | 001548 | 天弘上证50ETF联接A |

> 这些标的均覆盖回测窗口(nav 起点 ≤ 2024-01-02,延续至今)。

**交互**:
- 图例旁加一个 `<select>`。
- 默认 = 「首买基金」;**不持久化**,切 bot / 刷新都重置为默认。
- 未建仓 bot:「首买基金」项禁用,默认落到沪深300。

**数据按需拉取**:新增轻量接口
```
GET /api/backtest/benchmark?bot=<botId>&fund=<fundCode>
```
server 复用 `loadBenchmark` 逻辑(用指定 `fundCode`、anchor=该 bot 首个净值日),返回 `BotBenchmark` 形状的归一化序列。默认那条仍随主 payload 一起下发,选别的才发请求。

**联动重渲染**:选择后
1. 用返回的序列替换 state 里该 bot 的 `benchmark`;
2. 重绘净值图的参照曲线;
3. 用新基准重算并刷新「指标对比」表([renderMetricsPanel](../../../world/src/backtest-dashboard/index.html#L587));
4. 光标 tooltip 的「基准」值随之更新(读的就是 `bot.benchmark`,自动跟随)。

### 实现要点

- **server.ts**:`loadBenchmark` 重构为可接收可选的显式 `fundCode`(传入则跳过自动挑选);加默认指数兜底常量;在请求路由里加 `/api/backtest/benchmark` 分支,解析 `bot`/`fund` 参数,调 `loadBenchmark` 后返回 JSON。
- **index.html**:加 `<select>` UI;`onChange` → fetch 接口 → 更新 state → 局部重渲染详情(图 + 指标表)。复用现有 `attachCursor` 重新绑定。

## 不做(YAGNI)

- 不做任意基金搜索(只给 curated 宽基)。
- 不做选择持久化(localStorage)。
- 不引入独立指数表 / 复权净值。
- 不改买卖点合并逻辑(已完成,与本次无关)。

## 测试

- **A**:找一个无 buy 动作的 bot,确认有参照曲线(沪深300)。
- **B**:悬停建仓前的空仓日,右侧面板显示空仓而非最新持仓;悬停有持仓日正常显示当天快照。
- **C**:切换下拉到各指数,净值参照曲线与指标对比表同步更新;切 bot / 刷新后重置为首买基金;未建仓 bot 的「首买基金」项禁用。
- 浏览器实测(Playwright)golden path + 上述边界。
