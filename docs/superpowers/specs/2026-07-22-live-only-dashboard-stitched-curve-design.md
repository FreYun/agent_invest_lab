# live-only 看板：源 dash + 实盘增量 拼接连续曲线

> 日期：2026-07-22 ｜ 关联：`world/src/backtest-dashboard/runs.html`、`server.ts`；前置 `2026-07-21-live-intraday-run-design.md`

## Context（为什么做）

`http://172.31.41.105:18888/backtest-dashboard/runs.html` 现在展示所有历史回测 run（前端硬过滤只留 `dash-`），按指数分组、每行一个 run，带人工评级/点评/合格判定 + 绝对收益/年化/最大回撤/卡玛列。

54 个 `live-` 单指数 run 每交易日 14:00 decide / 18:30 settle 做真实盘中续跑（见前置 spec）。用户要把这张看板改成：

- **只展示 live run**，原来的静态 `dash-*` run 不再单独成行（**依旧是合格备份，仍在库、仍参与继承，只是不在看板列出**）。
- 每个 live run 一行 = **拼接连续曲线**：源 dash 回测历史（「原来的部分」）+ live 每日增量（「每天的增量」）几何链接成一条连续净值曲线，指标按拼接后的完整序列重算。
- **继承源 dash 评测**：live run 沿用其 `source_run_id` 对应 dash run 的人工评级/点评/合格判定/对标指数/可买基金；指标列按拼接曲线重算。不做「重评」UI。
- **空增量退化**：live 尚无实盘数据时（今天首次 decide 前，54 个全是此态），拼接退化为源 dash 曲线本身，指标=源 dash 的，标「待启动 · 0 交易日」；首个 live 日落地后自动接上。

## 地基（探查已确认）

### 1. 已有 OOS 拼接系统可复用
看板已有一套「回测历史 + 实盘增量」几何拼接，但只服务 bot101/102/103 三个多基金 bot：
- 表：`oos_bot_daily_snapshots` / `oos_bot_position_snapshots` / `oos_bot_orders` / `oos_bot_actions`（`live_run_id` 列区分）。
- `server.ts:1155 loadOosExtendedSeries(dbPath, botId, runId, liveSeries)`：两步几何链接——
  - 源段：`fund_bot_daily_snapshots WHERE run_id = OOS_BACKTEST_HISTORY_RUNS[botId]`，首点 `net_value` 归一到 1.0（`nav/firstHistoryNav`），`segment='backtest'`。
  - 实盘段：`scale = lastHistoryNav / firstLiveNav`，`nav = rawNav*scale`，`segment='daily_oos'`。
  - `cumulative_return_pct = (nav-1)*100`。
- `server.ts:1207 loadOosBot`：OOS 单 run 全量视图（曲线 + 持仓 + 订单 + 动作）。

**这套几何链接数学正是「拼接连续曲线」，直接抽象复用。** 差异仅在：live 的源 run_id 不是硬编码 `OOS_BACKTEST_HISTORY_RUNS[botId]`，而是每个 live run 自己 `state.json` 的 `source_run_id`。

### 2. live run 数据位置与源映射
- 54 个 `live-` run 写 `fund_bot_daily_snapshots`（`run_id='live-…'`），**当前无任何行**（今天 14:00 首次 decide、18:30 首次 settle 才落净值）。
- 每个 `runtime/runs/live-*/state.json`：`{ run_id, bots:[单个 botN], source_run_id:'dash-…', status }`。**单 bot、单源 dash**。
- OOS 的 `oos_*` 表只有 `oos-bot101/102/103-daily` 三行，与 live 无关。

### 3. runs.html 现状
- `runs.html:417 loadEvalRecords()` 拉 `/api/backtest/run-evals`（`fund_bot_run_eval` 表，人工评测），建记录 `_source='csv'`。
- `runs.html:425 ingestHistory()` 拉 `/api/backtest/all-runs`，**line 432 `if (!String(h.runId).startsWith('dash-')) continue`** — 只留 dash。
- 行 key = `run_id|bot`；`isPass/setMark/overrides` 按 key 存人工合格覆盖。
- 指标列来源：CSV 行来自评测表；hist 行来自 all-runs 的 `absReturnPct/annReturnPct/maxDrawdownPct`（`loadAllRunsSummary`，252 年化，末日累计，区间回撤）。

## 方案（A：服务端新端点，仿 OOS 系统）

### §1 拼接数学（抽象复用）
把 `loadOosExtendedSeries` 抽象成 `stitchSeries(dbPath, botId, historyRunId, liveSeries)`：入参化历史 run_id（OOS 传 `OOS_BACKTEST_HISTORY_RUNS[botId]`，live 传 `source_run_id`），逻辑不变（首点归一 + 末点 rebase 续接，`segment` 标 `backtest`/`live`）。`loadOosExtendedSeries` 改为薄封装调它，保证 OOS 视图零回归。

拼接后完整序列上算指标（与 `loadAllRunsSummary` 同口径）：
- 绝对收益 = `(末点 net_value − 1) × 100`
- 最大回撤 = 跑动峰值法（`min over t of (nav_t/peak_{≤t} − 1) × 100`）
- 年化 = 252 天年化（按拼接总交易日数）

### §2 服务端 `/api/backtest/live-runs`
新增 `loadLiveRunsSummary(dbPath, worldRoot)`：
1. 扫 `worldRoot/runs/live-*/state.json` → `{ liveRunId, botId=bots[0], sourceRunId }`（仿 `live_common.discover_live_runs`，单 bot）。
2. 每个 run：取 `fund_bot_daily_snapshots WHERE run_id=liveRunId` 为 `liveSeries` → `stitchSeries(dbPath, botId, sourceRunId, liveSeries)` → 完整序列算指标。
3. 返回每 run：`{ liveRunId, botId, sourceRunId, absReturnPct, annReturnPct, maxDrawdownPct, liveDays, lastLiveDate, status }`。`liveDays = liveSeries.length`，`status='待启动'` 当 `liveDays===0`。
4. 新增 GET 路由 `/api/backtest/live-runs`。

### §3 前端 runs.html 取数层
- `ingestHistory` 改为拉 `/api/backtest/live-runs`，**只渲染 live run**（删掉 `dash-` 过滤，改为遍历 live-runs）。
- 评测记录（`loadEvalRecords`，含 dash）照旧全量载入，但改为**只进 lookup map（key=`run_id|bot`）、不再直接建行**。
- 每个 live run 行：按 `sourceRunId|botId` 从 lookup 认领源 dash 的 评级/点评/合格判定/对标指数(`对标指数`)/可买基金(`可买基金`)/策略（继承），指标列用 live-runs 的拼接值覆盖（绝对收益/年化/最大回撤，卡玛按拼接年化/回撤重算）。
- 行 key 用 `liveRunId|botId`；合格判定默认继承源 dash 的 verdict（`overrides[sourceKey]` 或源 `_defaultPass`）。不新增重评 UI。

### §4 边界
- `liveDays===0`（今日全部）：`stitchSeries` 的 `liveSeries` 为空 → 退化为源 dash 归一曲线，指标=源 dash；行标「待启动 · 0 交易日」。首个 settle 落 `fund_bot_daily_snapshots` 后自动接上。
- dash run 不再单独成行，但 `fund_bot_run_eval` / `fund_bot_daily_snapshots` 里的 dash 数据全保留（合格备份 + 继承来源）。
- 若某 live run 的 `source_run_id` 在评测表查不到（异常）：降级为无评级、指标仍按拼接算、标注来源缺失，不抛错。

### §5 详情展开（承 runs.html 现有 `/api/backtest/bot` 展开）
live run 展开明细走拼接后的 `extendedSeries`（同 `stitchSeries`），源段 + 实盘段用 `segment` 区分着色。MVP 以表格指标 + 继承评测为主，详情曲线复用同一拼接函数。

## 验证

1. **单元** `stitchSeries`：造源段 + 实盘段假数据，验证归一（源首点=1.0）、rebase 续接连续（实盘首点=源末点）、累计/回撤/年化正确；空实盘段退化为源曲线（末点=源末点、`segment` 全 `backtest`）。
2. **端到端**：起 dashboard，`/api/backtest/live-runs` 返回 54 run、当前全 `liveDays=0 待启动`、指标=各自源 dash；页面只列 live、继承源 dash 评级。今天 settle 后挑一个 run 复查 `liveDays≥1` 且曲线续上、指标随拼接变化。
3. **不回归**：bot101/102/103 OOS 视图（`loadOosExtendedSeries` 薄封装 `stitchSeries`）指标/曲线与改前一致；`dash-*` 数据仍在库、评测端点仍返回。
4. **路由/继承覆盖**：54 个 live run 的 `source_run_id` 都能在评测记录里认领到源 dash（无孤儿），孤儿走 §4 降级。
