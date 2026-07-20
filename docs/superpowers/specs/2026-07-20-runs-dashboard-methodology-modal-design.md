# runs.html 净值图展开弹窗：方法论进化 + 当日反思

- 日期：2026-07-20
- 目标文件：`world/src/backtest-dashboard/runs.html`（前端）、`world/src/backtest-dashboard/server.ts`（新增只读接口）
- 状态：设计已确认，待写实现计划

## 1. 背景与目标

runs.html 是「按指数分组的 run 表 + 右侧净值图」视图。左栏 `.ix-left` 是各 run 的表格，右栏 `.ix-right`（440px）是当前选中 run 的净值图（已具备光标 / 时间窗口 / 仓位子图）。

现在要让用户能从**右侧净值图展开一个弹窗**，在弹窗里一处看全某个 (bot, run) 的：

1. **初始方法论**——agent 启动时被注入的 METHODOLOGY.md 全文。
2. **进化后最新方法论**——bot 用 `update_my_strategy` 反复重写后的最终 METHODOLOGY.md 全文。
3. **方法论进化轨迹**——每次修订的「日期 + 理由（复盘全文）+ 字数变化」时间线。
4. **当日反思**——与主看板 index.html 逻辑一致的反思面板。

## 2. 数据来源（全部可忠实还原，无需推测）

| 内容 | 来源 | 说明 |
|---|---|---|
| 初始方法论 | `<run>/workspaces/<bot>/strategies/index-products/`（run 启动时冻结的策略库快照）+ `strategy-assignments.json` 的 `strategy_id` | 用 `loadStrategyLibrary(冻结根)` + `renderActiveMethodology({botId, strategy, buyableFundCodes})` 重建，等于 agent 当时拿到的初始版，不受策略库后续改动影响 |
| 最新方法论 | `<run>/workspaces/<bot>/METHODOLOGY.md` | bot 修订后的最终态；未修订时它 == 初始 |
| 进化轨迹 | `<run>/strategies/<bot>.revisions.jsonl` | 每行 `{ts, reason, new_size, prior_size}`；文件不存在 = 未修订 |
| 当日反思 | 现成接口 `/api/backtest/bot-reflection?bot_id=&run_id=&trade_date=` | 服务端不改动 |

关键事实：`copyStrategyLibraryToWorkspace`（`run.ts:310`）在 run 启动时把整个策略库拷进 workspace 的 `strategies/index-products/`；`renderActiveMethodology`（`strategy-library.ts:138`）= `renderTaskHeader` + `stripInjectSkipBlocks(方法论正文)`。因此初始方法论可用冻结副本精确重建。中间版本只有 reason + 字数（无全文），符合「初始全文 + 最终全文 + 中间只有理由」的结构。

## 3. 服务端：新增只读接口

`GET /api/backtest/bot-methodology?bot_id=<id>&run_id=<id>`

返回：
```json
{
  "bot_id": "bot6",
  "run_id": "dash-...",
  "strategy_id": "liquor",
  "strategy_title": "中证酒指数投资框架",
  "initial": "…初始方法论全文…",
  "latest": "…最新方法论全文…",
  "revised": true,
  "revisions": [
    { "ts": "2025-10-20", "reason": "…复盘全文…", "new_size": 3471, "prior_size": 32431 }
  ]
}
```

实现要点：
- `initial`：定位 `<run>/workspaces/<bot>/strategies/index-products`，`loadStrategyLibrary` 后取 `strategy_id`（来自 `strategy-assignments.json` 的 `bots[botId]`）对应 strategy，调 `renderActiveMethodology`。复用既有函数，零重复实现。
- `latest`：读 `<run>/workspaces/<bot>/METHODOLOGY.md`。
- `revisions`：逐行解析 `<run>/strategies/<bot>.revisions.jsonl`；文件缺失 → `[]`、`revised:false`。
- `strategy_title`：从 loadStrategyLibrary 的 strategy 或 assignments 取。
- **优雅降级**：冻结库缺失 / strategy_id 取不到 / workspace 无 METHODOLOGY.md（旧 run）→ 对应字段返回 `null` 或空，HTTP 仍 200，不抛错。前端据此显示占位。

服务端从 `paths.ts` 已有的 helper（`shadowWorkspaceDir`、`strategyRevisionsFile`、`strategiesDir`）拿路径，避免硬编码。

## 4. 前端：弹窗结构（runs.html）

### 4.1 触发与容器
- 在 `.chart-panel` 头部（`chartHead`）加展开按钮 `⛶`。
- 点击 → 打开全屏遮罩弹窗：`position:fixed` 半透明背景 + 居中卡片（宽约 min(1100px, 92vw)、高 ≤ 90vh、单栏可滚动）。
- 关闭：右上 `✕` / `Esc` / 点遮罩背景。
- 目标 (bot_id, run_id) 取自被展开面板的 `panel._chart`。
- 弹窗内容**打开时才懒加载**（fetch methodology + reflection），不拖慢主表首屏。

### 4.2 单栏内容（自上而下）
1. **标题条**：`<bot_id> · <strategy_title>(<strategy_id>) · <run_id>` + `✕`。
2. **净值大图**：复用 `renderChartBody(bot, win)`（光标 / 时间窗口 / 仓位子图全带），放大尺寸展示。
3. **📐 方法论进化 时间线**：
   - 首项「初始版」。
   - 其后每条修订：`修订日期 · 第N次修订 · 正文 prior_size→new_size 字` + `reason` 复盘全文。
   - 无修订 → 「本 run 未改动方法论」。
4. **▸ 初始方法论全文**（`<details>` 折叠，默认收起）。
5. **▸ 最新方法论全文**（折叠）；若 `revised:false` 标注「与初始一致」。
6. **当日反思**：移植 index.html 反思面板：
   - `交易记忆窗口 / 当天决策` 两 tab；
   - 左右箭头翻反思日、日历选日、「最新」回落；
   - 复用 `/api/backtest/bot-reflection` + 极简 markdown 渲染 + `clipMemoryWindow` / `stripBeliefYaml`；
   - **与净值图光标相互独立**，各自维护自己的当前日。

### 4.3 样式
- 弹窗自带 CSS（遮罩、卡片、时间线、折叠块、反思 tab），沿用 runs.html 既有配色变量（`--panel` `--border` `--accent2` `--dim` 等）。
- 方法论全文与 reason 用等宽/正文样式，保留换行，长文可滚动。

## 5. 边界与容错

- 无修订 / 无冻结库 / 旧 run 无 workspace / 反思接口 404 → 均给占位文案，绝不白屏。
- methodology 与 reflection 分别 fetch，任一失败只影响自己那块。
- 重复展开同一面板复用已加载数据（可选缓存）。

## 6. 测试

- 服务端：给 `bot-methodology` 接口加单测——初始重建成功、revisions 解析、三类降级路径（缺冻结库 / 缺 revisions / 缺 workspace METHODOLOGY.md）。
- 前端：沿用 `node --test test/backtest-dashboard.test.ts`，保证既有 19 项不破；对新增纯函数（时间线构造、降级判定）加针对性断言。
- 手工核对：真实 run（如 bot101 有 7 次修订）弹窗时间线与 `revisions.jsonl` 逐条一致；初始全文与 workspace 冻结库重建一致。

## 7. 明确不做（YAGNI）

- 不做中间版本全文回放（数据本就只有 reason）。
- 不做净值图光标与反思日联动（用户明确要求二者独立）。
- 不改 index.html；不新增 methodology 的写接口。
- 不做 methodology 全文 diff 高亮（初始 vs 最新并排对比不在本期范围，仅折叠展示两份全文）。
