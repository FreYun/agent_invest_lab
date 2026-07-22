# live run 决策可见性（dashboard）设计

> 状态：待用户评审 → 通过后进入 writing-plans。
> 日期：2026-07-22　分支：feat/world-system

## 1. 背景与问题

54 个 live run 每交易日 14:00 由 `world/live/live_decide.py` 驱动做真实盘中决策，18:30 结算。决策数据**全部落库**，但当前 dashboard（`runs.html` 只列 live run 版）**没有任何视图读它**，且引入了一个回归。

### 回归（P0）
侧栏「末日决策三色标」（加▲/减▼/清⊘）曾对 dash 行显示。服务端 `loadLatestDecisions`（`world/src/backtest-dashboard/server.ts:1049`）用 `fund_bot_daily_snapshots` 求每个 (run,bot) 的「末日」。**live run 的 snapshots 为 0 条**（settle 才写），且 Task 6 把看板翻成「只列 live」后，三色标本就按 dash run_id 算 → live 行整片无标。结果：看板上「当日决策信息全没了」。

### 缺口（P1）
- 一级页（runs.html 表格）看不到每个 run 今天挂了什么决策。
- 没有决策流水视图（历史买卖动作 + 理由）。
- 详情净值图 live 段没有买卖点 marker（计划里 deferred 的那条）。

## 2. 数据地基（探查已确认，2026-07-22）

| 用途 | 表 · 条件 | key | live 覆盖 |
|---|---|---|---|
| **今日盘中决策**（方向/金额/理由/状态） | `fund_bot_orders` WHERE `order_date`=today AND `status`='pending' | `order_run_id` | 18 条 live，均 07-22 |
| 历史已确认动作 | `fund_bot_actions` WHERE `run_id` LIKE 'live-%' | `run_id`,`bot_id` | 1335 条，至 07-20 |
| 净值点（拼接曲线） | `fund_bot_daily_snapshots` | `run_id`,`bot_id` | 0 条（等 18:30 settle） |

关键字段取值域：
- `fund_bot_actions.action_type` ∈ {`ADD`, `REDUCE`}（511 / 824）；含 `before_weight`/`after_weight`/`amount`/`nav_used`/`action_date`/`fund_code`/`reason`。
- `fund_bot_orders.order_type` ∈ {`buy`, `sell`}；含 `order_amount`/`action_reason`/`status`(`pending`/`confirmed`)/`order_run_id`/`fund_code`。
- 挂单 pending → 18:30 settle 确认 → 转 `fund_bot_actions` 历史动作 + 写净值点。

## 3. 口径决定（用户已定）

- **一级页「今日决策」列**：严格只看今天的 pending 挂单（`order_date`=today、`status`=pending、live）。今天没下新单的 run（含 27 个 SKIP=已决过）显示「—」。
- **状态区分**：pending（未结算）与 confirmed（已确认）视觉区分——pending 标灰 / 虚线，settle 后转实。

## 4. 组件设计

### A. 服务端（server.ts）

**A1 · 修 `loadLatestDecisions`（回归修复 + 扩 live）**
不再强依赖 snapshots 求末日。改为：以 `fund_bot_actions` 的 `MAX(action_date)` 求每个 (run,bot) 末日动作方向，**并叠加今天的 pending 挂单**（buy=add / sell=reduce）。清仓判定：snapshots 缺失时改用当日动作后仓位 `after_weight`（末日动作后 `after_weight≈0` → clear），有 snapshot 时沿用 `cash_weight`。输出键含 live run_id。dash 行行为不变（仍有 snapshot 走原路径）。

**A2 · 新端点 `GET /api/backtest/today-decisions`**
返回今天所有 pending 挂单，按 `order_run_id|bot_id` 分组：`{ decisions: { "run_id|bot": { dir: 'buy'|'sell', items: [{fund, amount, reason}], status: 'pending' } } }`。供一级页「今日决策」列。

**A3 · 新端点 `GET /api/backtest/run-decisions?run_id=&bot=`**
返回单个 live run 的决策流水：`fund_bot_actions` 历史（按 action_date 升序，dir/fund/before_weight/after_weight/amount/reason）+ 今天 pending 挂单（标 `status:'pending'`）。供决策流水面板与曲线 marker。

### B. 前端 runs.html

**B1 · 「今日决策」列**（`COLS` 插入，位置在「状态」后）
渲染徽标：买 ▲（红）/卖 ▼（绿），hover 出理由（`action_reason` 截断 + title 全文）；pending 用灰色/虚线边框标「待确认」；无今日决策显示「—」。数据源 A2。

**B2 · 侧栏三色标**
消费修复后的 `/api/backtest/latest-decisions`（现含 live 键）。前端 `latestDecisions[r._key]` 逻辑不变，键自动匹配 live run。

**B3 · 决策流水面板**
点开某 run 时（runs.html 现有的展开/详情交互）展示 A3 的流水表：日期 · 方向 · 标的 · 仓位前→后 · 金额 · 理由；今日 pending 行标灰。

### C. 详情曲线 marker

**C1 · live 段买卖点**
详情净值图（消费 `/api/backtest/bot` → `loadLiveBotForRun` 的拼接 series）在 live 段叠加买卖点。做法二选一（plan 阶段定）：(a) `loadLiveBotForRun` 给 series 点附 `marker` 字段；(b) 前端另拉 A3 的 run-decisions 按日期对齐叠加。marker：ADD/buy=▲、REDUCE/sell=▼，pending 用空心/灰。**不改** dash 的 `backtest`/`daily_oos` 段既有着色与 marker。

## 5. 验证

1. 侧栏三色标恢复：今天有 pending 的 live run（如 live-bot7 卖、live-bot16 加）在其指数目录项打标。
2. 一级页今日决策列：live-bot7 显示 卖▼ + hover「沪深300估值87.24%分位极端高估…」；无今日单的 run 显「—」。
3. 流水面板：live-bot18 列出历史 ADD/REDUCE（至 07-20）+ 今日 pending。
4. 曲线 marker：挑一个今天有决策的 run，详情图 live 段见买卖点。
5. **不回归**：dash 详情（index.html / market-reports.html）既有 marker/曲线、`backtest`/`daily_oos` 着色不受影响；多基金 bot101-103 不受影响。
6. 18:30 settle 后复查：pending 转 confirmed，灰/虚线转实，净值点补上、曲线延伸。

## 6. 范围外

- live run 重评 UI（本次只继承，不重评）。
- 多基金 bot101-103 的决策展示（本次聚焦单指数 live run）。
- 决策与持仓堆叠图叠加（本次只做净值线上的买卖点）。
